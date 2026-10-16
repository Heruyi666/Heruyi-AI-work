"""End-to-end tests for the MCP server surface.

These tests exercise the real ``FastMCP`` tool functions (``query`` and
``health_check``) through the real lifespan, with PostgreSQL and OpenAI replaced
by in-memory doubles (:mod:`pg_mcp.testing.fakes`). They are deterministic and
require no external service, which is what the code review asked for
("Add mock fixtures for deterministic testing").
"""

import pytest

from pg_mcp.server import health_check, lifespan, mcp, query

pytestmark = pytest.mark.asyncio


class TestMCPServer:
    """E2E tests for MCP server functionality."""

    async def test_lifespan_initialization(self, offline_server) -> None:
        """The lifespan starts and stops all components without errors."""
        import pg_mcp.server as server_module

        async with lifespan(mcp):
            components = server_module.get_components()
            assert components is not None
            assert components.pools
            assert components.executors

        assert server_module.get_components() is None

    async def test_query_tool_sql_only(self, offline_server) -> None:
        """return_type='sql' returns the generated SQL without executing it."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="How many users?", return_type="sql")

        assert result["success"] is True
        assert result["generated_sql"] == "SELECT COUNT(*) FROM users;"
        assert "data" not in result
        assert offline_server.pool("blog_small").executed == []

    async def test_query_tool_with_execution(self, offline_server) -> None:
        """return_type='result' executes the SQL and returns rows."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="How many users?", return_type="result")

        assert result["success"] is True
        assert result["data"]["row_count"] == 1
        assert result["data"]["rows"] == [{"count": 1}]
        assert result["data"]["columns"] == ["count"]
        assert offline_server.pool("blog_small").executed == [
            "SELECT COUNT(*) FROM users LIMIT 10000"
        ]

    async def test_query_tool_invalid_return_type(self, offline_server) -> None:
        """An invalid return_type is rejected before any work happens."""
        async with lifespan(mcp):
            result = await query(question="SELECT 1", return_type="invalid")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_PARAMETER"
        assert result["tokens_used"] == 0

    async def test_query_tool_empty_question(self, offline_server) -> None:
        """An empty question is rejected by request validation."""
        async with lifespan(mcp):
            result = await query(question="   ")

        assert result["success"] is False
        assert result["error"]["code"] == "INVALID_REQUEST"

    async def test_query_tool_with_database_parameter(self, offline_server) -> None:
        """An explicit database name is honoured."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            result = await query(question="How many users?", database="blog_small")

        assert result["success"] is True
        assert offline_server.pool("blog_small").executed

    async def test_query_tool_unknown_database(self, offline_server) -> None:
        """An unknown database name is rejected with the list of alternatives."""
        async with lifespan(mcp):
            result = await query(question="How many users?", database="nope")

        assert result["success"] is False
        assert result["error"]["code"] == "database_error"
        assert result["error"]["details"]["available_databases"] == ["blog_small"]

    async def test_query_tool_response_format(self, offline_server) -> None:
        """Responses always expose tokens_used + request_id (fixed model bug)."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        async with lifespan(mcp):
            success = await query(question="How many users?")
            failure = await query(question="How many users?", database="nope")

        for payload in (success, failure):
            assert "tokens_used" in payload
            assert isinstance(payload["tokens_used"], int)
            assert payload["request_id"]

    async def test_health_check_tool(self, offline_server) -> None:
        """The health tool reports every configured database."""
        async with lifespan(mcp):
            report = await health_check()

        assert report["status"] in {"healthy", "degraded"}
        assert "blog_small" in report["databases"]
        assert report["components"]["rate_limiter"] is not None


class TestMCPServerErrors:
    """Error handling at the tool boundary."""

    async def test_query_before_initialization(self) -> None:
        """Calling the tool before startup returns a structured error."""
        import pg_mcp.server as server_module

        server_module._orchestrator = None
        result = await query(question="anything")

        assert result["success"] is False
        assert result["error"]["code"] == "SERVER_NOT_INITIALIZED"

    async def test_health_before_initialization(self) -> None:
        """Health check before startup reports unhealthy."""
        import pg_mcp.server as server_module

        server_module._health_checker = None
        result = await health_check()
        assert result["status"] == "unhealthy"

    async def test_malformed_question_handling(self, offline_server) -> None:
        """A question that is only whitespace is rejected gracefully."""
        async with lifespan(mcp):
            result = await query(question="\n\t  ")

        assert result["success"] is False
        assert "error" in result


class TestMCPServerLifecycle:
    """The server can be started repeatedly and serves nested queries."""

    async def test_multiple_lifespan_contexts(self, offline_server) -> None:
        """Start/stop cycles leave no stale state behind."""
        offline_server.configure(script=["SELECT COUNT(*) FROM users;"])
        for _ in range(2):
            async with lifespan(mcp):
                result = await query(question="How many users?")
                assert result["success"] is True

    async def test_sequential_queries_within_lifespan(self, offline_server) -> None:
        """Several queries can be served by a single running server."""
        offline_server.configure(
            script=["SELECT COUNT(*) FROM users;", "SELECT COUNT(*) FROM orders;"]
        )
        async with lifespan(mcp):
            first = await query(question="How many users?")
            second = await query(question="How many orders?")

        assert first["success"] and second["success"]
        assert offline_server.pool("blog_small").executed == [
            "SELECT COUNT(*) FROM users LIMIT 10000",
            "SELECT COUNT(*) FROM orders LIMIT 10000",
        ]
