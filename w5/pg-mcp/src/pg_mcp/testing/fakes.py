"""Offline test doubles.

These helpers let the whole pipeline run without PostgreSQL or an OpenAI API
key: the code review called out that integration/E2E tests "assume live Postgres
and OpenAI; no mocks or fixtures", which made the behaviour impossible to verify
deterministically.

The doubles are intentionally faithful about *interfaces* (they mimic ``asyncpg``
pools, the SQL generator and the result validator) so the production code paths
being exercised — validation, per-database routing, rate limiting, retry,
metrics — are the real ones.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

from pydantic import SecretStr

from pg_mcp.config.settings import (
    CacheConfig,
    DatabaseConfig,
    ObservabilityConfig,
    OpenAIConfig,
    ResilienceConfig,
    SecurityConfig,
    Settings,
    ValidationConfig,
)
from pg_mcp.models.query import ResultValidationResult

__all__ = [
    "FakeConnection",
    "FakePool",
    "OfflineTable",
    "StubSQLGenerator",
    "StubResultValidator",
    "make_pool_factory",
    "offline_pg_handler",
    "build_offline_settings",
    "DEFAULT_TABLES",
]

FAKE_VERSION = "PostgreSQL 16.2 (offline fixture), compiled by pg-mcp"


@dataclass(frozen=True)
class OfflineTable:
    """A fake table exposed by the offline introspection handler.

    Attributes:
        name: Table name (always created in the ``public`` schema).
        columns: Column definitions as ``(name, sql_type)`` pairs.
        comment: Optional table comment.
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    comment: str | None = None

    @property
    def primary_key(self) -> str | None:
        """Return the first column name, used as the fake primary key."""
        return self.columns[0][0] if self.columns else None


def _rows_for_columns(table: OfflineTable) -> list[dict[str, Any]]:
    """Build ``_get_columns`` result rows for a fake table.

    Args:
        table: Table definition.

    Returns:
        list[dict[str, Any]]: Rows shaped like the introspection query output.
    """
    return [
        {
            "column_name": name,
            "data_type": sql_type,
            "is_nullable": name != table.primary_key,
            "default_value": None,
            "comment": None,
        }
        for name, sql_type in table.columns
    ]


def offline_pg_handler(
    tables: Sequence[OfflineTable],
) -> Callable[[str, tuple[Any, ...]], Any]:
    """Create a SQL handler emulating PostgreSQL introspection and ``SELECT 1``.

    Args:
        tables: Fake tables the handler should expose.

    Returns:
        Callable[[str, tuple[Any, ...]], Any]: Handler ``(sql, args) -> rows``.
        It returns ``None`` for unrecognised statements so callers can decide
        how to react.
    """
    by_name = {table.name: table for table in tables}

    def handler(sql: str, args: tuple[Any, ...]) -> Any:
        normalized = " ".join(sql.split())

        # Health probe / generic liveness checks.
        if "SELECT 1" in normalized.upper() and "FROM" not in normalized.upper():
            return [{"?column?": 1}]

        if "SELECT version()" in normalized:
            return FAKE_VERSION

        # Views
        if "relkind = 'v'" in normalized:
            return []

        # Tables
        if "relkind = 'r'" in normalized:
            return [
                {
                    "schema_name": "public",
                    "table_name": table.name,
                    "comment": table.comment,
                }
                for table in tables
            ]

        # Enum types
        if "typtype = 'e'" in normalized:
            return []

        # Column metadata (parameterised by table name / schema)
        if "format_type" in normalized and "pg_attribute" in normalized:
            table_name = args[0] if args else None
            table = by_name.get(str(table_name))
            return _rows_for_columns(table) if table else []

        # Unique constraint probe (EXISTS)
        if "SELECT EXISTS(" in normalized:
            return False

        # Primary keys
        if "i.indisprimary" in normalized:
            table_name = args[0] if args else None
            table = by_name.get(str(table_name))
            pk = table.primary_key if table else None
            return [{"column_name": pk}] if pk else []

        # Foreign keys
        if "con.contype = 'f'" in normalized:
            return []

        # Secondary indexes
        if "NOT idx.indisprimary" in normalized:
            return []

        # Row count estimate
        if "reltuples" in normalized:
            return 0

        # A canned answer for aggregate probes so demos/tests show real rows.
        if normalized.upper().startswith("SELECT") and "COUNT(" in normalized.upper():
            return [{"count": 1}]

        # Unknown introspection query: behave like an empty relation.
        return []

    return handler


