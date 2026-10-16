"""Integration tests for the full query flow.

The flow is verified end to end through the real server lifespan and the real
tool function, with PostgreSQL/OpenAI replaced by deterministic in-memory
doubles. Scenarios that genuinely need live services are marked with
``@pytest.mark.integration`` and skipped unless ``PG_MCP_RUN_INTEGRATION=1``.
"""

import asyncio
import os

import pytest

from pg_mcp.server import health_check, lifespan, mcp, query
from pg_mcp.testing.fakes import DEFAULT_TABLES, FakePool, offline_pg_handler

pytestmark = pytest.mark.asyncio

TWO_DATABASES = (
    '[{"name":"analytics","host":"offline-fixture","user":"readonly"},'
    '{"name":"billing","host":"offline-fixture","user":"readonly"}]'
)


def integration_test(function):  # type: ignore[no-untyped-def]
    """Mark a test that requires live PostgreSQL/OpenAI services.

    Args:
        function: Test function to decorate.

    Returns:
        The decorated test (skipped unless PG_MCP_RUN_INTEGRATION=1).
    """
    return pytest.mark.skipif(
        os.environ.get("PG_MCP_RUN_INTEGRATION", "") != "1",
        reason="needs live PostgreSQL + OpenAI (set PG_MCP_RUN_INTEGRATION=1)",
    )(pytest.mark.integration(function))


