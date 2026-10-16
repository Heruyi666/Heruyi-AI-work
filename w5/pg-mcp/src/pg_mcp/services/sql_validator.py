"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.

Every violation carries a machine-readable ``reason`` in the raised error's
``details`` (``blocked_table``, ``blocked_column``, ``blocked_function``, ...)
so that observability can label rejections instead of guessing.
"""

from dataclasses import dataclass, field
from typing import Any, ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError
from pg_mcp.models.query import ValidationResult


@dataclass(frozen=True)
class Violation:
    """A structured security violation found while analysing SQL.

    Attributes:
        reason: Machine-readable reason code used as a metric label.
        message: Human readable message raised in the exception.
        details: Extra context attached to the raised error.
    """

    reason: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    - Enforcing the configured EXPLAIN policy
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except
    }

    # Allowed top-level expressions (including CTEs)
    ALLOWED_TOP_LEVEL: ClassVar = {
        exp.Select, exp.Union, exp.Intersect, exp.Except, exp.With, exp.Subquery
    }

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Schemas that expose PostgreSQL internals. Reading them leaks server
    # metadata (user accounts, object definitions, installed extensions) and is
    # never required to answer a business question (design review 0003 #7).
    SYSTEM_SCHEMAS: ClassVar = frozenset(
        {"pg_catalog", "information_schema", "pg_toast", "pg_temp"}
    )

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool | None = None,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
                Overrides ``config.blocked_tables`` when provided.
            blocked_columns: Optional list of column names to block access to.
                Overrides ``config.blocked_columns`` when provided.
            allow_explain: Whether to allow EXPLAIN statements. Defaults to
                ``config.allow_explain`` so the policy follows configuration.
        """
        self.config = config
        raw_tables = blocked_tables if blocked_tables is not None else config.blocked_tables
        raw_columns = blocked_columns if blocked_columns is not None else config.blocked_columns
        # A schema-qualified entry ("public.secrets") must also block the bare
        # name ("secrets"): the same table can be referenced either way.
        self.blocked_tables = self._expand_blocked_tables(raw_tables)
        self.blocked_columns = {c.lower() for c in raw_columns}
        self.allow_explain = config.allow_explain if allow_explain is None else allow_explain
        # Older/mocked configs may not declare the field yet; default to secure.
        self.block_system_schemas: bool = bool(
            getattr(config, "block_system_schemas", True)
        )

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    @staticmethod
    def _expand_blocked_tables(entries: list[str] | tuple[str, ...]) -> set[str]:
        """Lower-case blocked table entries and add bare names for qualified ones.

        Args:
            entries: Configured entries (``payments``, ``public.secrets``, ...).

        Returns:
            set[str]: Normalized entries including the unqualified variants.
        """
        expanded: set[str] = set()
        for entry in entries:
            normalized = entry.strip().lower()
            if not normalized:
                continue
            expanded.add(normalized)
            if "." in normalized:
                bare = normalized.rsplit(".", 1)[-1]
                if bare:
                    expanded.add(bare)
        return expanded

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints. The
                exception ``details`` always contain a ``reason`` code.
        """
        violation = self.find_violation(sql)
        if violation is not None:
            raise SecurityViolationError(
                violation.message,
                details={"reason": violation.reason, **violation.details},
            )

    def find_violation(self, sql: str) -> Violation | None:
        """Analyse SQL and return the first security violation, if any.

        Args:
            sql: SQL query string to analyse.

        Returns:
            Violation | None: The first violation found, or None when the SQL is safe.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: For structural problems such as multiple
                statements or disallowed commands (these short-circuit before
                any rule inspection).
        """
        statement = self._parse_single_statement(sql)

        # Allowed EXPLAIN statements are read-only plan inspections and are
        # returned as-is; the inner query is not inspected (see _parse_single_statement).
        if isinstance(statement, exp.Command):
            return None

        main_query = statement.this if isinstance(statement, exp.With) else statement
        if isinstance(statement, exp.With) and main_query is None:
            raise SQLParseError("WITH statement has no main query")

        checks = (
            self._check_statement_type(main_query),
            self._check_dangerous_functions(statement),
            self._check_cross_database(statement),
            self._check_system_schemas(statement),
            self._check_blocked_tables(statement),
            self._check_blocked_columns(statement),
            self._check_subquery_safety(statement),
        )
        for violation in checks:
            if violation is not None:
                return violation
        return None

    def build_validation_result(self, sql: str) -> ValidationResult:
        """Produce a :class:`ValidationResult` describing the checked SQL.

        Unlike :meth:`validate`, this method never raises: it is used to build
        the observable validation outcome attached to query responses (the
        implementation previously hardcoded ``is_valid=True`` here).

        Args:
            sql: SQL query string that was validated.

        Returns:
            ValidationResult: Detailed outcome of the validation checks.
        """
        violation = None
        parse_error: str | None = None
        try:
            violation = self.find_violation(sql)
        except (SecurityViolationError, SQLParseError) as e:
            parse_error = str(e)

        blocked_functions = self.find_blocked_functions(sql)
        is_select = self._looks_like_select(sql)

        return ValidationResult(
            is_valid=violation is None and parse_error is None,
            is_select=is_select,
            allows_data_modification=not is_select,
            uses_blocked_functions=blocked_functions,
            error_message=violation.message if violation else parse_error,
        )

    def find_blocked_functions(self, sql: str) -> list[str]:
        """Return the blocked functions referenced by the SQL.

        Args:
            sql: SQL query string.

        Returns:
            list[str]: Sorted, de-duplicated blocked function names.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return []
        if parsed is None:
            return []
        found = {
            func.name.lower()
            for func in parsed.find_all(exp.Func)
            if func.name and func.name.lower() in self.blocked_functions
        }
        return sorted(found)

    def _looks_like_select(self, sql: str) -> bool:
        """Check whether the top-level statement is a read-only SELECT.

        Args:
            sql: SQL query string.

        Returns:
            bool: True when the statement is a SELECT/UNION/WITH query.
        """
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception:
            return False
        if not parsed or parsed[0] is None:
            return False
        statement = parsed[0]
        if isinstance(statement, exp.Command):
            return str(statement.this).upper() == "EXPLAIN"
        if isinstance(statement, exp.With):
            return isinstance(statement.this, tuple(self.ALLOWED_STATEMENT_TYPES))
        return isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES))

    def _parse_single_statement(self, sql: str) -> exp.Expression:
        """Parse SQL into exactly one statement, enforcing structural rules.

        Args:
            sql: SQL query string.

        Returns:
            exp.Expression: The single parsed statement.

        Raises:
            SQLParseError: If the SQL is empty or cannot be parsed.
            SecurityViolationError: If there are multiple statements or the
                statement is a disallowed command.
        """
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted.",
                details={"reason": "multiple_statements", "statement_count": len(parsed)},
            )

        if not parsed or parsed[0] is None:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        if isinstance(statement, exp.Command):
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError(
                        "EXPLAIN statements are not allowed",
                        details={"reason": "explain_not_allowed"},
                    )
                # EXPLAIN is read-only and safe - it only shows query plans without
                # executing. sqlglot cannot parse EXPLAIN syntax reliably (it falls
                # back to Command), so the inner query is not re-validated.
                return statement
            raise SecurityViolationError(
                f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted.",
                details={"reason": "disallowed_command", "command": cmd_name},
            )

        return statement

    def _check_statement_type(self, statement: exp.Expression) -> Violation | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when the statement type is not allowed.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return Violation(
                    reason="forbidden_statement",
                    message=(
                        f"{stmt_name} statements are not allowed. "
                        "Only SELECT queries are permitted."
                    ),
                    details={"statement_type": stmt_name.lower()},
                )

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return Violation(
                reason="unsupported_statement",
                message=(
                    f"Statement type {stmt_type} is not allowed. "
                    "Only SELECT queries are permitted."
                ),
                details={"statement_type": stmt_type.lower()},
            )

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> Violation | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when a blocked function is used.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = func.name.lower() if func.name else ""

            if func_name in self.blocked_functions:
                return Violation(
                    reason="blocked_function",
                    message=f"Function '{func_name}' is blocked for security reasons",
                    details={"function": func_name},
                )

        return None

    def _check_cross_database(self, statement: exp.Expression) -> Violation | None:
        """Reject three-part names that name another database.

        A single connection can only read the database it is attached to, so a
        ``otherdb.public.users`` reference is either a mistake or an attempt to
        reach data the connection is not scoped to (test-plan review 0008 #2.5).

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when another database is referenced.
        """
        for table in statement.find_all(exp.Table):
            catalog = (table.catalog or "").lower()
            if catalog:
                return Violation(
                    reason="cross_database_reference",
                    message=(
                        f"Cross-database reference '{catalog}' is not allowed; "
                        "each request is bound to a single database"
                    ),
                    details={"catalog": catalog, "table": table.name},
                )
        return None

    def _check_system_schemas(self, statement: exp.Expression) -> Violation | None:
        """Reject references to PostgreSQL system schemas.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when a system schema is referenced.
        """
        if not self.block_system_schemas:
            return None

        for table in statement.find_all(exp.Table):
            schema = (table.db or "").lower()
            if schema in self.SYSTEM_SCHEMAS:
                return Violation(
                    reason="system_schema",
                    message=f"Access to system schema '{schema}' is not allowed",
                    details={"schema": schema, "table": table.name},
                )
        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> Violation | None:
        """Check for access to blocked tables.

        Both bare (``secrets``) and schema-qualified (``public.secrets``) entries
        are supported in the configured block list.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when a blocked table is referenced.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""
            candidates = {table_name}
            schema = (table.db or "").lower()
            if schema:
                candidates.add(f"{schema}.{table_name}")

            matched = candidates & self.blocked_tables
            if matched:
                blocked = sorted(matched)[0]
                return Violation(
                    reason="blocked_table",
                    message=f"Access to table '{blocked}' is not allowed",
                    details={"table": blocked},
                )

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> Violation | None:
        """Check for access to blocked columns.

        Supports three forms in the block list:

        * bare names (``password_hash``) — matches the column wherever it appears,
        * qualified names (``users.password_hash``) — matches when the column is
          qualified or when the table in scope is ``users``,
        * table wildcards (``users.*``) — matches ``SELECT *``, ``SELECT u.*``
          (aliases are resolved) and any column of that table.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when a blocked column is referenced.
        """
        if not self.blocked_columns:
            return None

        alias_map = self._build_alias_map(statement)
        wildcards = {entry for entry in self.blocked_columns if entry.endswith(".*")}
        tables_in_scope = set(alias_map.values())

        # 1) Star projections: SELECT * / SELECT t.* / SELECT t2.* (aliased)
        for star in statement.find_all(exp.Star):
            qualifier = ""
            parent = star.parent
            if isinstance(parent, exp.Column) and parent.table:
                qualifier = parent.table.lower()

            if qualifier:
                resolved = alias_map.get(qualifier, qualifier)
                for entry in (f"{qualifier}.*", f"{resolved}.*"):
                    if entry in wildcards:
                        return Violation(
                            reason="blocked_column",
                            message=f"Access to column '{entry}' is not allowed",
                            details={"column": entry},
                        )
                continue

            for table in sorted(tables_in_scope):
                entry = f"{table}.*"
                if entry in wildcards:
                    return Violation(
                        reason="blocked_column",
                        message=f"Access to column '{entry}' is not allowed",
                        details={"column": entry},
                    )

        # 2) Explicit column references
        for column in statement.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue  # handled by the star projection check above

            column_name = column.name.lower() if column.name else ""
            if not column_name:
                continue

            qualifier = (column.table or "").lower()
            candidates = {column_name}

            if qualifier:
                resolved = alias_map.get(qualifier, qualifier)
                candidates.update({f"{qualifier}.{column_name}", f"{resolved}.{column_name}"})
                wildcard_entry = f"{resolved}.*"
                if wildcard_entry in wildcards:
                    return Violation(
                        reason="blocked_column",
                        message=f"Access to column '{wildcard_entry}' is not allowed",
                        details={"column": wildcard_entry},
                    )
            else:
                for table in sorted(tables_in_scope):
                    wildcard_entry = f"{table}.*"
                    if wildcard_entry in wildcards:
                        return Violation(
                            reason="blocked_column",
                            message=f"Access to column '{wildcard_entry}' is not allowed",
                            details={"column": wildcard_entry},
                        )
                    candidates.add(f"{table}.{column_name}")

            matched = candidates & self.blocked_columns
            if matched:
                blocked = sorted(matched)[0]
                return Violation(
                    reason="blocked_column",
                    message=f"Access to column '{blocked}' is not allowed",
                    details={"column": blocked},
                )

        return None

    @staticmethod
    def _build_alias_map(statement: exp.Expression) -> dict[str, str]:
        """Map table aliases (and names) to their underlying table name.

        Args:
            statement: Parsed SQL statement.

        Returns:
            dict[str, str]: ``{alias_or_name → table_name}`` in lowercase.
        """
        mapping: dict[str, str] = {}
        for table in statement.find_all(exp.Table):
            if not table.name:
                continue
            name = table.name.lower()
            mapping[name] = name
            alias = (getattr(table, "alias", "") or "").lower()
            if alias:
                mapping[alias] = name
        return mapping

    def _check_subquery_safety(self, statement: exp.Expression) -> Violation | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Violation | None: Violation when a subquery is not a SELECT.
        """
        # Find all subqueries
        for subquery in statement.find_all(exp.Subquery):
            if subquery.this:
                inner_stmt = subquery.this

                # Check if the inner statement is a forbidden type
                for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
                    if isinstance(inner_stmt, forbidden_type):
                        stmt_name = forbidden_type.__name__.upper()
                        return Violation(
                            reason="subquery_write",
                            message=f"{stmt_name} statements in subqueries are not allowed",
                            details={"statement_type": stmt_name.lower()},
                        )

                # Ensure it's a SELECT
                if not isinstance(inner_stmt, (exp.Select, exp.With)):
                    return Violation(
                        reason="unsafe_subquery",
                        message="Subqueries must contain only SELECT statements",
                        details={"statement_type": type(inner_stmt).__name__.lower()},
                    )

        return None

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e
