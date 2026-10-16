"""FastMCP server for PostgreSQL natural language query interface.

This module implements the MCP server using FastMCP, exposing the query
functionality as an MCP tool plus a health check tool. Component construction
is delegated to :mod:`pg_mcp.app` so that the server, tests and offline tooling
all share the same (now correctly wired) object graph.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from asyncpg import Pool
from mcp.server.fastmcp import FastMCP

from pg_mcp.app import AppComponents, build_components
from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import Settings
from pg_mcp.models.errors import AuthError
from pg_mcp.models.query import QueryRequest, QueryResponse, ReturnType
from pg_mcp.observability.logging import configure_logging, get_logger
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.health import HealthChecker
from pg_mcp.services.orchestrator import QueryOrchestrator

logger = get_logger(__name__)

# Global state for lifespan management
_settings: Settings | None = None
_pools: dict[str, Pool] | None = None
_schema_cache: SchemaCache | None = None
_orchestrator: QueryOrchestrator | None = None
_metrics: MetricsCollector | None = None
_circuit_breaker: CircuitBreaker | None = None
_rate_limiter: MultiRateLimiter | None = None
_health_checker: HealthChecker | None = None
_components: AppComponents | None = None


def get_components() -> AppComponents | None:
    """Return the initialized component graph, if any.

    Returns:
        AppComponents | None: The wired components or None before startup.
    """
    return _components


@asynccontextmanager
async def lifespan(_app: FastMCP) -> AsyncIterator[None]:
    """Lifespan context manager for server initialization and cleanup.

    This function manages the complete lifecycle of the MCP server:

    Startup:
        1. Load configuration from Settings
        2. Configure logging
        3. Create database connection pools (one per configured database)
        4. Load schema cache for all databases
        5. Initialize metrics collector and start the metrics HTTP server
        6. Build service components (validator with security policies, one
           executor per database, generators, result validator)
        7. Initialize resilience components (shared circuit breaker, rate limiter)
        8. Create the query orchestrator and health checker

    Shutdown:
        1. Stop schema auto-refresh (if enabled)
        2. Close all database connection pools

    Yields:
        None

    Example:
        >>> async with lifespan(mcp):
        ...     # Server is running with all components initialized
        ...     pass
    """
    global _settings, _pools, _schema_cache, _orchestrator, _metrics
    global _circuit_breaker, _rate_limiter, _health_checker, _components

    logger.info("Starting PostgreSQL MCP Server initialization...")

    try:
        # 1. Load Settings
        logger.info("Loading configuration...")
        _settings = Settings()

        # 2. Configure logging
        logger.info("Configuring logging...")
        configure_logging(
            level=_settings.observability.log_level,
            log_format=_settings.observability.log_format,
            enable_sensitive_filter=True,
        )

        logger.info(
            "Configuration loaded",
            extra={
                "environment": _settings.environment,
                "log_level": _settings.observability.log_level,
                "databases": [db.name for db in _settings.databases],
            },
        )

        # 3. Initialize metrics collector and optionally start the HTTP endpoint.
        _metrics = MetricsCollector()
        if _settings.observability.metrics_enabled:
            from prometheus_client import start_http_server

            start_http_server(_settings.observability.metrics_port)
            logger.info(f"Metrics server started on port {_settings.observability.metrics_port}")

        # 4-8. Build the object graph (pools, executors, validator with security
        # policies, rate limiter, orchestrator, health checker).
        _components = await build_components(_settings, metrics=_metrics)

        _pools = _components.pools
        _schema_cache = _components.schema_cache
        _orchestrator = _components.orchestrator
        _circuit_breaker = _components.circuit_breaker
        _rate_limiter = _components.rate_limiter
        _health_checker = _components.health_checker

        logger.info("PostgreSQL MCP Server initialization complete!")
        logger.info(
            "Server ready to accept requests",
            extra={
                "databases": sorted(_pools.keys()),
                "cache_enabled": _settings.cache.enabled,
                "metrics_enabled": _settings.observability.metrics_enabled,
                "blocked_tables": _settings.security.blocked_tables,
                "blocked_columns": _settings.security.blocked_columns,
                "allow_explain": _settings.security.allow_explain,
                "max_concurrent_queries": _settings.resilience.max_concurrent_queries,
                "max_concurrent_llm_calls": _settings.resilience.max_concurrent_llm_calls,
            },
        )

        # Yield to run the server
        yield

    finally:
        # Shutdown sequence
        logger.info("Starting PostgreSQL MCP Server shutdown...")

        if _schema_cache is not None:
            try:
                import asyncio

                await asyncio.wait_for(_schema_cache.stop_auto_refresh(), timeout=3.0)
                logger.info("Schema auto-refresh stopped")
            except TimeoutError:
                logger.warning("Schema auto-refresh stop timed out")
            except Exception as e:
                logger.warning(f"Error stopping schema auto-refresh: {e!s}")

        if _components is not None:
            try:
                await _components.shutdown(timeout=5.0)
                logger.info("Database connection pools closed")
            except Exception as e:
                logger.error(f"Error closing connection pools: {e!s}")
            finally:
                _components = None
                _orchestrator = None
                _health_checker = None

        logger.info("PostgreSQL MCP Server shutdown complete")


# Create FastMCP server instance with lifespan
mcp = FastMCP("pg-mcp", lifespan=lifespan)


@mcp.tool()
async def query(
    question: str,
    database: str | None = None,
    return_type: str = "result",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Execute a natural language query against PostgreSQL database.

    This tool converts natural language questions into SQL queries and executes
    them against the specified PostgreSQL database. It includes comprehensive
    security validation, result verification, and error handling.

    Args:
        question: Natural language description of the query.
            Examples:
                - "How many users registered in the last 30 days?"
                - "Show me the top 10 products by revenue"
                - "What is the average order value by country?"

        database: Target database name (optional if only one database is configured).
            If not specified and only one database is available, it will be
            automatically selected. When several databases are configured the
            executor bound to the resolved database is used, so a request for
            database B never runs against database A.

        return_type: Type of result to return.
            Options:
                - "sql": Return only the generated SQL query without executing it
                - "result": Execute the query and return results (default)

        api_key: Credential for role-based access control. Required only when
            ``AUTH_ENABLED=true``; ignored otherwise (design review 0003 #1).

    Returns:
        dict: Query response containing:
            - success (bool): Whether the query succeeded
            - generated_sql (str): The generated SQL query
            - data (dict): Query results if executed (columns, rows, row_count, etc.)
            - error (dict): Error information if query failed
            - confidence (int): Confidence score (0-100) for result quality
            - tokens_used (int): Number of LLM tokens consumed (always present)
            - request_id (str): Correlation id for logs/metrics
            - warning (str): Advisorary message, e.g. low confidence

    Examples:
        >>> # Get query results
        >>> result = await query(
        ...     question="How many active users are there?",
        ...     return_type="result"
        ... )
        >>> print(result["data"]["rows"])

        >>> # Get SQL only
        >>> result = await query(
        ...     question="Count all products",
        ...     return_type="sql"
        ... )
        >>> print(result["generated_sql"])

    Raises:
        This function does not raise exceptions. All errors are captured and
        returned in the response with success=False and error details.

    Security:
        - Only SELECT queries are allowed (no INSERT, UPDATE, DELETE, DROP, etc.)
        - Dangerous PostgreSQL functions are blocked (pg_sleep, file operations, etc.)
        - Configured sensitive tables/columns are blocked (SECURITY_BLOCKED_TABLES /
          SECURITY_BLOCKED_COLUMNS), including schema-qualified and wildcard entries
        - EXPLAIN is rejected unless SECURITY_ALLOW_EXPLAIN=true
        - Query execution timeout is enforced
        - Row count limits prevent memory exhaustion
        - All queries run in read-only transactions
        - Concurrency is capped by the configured rate limits
        - When AUTH_ENABLED=true, every call is authenticated and authorized per
          database, and an audit record is written
    """
    if _orchestrator is None:
        return {
            "success": False,
            "error": {
                "code": "SERVER_NOT_INITIALIZED",
                "message": "Server not initialized properly",
                "details": None,
            },
            "tokens_used": 0,
        }

    # Validate return_type
    if return_type not in ("sql", "result"):
        return {
            "success": False,
            "error": {
                "code": "INVALID_PARAMETER",
                "message": f"Invalid return_type: '{return_type}'. Must be 'sql' or 'result'.",
                "details": {"return_type": return_type},
            },
            "tokens_used": 0,
        }

    # Authenticate and authorize before any work is done (when enabled).
    authenticator = _components.authenticator if _components is not None else None
    if authenticator is not None and authenticator.enabled:
        try:
            async with request_context(get_request_id()) as rid:
                principal = authenticator.authenticate(
                    api_key, request_id=rid, database=database
                )
                authenticator.authorize(principal, database, request_id=rid)
        except AuthError as e:
            return {
                "success": False,
                "error": {
                    "code": e.code.value,
                    "message": e.message,
                    "details": e.details,
                },
                "tokens_used": 0,
            }

    # Build request
    try:
        request = QueryRequest(
            question=question,
            database=database,
            return_type=ReturnType(return_type),
        )
    except Exception as e:
        return {
            "success": False,
            "error": {
                "code": "INVALID_REQUEST",
                "message": f"Invalid request parameters: {e!s}",
                "details": {"error": str(e)},
            },
            "tokens_used": 0,
        }

    # Execute query through orchestrator (request_id is generated and propagated
    # through the whole pipeline by the orchestrator itself).
    try:
        async with request_context(get_request_id()):
            response: QueryResponse = await _orchestrator.execute_query(request)
        result = response.to_dict()
        # Ensure tokens_used is always present (to_dict already guarantees it).
        if "tokens_used" not in result:
            result["tokens_used"] = 0
        return result
    except Exception as e:
        logger.exception("Unexpected error in query tool")
        return {
            "success": False,
            "error": {
                "code": "INTERNAL_ERROR",
                "message": f"Internal server error: {e!s}",
                "details": {"error_type": type(e).__name__},
            },
            "tokens_used": 0,
        }


@mcp.tool()
async def health_check() -> dict[str, Any]:
    """Report the health of the server and its dependencies.

    Probes every configured database pool, reports schema cache coverage and
    exposes circuit breaker / rate limiter state.

    Returns:
        dict: Health report with an overall ``status`` of ``healthy``,
        ``degraded`` or ``unhealthy``, per-database probe results and component
        diagnostics.

    Example:
        >>> report = await health_check()
        >>> report["status"]
        'healthy'
    """
    if _health_checker is None:
        return {
            "status": "unhealthy",
            "error": "Server not initialized properly",
        }
    report = await _health_checker.check()
    if _components is not None and _components.security_posture is not None:
        report["security_posture"] = _components.security_posture.to_dict()
    return report


if __name__ == "__main__":
    """Run the server when executed directly."""
    import anyio

    anyio.run(mcp.run_stdio_async)
