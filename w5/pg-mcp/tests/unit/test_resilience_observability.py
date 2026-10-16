"""Tests for resilience + observability wiring on the live request path.

Covers review items:
- "Rate Limiting Not Applied"
- "Metrics/tracing not instrumented in request path"
- "No health check endpoint"
- "Session parameter setup" / "Pool lifecycle" coverage gaps
"""

import asyncio
import logging

import pytest

from pg_mcp.app import build_components
from pg_mcp.config.settings import DatabaseConfig, SecurityConfig
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.services.health import HealthChecker
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.testing.fakes import (
    DEFAULT_TABLES,
    FakePool,
    StubResultValidator,
    StubSQLGenerator,
    build_offline_settings,
    make_pool_factory,
    offline_pg_handler,
)


def make_pools(*names: str, **kwargs: object) -> dict[str, FakePool]:
    """Create fake pools sharing the offline introspection fixture.

    Args:
        *names: Database names.
        **kwargs: Extra FakePool keyword arguments.

    Returns:
        dict[str, FakePool]: Fake pools keyed by database name.
    """
    handler = offline_pg_handler(DEFAULT_TABLES)
    return {name: FakePool(name, handler=handler, **kwargs) for name in names}


async def build_components_for(
    settings: object,
    pools: dict[str, FakePool],
    *,
    script: list[str] | None = None,
    generator: StubSQLGenerator | None = None,
    result_validator: StubResultValidator | None = None,
    metrics: MetricsCollector | None = None,
    load_schemas: bool = True,
) -> object:
    """Build offline components for a test.

    Schemas are introspected eagerly (``load_schemas=True``) and the resulting
    statements are then cleared, so ``pool.executed``/``pool.attempts`` only
    reflect whatever the test itself does.

    Args:
        settings: Settings object.
        pools: Fake pools.
        script: SQL answers for the stub generator.
        generator: Explicit generator.
        result_validator: Explicit result validator.
        metrics: Optional metrics collector.
        load_schemas: Whether to introspect schemas during start-up.

    Returns:
        object: The wired component graph.
    """
    components = await build_components(
        settings,  # type: ignore[arg-type]
        pool_factory=make_pool_factory(pools),
        sql_generator=generator or StubSQLGenerator(script or ["SELECT 1;"]),  # type: ignore[arg-type]
        result_validator=result_validator or StubResultValidator(),  # type: ignore[arg-type]
        metrics=metrics,
        load_schemas=load_schemas,
    )
    for pool in pools.values():
        pool.clear_recordings()
    return components


def sample_key(name: str, **labels: str) -> str:
    """Build a Prometheus sample key the way ``MetricsCollector.snapshot`` does.

    Args:
        name: Metric name.
        **labels: Label values (sorted alphabetically by the collector).

    Returns:
        str: The key used in the snapshot dictionary.
    """
    if not labels:
        return name
    rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
    return f"{name}{{{rendered}}}"


class TestRateLimiting:
    """The configured rate limits must apply to the request path."""

    @pytest.mark.asyncio
    async def test_llm_concurrency_limit_rejects_excess_requests(self) -> None:
        """Requests beyond the LLM concurrency limit are rejected, not queued forever."""
        settings = build_offline_settings(
            databases=("analytics",), max_concurrent_llm_calls=1, acquire_timeout=0.1
        )
        metrics = MetricsCollector()
        metrics.reset_all_metrics()
        pools = make_pools("analytics")
        components = await build_components_for(
            settings,
            pools,
            generator=StubSQLGenerator(["SELECT 1;"], latency=0.3),
            metrics=metrics,
        )

        responses = await asyncio.gather(
            *[
                components.orchestrator.execute_query(  # type: ignore[attr-defined]
                    QueryRequest(question=f"q{i}", database="analytics", return_type=ReturnType.SQL)
                )
                for i in range(3)
            ]
        )

        codes = ["success" if r.success else r.error.code for r in responses]
        assert codes.count("rate_limit_exceeded") == 2
        assert codes.count("success") == 1
        assert (
            metrics.snapshot()['pg_mcp_rate_limit_rejections_total{limiter="llm"}'] == 2.0
        )
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_database_concurrency_limit_rejects_excess_requests(self) -> None:
        """Database execution is rate limited as well."""
        settings = build_offline_settings(
            databases=("analytics",),
            max_concurrent_llm_calls=10,
            max_concurrent_queries=1,
            acquire_timeout=0.1,
            validation_enabled=False,
        )
        pools = make_pools("analytics", latency=0.3)
        components = await build_components_for(
            settings,
            pools,
            generator=StubSQLGenerator(["SELECT 1;"], latency=0.0),
        )

        responses = await asyncio.gather(
            *[
                components.orchestrator.execute_query(  # type: ignore[attr-defined]
                    QueryRequest(question=f"q{i}", database="analytics")
                )
                for i in range(3)
            ]
        )

        codes = ["success" if r.success else r.error.code for r in responses]
        assert codes.count("rate_limit_exceeded") == 2
        await components.shutdown()  # type: ignore[attr-defined]


