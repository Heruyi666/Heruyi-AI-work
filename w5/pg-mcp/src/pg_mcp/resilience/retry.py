"""Retry with exponential backoff for transient failures.

This module implements the retry/backoff behaviour promised by the design
document (§7 Resilience) and consumes ``ResilienceConfig`` values that were
previously defined but never used (``max_retries``, ``retry_delay``,
``backoff_factor``).

Typical usage::

    policy = RetryPolicy.from_config(settings.resilience)
    rows = await retry_async(
        lambda: pool.fetch(sql),
        policy=policy,
        is_retryable=is_retryable_db_error,
        operation="db.execute",
    )
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import asyncpg

from pg_mcp.config.settings import ResilienceConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError, PgMcpError

T = TypeVar("T")

__all__ = [
    "RetryPolicy",
    "retry_async",
    "is_retryable_db_error",
    "is_retryable_llm_error",
    "RETRYABLE_POSTGRES_SQLSTATES",
]


# PostgreSQL SQLSTATEs that are safe to retry (transient by nature).
RETRYABLE_POSTGRES_SQLSTATES: frozenset[str] = frozenset(
    {
        "08000",  # connection_exception
        "08003",  # connection_does_not_exist
        "08006",  # connection_failure
        "08001",  # sqlclient_unable_to_establish_sqlconnection
        "08004",  # sqlserver_rejected_establishment_of_sqlconnection
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "53300",  # too_many_connections
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative retry policy with exponential backoff and jitter.

    Attributes:
        max_retries: Number of *additional* attempts after the first one.
        initial_delay: Delay in seconds before the first retry.
        backoff_factor: Multiplier applied to the delay after each attempt.
        max_delay: Upper bound for a single delay in seconds.
        jitter: Fraction of the delay randomised (0 disables jitter).
    """

    max_retries: int = 3
    initial_delay: float = 1.0
    backoff_factor: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.1

    def __post_init__(self) -> None:
        """Validate the policy definition."""
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.initial_delay < 0:
            raise ValueError("initial_delay must be >= 0")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if not 0.0 <= self.jitter <= 1.0:
            raise ValueError("jitter must be between 0 and 1")

    @classmethod
    def from_config(cls, config: ResilienceConfig) -> "RetryPolicy":
        """Build a policy from :class:`ResilienceConfig`.

        Args:
            config: Resilience configuration section.

        Returns:
            RetryPolicy: Policy derived from the configuration values.
        """
        return cls(
            max_retries=config.max_retries,
            initial_delay=config.retry_delay,
            backoff_factor=config.backoff_factor,
            max_delay=config.max_retry_delay,
            jitter=config.retry_jitter,
        )

    @property
    def total_attempts(self) -> int:
        """Maximum number of attempts performed by this policy."""
        return self.max_retries + 1

    def delay_for_attempt(self, attempt: int, *, randomize: bool = True) -> float:
        """Compute the delay to apply before the given retry attempt.

        Args:
            attempt: Zero-based index of the *failed* attempt (0 for the first).
            randomize: Whether to apply jitter (disabled in deterministic tests).

        Returns:
            float: Delay in seconds, never negative.
        """
        delay = self.initial_delay * (self.backoff_factor**attempt)
        delay = min(delay, self.max_delay)
        if randomize and self.jitter > 0:
            # Full jitter on the upper side only keeps the floor predictable.
            # Jitter only spreads retries in time; it is not security relevant.
            delay *= 1.0 + random.uniform(0.0, self.jitter)  # noqa: S311
        return max(0.0, delay)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[BaseException], bool] | None = None,
    operation_name: str = "operation",
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    metrics: Any | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Execute an async operation with retry and exponential backoff.

    Args:
        operation: Zero-argument callable returning an awaitable.
        policy: Retry policy describing attempts and delays.
        is_retryable: Predicate deciding whether an exception is transient.
            When omitted, every exception is treated as retryable.
        operation_name: Label used in logs and metrics.
        on_retry: Optional callback ``(attempt, error, delay)`` invoked before sleeping.
        metrics: Optional :class:`~pg_mcp.observability.metrics.MetricsCollector`
            used to record retry attempts.
        sleep: Injectable sleep function (defaults to :func:`asyncio.sleep`).

    Returns:
        T: The value returned by a successful attempt.

    Raises:
        BaseException: The last exception raised when retries are exhausted or
            when the error is not retryable.
    """
    sleeper = sleep or asyncio.sleep
    last_error: BaseException | None = None

    for attempt in range(policy.total_attempts):
        try:
            return await operation()
        except BaseException as error:
            last_error = error
            retryable = is_retryable(error) if is_retryable is not None else True
            attempts_left = policy.max_retries - attempt

            if not retryable or attempts_left <= 0:
                if metrics is not None:
                    metrics.increment_retry_attempts(
                        operation=operation_name, outcome="exhausted" if retryable else "fatal"
                    )
                raise

            delay = policy.delay_for_attempt(attempt)
            if metrics is not None:
                metrics.increment_retry_attempts(operation=operation_name, outcome="retried")
            if on_retry is not None:
                on_retry(attempt, error, delay)
            await sleeper(delay)

    # Defensive: the loop always returns or raises.
    raise AssertionError("unreachable") from last_error  # pragma: no cover


def is_retryable_db_error(error: BaseException) -> bool:
    """Decide whether a database error is transient and worth retrying.

    Args:
        error: Exception raised while talking to PostgreSQL.

    Returns:
        bool: True when the error is considered transient.
    """
    if isinstance(
        error,
        (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            TimeoutError,
            ConnectionError,
            OSError,
        ),
    ):
        return True

    sqlstate = getattr(error, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate in RETRYABLE_POSTGRES_SQLSTATES:
        return True

    if isinstance(error, PgMcpError):
        details = error.details or {}
        detail_state = details.get("error_code")
        if isinstance(detail_state, str) and detail_state in RETRYABLE_POSTGRES_SQLSTATES:
            return True
        if details.get("retryable") is True:
            return True

    return False


def is_retryable_llm_error(error: BaseException) -> bool:
    """Decide whether an LLM error is transient and worth retrying.

    Timeouts and "service unavailable" conditions (including rate limits) are
    retried; invalid requests and parse failures are not.

    Args:
        error: Exception raised while calling the LLM.

    Returns:
        bool: True when the error is considered transient.
    """
    if isinstance(error, (LLMTimeoutError, LLMUnavailableError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, LLMError):
        return "rate limit" in str(error).lower()
    return False
