"""Tests for multi-database routing and component wiring.

These tests cover the Priority 1 review item "Database Selection Not Respected":
before the fix the server always used a single executor, so a request for
database B could run against database A.
"""

import pytest

from pg_mcp.app import build_components
from pg_mcp.models.query import QueryRequest, ReturnType
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


class TestComponentWiring:
    """The composition root must expose one executor per database."""

    @pytest.mark.asyncio
    async def test_one_executor_per_database(self) -> None:
        """Every configured database gets its own pool and executor."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=True,
        )

        assert set(components.pools) == {"analytics", "billing"}
        assert set(components.executors) == {"analytics", "billing"}
        assert components.executors["analytics"].pool is pools["analytics"]
        assert components.executors["billing"].pool is pools["billing"]
        assert components.executors["analytics"].db_name == "analytics"
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_schema_introspection_populates_cache(self) -> None:
        """Startup introspection fills the schema cache for every database."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=True,
        )

        for name in ("analytics", "billing"):
            schema = components.schema_cache.get(name)
            assert schema is not None
            assert {table.table_name for table in schema.tables} == {
                table.name for table in DEFAULT_TABLES
            }
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_security_policy_wired_into_validator(self) -> None:
        """blocked_tables/blocked_columns/allow_explain reach the validator."""
        settings = build_offline_settings(
            databases=("analytics",),
            blocked_tables=("payments",),
            blocked_columns=("users.password_hash",),
            allow_explain=True,
        )
        pools = make_pools("analytics")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=False,
        )

        validator = components.sql_validator
        assert validator.blocked_tables == {"payments"}
        assert validator.blocked_columns == {"users.password_hash"}
        assert validator.allow_explain is True
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_rate_limits_come_from_settings(self) -> None:
        """The rate limiter reflects the configured concurrency limits."""
        settings = build_offline_settings(
            databases=("analytics",),
            max_concurrent_queries=3,
            max_concurrent_llm_calls=2,
        )
        pools = make_pools("analytics")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=False,
        )

        assert components.rate_limiter.query_limiter.max_concurrent == 3
        assert components.rate_limiter.llm_limiter.max_concurrent == 2
        assert components.orchestrator.rate_limiter is components.rate_limiter
        await components.shutdown()


class TestDatabaseRouting:
    """Requests must run on the executor of the resolved database."""

    @pytest.mark.asyncio
    async def test_query_hits_requested_database_only(self) -> None:
        """A request for 'billing' executes only on the billing pool."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        generator = StubSQLGenerator(["SELECT COUNT(*) FROM orders;"])
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=generator,  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=True,
        )
        for pool in pools.values():
            pool.clear_recordings()

        response = await components.orchestrator.execute_query(
            QueryRequest(question="How many orders?", database="billing")
        )

        assert response.success is True
        # LIMIT is pushed down so the database stops early (design review 0003 #2).
        assert pools["billing"].executed == ["SELECT COUNT(*) FROM orders LIMIT 10000"]
        assert pools["analytics"].executed == []
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_each_database_can_be_targeted(self) -> None:
        """Both databases are reachable through the same orchestrator."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        generator = StubSQLGenerator(
            ["SELECT COUNT(*) FROM users;", "SELECT COUNT(*) FROM orders;"]
        )
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=generator,  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=True,
        )
        for pool in pools.values():
            pool.clear_recordings()

        await components.orchestrator.execute_query(
            QueryRequest(question="count users", database="analytics")
        )
        await components.orchestrator.execute_query(
            QueryRequest(question="count orders", database="billing")
        )

        assert len(pools["analytics"].executed) == 1
        assert len(pools["billing"].executed) == 1
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_unknown_database_is_rejected_with_alternatives(self) -> None:
        """An unknown database name fails fast and lists the configured ones."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="anything", database="reporting")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "database_error"
        assert response.error.details is not None
        assert response.error.details["available_databases"] == ["analytics", "billing"]
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_ambiguous_selection_requires_database(self) -> None:
        """With several databases configured the caller must pick one."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        pools = make_pools("analytics", "billing")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="anything", return_type=ReturnType.RESULT)
        )

        assert response.success is False
        assert response.error is not None
        assert "Multiple databases" in response.error.message
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_single_database_is_auto_selected(self) -> None:
        """A single configured database is selected automatically."""
        settings = build_offline_settings(databases=("analytics",))
        pools = make_pools("analytics")
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT 1;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="anything")
        )

        assert response.success is True
        assert pools["analytics"].executed
        await components.shutdown()
