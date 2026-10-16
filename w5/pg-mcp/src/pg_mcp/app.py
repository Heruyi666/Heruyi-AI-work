"""Application composition root.

This module wires the whole application together in one place so that:

* the MCP server (``pg_mcp.server``) and offline tooling/tests share exactly the
  same object graph — no more "the server always uses a single executor";
* dependencies such as the pool factory, LLM generator and result validator can
  be injected for deterministic offline runs.

The previous implementation built components inline inside the lifespan
function, which is why multi-database routing, security wiring (blocked
tables/columns, EXPLAIN policy), rate limiting and metrics ended up missing.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.resilience.retry import RetryPolicy
from pg_mcp.security.auth import Authenticator
from pg_mcp.security.posture import (
    SecurityPosture,
    assess_security_posture,
    log_security_posture,
)
from pg_mcp.services.health import HealthChecker
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)

__all__ = ["AppComponents", "build_components"]


@dataclass
class AppComponents:
    """The fully wired application object graph.

    Attributes:
        settings: Effective settings used to build the graph.
        pools: Connection pools keyed by database name.
        executors: SQL executors keyed by database name (one per database).
        schema_cache: Schema cache instance.
        sql_validator: Security validator wired with the configured policies.
        orchestrator: Query orchestrator routed to the per-database executors.
        health_checker: Health probe aggregator.
        circuit_breaker: Shared LLM circuit breaker.
        rate_limiter: Shared rate limiter for LLM/database calls.
        metrics: Metrics collector.
        sql_generator: SQL generator used by the orchestrator (a double in tests).
        result_validator: Result validator used by the orchestrator.
        authenticator: API-key authentication / per-database authorization.
        security_posture: Startup security posture report for this configuration.
    """

    settings: Settings
    pools: dict[str, Pool]
    executors: dict[str, SQLExecutor]
    schema_cache: SchemaCache
    sql_validator: SQLValidator
    orchestrator: QueryOrchestrator
    health_checker: HealthChecker
    circuit_breaker: CircuitBreaker
    rate_limiter: MultiRateLimiter
    metrics: MetricsCollector
    sql_generator: Any
    result_validator: Any
    authenticator: Authenticator
    security_posture: SecurityPosture

    async def shutdown(self, *, timeout: float = 5.0) -> None:  # noqa: ASYNC109
        """Release resources owned by this component graph.

        Args:
            timeout: Maximum seconds to wait for each pool to close.
        """
        await self.schema_cache.stop_auto_refresh()
        await close_pools(self.pools, timeout=timeout)


async def build_components(
    settings: Settings,
    *,
    pool_factory: Callable[[list[Any]], Any] = create_pools,
    sql_generator: SQLGenerator | None = None,
    result_validator: ResultValidator | None = None,
    sql_validator: SQLValidator | None = None,
    metrics: MetricsCollector | None = None,
    load_schemas: bool = True,
    fail_fast_on_schema_error: bool = False,
) -> AppComponents:
    """Build the application object graph from settings.

    Args:
        settings: Effective application settings.
        pool_factory: Coroutine creating ``{db_name: pool}`` from a list of
            database configs. Overridable for offline/deterministic runs.
        sql_generator: Optional SQL generator (defaults to the OpenAI backed one).
        result_validator: Optional result validator (defaults to the OpenAI one).
        sql_validator: Optional SQL validator; when omitted it is constructed
            with ``blocked_tables``/``blocked_columns``/``allow_explain`` taken
            from ``settings.security`` (the security wiring that was missing).
        metrics: Optional metrics collector (defaults to the global singleton).
        load_schemas: Whether to eagerly introspect schemas at startup.
        fail_fast_on_schema_error: Raise when a schema cannot be loaded instead
            of degrading (the orchestrator can also load schemas lazily).

    Returns:
        AppComponents: The wired object graph.

    Raises:
        Exception: Propagates pool creation failures, and schema load failures
            when ``fail_fast_on_schema_error`` is True.
    """
    security = settings.security
    resilience = settings.resilience
    collected_metrics = metrics if metrics is not None else MetricsCollector()

    # 1. Connection pools — one per configured database.
    pools: dict[str, Pool] = await pool_factory(settings.databases)
    logger.info(
        "Created connection pools",
        extra={"databases": sorted(pools.keys())},
    )

    # 2. Schema cache.
    schema_cache = SchemaCache(settings.cache)
    if load_schemas:
        for db_name, pool in pools.items():
            try:
                schema = await schema_cache.load(db_name, pool)
            except Exception as e:
                logger.error(
                    "Failed to load schema at startup; it will be loaded lazily on demand",
                    extra={"database": db_name, "error": str(e)},
                )
                if fail_fast_on_schema_error:
                    await close_pools(pools)
                    raise
            else:
                logger.info(
                    "Schema loaded",
                    extra={"database": db_name, "tables": len(schema.tables)},
                )

    # 3. Security validator with the configured sensitive-resource policies.
    validator = sql_validator or SQLValidator(
        config=security,
        blocked_tables=security.blocked_tables,
        blocked_columns=security.blocked_columns,
        allow_explain=security.allow_explain,
    )
    logger.info(
        "Security policies wired into validator",
        extra={
            "blocked_tables": security.blocked_tables,
            "blocked_columns": security.blocked_columns,
            "allow_explain": security.allow_explain,
            "allow_write_operations": security.allow_write_operations,
        },
    )

    # 4. LLM services.
    generator = sql_generator or SQLGenerator(settings.openai)
    validator_service = result_validator or ResultValidator(
        settings.openai, settings.validation, settings.security
    )

    # 5. Resilience primitives.
    retry_policy = RetryPolicy.from_config(resilience)
    circuit_breaker = CircuitBreaker(
        failure_threshold=resilience.circuit_breaker_threshold,
        recovery_timeout=resilience.circuit_breaker_timeout,
    )
    rate_limiter = MultiRateLimiter(
        query_limit=resilience.max_concurrent_queries,
        llm_limit=resilience.max_concurrent_llm_calls,
    )

    # 6. One executor per database (multi-database routing).
    executors: dict[str, SQLExecutor] = {}
    for db_name, pool in pools.items():
        db_config = settings.get_database(db_name)
        if db_config is None:  # pragma: no cover - defensive
            raise ValueError(f"Missing database configuration for '{db_name}'")
        executors[db_name] = SQLExecutor(
            pool=pool,
            security_config=security,
            db_config=db_config,
            db_name=db_name,
            retry_policy=retry_policy,
            metrics=collected_metrics,
        )
    logger.info("Created SQL executors", extra={"databases": sorted(executors.keys())})

    # 7. Orchestrator with routing + resilience + observability wired in.
    orchestrator = QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        result_validator=validator_service,
        schema_cache=schema_cache,
        pools=pools,
        executors=executors,
        resilience_config=resilience,
        validation_config=settings.validation,
        circuit_breaker=circuit_breaker,
        rate_limiter=rate_limiter,
        metrics=collected_metrics,
        retry_policy=retry_policy,
    )

    health_checker = HealthChecker(
        pools=pools,
        schema_cache=schema_cache,
        circuit_breaker=circuit_breaker,
        rate_limiter=rate_limiter,
        metrics=collected_metrics,
    )

    authenticator = Authenticator(settings.auth)

    # Observe (never mutate) the effective security configuration so that a
    # deployment running with protections off says so at startup.
    posture = assess_security_posture(settings)
    log_security_posture(posture)

    return AppComponents(
        settings=settings,
        pools=pools,
        executors=executors,
        schema_cache=schema_cache,
        sql_validator=validator,
        orchestrator=orchestrator,
        health_checker=health_checker,
        circuit_breaker=circuit_breaker,
        rate_limiter=rate_limiter,
        metrics=collected_metrics,
        sql_generator=generator,
        result_validator=validator_service,
        authenticator=authenticator,
        security_posture=posture,
    )
