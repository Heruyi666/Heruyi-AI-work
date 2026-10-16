"""Regression guard for the schema-cache stampede found under load.

A burst of concurrent requests against a *cold* cache used to make every
request run its own schema introspection, before the concurrency limiter was
ever consulted.  With 60 concurrent callers that meant 60 simultaneous
introspection round-trips against the same database -- the rate limiter's
DoS protection was bypassed entirely.

The fix is per-database double-checked locking inside ``SchemaCache.load``,
so the first caller introspects and everyone else re-reads the cache.

These tests assert the *concurrency* property, which a sequential unit test
can never observe.
"""

from __future__ import annotations

import asyncio

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import CacheConfig
from pg_mcp.testing.fakes import DEFAULT_TABLES, FakePool, offline_pg_handler


def _make_cache() -> SchemaCache:
    """Build an enabled schema cache.

    Returns:
        SchemaCache: A cache with a long TTL.
    """
    return SchemaCache(CacheConfig(enabled=True, schema_ttl=3600))


class TestConcurrentIntrospection:
    """A cold cache must be introspected once, not once per caller."""

    async def test_concurrent_loads_introspect_only_once(self) -> None:
        """60 concurrent callers produce a single introspection burst."""
        cache = _make_cache()
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))

        schemas = await asyncio.gather(*[cache.load("analytics", pool) for _ in range(60)])

        assert len(schemas) == 60
        assert all(s is not None for s in schemas)
        # Every caller shares the very same object -> one introspection.
        assert len({id(s) for s in schemas}) == 1

    async def test_peak_concurrency_stays_at_one(self) -> None:
        """The stampede fix keeps simultaneous connections to one."""
        cache = _make_cache()
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        pool.clear_recordings()

        await asyncio.gather(*[cache.load("analytics", pool) for _ in range(60)])

        assert pool.peak_in_use == 1, (
            f"expected a single introspection, peak was {pool.peak_in_use}"
        )
        assert pool.in_use == 0

    async def test_expired_cache_refreshes_once(self) -> None:
        """A cache expiry is also a stampede point and behaves the same."""
        cache = _make_cache()
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))

        await cache.load("analytics", pool)
        pool.clear_recordings()

        # Force expiry so every caller sees a missing entry again.
        cache._cache.pop("analytics", None)
        cache._cache_timestamps.pop("analytics", None)

        await asyncio.gather(*[cache.load("analytics", pool) for _ in range(40)])

        assert pool.peak_in_use == 1, f"peak was {pool.peak_in_use}"

    async def test_different_databases_are_isolated(self) -> None:
        """Two databases must not serialise against each other."""
        cache = _make_cache()
        first = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        second = FakePool("billing", handler=offline_pg_handler(DEFAULT_TABLES))

        await asyncio.gather(
            *[cache.load("analytics", first) for _ in range(20)],
            *[cache.load("billing", second) for _ in range(20)],
        )

        # Each database introspected exactly once, independently of the other.
        assert first.peak_in_use == 1
        assert second.peak_in_use == 1

    async def test_disabled_cache_still_introspects_per_caller(self) -> None:
        """With caching off there is nothing to share, so each call loads."""
        cache = SchemaCache(CacheConfig(enabled=False))
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        pool.clear_recordings()

        await asyncio.gather(*[cache.load("analytics", pool) for _ in range(5)])

        assert pool.attempts >= 5
        assert cache.get("analytics") is None