class TestTransientFailureRetry:
    """Executors retry transient database failures with backoff."""

    @pytest.mark.asyncio
    async def test_executor_retries_transient_connection_failure(self) -> None:
        """A flapping connection is retried until it succeeds."""
        settings = build_offline_settings(
            databases=("analytics",), max_retries=3, retry_delay=0.01, backoff_factor=1.0
        )
        pools = make_pools("analytics")
        components = await build_components_for(
            settings, pools, script=["SELECT COUNT(*) FROM users;"]
        )
        # The database starts flapping only after start-up, so the retries below
        # are spent on the user query itself.
        pools["analytics"].failures_before_success = 2

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="count users", database="analytics")
        )

        assert response.success is True
        assert pools["analytics"].attempts == 3
        snapshot = components.metrics.snapshot()  # type: ignore[attr-defined]
        retry_key = 'pg_mcp_retry_attempts_total{operation="db.execute",outcome="retried"}'
        assert snapshot.get(retry_key) == 2.0
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_executor_gives_up_after_retry_budget(self) -> None:
        """When retries are exhausted the error surfaces to the caller."""
        settings = build_offline_settings(
            databases=("analytics",), max_retries=1, retry_delay=0.01, backoff_factor=1.0
        )
        pools = make_pools("analytics")
        components = await build_components_for(
            settings, pools, script=["SELECT COUNT(*) FROM users;"]
        )
        pools["analytics"].failures_before_success = 5

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="count users", database="analytics")
        )

        assert response.success is False
        assert response.error is not None
        assert pools["analytics"].attempts == 2
        await components.shutdown()  # type: ignore[attr-defined]


class TestExecutorSessionAndTransaction:
    """Session parameters and read-only transactions must be applied."""

    @pytest.mark.asyncio
    async def test_session_parameters_and_readonly_transaction(self) -> None:
        """statement_timeout, search_path and a read-only transaction are enforced."""
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        security = SecurityConfig(max_execution_time=7.0, safe_search_path="reporting")
        executor = SQLExecutor(
            pool=pool,  # type: ignore[arg-type]
            security_config=security,
            db_config=DatabaseConfig(name="analytics"),
            db_name="analytics",
        )

        rows, count = await executor.execute("SELECT 1;")

        assert count == 1
        assert rows == [{"?column?": 1}]
        assert pool.transactions == [{"readonly": True}]
        # Session values are bound as parameters (design review 0003 #7): the
        # statement text must never contain an interpolated value.
        statements = " | ".join(pool.session_statements)
        assert "set_config('statement_timeout', $1, false)" in statements
        assert "set_config('search_path', $1, false)" in statements
        assert (
            "SELECT set_config('statement_timeout', $1, false)",
            ("7000",),
        ) in pool.session_params
        assert (
            "SELECT set_config('search_path', $1, false)",
            ("reporting",),
        ) in pool.session_params
        assert "7000" not in statements
        assert "reporting" not in statements

    @pytest.mark.asyncio
    async def test_readonly_role_is_applied_when_configured(self) -> None:
        """A configured read-only role is switched to."""
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        executor = SQLExecutor(
            pool=pool,  # type: ignore[arg-type]
            security_config=SecurityConfig(readonly_role="readonly"),
            db_config=DatabaseConfig(name="analytics"),
            db_name="analytics",
        )

        await executor.execute("SELECT 1;")

        # The role name is an identifier and must be quoted, not bare-interpolated.
        assert 'SET ROLE "readonly"' in pool.session_statements

    @pytest.mark.asyncio
    async def test_invalid_search_path_is_rejected(self) -> None:
        """A suspicious search_path configuration is refused, not interpolated."""
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        executor = SQLExecutor(
            pool=pool,  # type: ignore[arg-type]
            security_config=SecurityConfig(safe_search_path="public; DROP TABLE users"),
            db_config=DatabaseConfig(name="analytics"),
            db_name="analytics",
        )

        from pg_mcp.models.errors import DatabaseError

        with pytest.raises(DatabaseError, match="Invalid search_path"):
            await executor.execute("SELECT 1;")


