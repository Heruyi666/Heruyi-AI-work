"""Health checking for the PostgreSQL MCP Server.

The design document promised a health check endpoint; this module implements
the underlying probe logic so that both the MCP server and offline tooling can
reuse it.

The probe verifies:
- every configured database pool answers ``SELECT 1``
- a schema is cached for every database
- circuit breaker and rate limiter state is exposed for diagnostics
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from asyncpg import Pool

if TYPE_CHECKING:
    from pg_mcp.cache.schema_cache import SchemaCache
    from pg_mcp.observability.metrics import MetricsCollector
    from pg_mcp.resilience.circuit_breaker import CircuitBreaker
    from pg_mcp.resilience.rate_limiter import MultiRateLimiter

logger = logging.getLogger(__name__)

HealthStatus = Literal["healthy", "degraded", "unhealthy"]


class HealthChecker:
    """Aggregates component-level health signals into one report.

    Example:
        >>> checker = HealthChecker(pools={"mydb": pool}, schema_cache=cache)
        >>> report = await checker.check()
        >>> report["status"]
        'healthy'
    """

    def __init__(
        self,
        *,
        pools: dict[str, Pool],
        schema_cache: "SchemaCache | None" = None,
        circuit_breaker: "CircuitBreaker | None" = None,
        rate_limiter: "MultiRateLimiter | None" = None,
        metrics: "MetricsCollector | None" = None,
    ) -> None:
        """Initialize the health checker.

        Args:
            pools: Connection pools keyed by database name.
            schema_cache: Schema cache used to report cached databases.
            circuit_breaker: LLM circuit breaker, if configured.
            rate_limiter: Multi rate limiter, if configured.
            metrics: Metrics collector used to count health checks.
        """
        self.pools = pools
        self.schema_cache = schema_cache
        self.circuit_breaker = circuit_breaker
        self.rate_limiter = rate_limiter
        self.metrics = metrics

    async def check(self, *, timeout: float = 5.0) -> dict[str, Any]:  # noqa: ASYNC109
        """Run the health probe.

        Args:
            timeout: Per-database probe timeout in seconds.

        Returns:
            dict[str, Any]: Report containing ``status``, per-database results
            and resilience component state.
        """
        databases: dict[str, dict[str, Any]] = {}

        for name, pool in self.pools.items():
            reachable = await self._probe(name, pool, timeout)
            cached = bool(self.schema_cache and self.schema_cache.get(name) is not None)
            databases[name] = {
                "reachable": reachable,
                "schema_cached": cached,
                "status": "up" if reachable else "down",
            }

        status = self._overall_status(databases)
        report: dict[str, Any] = {
            "status": status,
            "timestamp": datetime.now(UTC).isoformat(),
            "databases": databases,
            "components": {
                "schema_cache": {
                    "cached_databases": (
                        sorted(self.schema_cache.get_cached_databases())
                        if self.schema_cache is not None
                        else []
                    )
                },
                "circuit_breaker": (
                    self.circuit_breaker.get_stats() if self.circuit_breaker is not None else None
                ),
                "rate_limiter": (
                    self.rate_limiter.get_all_stats() if self.rate_limiter is not None else None
                ),
            },
        }

        if self.metrics is not None:
            self.metrics.record_health_check(status)

        logger.info(
            "Health check completed",
            extra={"status": status, "databases": len(databases)},
        )
        return report

    @staticmethod
    def _overall_status(databases: dict[str, dict[str, Any]]) -> HealthStatus:
        """Derive the aggregate status from the per-database probes.

        Args:
            databases: Per-database probe results.

        Returns:
            HealthStatus: ``unhealthy`` when nothing is reachable, ``degraded``
            when only part of the fleet answers, otherwise ``healthy``.
        """
        if not databases:
            return "unhealthy"
        reachable = [db for db in databases.values() if db["reachable"]]
        if not reachable:
            return "unhealthy"
        if len(reachable) < len(databases):
            return "degraded"
        if any(not db["schema_cached"] for db in databases.values()):
            return "degraded"
        return "healthy"

    @staticmethod
    async def _probe(name: str, pool: Pool, timeout: float) -> bool:  # noqa: ASYNC109
        """Run ``SELECT 1`` against a pool.

        Args:
            name: Database name used for logging.
            pool: Pool to probe.
            timeout: Probe timeout in seconds.

        Returns:
            bool: True when the database answered.
        """
        try:
            async with asyncio.timeout(timeout):
                async with pool.acquire() as conn:
                    await conn.fetch("SELECT 1")
            return True
        except Exception as e:
            logger.warning(
                "Database health probe failed",
                extra={"database": name, "error": str(e)},
            )
            return False
