"""Live PostgreSQL regression suite.

Unlike the offline doubles, these tests talk to a **real** PostgreSQL server, so
behaviour that only manifests against a real backend is actually covered:

* connection pool creation/teardown and real query execution
* schema introspection cross-checked against ``information_schema``
* per-database routing (two databases on the same server)
* ``SET ROLE`` (including case-sensitive role names), ``statement_timeout`` and
  ``search_path`` session hardening as PostgreSQL sees it
* row-cap push-down and read-only transaction enforcement

The suite is opt-in: it is skipped unless ``PG_MCP_RUN_INTEGRATION=1``. Start a
server first with ``uv run python scripts/local_postgres.py up``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import asyncpg
import pytest

from pg_mcp.app import AppComponents, build_components
from pg_mcp.config.settings import DatabaseConfig, Settings, reset_settings
from pg_mcp.db.pool import close_pools, create_pools
from pg_mcp.models.errors import DatabaseError, ExecutionTimeoutError, PgMcpError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    ComponentsFactory = Callable[..., Awaitable[AppComponents]]

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

DEFAULT_PORT = int(os.environ.get("PG_MCP_TEST_PORT", "55432"))
READONLY_ROLE = "readonly_user"
#: Mixed-case role used to prove the quoted ``SET ROLE`` fix (0003 #6).
CASE_SENSITIVE_ROLE = "ReadOnly_User"


def live_enabled() -> bool:
    """Return True when the live suite is enabled.

    Returns:
        bool: True when ``PG_MCP_RUN_INTEGRATION=1``.
    """
    return os.environ.get("PG_MCP_RUN_INTEGRATION", "").strip() == "1"


def databases_json(port: int = DEFAULT_PORT) -> str:
    """Build the ``DATABASES`` value for the local cluster.

    Args:
        port: TCP port the cluster listens on.

    Returns:
        str: JSON array accepted by :class:`Settings`.
    """
    return (
        f'[{{"name":"blog_small","host":"127.0.0.1","port":{port},"user":"postgres",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":4}},'
        f'{{"name":"analytics","host":"127.0.0.1","port":{port},"user":"postgres",'
        f'"password":"","ssl_mode":"disable","min_pool_size":1,"max_pool_size":4}}]'
    )


@pytest.fixture(autouse=True)
def require_live_postgres() -> None:
    """Skip the module unless the live suite is enabled."""
    if not live_enabled():
        pytest.skip("set PG_MCP_RUN_INTEGRATION=1 (start scripts/local_postgres.py up first)")


@pytest.fixture
async def live_components(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ComponentsFactory]:
    """Build a real component graph, closing the pools afterwards.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Yields:
        ComponentsFactory: Factory building components with extra environment
        variables applied on top of the live defaults.
    """

    monkeypatch.setenv("DATABASES", os.environ.get("DATABASES") or databases_json())
    built: list[AppComponents] = []

    async def factory(**env: str) -> AppComponents:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings()
        components = await build_components(Settings(), load_schemas=True)
        built.append(components)
        return components

    yield factory
    for components in built:
        await close_pools(components.pools)


class TestRealConnection:
    """Real pool creation and query execution."""

    async def test_pool_executes_real_query(self, live_components: ComponentsFactory) -> None:
        """A real SELECT returns rows from the seeded fixture."""
        components = await live_components()
        rows, total = await components.executors["blog_small"].execute(
            "SELECT count(*) AS n FROM users"
        )
        assert rows[0]["n"] >= 1
        assert total == 1

    async def test_schema_introspection_matches_information_schema(
        self, live_components: ComponentsFactory
    ) -> None:
        """Introspected tables/columns agree with ``information_schema``."""
        components = await live_components()
        schema = components.schema_cache.get("blog_small")
        assert schema is not None

        pool = components.pools["blog_small"]
        async with pool.acquire() as connection:
            real_tables = {
                row["table_name"]
                for row in await connection.fetch(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
                )
            }
            real_columns = {
                (row["table_name"], row["column_name"])
                for row in await connection.fetch(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            }

        introspected_tables = {table.table_name for table in schema.tables}
        assert {"users", "posts", "comments"} <= introspected_tables
        # Tables *and* views are introspected; nothing invented, nothing missing.
        assert introspected_tables == real_tables

        introspected_columns = {
            (table.table_name, column.name) for table in schema.tables for column in table.columns
        }
        assert introspected_columns <= real_columns

    async def test_postgres_version_is_reported(self, live_components: ComponentsFactory) -> None:
        """The introspector reports the real server version."""
        components = await live_components()
        schema = components.schema_cache.get("blog_small")
        assert schema is not None
        assert schema.version and "PostgreSQL" in schema.version


class TestMultiDatabaseRouting:
    """Two databases on one server must stay isolated."""

    async def test_each_database_returns_its_own_data(
        self, live_components: ComponentsFactory
    ) -> None:
        """blog_small sees users, analytics sees events."""
        components = await live_components()
        blog_rows, _ = await components.executors["blog_small"].execute(
            "SELECT count(*) AS n FROM users"
        )
        analytics_rows, _ = await components.executors["analytics"].execute(
            "SELECT count(*) AS n FROM events"
        )
        assert blog_rows[0]["n"] >= 1
        assert analytics_rows[0]["n"] == 5

    async def test_unknown_table_on_other_database_raises(
        self, live_components: ComponentsFactory
    ) -> None:
        """A table that only exists in one database fails in the other."""
        components = await live_components()
        with pytest.raises(DatabaseError):
            await components.executors["analytics"].execute("SELECT count(*) FROM users")

    async def test_pool_creation_failure_propagates(self) -> None:
        """Unreachable database aborts pool creation instead of silently skipping."""
        configs = [
            DatabaseConfig(
                name="blog_small",
                host="127.0.0.1",
                port=DEFAULT_PORT,
                user="postgres",
                ssl_mode="disable",
                min_pool_size=1,
                max_pool_size=2,
            ),
            DatabaseConfig(
                name="does_not_exist",
                host="127.0.0.1",
                port=DEFAULT_PORT,
                user="postgres",
                ssl_mode="disable",
                min_pool_size=1,
                max_pool_size=2,
            ),
        ]
        with pytest.raises(asyncpg.InvalidCatalogNameError):
            await create_pools(configs)


class TestSessionHardening:
    """Session parameters PostgreSQL actually enforces."""

    async def test_search_path_is_set_on_the_real_session(
        self, live_components: ComponentsFactory
    ) -> None:
        """``search_path`` is pinned to the configured safe value."""
        components = await live_components()
        rows, _ = await components.executors["blog_small"].execute(
            "SELECT current_setting('search_path') AS path"
        )
        assert rows[0]["path"] == "public"

    async def test_statement_timeout_is_enforced(self, live_components: ComponentsFactory) -> None:
        """A slow query is aborted instead of hanging the server."""
        components = await live_components()
        with pytest.raises((ExecutionTimeoutError, DatabaseError)):
            await components.executors["blog_small"].execute("SELECT pg_sleep(3)", timeout=0.5)

    async def test_readonly_role_is_applied(self, live_components: ComponentsFactory) -> None:
        """``SET ROLE`` switches the session to the read-only role."""
        components = await live_components(SECURITY_READONLY_ROLE=READONLY_ROLE)
        rows, _ = await components.executors["blog_small"].execute("SELECT current_user AS role")
        assert rows[0]["role"] == READONLY_ROLE

    async def test_quoted_role_preserves_case(self, live_components: ComponentsFactory) -> None:
        """A mixed-case role name survives ``SET ROLE`` (quoted identifier).

        Unquoted identifiers fold to lower case in PostgreSQL, so an unquoted
        ``SET ROLE`` would fail with ``role "readonly_user" does not exist``.
        """
        components = await live_components()
        pool = components.pools["blog_small"]
        async with pool.acquire() as connection:
            await connection.execute(f'DROP ROLE IF EXISTS "{CASE_SENSITIVE_ROLE}"')
            await connection.execute(f'CREATE ROLE "{CASE_SENSITIVE_ROLE}"')
        try:
            components.executors["blog_small"].security_config.readonly_role = CASE_SENSITIVE_ROLE
            rows, _ = await components.executors["blog_small"].execute(
                "SELECT current_user AS role"
            )
            assert rows[0]["role"] == CASE_SENSITIVE_ROLE
        finally:
            async with pool.acquire() as connection:
                await connection.execute("SET ROLE postgres")
                await connection.execute(f'DROP ROLE IF EXISTS "{CASE_SENSITIVE_ROLE}"')

    async def test_writes_are_rejected_in_readonly_transaction(
        self, live_components: ComponentsFactory
    ) -> None:
        """The executor runs inside a read-only transaction: writes cannot land."""
        components = await live_components()
        before, _ = await components.executors["blog_small"].execute(
            "SELECT count(*) AS n FROM users"
        )
        with pytest.raises(PgMcpError):
            await components.executors["blog_small"].execute("DELETE FROM users")
        after, _ = await components.executors["blog_small"].execute(
            "SELECT count(*) AS n FROM users"
        )
        assert before[0]["n"] == after[0]["n"]


class TestRowLimit:
    """The row cap is pushed into the statement."""

    async def test_row_cap_is_pushed_down(self, live_components: ComponentsFactory) -> None:
        """Only ``max_rows`` rows come back from the server."""
        components = await live_components(SECURITY_MAX_ROWS="3")
        rows, row_count = await components.executors["blog_small"].execute("SELECT id FROM posts")
        assert len(rows) <= 3
        # The cap is a SQL LIMIT, so the server never produced more rows; the
        # reported count is therefore the returned count (no phantom total).
        assert row_count == len(rows)

    async def test_row_cap_does_not_break_small_results(
        self, live_components: ComponentsFactory
    ) -> None:
        """Queries below the cap are returned unchanged."""
        components = await live_components(SECURITY_MAX_ROWS="100")
        rows, total = await components.executors["blog_small"].execute(
            "SELECT id FROM categories ORDER BY id"
        )
        assert len(rows) == total
        assert len(rows) >= 1


class TestValidatorAgainstRealDatabase:
    """Security validation holds before anything reaches the server."""

    async def test_drop_table_is_blocked_and_table_survives(
        self, live_components: ComponentsFactory
    ) -> None:
        """A blocked statement never executes against the real database."""
        components = await live_components()
        is_valid, error = components.sql_validator.validate("DROP TABLE users")
        assert is_valid is False
        assert error

        rows, _ = await components.executors["blog_small"].execute(
            "SELECT count(*) AS n FROM users"
        )
        assert rows[0]["n"] >= 1

    async def test_system_schema_access_is_blocked(
        self, live_components: ComponentsFactory
    ) -> None:
        """``pg_catalog`` probing is rejected before execution."""
        components = await live_components()
        is_valid, error = components.sql_validator.validate("SELECT * FROM pg_catalog.pg_authid")
        assert is_valid is False
        assert error

    async def test_cross_database_reference_is_blocked(
        self, live_components: ComponentsFactory
    ) -> None:
        """Three-part names pointing at another database are rejected."""
        components = await live_components()
        is_valid, error = components.sql_validator.validate("SELECT * FROM analytics.public.events")
        assert is_valid is False
        assert error