class FakeConnection:
    """Minimal asyncpg connection double.

    Records every statement it sees (including session ``SET`` statements) so
    tests can assert on execution order and on which database was targeted.
    """

    def __init__(self, pool: FakePool) -> None:
        """Initialize the connection.

        Args:
            pool: Owning pool, used for handler + recording.
        """
        self._pool = pool

    @asynccontextmanager
    async def transaction(self, **kwargs: Any) -> AsyncIterator[FakeConnection]:
        """No-op transaction context manager.

        Args:
            **kwargs: Ignored (``readonly=True`` is asserted by the pool).

        Yields:
            FakeConnection: This connection.
        """
        self._pool.transactions.append(dict(kwargs))
        yield self

    async def execute(self, query: str, *args: Any) -> str:
        """Record a non-returning statement.

        Args:
            query: SQL statement.
            *args: Statement parameters.

        Returns:
            str: asyncpg-style status tag.
        """
        self._pool.session_statements.append(query)
        self._pool.session_params.append((query, args))
        return "SET"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Return rows from the pool handler.

        Args:
            query: SQL statement.
            *args: Statement parameters.

        Returns:
            list[dict[str, Any]]: Rows (possibly empty).
        """
        rows = await self._pool.run(query, args)
        return rows if isinstance(rows, list) else []

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Return the first column of the first row.

        Args:
            query: SQL statement.
            *args: Statement parameters.

        Returns:
            Any: Scalar value or None.
        """
        rows = await self._pool.run(query, args)
        if isinstance(rows, list) and rows:
            first = rows[0]
            return next(iter(first.values())) if isinstance(first, dict) else first
        return rows


class FakePool:
    """In-memory stand-in for an :class:`asyncpg.Pool`.

    Attributes:
        name: Logical database name.
        executed: Statements issued through ``fetch`` (i.e. user queries).
        session_statements: Statements issued through ``execute`` (e.g. ``SET``).
        transactions: Transaction options of every opened transaction.
    """

    def __init__(
        self,
        name: str,
        *,
        handler: Callable[[str, tuple[Any, ...]], Any] | None = None,
        latency: float = 0.0,
        failures_before_success: int = 0,
        failure_factory: Callable[[], BaseException] | None = None,
    ) -> None:
        """Initialize the fake pool.

        Args:
            name: Logical database name.
            handler: SQL handler returning rows/scalars.
            latency: Artificial delay (seconds) applied to each statement.
            failures_before_success: Number of failures to raise before
                answering successfully (used to demonstrate retry/backoff).
            failure_factory: Callable producing the exception to raise.
        """
        self.name = name
        self._handler = handler or (lambda sql, args: [])
        self.latency = latency
        self.failures_before_success = failures_before_success
        self.failure_factory = failure_factory or (
            lambda: ConnectionResetError("simulated transient connection failure")
        )
        self.executed: list[str] = []
        self.session_statements: list[str] = []
        self.session_params: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions: list[dict[str, Any]] = []
        self.closed = False
        self.attempts = 0
        self.in_use = 0
        self.acquires = 0
        self.peak_in_use = 0
        self._failures_before_success = failures_before_success
        self._remaining_failures = failures_before_success

    @property
    def failures_before_success(self) -> int:
        """Number of failures still programmed for this pool.

        Assigning to this property also resets the pending failure budget, so a
        pool can be made flaky *after* it has been used (e.g. schema loaded
        successfully, then the database "goes down").
        """
        return self._failures_before_success

    @failures_before_success.setter
    def failures_before_success(self, value: int) -> None:
        self._failures_before_success = value
        self._remaining_failures = value

    @property
    def failures_remaining(self) -> int:
        """Pending failures that will still be raised by :meth:`run`."""
        return self._remaining_failures

    def reset_attempts(self) -> None:
        """Reset the statement counter (used to isolate one request in demos)."""
        self.attempts = 0

    def clear_recordings(self) -> None:
        """Forget everything recorded so far (used after schema introspection).

        Schema introspection issues dozens of statements; tests that assert on
        *user* queries clear the recordings first so the assertions stay readable.
        """
        self.executed.clear()
        self.session_statements.clear()
        self.session_params.clear()
        self.transactions.clear()
        self.attempts = 0
        self.acquires = 0
        self.peak_in_use = 0

    async def run(self, sql: str, args: tuple[Any, ...]) -> Any:
        """Dispatch a statement, applying latency and programmed failures.

        Args:
            sql: SQL statement.
            args: Statement parameters.

        Returns:
            Any: Handler result.

        Raises:
            BaseException: The programmed transient failure, when still pending.
        """
        if self.latency:
            await asyncio.sleep(self.latency)
        self.attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise self.failure_factory()
        if not sql.lstrip().upper().startswith(("SET", "BEGIN", "COMMIT")):
            self.executed.append(sql)
        return self._handler(sql, args)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        """Acquire a fake connection.

        Yields:
            FakeConnection: The connection double.
        """
        self.in_use += 1
        self.acquires += 1
        if self.in_use > self.peak_in_use:
            self.peak_in_use = self.in_use
        conn = FakeConnection(self)
        try:
            yield conn
        finally:
            self.in_use -= 1

    async def close(self) -> None:
        """Mark the pool as closed."""
        self.closed = True

    def terminate(self) -> None:
        """Mark the pool as terminated."""
        self.closed = True

    def get_size(self) -> int:
        """Report the (fake) pool size."""
        return 5 if not self.closed else 0

    def get_idle_size(self) -> int:
        """Report the (fake) number of idle connections."""
        return 5 if not self.closed else 0

    @property
    def executed_sql(self) -> list[str]:
        """Return the user queries executed against this pool."""
        return list(self.executed)


