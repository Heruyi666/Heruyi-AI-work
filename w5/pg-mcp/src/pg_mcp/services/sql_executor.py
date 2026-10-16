"""SQL executor for PostgreSQL queries.

This module provides safe SQL execution with session parameter configuration,
result serialization, retry on transient failures, metrics instrumentation and
row limiting to prevent memory overflow.
"""

import asyncio
import datetime
import decimal
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import asyncpg
import sqlglot
from asyncpg import Connection, Pool

from pg_mcp.config.settings import DatabaseConfig, SecurityConfig
from pg_mcp.models.errors import DatabaseError, ExecutionTimeoutError, PgMcpError
from pg_mcp.resilience.retry import RetryPolicy, is_retryable_db_error, retry_async

if TYPE_CHECKING:
    from pg_mcp.observability.metrics import MetricsCollector

logger = logging.getLogger(__name__)


class SQLExecutor:
    """SQL executor using asyncpg with security measures.

    This executor ensures safe query execution by:
    1. Setting session parameters (timeout, search_path, role)
    2. Running queries in read-only transactions
    3. Limiting the number of returned rows
    4. Serializing PostgreSQL-specific data types
    5. Retrying transient connection/serialization failures with backoff

    Example:
        >>> executor = SQLExecutor(pool, security_config, db_config)
        >>> results, count = await executor.execute("SELECT * FROM users")
        >>> print(f"Retrieved {count} rows")
    """

    def __init__(
        self,
        pool: Pool,
        security_config: SecurityConfig,
        db_config: DatabaseConfig,
        *,
        db_name: str | None = None,
        retry_policy: RetryPolicy | None = None,
        metrics: "MetricsCollector | None" = None,
    ) -> None:
        """Initialize SQL executor.

        Args:
            pool: asyncpg connection pool for database connections.
            security_config: Security configuration including timeouts and limits.
            db_config: Database configuration including connection parameters.
            db_name: Logical database name used for logs and metrics labels.
            retry_policy: Policy applied to transient database failures.
            metrics: Optional metrics collector for database instrumentation.
        """
        self.pool = pool
        self.security_config = security_config
        self.db_config = db_config
        self.db_name = db_name or db_config.name
        self.retry_policy = retry_policy
        self.metrics = metrics

    async def execute(
        self,
        sql: str,
        timeout: float | None = None,  # noqa: ASYNC109
        max_rows: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute SQL query with security measures and transient-failure retries.

        This method:
        1. Acquires a connection from the pool
        2. Starts a read-only transaction
        3. Sets session parameters (timeout, search_path, role)
        4. Executes the query with timeout
        5. Limits the number of returned rows
        6. Serializes special PostgreSQL types

        Transient errors (connection resets, serialization failures, deadlocks,
        too many connections, ...) are retried according to ``retry_policy``.

        Args:
            sql: SQL query to execute (should already be validated).
            timeout: Query timeout in seconds (uses config default if None).
            max_rows: Maximum rows to return (uses config default if None).

        Returns:
            tuple: (results, row_count) where:
                - results: List of row dictionaries with serialized values
                - row_count: Number of rows returned by the server. The row cap
                  is pushed into the statement as ``LIMIT`` (see
                  ``enforce_row_limit``), so this is the number of rows
                  PostgreSQL produced rather than the size of the table — ask
                  for ``COUNT(*)`` when the unrestricted total is needed.

        Raises:
            ExecutionTimeoutError: If query execution exceeds timeout.
            DatabaseError: If database operation fails.

        Example:
            >>> results, count = await executor.execute(
            ...     "SELECT id, name FROM users WHERE active = true",
            ...     timeout=10.0,
            ...     max_rows=1000
            ... )
            >>> print(f"Retrieved {len(results)} of {count} total rows")
        """
        # Use configured defaults if not specified
        timeout = timeout or self.security_config.max_execution_time
        max_rows = max_rows or self.security_config.max_rows

        started = time.perf_counter()
        try:
            if self.retry_policy is None:
                return await self._execute_once(sql, timeout, max_rows)

            return await retry_async(
                lambda: self._execute_once(sql, timeout, max_rows),
                policy=self.retry_policy,
                is_retryable=is_retryable_db_error,
                operation_name="db.execute",
                metrics=self.metrics,
                on_retry=self._log_retry,
            )
        finally:
            duration = time.perf_counter() - started
            if self.metrics is not None:
                self.metrics.observe_db_query_duration(duration)
                self.metrics.set_db_connections_active(self.db_name, self._pool_in_use())

    def _log_retry(self, attempt: int, error: BaseException, delay: float) -> None:
        """Log a retry decision for a transient database failure.

        Args:
            attempt: Zero-based index of the failed attempt.
            error: The exception that triggered the retry.
            delay: Delay in seconds applied before the next attempt.
        """
        logger.warning(
            "Retrying database execution after transient error",
            extra={
                "database": self.db_name,
                "attempt": attempt + 1,
                "delay_seconds": round(delay, 3),
                "error": str(error),
            },
        )

    def _pool_in_use(self) -> int:
        """Return the number of connections currently in use.

        Returns:
            int: In-use connection count, or 0 when the pool does not expose it.
        """
        try:
            return int(self.pool.get_size()) - int(self.pool.get_idle_size())
        except Exception:
            return 0

    async def _execute_once(
        self,
        sql: str,
        timeout: float,  # noqa: ASYNC109
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Run the query once (single attempt, no retry).

        Args:
            sql: SQL query to execute.
            timeout: Query timeout in seconds.
            max_rows: Maximum rows to return.

        Returns:
            tuple: (results, total_row_count)

        Raises:
            ExecutionTimeoutError: If query execution exceeds timeout.
            DatabaseError: If database operation fails.
        """
        try:
            async with (
                self.pool.acquire() as connection,
                connection.transaction(readonly=True),
            ):
                # Set session parameters for security
                await self._set_session_params(connection, timeout)

                # Push the row cap into the statement instead of fetching every
                # row and slicing afterwards, which can exhaust memory on large
                # tables (design review 0003 #2).
                effective_sql = self._apply_row_limit(sql, max_rows)

                # Execute query with timeout
                try:
                    records = await asyncio.wait_for(
                        connection.fetch(effective_sql),
                        timeout=timeout,
                    )
                except TimeoutError as e:
                    raise ExecutionTimeoutError(
                        message=f"Query execution exceeded timeout of {timeout} seconds",
                        details={
                            "timeout_seconds": timeout,
                            "sql": sql[:200],  # Include truncated SQL for debugging
                        },
                    ) from e

                # Track total count before limiting
                total_count = len(records)

                # Limit number of returned rows
                if len(records) > max_rows:
                    records = records[:max_rows]

                # Convert asyncpg.Record to dict
                results = [dict(record) for record in records]

                # Serialize special PostgreSQL types
                results = self._serialize_results(results)

                return results, total_count

        except ExecutionTimeoutError:
            # Re-raise timeout errors as-is
            raise
        except asyncpg.PostgresError as e:
            # Wrap PostgreSQL errors
            raise DatabaseError(
                message=f"Database query failed: {e!s}",
                details={
                    "error_code": e.sqlstate if hasattr(e, "sqlstate") else None,
                    "error_message": str(e),
                    "sql": sql[:200],  # Include truncated SQL for debugging
                },
            ) from e
        except PgMcpError:
            raise
        except Exception as e:
            # Catch-all for unexpected errors
            raise DatabaseError(
                message=f"Unexpected error during query execution: {e!s}",
                details={
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "retryable": isinstance(e, (ConnectionError, OSError, TimeoutError)),
                },
            ) from e

    async def _set_session_params(
        self,
        conn: Connection,
        timeout: float,  # noqa: ASYNC109
    ) -> None:
        """Set session parameters to ensure safe query execution.

        This method configures the database session with:
        1. statement_timeout: Prevents long-running queries
        2. search_path: Prevents schema injection attacks
        3. SET ROLE: Switches to read-only role if configured

        Args:
            conn: Database connection to configure.
            timeout: Query timeout in seconds (converted to milliseconds).

        Raises:
            DatabaseError: If setting session parameters fails.

        Note:
            These settings apply only to the current transaction and are
            automatically reset when the connection is returned to the pool.
        """
        try:
            # Set statement timeout (PostgreSQL expects milliseconds).
            # Values are bound as parameters via set_config() so that nothing is
            # ever interpolated into the statement text (design review 0003 #7).
            timeout_ms = int(timeout * 1000)
            await conn.execute(
                "SELECT set_config('statement_timeout', $1, false)", str(timeout_ms)
            )

            # Set safe search_path to prevent schema injection.
            search_path = self.security_config.safe_search_path
            if not all(c.isalnum() or c in ("_", ",", " ") for c in search_path):
                raise DatabaseError(
                    message="Invalid search_path configuration",
                    details={"search_path": search_path},
                )
            await conn.execute(
                "SELECT set_config('search_path', $1, false)", search_path
            )

            # Switch to read-only role if configured.
            if self.security_config.readonly_role:
                readonly_role = self.security_config.readonly_role
                # Validate role name contains only safe characters.
                if not all(c.isalnum() or c == "_" for c in readonly_role):
                    raise DatabaseError(
                        message="Invalid readonly_role configuration",
                        details={"readonly_role": readonly_role},
                    )
                # A role name is an identifier: quote it so it cannot be broken
                # out of, and so case is preserved (unquoted names fold to
                # lower case). The character allow-list above already excludes
                # quotes, so this is defence in depth.
                await conn.execute(f'SET ROLE "{readonly_role}"')

        except asyncpg.PostgresError as e:
            raise DatabaseError(
                message=f"Failed to set session parameters: {e!s}",
                details={
                    "error_code": e.sqlstate if hasattr(e, "sqlstate") else None,
                    "timeout_ms": timeout_ms,
                    "search_path": self.security_config.safe_search_path,
                    "readonly_role": self.security_config.readonly_role,
                },
            ) from e

    def _apply_row_limit(self, sql: str, max_rows: int) -> str:
        """Push ``max_rows`` into the SQL so the database stops early.

        Fetching every row and slicing in Python means a ``SELECT *`` on a large
        table materialises millions of rows before anything is discarded. Adding
        ``LIMIT`` lets PostgreSQL stop scanning once the cap is reached. The
        slice in :meth:`_execute_once` stays as a second line of defence for the
        case where the query already carries a larger ``LIMIT``.

        Args:
            sql: Statement produced by the generator.
            max_rows: Configured maximum number of rows.

        Returns:
            str: The statement, with a trailing ``LIMIT`` when needed.
        """
        if not getattr(self.security_config, "enforce_row_limit", True):
            return sql

        stripped = sql.strip().rstrip(";").strip()
        try:
            parsed = sqlglot.parse_one(stripped, read="postgres")
        except Exception:
            return sql
        if parsed is None:
            return sql

        existing = parsed.args.get("limit")
        if existing is not None:
            # The query already caps itself; the Python-side slice still applies.
            return sql

        return f"{stripped} LIMIT {int(max_rows)}"

    def _serialize_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serialize PostgreSQL-specific types to JSON-compatible types.

        This method handles serialization of types that are not natively
        JSON-serializable, including:
        - datetime types: converted to ISO format strings
        - decimal.Decimal: converted to float
        - uuid.UUID: converted to string
        - bytes: converted to hexadecimal string
        - Nested lists/dicts: recursively serialized

        Args:
            results: List of row dictionaries with potentially unserializable values.

        Returns:
            list: Results with all values serialized to JSON-compatible types.

        Example:
            >>> results = [
            ...     {"id": 1, "created": datetime.datetime(2024, 1, 1, 12, 0)},
            ...     {"id": 2, "price": decimal.Decimal("99.99")}
            ... ]
            >>> serialized = executor._serialize_results(results)
            >>> serialized[0]["created"]  # "2024-01-01T12:00:00"
            >>> serialized[1]["price"]  # 99.99
        """

        def serialize_value(value: Any) -> Any:
            """Recursively serialize a single value.

            Args:
                value: Value to serialize.

            Returns:
                Serialized value that is JSON-compatible.
            """
            # Handle None
            if value is None:
                return None

            # Handle datetime types
            if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
                return value.isoformat()

            # Handle timedelta
            if isinstance(value, datetime.timedelta):
                return str(value)

            # Handle Decimal (convert to float)
            if isinstance(value, decimal.Decimal):
                return float(value)

            # Handle UUID
            if isinstance(value, uuid.UUID):
                return str(value)

            # Handle bytes (convert to hex string)
            if isinstance(value, bytes):
                return value.hex()

            # Handle lists and tuples (recursively serialize)
            if isinstance(value, (list, tuple)):
                return [serialize_value(v) for v in value]

            # Handle dicts (recursively serialize values)
            if isinstance(value, dict):
                return {k: serialize_value(v) for k, v in value.items()}

            # Return other types as-is (str, int, float, bool, etc.)
            return value

        # Serialize all values in all rows
        return [{key: serialize_value(value) for key, value in row.items()} for row in results]
