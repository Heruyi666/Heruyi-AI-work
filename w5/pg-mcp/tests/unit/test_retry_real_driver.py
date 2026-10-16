"""Retry decisions must hold for the *real* asyncpg driver, not only for fakes.

The offline suite exercises ``is_retryable_db_error`` with hand-rolled
exceptions.  That proves the policy logic, but not that the policy recognises
what asyncpg actually raises.  These tests instantiate genuine
``asyncpg`` exception classes -- no server required, every class carries its
SQLSTATE as a class attribute -- and assert the verdict.

This closes the gap "resilience verified only against doubles" without
needing a live PostgreSQL instance.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from pg_mcp.resilience.retry import (
    RETRYABLE_POSTGRES_SQLSTATES,
    RetryPolicy,
    is_retryable_db_error,
    retry_async,
)

# (asyncpg class, expected verdict, why)
REAL_DRIVER_CASES: list[tuple[type[BaseException], bool, str]] = [
    (asyncpg.ConnectionDoesNotExistError, True, "connection gone -> transient"),
    (asyncpg.PostgresConnectionError, True, "transport failure -> transient"),
    (asyncpg.TooManyConnectionsError, True, "pool exhausted -> transient"),
    (asyncpg.DeadlockDetectedError, True, "deadlock -> transient"),
    (asyncpg.SerializationError, True, "serialization failure -> transient"),
    (asyncpg.QueryCanceledError, False, "user/statement cancel -> not retried"),
    (asyncpg.DivisionByZeroError, False, "logic error -> not retried"),
    (asyncpg.UndefinedTableError, False, "schema error -> not retried"),
    (asyncpg.PostgresSyntaxError, False, "bad SQL -> not retried"),
    (asyncpg.CheckViolationError, False, "constraint error -> not retried"),
    (asyncpg.InsufficientPrivilegeError, False, "permission error -> not retried"),
]


def _instantiate(cls: type[BaseException]) -> BaseException:
    """Build an instance of an asyncpg error without touching a server.

    Args:
        cls: The asyncpg exception class to instantiate.

    Returns:
        BaseException: A usable exception instance carrying its SQLSTATE.
    """
    try:
        return cls("simulated driver failure")  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - defensive for odd signatures
        return cls()  # type: ignore[call-arg]


def _sqlstate_of(cls: type[BaseException]) -> str | None:
    """Read the SQLSTATE declared by an asyncpg exception class.

    Args:
        cls: The asyncpg exception class.

    Returns:
        str | None: The SQLSTATE string, when the class declares one.
    """
    return getattr(cls, "sqlstate", None)


class TestRealAsyncpgErrorClassification:
    """``is_retryable_db_error`` must agree with asyncpg's own taxonomy."""

    @pytest.mark.parametrize(
        ("error_cls", "expected", "why"),
        REAL_DRIVER_CASES,
        ids=[c[0].__name__ for c in REAL_DRIVER_CASES],
    )
    def test_verdict_matches_real_driver_class(
        self, error_cls: type[BaseException], expected: bool, why: str
    ) -> None:
        """Real asyncpg exception classes are classified as documented."""
        error = _instantiate(error_cls)
        assert is_retryable_db_error(error) is expected, (
            f"{error_cls.__name__} sqlstate={_sqlstate_of(error_cls)!r}: {why}"
        )

    def test_every_retryable_class_declares_a_known_sqlstate(self) -> None:
        """Class-level SQLSTATEs must be ones our policy actually lists.

        Guards against a class being retryable for an accidental reason
        (e.g. subclassing) while its SQLSTATE is unknown to the policy.
        """
        for cls, expected, _ in REAL_DRIVER_CASES:
            if not expected:
                continue
            state = _sqlstate_of(cls)
            assert state is not None, f"{cls.__name__} declares no SQLSTATE"
            assert state in RETRYABLE_POSTGRES_SQLSTATES, (
                f"{cls.__name__} sqlstate={state!r} missing from RETRYABLE_POSTGRES_SQLSTATES"
            )

    def test_non_retryable_classes_are_not_covered_by_the_sqlstate_set(self) -> None:
        """A negative verdict must not come from a missing SQLSTATE entry."""
        for cls, expected, _ in REAL_DRIVER_CASES:
            if expected:
                continue
            state = _sqlstate_of(cls)
            if state is None:
                continue
            assert state not in RETRYABLE_POSTGRES_SQLSTATES, (
                f"{cls.__name__} has retryable sqlstate {state!r} but is expected fatal"
            )


class TestRetryAgainstRealDriverErrors:
    """End-to-end retry behaviour when the *real* driver raises."""

    async def test_transient_connection_error_is_retried_then_recovers(self) -> None:
        """A real ``ConnectionDoesNotExistError`` triggers real backoff retries."""
        attempts = 0
        delays: list[float] = []

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise _instantiate(asyncpg.ConnectionDoesNotExistError)
            return "ok"

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        policy = RetryPolicy(max_retries=3, initial_delay=0.01, backoff_factor=2.0, jitter=0.0)

        result = await retry_async(
            operation,
            policy=policy,
            is_retryable=is_retryable_db_error,
            operation_name="db.execute",
            sleep=fake_sleep,
        )

        assert result == "ok"
        assert attempts == 3
        assert delays == pytest.approx([0.01, 0.02], rel=0.05)

    async def test_fatal_driver_error_is_not_retried(self) -> None:
        """A real ``UndefinedTableError`` fails fast instead of burning retries."""
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise _instantiate(asyncpg.UndefinedTableError)

        policy = RetryPolicy(max_retries=3, initial_delay=0.01, jitter=0.0)

        with pytest.raises(asyncpg.UndefinedTableError):
            await retry_async(
                operation,
                policy=policy,
                is_retryable=is_retryable_db_error,
                operation_name="db.execute",
                sleep=lambda _d: asyncio.sleep(0),
            )

        assert attempts == 1, "fatal error must not consume the retry budget"

    async def test_query_cancel_does_not_retry(self) -> None:
        """A cancelled query is a user decision, not a transient fault."""
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise _instantiate(asyncpg.QueryCanceledError)

        policy = RetryPolicy(max_retries=3, initial_delay=0.01, jitter=0.0)

        with pytest.raises(asyncpg.QueryCanceledError):
            await retry_async(
                operation,
                policy=policy,
                is_retryable=is_retryable_db_error,
                operation_name="db.execute",
                sleep=lambda _d: asyncio.sleep(0),
            )

        assert attempts == 1
