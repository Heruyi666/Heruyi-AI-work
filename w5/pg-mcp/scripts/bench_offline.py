#!/usr/bin/env python
"""Concurrency and resource bench for pg-mcp (fully offline).

Unit tests prove each behaviour once.  This bench proves they *hold under
load*, which is where the interesting failure modes live:

1. **Rate-limit compliance** -- the configured concurrency ceiling must never
   be exceeded, no matter how many callers arrive at once.
2. **No connection leak** -- after a burst, no connection may still be held.
3. **Retry budget** -- a failing dependency must not be hammered more times
   than ``max_retries + 1``.
4. **Latency profile** -- p50/p95 of the happy path.

Usage::

    uv run python scripts/bench_offline.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pg_mcp.app import AppComponents, build_components  # noqa: E402
from pg_mcp.models.query import QueryRequest  # noqa: E402
from pg_mcp.observability.logging import configure_logging  # noqa: E402
from pg_mcp.observability.metrics import MetricsCollector  # noqa: E402
from pg_mcp.testing.fakes import (  # noqa: E402
    DEFAULT_TABLES,
    FakePool,
    StubResultValidator,
    StubSQLGenerator,
    build_offline_settings,
    make_pool_factory,
    offline_pg_handler,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one assertion.

    Args:
        name: Assertion label.
        ok: Whether it held.
        detail: Extra context printed alongside.
    """
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  {detail}" if detail else ""))


def field(label: str, value: Any) -> None:
    """Print an observed value.

    Args:
        label: Name of the measurement.
        value: Observed value.
    """
    print(f"         - {label}: {value}")


def topic(title: str) -> None:
    """Print a section header.

    Args:
        title: Section title.
    """
    print(f"\n{title}")


async def build(
    settings: Any,
    pools: dict[str, FakePool],
    script: list[str],
    *,
    metrics: MetricsCollector | None = None,
    latency: float = 0.0,
) -> AppComponents:
    """Wire an offline component graph.

    Args:
        settings: Settings to use.
        pools: Fake pools keyed by database name.
        script: SQL answers handed out by the stub generator.
        metrics: Optional metrics collector.
        latency: Artificial per-statement latency.

    Returns:
        AppComponents: The wired graph.
    """
    return await build_components(
        settings,
        pool_factory=make_pool_factory(pools),
        sql_generator=StubSQLGenerator(script),
        result_validator=StubResultValidator(confidence=95),
        metrics=metrics,
        load_schemas=False,
    )


async def bench_rate_limit_compliance() -> None:
    """The DB concurrency ceiling must hold under a burst.

    Two distinct behaviours are measured, because the limiter is a semaphore:

    * ``1a`` generous acquire timeout -> callers **queue**; nothing is
      rejected, but the number of connections in use must never exceed the
      ceiling.
    * ``1b`` tiny acquire timeout -> callers that cannot get a slot fast
      enough are **rejected** with ``rate_limit_exceeded``.
    """
    topic("[1a] DB concurrency ceiling under a 120-request burst (queueing mode)")

    ceiling = 8
    settings = build_offline_settings(
        databases=("analytics",),
        max_concurrent_queries=ceiling,
        max_concurrent_llm_calls=ceiling,
        max_retries=0,
        acquire_timeout=5.0,
    )
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES), latency=0.01)
    components = await build(settings, {"analytics": pool}, ["SELECT COUNT(*) FROM users;"])

    # Warm up so lazy schema introspection does not pollute the measurements.
    await components.orchestrator.execute_query(
        QueryRequest(question="How many users?", database="analytics")
    )
    pool.clear_recordings()

    async def one() -> bool:
        response = await components.orchestrator.execute_query(
            QueryRequest(question="How many users?", database="analytics")
        )
        return bool(response.success)

    results = await asyncio.gather(*[one() for _ in range(120)], return_exceptions=True)
    successes = sum(1 for r in results if r is True)
    errors = sum(1 for r in results if isinstance(r, BaseException))

    field("configured ceiling", ceiling)
    field("requests issued", len(results))
    field("peak connections in use (true concurrency)", pool.peak_in_use)
    field("successes", successes)
    field("unexpected exceptions", errors)
    field("connections still held", pool.in_use)

    check(
        "peak DB concurrency never exceeded the ceiling",
        pool.peak_in_use <= ceiling,
        f"peak={pool.peak_in_use} ceiling={ceiling}",
    )
    check("queueing mode rejected nobody", successes == 120, f"successes={successes}")
    check("no connection left in use (no leak)", pool.in_use == 0, f"in_use={pool.in_use}")

    await components.shutdown()

    topic("[1b] Overload handling when callers cannot get a slot in time")

    settings = build_offline_settings(
        databases=("analytics",),
        max_concurrent_queries=2,
        max_concurrent_llm_calls=2,
        max_retries=0,
        # 0.1s is the configured lower bound (guards against typos that would
        # reject almost everything); it is still far below the ~1.5s it takes
        # to drain 60 requests through 2 slots.
        acquire_timeout=0.1,
    )
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES), latency=0.05)
    components = await build(settings, {"analytics": pool}, ["SELECT COUNT(*) FROM users;"])

    results = await asyncio.gather(*[one() for _ in range(60)], return_exceptions=True)
    rejected = 0
    for r in results:
        if isinstance(r, BaseException):
            continue
        if not r:
            rejected += 1

    field("configured ceiling", 2)
    field("requests issued", len(results))
    field("peak connections in use", pool.peak_in_use)
    field("rejected (rate_limit_exceeded)", rejected)

    check(
        "a tight timeout turns overload into explicit rejections",
        rejected > 0,
        f"rejected={rejected}",
    )
    check(
        "even in overload the ceiling held",
        pool.peak_in_use <= 2,
        f"peak={pool.peak_in_use}",
    )
    check("no connection leaked under overload", pool.in_use == 0)

    await components.shutdown()


