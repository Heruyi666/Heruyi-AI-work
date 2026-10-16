"""Tests for pool lifecycle: multi-database creation, rollback and shutdown.

``db/pool.py`` was the least covered module because the offline suite never
exercises the multi-pool path.  These tests drive it with a stubbed
``asyncpg.create_pool`` so the real orchestration logic -- creation order,
rollback on partial failure, and forced termination on timeout -- is covered
without a PostgreSQL server.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db import pool as pool_module


class _StubPool:
    """Minimal stand-in for an asyncpg pool.

    Attributes:
        closed: Set once ``close`` has been awaited.
        terminated: Set once ``terminate`` has been called.
        hang: When True, ``close`` never returns (simulates a stuck pool).
        raise_on_close: When True, ``close`` raises instead of returning.
    """

    def __init__(
        self,
        *,
        hang: bool = False,
        raise_on_close: bool = False,
    ) -> None:
        self.closed = False
        self.terminated = False
        self.hang = hang
        self.raise_on_close = raise_on_close

    async def close(self) -> None:
        if self.hang:
            await asyncio.sleep(10)
        if self.raise_on_close:
            raise RuntimeError("close failed")
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


def _patch_create_pool(
    monkeypatch: pytest.MonkeyPatch,
    created: list[_StubPool],
    *,
    fail_on: str | None = None,
    hang_for: str | None = None,
) -> None:
    """Replace ``asyncpg.create_pool`` with a recording stub.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        created: List collecting every stub pool handed out.
        fail_on: Database name that should raise instead of returning a pool.
        hang_for: Database name whose pool should hang on ``close``.
    """

    async def fake_create_pool(**kwargs: Any) -> _StubPool:
        name = kwargs.get("database")
        if fail_on is not None and name == fail_on:
            raise OSError(f"cannot reach {name}")
        stub = _StubPool(hang=(hang_for is not None and name == hang_for))
        created.append(stub)
        return stub  # type: ignore[return-value]

    monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)


class TestCreatePools:
    """Multi-database pool creation."""

    async def test_empty_configuration_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Starting with no database at all is a configuration error."""
        created: list[_StubPool] = []
        _patch_create_pool(monkeypatch, created)

        with pytest.raises(ValueError, match="At least one database"):
            await pool_module.create_pools([])

        assert created == []

    async def test_every_database_gets_its_own_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pools are keyed by database name."""
        created: list[_StubPool] = []
        _patch_create_pool(monkeypatch, created)

        pools = await pool_module.create_pools(
            [DatabaseConfig(name="analytics"), DatabaseConfig(name="billing")]
        )

        assert set(pools) == {"analytics", "billing"}
        assert len(created) == 2

    async def test_partial_failure_rolls_back_created_pools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A late failure must not leak the pools created before it."""
        created: list[_StubPool] = []
        _patch_create_pool(monkeypatch, created, fail_on="billing")

        with pytest.raises(OSError):
            await pool_module.create_pools(
                [DatabaseConfig(name="analytics"), DatabaseConfig(name="billing")]
            )

        assert len(created) == 1, "only the first pool should have been created"
        assert created[0].closed is True, "the surviving pool must be closed"


class TestClosePools:
    """Shutdown behaviour, including the paths that force termination."""

    async def test_graceful_close_marks_every_pool(self) -> None:
        """Normal shutdown closes every pool without terminating."""
        pools = {"a": _StubPool(), "b": _StubPool()}

        await pool_module.close_pools(pools)  # type: ignore[arg-type]

        assert all(p.closed for p in pools.values())
        assert not any(p.terminated for p in pools.values())

    async def test_timeout_forces_termination(self) -> None:
        """A pool that will not close is terminated rather than awaited."""
        stuck = _StubPool(hang=True)
        pools = {"stuck": stuck}

        await pool_module.close_pools(pools, timeout=0.05)  # type: ignore[arg-type]

        assert stuck.terminated is True, "a stuck pool must be terminated"

    async def test_error_during_close_forces_termination(self) -> None:
        """A failing close still releases the pool."""
        broken = _StubPool(raise_on_close=True)
        pools = {"broken": broken}

        await pool_module.close_pools(pools)  # type: ignore[arg-type]

        assert broken.terminated is True

    async def test_one_bad_pool_does_not_block_the_others(self) -> None:
        """Shutdown continues past a failing pool."""
        broken = _StubPool(raise_on_close=True)
        healthy = _StubPool()
        pools = {"broken": broken, "healthy": healthy}

        await pool_module.close_pools(pools)  # type: ignore[arg-type]

        assert broken.terminated is True
        assert healthy.closed is True