class StubSQLGenerator:
    """Programmable stand-in for :class:`~pg_mcp.services.sql_generator.SQLGenerator`.

    Answers are consumed in order. Each answer can be:
    - ``str``: returned as the generated SQL,
    - ``BaseException``: raised,
    - ``Callable[[dict], str]``: invoked with the call kwargs.

    When the script is exhausted the last answer is repeated.
    """

    def __init__(
        self,
        answers: Sequence[Any],
        *,
        latency: float = 0.0,
        tokens: int | None = 42,
    ) -> None:
        """Initialize the stub.

        Args:
            answers: Scripted responses.
            latency: Artificial latency per call in seconds.
            tokens: Token count reported through ``last_tokens_used``.
        """
        if not answers:
            raise ValueError("At least one answer is required")
        self.answers = list(answers)
        self.latency = latency
        self.tokens = tokens
        self.last_tokens_used: int | None = None
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        question: str,
        schema: Any,
        context: str | None = None,
        previous_attempt: str | None = None,
        error_feedback: str | None = None,
    ) -> str:
        """Return the next scripted answer.

        Args:
            question: Natural language question.
            schema: Database schema (ignored).
            context: Extra context (ignored).
            previous_attempt: Previous SQL, recorded for assertions.
            error_feedback: Validation error feedback, recorded for assertions.

        Returns:
            str: Generated SQL.

        Raises:
            BaseException: When the scripted answer is an exception.
        """
        self.calls.append(
            {
                "question": question,
                "previous_attempt": previous_attempt,
                "error_feedback": error_feedback,
                "started_at": time.monotonic(),
            }
        )
        if self.latency:
            await asyncio.sleep(self.latency)

        index = min(len(self.calls) - 1, len(self.answers) - 1)
        answer = self.answers[index]
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(
                {
                    "question": question,
                    "previous_attempt": previous_attempt,
                    "error_feedback": error_feedback,
                }
            )
        self.last_tokens_used = self.tokens
        return str(answer)


class StubResultValidator:
    """Stand-in for :class:`~pg_mcp.services.result_validator.ResultValidator`."""

    def __init__(
        self,
        confidence: int = 95,
        *,
        explanation: str = "Results appear to answer the question.",
        suggestion: str | None = None,
        error: BaseException | None = None,
        latency: float = 0.0,
        tokens: int | None = 12,
    ) -> None:
        """Initialize the stub.

        Args:
            confidence: Confidence score to report.
            explanation: Explanation text to report.
            suggestion: Optional suggestion text.
            error: When set, ``validate`` raises this error instead.
            latency: Artificial latency per call in seconds.
            tokens: Token count reported through ``last_tokens_used``.
        """
        self.confidence = confidence
        self.explanation = explanation
        self.suggestion = suggestion
        self.error = error
        self.latency = latency
        self.tokens = tokens
        self.last_tokens_used: int | None = None
        self.calls = 0

    async def validate(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
    ) -> ResultValidationResult:
        """Validate (fake) query results.

        Args:
            question: Original question.
            sql: Executed SQL.
            results: Result rows.
            row_count: Total row count.

        Returns:
            ResultValidationResult: Scripted validation outcome.

        Raises:
            BaseException: The programmed error, when set.
        """
        self.calls += 1
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.error is not None:
            raise self.error
        self.last_tokens_used = self.tokens
        return ResultValidationResult(
            confidence=self.confidence,
            explanation=self.explanation,
            suggestion=self.suggestion,
            is_acceptable=self.confidence >= 70,
        )


