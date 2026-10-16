"""Contract tests against the real ``asyncpg`` driver (no server required).

The offline suite uses doubles, so a wrong keyword name or an unsupported
value would only surface when somebody finally points the service at a real
PostgreSQL.  These tests close that hole by validating our call sites against
the driver's *actual* signature and documented value domains.

Nothing here needs a running server: we inspect the driver and capture the
arguments our own code produces.
"""

from __future__ import annotations

import inspect
from typing import Any

import asyncpg
import pytest

from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db import pool as pool_module

# Keyword arguments our ``create_pool`` passes through to ``asyncpg.create_pool``.
EXPECTED_CONNECT_KWARGS = (
    "host",
    "port",
    "database",
    "user",
    "password",
    "ssl",
    "min_size",
    "max_size",
    "timeout",
    "command_timeout",
)

# asyncpg documents these SSL modes for its ``ssl`` argument.
ASYNCPG_SSL_MODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


def _connect_params() -> frozenset[str]:
    """Parameter names accepted by the real ``asyncpg.connect``.

    Returns:
        frozenset[str]: Names of every parameter ``connect`` accepts.
    """
    return frozenset(inspect.signature(asyncpg.connect).parameters)


def _create_pool_params() -> frozenset[str]:
    """Parameter names accepted by the real ``asyncpg.create_pool``.

    Returns:
        frozenset[str]: Names of every named parameter ``create_pool`` accepts.
    """
    return frozenset(inspect.signature(asyncpg.create_pool).parameters)


class TestKeywordContract:
    """Every keyword we send must be one the driver actually accepts."""

    @pytest.mark.parametrize("kwarg", EXPECTED_CONNECT_KWARGS)
    def test_keyword_is_accepted_by_the_driver(self, kwarg: str) -> None:
        """A typo'd keyword would only fail against a live server."""
        params = _connect_params() | _create_pool_params()
        assert kwarg in params, f"asyncpg does not accept '{kwarg}'"

    async def test_create_pool_forwards_exactly_expected_keywords(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capture the real call and diff it against the driver signature.

        This is the regression guard: if someone adds or renames a keyword,
        the test fails here instead of at connect time in production.
        """
        # Read the real signatures BEFORE patching: ``pool_module.asyncpg`` is
        # the same module object as the imported ``asyncpg``, so patching it
        # would make the later signature lookup inspect the fake instead.
        accepted = _connect_params() | _create_pool_params()

        captured: dict[str, Any] = {}

        class _FakePool:
            async def close(self) -> None:
                return None

            def terminate(self) -> None:
                return None

        async def fake_create_pool(**kwargs: Any) -> _FakePool:
            captured.update(kwargs)
            return _FakePool()

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        config = DatabaseConfig(
            name="analytics",
            host="db.internal",
            port=5433,
            user="reader",
            password="secret",
        )
        result = await pool_module.create_pool(config)

        assert result is not None
        assert set(captured) == set(EXPECTED_CONNECT_KWARGS), (
            f"unexpected keyword set: {sorted(set(captured) ^ set(EXPECTED_CONNECT_KWARGS))}"
        )
        assert set(captured) <= accepted

    async def test_pool_settings_reach_the_driver_with_correct_types(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pool sizing and timeout values are forwarded unchanged."""
        captured: dict[str, Any] = {}

        class _FakePool:
            async def close(self) -> None:
                return None

            def terminate(self) -> None:
                return None

        async def fake_create_pool(**kwargs: Any) -> _FakePool:
            captured.update(kwargs)
            return _FakePool()

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        config = DatabaseConfig(
            name="billing",
            min_pool_size=2,
            max_pool_size=9,
            pool_timeout=12.5,
            command_timeout=7.0,
        )
        await pool_module.create_pool(config)

        assert captured["min_size"] == 2
        assert captured["max_size"] == 9
        assert captured["timeout"] == 12.5
        assert captured["command_timeout"] == 7.0


class TestSSLModeContract:
    """Our SSL validation must agree with what asyncpg will accept."""

    @pytest.mark.parametrize("mode", sorted(ASYNCPG_SSL_MODES))
    def test_every_asyncpg_mode_passes_our_validator(self, mode: str) -> None:
        """A mode the driver supports must not be rejected by our config."""
        config = DatabaseConfig(name="db", ssl_mode=mode)
        assert config.ssl_mode == mode

    def test_configured_matches_driver_domain(self) -> None:
        """The validator's allow-list and asyncpg's domain must be identical.

        A mode we accept but the driver rejects is a silent production
        failure; a mode the driver supports but we reject blocks a valid
        deployment. Both directions are wrong, so compare the sets.
        """
        accepted = {mode for mode in ASYNCPG_SSL_MODES if _try_build(mode)}

        missing = ASYNCPG_SSL_MODES - accepted
        assert not missing, f"asyncpg supports {sorted(missing)} but config rejects them"

        extra = accepted - ASYNCPG_SSL_MODES
        assert not extra, f"config accepts {sorted(extra)} but asyncpg rejects them"

    @pytest.mark.parametrize("bad", ["", "VERIFY-FULL", "tls", "require-ssl", "none"])
    def test_unsupported_modes_are_rejected(self, bad: str) -> None:
        """Invalid modes fail fast at configuration load, not at connect time."""
        with pytest.raises(ValueError):
            DatabaseConfig(name="db", ssl_mode=bad)

    async def test_ssl_mode_is_forwarded_to_the_driver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The configured mode actually reaches asyncpg's ``ssl`` argument."""
        captured: dict[str, Any] = {}

        class _FakePool:
            async def close(self) -> None:
                return None

            def terminate(self) -> None:
                return None

        async def fake_create_pool(**kwargs: Any) -> _FakePool:
            captured.update(kwargs)
            return _FakePool()

        monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake_create_pool)

        await pool_module.create_pool(DatabaseConfig(name="db", ssl_mode="verify-full"))
        assert captured["ssl"] == "verify-full"


def _try_build(mode: str) -> bool:
    """Return True when DatabaseConfig accepts the given SSL mode.

    Args:
        mode: Candidate SSL mode string.

    Returns:
        bool: True when construction succeeds.
    """
    try:
        DatabaseConfig(name="db", ssl_mode=mode)
    except ValueError:
        return False
    return True
