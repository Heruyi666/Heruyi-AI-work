"""Advanced security tests (test-plan review 0008, section 2.5).

The review noted that the security suite only had shallow cases. These cover the
scenarios it listed as missing: ``search_path`` injection, privilege escalation,
cross-database references, system-schema probing and credential leakage in error
details.
"""

import pytest

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError
from pg_mcp.services.sql_validator import SQLValidator


@pytest.fixture
def validator() -> SQLValidator:
    """Validator with default hardening (system schemas blocked)."""
    return SQLValidator(SecurityConfig())


def _reason(validator: SQLValidator, sql: str) -> str | None:
    """Return the violation reason, or None when the SQL passes.

    Args:
        validator: Validator under test.
        sql: Statement to inspect.

    Returns:
        str | None: ``details["reason"]`` when rejected.
    """
    try:
        violation = validator.find_violation(sql)
    except SecurityViolationError as exc:
        return str(exc.details.get("reason"))
    return violation.reason if violation else None


class TestSearchPathInjection:
    """search_path must not be steerable from user input."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SET search_path TO malicious_schema; SELECT * FROM users",
            "SELECT * FROM users; SET search_path = 'evil'",
            "SET search_path = 'evil'; SELECT 1",
        ],
    )
    def test_search_path_manipulation_blocked(
        self, validator: SQLValidator, sql: str
    ) -> None:
        """Any attempt to smuggle a SET in is rejected as multiple statements."""
        assert _reason(validator, sql) == "multiple_statements"

    def test_search_path_alone_is_rejected(self, validator: SQLValidator) -> None:
        """A bare SET is not a SELECT and is refused on its own."""
        # SQLGlot parses SET as exp.Set, which is in FORBIDDEN_STATEMENT_TYPES.
        assert _reason(validator, "SET search_path TO evil") == "forbidden_statement"


class TestPrivilegeEscalation:
    """Role and privilege statements must never run."""

    def test_set_role_is_rejected(self, validator: SQLValidator) -> None:
        """Switching to a superuser role is refused."""
        assert _reason(validator, "SET ROLE postgres") == "disallowed_command"

    def test_grant_is_rejected(self, validator: SQLValidator) -> None:
        """Granting privileges is a forbidden statement."""
        assert _reason(validator, "GRANT ALL ON users TO public") == "forbidden_statement"

    def test_create_role_is_rejected(self, validator: SQLValidator) -> None:
        """Creating a role is refused."""
        assert _reason(validator, "CREATE ROLE hacker SUPERUSER") == "disallowed_command"

    def test_drop_and_alter_are_rejected(self, validator: SQLValidator) -> None:
        """Destructive DDL is refused."""
        assert _reason(validator, "DROP TABLE users") == "forbidden_statement"
        assert _reason(validator, "ALTER TABLE users ADD COLUMN x int") == (
            "forbidden_statement"
        )


class TestCrossDatabaseReferences:
    """A request is bound to one database."""

    def test_three_part_name_is_rejected(self, validator: SQLValidator) -> None:
        """Naming another database is refused."""
        assert (
            _reason(validator, "SELECT * FROM otherdb.public.users")
            == "cross_database_reference"
        )

    def test_two_part_name_is_allowed(self, validator: SQLValidator) -> None:
        """A schema-qualified name inside the current database is fine."""
        assert _reason(validator, "SELECT * FROM public.users") is None


class TestSystemSchemaAccess:
    """PostgreSQL internals are off limits (design review 0003 #7)."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM pg_catalog.pg_shadow",
            "SELECT usename FROM pg_catalog.pg_user",
            "SELECT * FROM information_schema.tables",
        ],
    )
    def test_system_schemas_blocked(self, validator: SQLValidator, sql: str) -> None:
        """System schema reads are refused."""
        assert _reason(validator, sql) == "system_schema"

    def test_blocking_can_be_disabled(self) -> None:
        """Operators can opt out when they intentionally introspect."""
        config = SecurityConfig(block_system_schemas=False)
        permissive = SQLValidator(config)
        assert _reason(permissive, "SELECT * FROM information_schema.tables") is None


class TestErrorDetailHygiene:
    """Errors must not become an information leak."""

    def test_error_details_do_not_echo_the_whole_query(self) -> None:
        """The rejection payload carries the reason, not a full SQL dump."""
        validator = SQLValidator(SecurityConfig(blocked_tables=["secrets"]))
        try:
            validator.validate_or_raise("SELECT * FROM secrets")
        except SecurityViolationError as exc:
            assert exc.details.get("reason") == "blocked_table"
            assert "secrets" in str(exc.details)
        else:  # pragma: no cover - the call must raise
            pytest.fail("expected a security violation")

    def test_connection_settings_are_not_leaked(self) -> None:
        """Nothing in the validator carries credentials."""
        validator = SQLValidator(SecurityConfig())
        assert "password" not in str(vars(validator)).lower()
