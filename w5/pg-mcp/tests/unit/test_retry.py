"""Unit tests for the retry/backoff module.

These tests cover the ``resilience/retry.py`` module that was missing entirely
before (design §7 promised retry/backoff, ``ResilienceConfig`` fields were dead).
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from pg_mcp.config.settings import ResilienceConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ExecutionTimeoutError,
    LLMError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.retry import (
    RetryPolicy,
    is_retryable_db_error,
    is_retryable_llm_error,
    retry_async,
)


def _sleeper(record: list[float]) -> Callable[[float], Awaitable[None]]:
    """Build an async sleep function that records delays instead of waiting.

    Args:
        record: List the recorded delays are appended to.

    Returns:
        Callable[[float], Awaitable[None]]: Async sleep double.
    """

    async def _sleep(delay: float) -> None:
        record.append(delay)

    return _sleep


class TestRetryPolicy:
    """Tests for the RetryPolicy value object."""

    def test_defaults(self) -> None:
        """Default policy is 3 retries with exponential backoff."""
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.total_attempts == 4
        assert policy.initial_delay == 1.0
        assert policy.backoff_factor == 2.0

    def test_from_config_consumes_resilience_config(self) -> None:
        """ResilienceConfig values (previously unused) drive the policy."""
        config = ResilienceConfig(
            max_retries=4,
            retry_delay=0.25,
            backoff_factor=3.0,
            max_retry_delay=5.0,
            retry_jitter=0.0,
        )
        policy = RetryPolicy.from_config(config)
        assert policy.max_retries == 4
        assert policy.initial_delay == 0.25
        assert policy.backoff_factor == 3.0
        assert policy.max_delay == 5.0

    def test_delay_grows_exponentially_and_is_capped(self) -> None:
        """Delays follow initial_delay * factor**attempt, capped by max_delay."""
        policy = RetryPolicy(
            max_retries=5,
            initial_delay=0.5,
            backoff_factor=2.0,
            max_delay=3.0,
            jitter=0.0,
        )
        delays = [policy.delay_for_attempt(i, randomize=False) for i in range(5)]
        assert delays == [0.5, 1.0, 2.0, 3.0, 3.0]

    def test_jitter_stays_within_configured_bounds(self) -> None:
        """Jitter only ever increases the delay, by at most the jitter fraction."""
        policy = RetryPolicy(initial_delay=1.0, backoff_factor=1.0, jitter=0.5)
        for _ in range(50):
            delay = policy.delay_for_attempt(0)
            assert 1.0 <= delay <= 1.5

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"max_retries": -1}, "max_retries"),
            ({"initial_delay": -1.0}, "initial_delay"),
            ({"backoff_factor": 0.5}, "backoff_factor"),
            ({"jitter": 2.0}, "jitter"),
        ],
    )
    def test_invalid_policy_rejected(self, kwargs: dict[str, float], message: str) -> None:
        """Invalid policy values raise ValueError."""
        with pytest.raises(ValueError, match=message):
            RetryPolicy(**kwargs)  # type: ignore[arg-type]


class TestRetryAsync:
    """Tests for the retry_async helper."""

    @pytest.mark.asyncio
    async def test_success_without_retry(self) -> None:
        """A successful first attempt performs no sleeping."""
        sleeps: list[float] = []

        async def operation() -> str:
            return "ok"

        result = await retry_async(
            operation,
            policy=RetryPolicy(max_retries=3),
            sleep=_sleeper(sleeps),
        )
        assert result == "ok"
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_retries_then_succeeds_with_backoff(self) -> None:
        """Transient failures are retried with increasing delays."""
        sleeps: list[float] = []
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("transient")
            return "ok"

        result = await retry_async(
            operation,
            policy=RetryPolicy(max_retries=3, initial_delay=0.1, backoff_factor=2.0, jitter=0.0),
            is_retryable=lambda exc: isinstance(exc, ConnectionError),
            sleep=_sleeper(sleeps),
        )
        assert result == "ok"
        assert attempts == 3
        assert sleeps == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self) -> None:
        """Non-retryable errors are not retried."""
        attempts = 0
        sleeps: list[float] = []

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            await retry_async(
                operation,
                policy=RetryPolicy(max_retries=3),
                is_retryable=lambda exc: isinstance(exc, ConnectionError),
                sleep=_sleeper(sleeps),
            )
        assert attempts == 1
        assert sleeps == []

    @pytest.mark.asyncio
    async def test_exhausted_retries_raise_last_error(self) -> None:
        """The last error propagates once the policy is exhausted."""
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            raise ConnectionError(f"failure {attempts}")

        with pytest.raises(ConnectionError, match="failure 3"):
            await retry_async(
                operation,
                policy=RetryPolicy(max_retries=2, initial_delay=0.0, jitter=0.0),
                sleep=asyncio.sleep,
            )
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_metrics_and_callback_are_recorded(self) -> None:
        """Retry attempts and callbacks are recorded for observability."""
        metrics = MetricsCollector()
        metrics.reset_all_metrics()
        retries: list[tuple[int, str, float]] = []
        attempts = 0

        async def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("transient")
            return "ok"

        await retry_async(
            operation,
            policy=RetryPolicy(max_retries=2, initial_delay=0.05, jitter=0.0),
            is_retryable=lambda exc: True,
            operation_name="db.execute",
            metrics=metrics,
            on_retry=lambda attempt, error, delay: retries.append((attempt, str(error), delay)),
            sleep=asyncio.sleep,
        )
        assert retries == [(0, "transient", 0.05)]
        snapshot = metrics.snapshot()
        retry_key = 'pg_mcp_retry_attempts_total{operation="db.execute",outcome="retried"}'
        assert snapshot[retry_key] == 1.0


class TestRetryPredicates:
    """Tests for the transient-error predicates."""

    def test_connection_errors_are_retryable(self) -> None:
        """Connection resets and OSErrors are transient."""
        assert is_retryable_db_error(ConnectionResetError("reset"))
        assert is_retryable_db_error(TimeoutError("timed out"))

    def test_retryable_sqlstate_via_pgmcperror_details(self) -> None:
        """Serialization failures/deadlocks reported in details are retryable."""
        error = DatabaseError(
            "deadlock detected",
            details={"error_code": "40P01"},
        )
        assert is_retryable_db_error(error)

        fatal = DatabaseError("syntax error", details={"error_code": "42601"})
        assert not is_retryable_db_error(fatal)

    def test_query_timeouts_are_not_retried(self) -> None:
        """Our own execution timeout is a policy decision, not a transient fault."""
        assert not is_retryable_db_error(ExecutionTimeoutError("too slow"))

    def test_llm_predicates(self) -> None:
        """Timeouts/unavailable are retryable, bad requests are not."""
        assert is_retryable_llm_error(LLMTimeoutError("timeout"))
        assert is_retryable_llm_error(LLMUnavailableError("API rate limit exceeded"))
        assert not is_retryable_llm_error(LLMError("invalid request payload"))