async def bench_retry_budget() -> None:
    """A failing dependency is not hammered beyond the policy."""
    topic("[2] Retry budget under persistent failure")

    max_retries = 2
    settings = build_offline_settings(
        databases=("analytics",),
        max_retries=max_retries,
        retry_delay=0.02,
        backoff_factor=1.5,
    )
    pool = FakePool(
        "analytics",
        handler=offline_pg_handler(DEFAULT_TABLES),
        failures_before_success=999,
    )
    components = await build(settings, {"analytics": pool}, ["SELECT COUNT(*) FROM users;"])

    attempts_before = pool.attempts
    errors = 0
    for _ in range(10):
        response = await components.orchestrator.execute_query(
            QueryRequest(question="How many users?", database="analytics")
        )
        if not response.success:
            errors += 1

    used = pool.attempts - attempts_before
    budget = (max_retries + 1) * 10

    field("requests", 10)
    field("max_retries", max_retries)
    field("total DB attempts", used)
    field("theoretical upper bound", budget)
    field("failed requests", errors)

    check(
        "total attempts stayed within max_retries+1 per request",
        used <= budget,
        f"used={used} bound={budget}",
    )
    check("every request failed as expected", errors == 10, f"errors={errors}")
    check("no connection leaked during failures", pool.in_use == 0)

    await components.shutdown()


async def bench_latency_profile() -> None:
    """Measure the happy-path latency distribution."""
    topic("[3] Latency profile (sequential happy path)")

    settings = build_offline_settings(databases=("analytics",), max_retries=0)
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES), latency=0.005)
    components = await build(settings, {"analytics": pool}, ["SELECT COUNT(*) FROM users;"])

    durations: list[float] = []
    for _ in range(50):
        start = time.perf_counter()
        await components.orchestrator.execute_query(
            QueryRequest(question="How many users?", database="analytics")
        )
        durations.append(time.perf_counter() - start)

    p50 = statistics.median(durations)
    p95 = sorted(durations)[int(len(durations) * 0.95)]
    total = sum(durations)

    field("requests", len(durations))
    field("p50", f"{p50 * 1000:.1f} ms")
    field("p95", f"{p95 * 1000:.1f} ms")
    field("total", f"{total * 1000:.1f} ms")
    field("throughput", f"{len(durations) / total:.0f} req/s (sequential)")

    check("p95 stays under 100 ms for the offline path", p95 < 0.1, f"p95={p95 * 1000:.1f}ms")
    check("no connection leak after the run", pool.in_use == 0)

    await components.shutdown()


async def bench_metric_cardinality() -> None:
    """Metrics stay bounded: series must not grow forever with requests.

    Label values are drawn from a fixed vocabulary (database name, status),
    so the series count may grow while new combinations appear -- but it must
    stop growing once every combination has been seen.
    """
    topic("[4] Metric cardinality stabilises under load")

    metrics = MetricsCollector()
    metrics.reset_all_metrics()
    settings = build_offline_settings(databases=("analytics",), max_retries=0)
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
    components = await build(
        settings, {"analytics": pool}, ["SELECT COUNT(*) FROM users;"], metrics=metrics
    )

    async def burst(n: int) -> None:
        for _ in range(n):
            await components.orchestrator.execute_query(
                QueryRequest(question="How many users?", database="analytics")
            )

    await burst(20)
    after_first = len(metrics.snapshot())
    await burst(60)
    after_second = len(metrics.snapshot())

    field("distinct series after 20 requests", after_first)
    field("distinct series after 80 requests", after_second)

    check(
        "series count stabilises (no per-request label explosion)",
        after_second == after_first,
        f"first={after_first} second={after_second}",
    )
    check("series count stays small", after_second < 50, f"series={after_second}")
    check("no connection leak", pool.in_use == 0)

    await components.shutdown()


async def main() -> int:
    """Run every bench scenario.

    Returns:
        int: Process exit code (0 when every check passed).
    """
    configure_logging(level="ERROR")
    print("=" * 78)
    print("pg-mcp concurrency / resource bench (offline, no PostgreSQL required)")
    print("=" * 78)

    await bench_rate_limit_compliance()
    await bench_retry_budget()
    await bench_latency_profile()
    await bench_metric_cardinality()

    print()
    print("=" * 78)
    print(f"Summary: {PASS} passed, {FAIL} failed")
    print("=" * 78)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