def make_pool_factory(pools: dict[str, FakePool]) -> Callable[[list[DatabaseConfig]], Any]:
    """Build a ``create_pools`` replacement backed by fake pools.

    Args:
        pools: Fake pools keyed by database name.

    Returns:
        Callable[[list[DatabaseConfig]], Any]: Async factory returning
        ``{db_name: pool}`` for the requested configurations.
    """

    async def factory(configs: list[DatabaseConfig]) -> dict[str, FakePool]:
        missing = [cfg.name for cfg in configs if cfg.name not in pools]
        if missing:
            raise KeyError(f"No fake pool registered for databases: {missing}")
        return {cfg.name: pools[cfg.name] for cfg in configs}

    return factory


DEFAULT_TABLES: tuple[OfflineTable, ...] = (
    OfflineTable(
        name="users",
        columns=(
            ("id", "integer"),
            ("email", "character varying"),
            ("password_hash", "character varying"),
            ("created_at", "timestamp with time zone"),
        ),
        comment="Application users",
    ),
    OfflineTable(
        name="orders",
        columns=(
            ("id", "integer"),
            ("user_id", "integer"),
            ("total_amount", "numeric"),
            ("status", "character varying"),
        ),
        comment="Customer orders",
    ),
    OfflineTable(
        name="payments",
        columns=(
            ("id", "integer"),
            ("order_id", "integer"),
            ("card_number", "character varying"),
        ),
        comment="Sensitive payment records",
    ),
)


def build_offline_settings(
    *,
    databases: Sequence[str] = ("analytics", "billing"),
    blocked_tables: Sequence[str] = (),
    blocked_columns: Sequence[str] = (),
    allow_explain: bool = False,
    max_question_length: int = 10000,
    min_confidence_score: int = 70,
    max_retries: int = 2,
    retry_delay: float = 0.05,
    backoff_factor: float = 2.0,
    max_concurrent_queries: int = 10,
    max_concurrent_llm_calls: int = 5,
    acquire_timeout: float = 5.0,
    validation_enabled: bool = True,
    metrics_enabled: bool = False,
) -> Settings:
    """Create a :class:`Settings` instance suitable for offline runs.

    Args:
        databases: Logical names of the databases to configure.
        blocked_tables: Sensitive tables for the security policy.
        blocked_columns: Sensitive columns for the security policy.
        allow_explain: Whether EXPLAIN statements are permitted.
        max_question_length: Input length guard.
        min_confidence_score: Confidence threshold for warnings.
        max_retries: Retry attempts for transient failures.
        retry_delay: Initial retry delay (small by default to keep demos fast).
        backoff_factor: Exponential backoff multiplier.
        max_concurrent_queries: Database concurrency limit.
        max_concurrent_llm_calls: LLM concurrency limit.
        acquire_timeout: Seconds to wait for a rate limiter slot.
        validation_enabled: Whether LLM result validation runs.
        metrics_enabled: Kept False so no metrics HTTP port is opened.

    Returns:
        Settings: Fully populated settings object (no ``.env`` needed).
    """
    db_configs = [
        DatabaseConfig(
            name=name,
            host="offline-fixture",
            port=5432,
            user="readonly_user",
            password="offline-secret",  # noqa: S106 - fixture credential, never connects
            min_pool_size=1,
            max_pool_size=2,
        )
        for name in databases
    ]

    return Settings(
        environment="development",
        databases=db_configs,
        openai=OpenAIConfig(api_key=SecretStr("sk-offline-demo-key"), model="offline-stub"),
        security=SecurityConfig(
            blocked_tables=list(blocked_tables),
            blocked_columns=list(blocked_columns),
            allow_explain=allow_explain,
        ),
        validation=ValidationConfig(
            enabled=validation_enabled,
            max_question_length=max_question_length,
            min_confidence_score=min_confidence_score,
        ),
        cache=CacheConfig(enabled=True),
        resilience=ResilienceConfig(
            max_retries=max_retries,
            retry_delay=retry_delay,
            backoff_factor=backoff_factor,
            max_concurrent_queries=max_concurrent_queries,
            max_concurrent_llm_calls=max_concurrent_llm_calls,
            acquire_timeout=acquire_timeout,
        ),
        observability=ObservabilityConfig(metrics_enabled=metrics_enabled, log_level="WARNING"),
    )