class TestMetricsInstrumentation:
    """Metrics must be emitted from the real request path."""

    @pytest.mark.asyncio
    async def test_success_and_failure_metrics(self) -> None:
        """Query, LLM, DB and rejection metrics are all recorded."""
        settings = build_offline_settings(
            databases=("analytics",), blocked_tables=("payments",), max_retries=0
        )
        metrics = MetricsCollector()
        metrics.reset_all_metrics()
        pools = make_pools("analytics")
        components = await build_components_for(
            settings,
            pools,
            script=["SELECT COUNT(*) FROM users;", "SELECT * FROM payments;"],
            metrics=metrics,
        )

        ok = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="count users", database="analytics")
        )
        blocked = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="payments", database="analytics")
        )
        assert ok.success is True
        assert blocked.success is False

        snapshot = metrics.snapshot()
        requests = "pg_mcp_query_requests_total"
        assert snapshot[sample_key(requests, database="analytics", status="success")] == 1.0
        denied = sample_key(requests, database="analytics", status="security_violation")
        assert snapshot[denied] == 1.0
        assert snapshot[sample_key("pg_mcp_sql_rejected_total", reason="blocked_table")] == 1.0
        generate = sample_key("pg_mcp_llm_calls_total", operation="generate_sql")
        assert snapshot[generate] == 2.0
        # prometheus_client appends `_total` to Counter samples (README contract).
        tokens = sample_key("pg_mcp_llm_tokens_used_total", operation="generate_sql")
        assert snapshot[tokens] >= 42.0
        assert snapshot["pg_mcp_db_query_duration_seconds_count"] == 1.0
        assert snapshot["pg_mcp_query_duration_seconds_count"] == 2.0
        validate = sample_key("pg_mcp_llm_calls_total", operation="validate_result")
        assert snapshot[validate] == 1.0
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_schema_cache_age_is_published(self) -> None:
        """Cache hits publish the schema cache age gauge."""
        settings = build_offline_settings(databases=("analytics",))
        metrics = MetricsCollector()
        metrics.reset_all_metrics()
        pools = make_pools("analytics")
        components = await build_components_for(
            settings, pools, script=["SELECT 1;"], metrics=metrics
        )
        await components.schema_cache.load("analytics", pools["analytics"])  # type: ignore[attr-defined]

        await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="q", database="analytics", return_type=ReturnType.SQL)
        )

        assert 'pg_mcp_schema_cache_age_seconds{database="analytics"}' in metrics.snapshot()
        await components.shutdown()  # type: ignore[attr-defined]


class TestTracing:
    """request_id must be generated once and propagated through the pipeline."""

    @pytest.mark.asyncio
    async def test_request_id_is_returned_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The response carries the request id used in the logs."""
        settings = build_offline_settings(databases=("analytics",))
        pools = make_pools("analytics")
        components = await build_components_for(settings, pools, script=["SELECT 1;"])

        with caplog.at_level(logging.INFO):
            response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
                QueryRequest(question="q", database="analytics", return_type=ReturnType.SQL)
            )

        request_id = getattr(response, "request_id", None)
        assert request_id
        logged_ids = {
            getattr(record, "request_id", None) for record in caplog.records
        } - {None}
        assert request_id in logged_ids
        await components.shutdown()  # type: ignore[attr-defined]


class TestHealthCheck:
    """The health checker aggregates component state."""

    @pytest.mark.asyncio
    async def test_healthy_when_all_databases_answer(self) -> None:
        """All databases up + schemas cached = healthy."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        metrics = MetricsCollector()
        metrics.reset_all_metrics()
        pools = make_pools("analytics", "billing")
        components = await build_components_for(settings, pools, metrics=metrics)
        for name in pools:
            await components.schema_cache.load(name, pools[name])  # type: ignore[attr-defined]

        report = await components.health_checker.check()  # type: ignore[attr-defined]

        assert report["status"] == "healthy"
        assert report["databases"]["analytics"]["schema_cached"] is True
        assert report["components"]["circuit_breaker"]["state"] == "closed"
        assert report["components"]["rate_limiter"]["queries"]["max_concurrent"] == 10
        assert metrics.snapshot()['pg_mcp_health_checks_total{status="healthy"}'] == 1.0
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_degraded_when_one_database_is_down(self) -> None:
        """A partially reachable fleet is reported as degraded."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        pools["billing"].failures_before_success = 99
        components = await build_components_for(settings, pools)

        report = await components.health_checker.check()  # type: ignore[attr-defined]

        assert report["status"] == "degraded"
        assert report["databases"]["billing"]["reachable"] is False
        assert report["databases"]["analytics"]["reachable"] is True
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_unhealthy_when_no_database_answers(self) -> None:
        """No reachable database = unhealthy."""
        settings = build_offline_settings(databases=("analytics",))
        pools = make_pools("analytics", failures_before_success=99)
        components = await build_components_for(settings, pools)

        report = await components.health_checker.check()  # type: ignore[attr-defined]

        assert report["status"] == "unhealthy"
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_health_checker_without_pools_is_unhealthy(self) -> None:
        """An empty pool mapping reports unhealthy."""
        checker = HealthChecker(pools={})
        report = await checker.check()
        assert report["status"] == "unhealthy"
        assert report["databases"] == {}
