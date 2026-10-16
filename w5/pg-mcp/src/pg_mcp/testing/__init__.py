"""Offline testing/demo helpers.

This package contains test doubles and fixture builders used by the test suite
and by ``scripts/demo_offline.py``. Nothing in the production request path
imports it.
"""

from pg_mcp.testing.fakes import (
    DEFAULT_TABLES,
    FakeConnection,
    FakePool,
    OfflineTable,
    StubResultValidator,
    StubSQLGenerator,
    build_offline_settings,
    make_pool_factory,
    offline_pg_handler,
)

__all__ = [
    "FakeConnection",
    "FakePool",
    "OfflineTable",
    "StubSQLGenerator",
    "StubResultValidator",
    "make_pool_factory",
    "offline_pg_handler",
    "build_offline_settings",
    "DEFAULT_TABLES",
]