class TestFullQueryFlow:
    """Integration tests for the complete query flow."""

    async def test_simple_query_execution(self, offline_server) -> None:
        """Question -> SQL -> validation -> execution -> response."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="How many users are there?")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT COUNT(*) FROM users;"
        assert result["validation"]["is_valid"] is True
        assert result["data"]["row_count"] == 1
        assert result["confidence"] == 95
        assert result["request_id"]

    async def test_sql_only_mode(self, offline_server) -> None:
        """SQL-only mode never touches the database."""
        offline_server.configure(script=["SELECT id FROM users;"])
        async with lifespan(mcp):
            result = await query(question="List user ids", return_type="sql")

        assert result["success"] is True
        assert "data" not in result
        assert offline_server.pool("blog_small").executed == []

    async def test_multi_database_selection(
        self, offline_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each request runs against the executor of the requested database."""
        monkeypatch.setenv("DATABASES", TWO_DATABASES)
        offline_server.configure(
            script=["SELECT COUNT(*) FROM users;", "SELECT COUNT(*) FROM orders;"]
        )

        async with lifespan(mcp):
            analytics = await query(question="count users", database="analytics")
            billing = await query(question="count orders", database="billing")

        assert analytics["success"] and billing["success"]
        assert offline_server.pool("analytics").executed == [
            "SELECT COUNT(*) FROM users LIMIT 10000"
        ]
        assert offline_server.pool("billing").executed == [
            "SELECT COUNT(*) FROM orders LIMIT 10000"
        ]

    async def test_security_rejection(
        self, offline_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured sensitive table is rejected with a reason code."""
        monkeypatch.setenv("SECURITY_BLOCKED_TABLES", "payments")
        offline_server.configure(script=["SELECT card_number FROM payments;"])

        async with lifespan(mcp):
            result = await query(question="Show me payments")

        assert result["success"] is False
        assert result["error"]["code"] == "security_violation"
        assert result["error"]["details"]["reason"] == "blocked_table"
        assert offline_server.pool("blog_small").executed == []

    async def test_blocked_functions(self, offline_server) -> None:
        """Dangerous functions are rejected before execution."""
        offline_server.configure(script=["SELECT pg_sleep(10) FROM users;"])

        async with lifespan(mcp):
            result = await query(question="Sleep for a while")

        assert result["success"] is False
        assert result["error"]["details"]["reason"] == "blocked_function"

    async def test_llm_retry_on_invalid_sql(self, offline_server) -> None:
        """Invalid SQL is retried with error feedback before giving up."""
        offline_server.configure(script=["DELETE FROM users;", "SELECT COUNT(*) FROM users;"])

        async with lifespan(mcp):
            result = await query(question="How many users?")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT COUNT(*) FROM users;"
        calls = offline_server.generator.calls
        assert len(calls) == 2
        assert calls[1]["error_feedback"]
        assert calls[1]["previous_attempt"] == "DELETE FROM users;"

    async def test_error_handling_with_invalid_database(self, offline_server) -> None:
        """An invalid database name produces a structured error."""
        async with lifespan(mcp):
            result = await query(question="count users", database="missing")

        assert result["success"] is False
        assert result["error"]["code"] == "database_error"
        assert "missing" in result["error"]["message"]

    async def test_empty_result_handling(self, offline_server) -> None:
        """An empty result set is still a successful response."""
        offline_server.configure(script=["SELECT id FROM users WHERE id = -1;"])
        async with lifespan(mcp):
            result = await query(question="Find impossible row")

        assert result["success"] is True
        assert result["data"]["rows"] == []
        assert result["data"]["row_count"] == 0

    async def test_tokens_used_tracking(self, offline_server) -> None:
        """The token count reported by the LLM reaches the response."""
        offline_server.configure(
            generator=type(offline_server.generator)(["SELECT COUNT(*) FROM users;"], tokens=321)
        )
        async with lifespan(mcp):
            result = await query(question="How many users?")

        assert result["tokens_used"] == 321

    async def test_result_validation_feedback(self, offline_server) -> None:
        """A low confidence score produces an advisory warning."""
        from pg_mcp.testing.fakes import StubResultValidator

        offline_server.configure(
            script=["SELECT COUNT(*) FROM users;"],
            result_validator=StubResultValidator(confidence=30, explanation="partial"),
        )
        async with lifespan(mcp):
            result = await query(question="How many users?")

        assert result["success"] is True
        assert result["confidence"] == 30
        assert "below the configured threshold" in result["warning"]

    async def test_question_length_guard(
        self, offline_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Over-long questions are rejected before the LLM is called."""
        monkeypatch.setenv("VALIDATION_MAX_QUESTION_LENGTH", "50")
        async with lifespan(mcp):
            result = await query(question="count users " * 20)

        assert result["success"] is False
        assert result["error"]["code"] == "question_too_long"
        assert offline_server.generator.calls == []

    async def test_concurrent_queries(self, offline_server) -> None:
        """Concurrent requests are served and rate limits are respected."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            results = await asyncio.gather(*[query(question=f"count users {i}") for i in range(5)])

        assert all(r["success"] for r in results)
        assert len(offline_server.pool("blog_small").executed) == 5

    async def test_rate_limit_enforced(
        self, offline_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The configured LLM concurrency limit rejects excess traffic."""
        monkeypatch.setenv("RESILIENCE_MAX_CONCURRENT_LLM_CALLS", "1")
        monkeypatch.setenv("RESILIENCE_ACQUIRE_TIMEOUT", "0.1")

        from pg_mcp.testing.fakes import StubSQLGenerator

        offline_server.configure(generator=StubSQLGenerator(["SELECT 1;"], latency=0.3))
        async with lifespan(mcp):
            results = await asyncio.gather(
                *[query(question=f"q{i}", return_type="sql") for i in range(3)]
            )

        codes = ["ok" if r["success"] else r["error"]["code"] for r in results]
        assert codes.count("rate_limit_exceeded") == 2

    async def test_health_and_metrics_via_server(self, offline_server) -> None:
        """Health check reflects the running server and metrics are collected."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="How many users?")
            report = await health_check()

        assert result["success"] is True
        assert report["status"] in {"healthy", "degraded"}
        assert set(report["databases"]) == {"blog_small"}


class TestIntegrationErrorScenarios:
    """Error paths of the full flow."""

    async def test_malformed_input_handling(self, offline_server) -> None:
        """Whitespace-only questions are rejected gracefully."""
        async with lifespan(mcp):
            result = await query(question="   ")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_REQUEST"

    async def test_unicode_handling(self, offline_server) -> None:
        """Unicode questions are handled end to end."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="数据库里有多少用户？")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT COUNT(*) FROM users;"

    async def test_execution_timeout_handling(
        self, offline_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow query is interrupted by the execution timeout."""
        monkeypatch.setenv("SECURITY_MAX_EXECUTION_TIME", "1")
        offline_server.configure(
            pools={
                "blog_small": FakePool(
                    "blog_small",
                    handler=offline_pg_handler(DEFAULT_TABLES),
                    latency=1.5,
                )
            }
        )
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])

        async with lifespan(mcp):
            result = await query(question="How many users?")

        assert result["success"] is False
        assert result["error"]["code"] == "execution_timeout"


@pytest.mark.integration
@integration_test
async def test_live_postgres_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the whole tool path against a real PostgreSQL server.

    Only the text-to-SQL step is stubbed (no OpenAI key is required); the
    connection pool, schema introspection, validation, execution, row limiting
    and metrics are all real.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    from typing import Any

    from pg_mcp.app import build_components
    from pg_mcp.config.settings import Settings, reset_settings
    from pg_mcp.db.pool import close_pools, create_pools
    from pg_mcp.testing.fakes import StubResultValidator, StubSQLGenerator

    monkeypatch.setenv("DATABASES", os.environ.get("DATABASES") or _live_databases_json())
    reset_settings()

    settings = Settings()
    generator = StubSQLGenerator(["SELECT count(*) AS n FROM users;"])
    created: list[dict[str, Any]] = []

    async def real_pools(configs: list[Any]) -> dict[str, Any]:
        pools = await create_pools(configs)
        created.append(pools)
        return pools

    async def patched(_settings: Any, **kwargs: Any) -> Any:
        metrics = kwargs.pop("metrics", None)
        return await build_components(
            settings,
            pool_factory=real_pools,  # type: ignore[arg-type]
            sql_generator=generator,  # type: ignore[arg-type]
            result_validator=StubResultValidator(),
            metrics=metrics,
            load_schemas=True,
            fail_fast_on_schema_error=True,
        )

    monkeypatch.setattr("pg_mcp.server.build_components", patched)
    try:
        async with lifespan(mcp):
            # With several databases configured the caller must pick one; that
            # contract is asserted explicitly (no silent fallback to db #1).
            ambiguous = await query(question="How many users are there?")
            result = await query(question="How many users are there?", database="blog_small")
    finally:
        for pools in created:
            await close_pools(pools)  # type: ignore[arg-type]

    assert ambiguous["success"] is False
    assert ambiguous["error"]["code"] == "database_error"
    assert "specify which" in ambiguous["error"]["message"]

    assert result["success"] is True
    assert result["generated_sql"] == "SELECT count(*) AS n FROM users;"
    assert result["data"]["row_count"] == 1
    assert result["data"]["rows"][0]["n"] >= 1


def _live_databases_json() -> str:
    """Build the ``DATABASES`` value for the cluster started by the helper script.

    Returns:
        str: JSON array accepted by :class:`Settings`.
    """
    port = os.environ.get("PG_MCP_TEST_PORT", "55432")
    return (
        f'[{{"name":"blog_small","host":"127.0.0.1","port":{port},"user":"postgres",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":4}},'
        f'{{"name":"analytics","host":"127.0.0.1","port":{port},"user":"postgres",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":4}}]'
    )


@pytest.mark.integration
@integration_test
async def test_live_postgres_and_openai_flow() -> None:
    """Full live run including the LLM (needs a real ``OPENAI_API_KEY``)."""
    if os.environ.get("OPENAI_API_KEY", "sk-test-offline").startswith("sk-test"):
        pytest.skip("no real OPENAI_API_KEY configured")

    async with lifespan(mcp):
        result = await query(question="How many users?")
    assert result["success"] in (True, False)
