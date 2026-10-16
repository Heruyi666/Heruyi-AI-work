"""Unit tests for schema introspection (test-plan review 0008, section 2.3).

The introspection queries are the contract between the server and PostgreSQL; if
they drift, every generated SQL statement is wrong. These tests run them against
the offline handler so they stay fast and deterministic.
"""

import pytest

from pg_mcp.db.introspection import SchemaIntrospector
from pg_mcp.testing.fakes import DEFAULT_TABLES, FakePool, offline_pg_handler


@pytest.fixture
def pool() -> FakePool:
    """Fake pool answering the introspection queries."""
    return FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))


class TestIntrospection:
    """introspect() assembles a complete schema."""

    @pytest.mark.asyncio
    async def test_database_name_is_recorded(self, pool: FakePool) -> None:
        """The schema carries the database it came from."""
        introspector = SchemaIntrospector(pool, "analytics")  # type: ignore[arg-type]
        schema = await introspector.introspect()

        assert schema.database_name == "analytics"

    @pytest.mark.asyncio
    async def test_all_tables_are_discovered(self, pool: FakePool) -> None:
        """Every table exposed by the handler appears in the schema."""
        introspector = SchemaIntrospector(pool, "analytics")  # type: ignore[arg-type]
        schema = await introspector.introspect()

        assert {table.table_name for table in schema.tables} == {
            table.name for table in DEFAULT_TABLES
        }

    @pytest.mark.asyncio
    async def test_columns_are_loaded_with_types(self, pool: FakePool) -> None:
        """Columns carry their PostgreSQL type and nullability."""
        introspector = SchemaIntrospector(pool, "analytics")  # type: ignore[arg-type]
        schema = await introspector.introspect()

        users = next(table for table in schema.tables if table.table_name == "users")
        assert users.columns, "users must expose columns"
        assert all(column.data_type for column in users.columns)

    @pytest.mark.asyncio
    async def test_schema_is_renderable_for_prompts(self, pool: FakePool) -> None:
        """The schema can be rendered into the LLM prompt context."""
        introspector = SchemaIntrospector(pool, "analytics")  # type: ignore[arg-type]
        schema = await introspector.introspect()

        context = schema.to_prompt_context()
        assert "users" in context

    @pytest.mark.asyncio
    async def test_unknown_database_yields_empty_schema(self) -> None:
        """A handler with no tables produces an empty schema, not an error."""
        empty_pool = FakePool("empty", handler=offline_pg_handler([]))
        introspector = SchemaIntrospector(empty_pool, "empty")  # type: ignore[arg-type]
        schema = await introspector.introspect()

        assert schema.tables == []
