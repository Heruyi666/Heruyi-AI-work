"""Pytest configuration and shared fixtures.

The suite is **deterministic and offline by default**: PostgreSQL and OpenAI are
replaced with in-memory doubles installed through ``pg_mcp.server.build_components``.
This is the "add mock fixtures for deterministic testing" item from the code
review — the previous integration/E2E tests assumed live services and could not
run in CI or on a developer laptop without a database.

Setting ``PG_MCP_RUN_INTEGRATION=1`` switches the suite to **live mode**: the
database environment variables are no longer overwritten, so the opt-in tests
under ``tests/integration`` talk to a real PostgreSQL instance (start one with
``uv run python scripts/local_postgres.py up``).
"""

import os
from typing import Any

import pytest

from pg_mcp.app import build_components as real_build_components
from pg_mcp.config.settings import reset_settings
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.testing.fakes import (
    DEFAULT_TABLES,
    FakePool,
    StubResultValidator,
    StubSQLGenerator,
    make_pool_factory,
    offline_pg_handler,
)

DEFAULT_DB_NAME = "blog_small"


def live_mode() -> bool:
    """Return True when the suite runs against real services.

    Returns:
        bool: True when ``PG_MCP_RUN_INTEGRATION=1``.
    """
    return os.environ.get("PG_MCP_RUN_INTEGRATION", "").strip() == "1"


@pytest.fixture(autouse=True)
def reset_config() -> None:
    """Reset global settings before each test."""
    reset_settings()


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a hermetic environment (no metrics server, fake API key).

    In live mode the database settings are left untouched so that ``DATABASES``
    (or ``DATABASE_*``) from the caller's environment reaches ``Settings``.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    if live_mode():
        # Keep DATABASES/DATABASE_* as provided; only neutralise the OpenAI key
        # (the live suite never calls the model) and the metrics HTTP server.
        monkeypatch.setenv("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY", "sk-test-offline"))
        monkeypatch.setenv("OBSERVABILITY_METRICS_ENABLED", "false")
        monkeypatch.setenv("OBSERVABILITY_LOG_LEVEL", "WARNING")
        return

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-offline")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("OBSERVABILITY_METRICS_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DATABASE_NAME", DEFAULT_DB_NAME)
    monkeypatch.setenv("DATABASE_HOST", "offline-fixture")
    monkeypatch.setenv("DATABASE_USER", "readonly_user")
    monkeypatch.setenv("DATABASE_PASSWORD", "offline-secret")
    monkeypatch.setenv("DATABASE_MIN_POOL_SIZE", "1")
    monkeypatch.setenv("DATABASE_MAX_POOL_SIZE", "2")
    monkeypatch.delenv("DATABASES", raising=False)


@pytest.fixture(autouse=True)
def clean_metrics() -> MetricsCollector:
    """Reset Prometheus metrics before each test.

    Returns:
        MetricsCollector: The shared metrics singleton, reset.
    """
    collector = MetricsCollector()
    collector.reset_all_metrics()
    return collector


class OfflineServer:
    """Installs offline doubles into ``pg_mcp.server`` for a test.

    Attributes:
        pools: Fake pools keyed by database name (auto-created on demand).
        generator: Scripted SQL generator.
        result_validator: Scripted result validator.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Initialize the harness.

        Args:
            monkeypatch: Pytest monkeypatch fixture.
        """
        self._monkeypatch = monkeypatch
        self._handler = offline_pg_handler(DEFAULT_TABLES)
        self.pools: dict[str, FakePool] = {}
        self.generator = StubSQLGenerator(["SELECT COUNT(*) FROM users;"])
        self.result_validator = StubResultValidator()

    def configure(
        self,
        *,
        script: list[Any] | None = None,
        generator: StubSQLGenerator | None = None,
        result_validator: StubResultValidator | None = None,
        pools: dict[str, FakePool] | None = None,
    ) -> None:
        """Override the doubles used by the next server start.

        Args:
            script: Convenience shortcut to replace the generator script.
            generator: Explicit generator double.
            result_validator: Explicit result validator double.
            pools: Explicit pool mapping (replacing auto-created ones).
        """
        if script is not None:
            self.generator = StubSQLGenerator(script)
        if generator is not None:
            self.generator = generator
        if result_validator is not None:
            self.result_validator = result_validator
        if pools is not None:
            self.pools = dict(pools)

    def pool(self, name: str) -> FakePool:
        """Return (creating if needed) the fake pool for a database.

        Args:
            name: Database name.

        Returns:
            FakePool: The fake pool.
        """
        if name not in self.pools:
            self.pools[name] = FakePool(name, handler=self._handler)
        return self.pools[name]

    def install(self) -> None:
        """Patch ``pg_mcp.server.build_components`` to use the doubles."""
        harness = self

        async def patched(settings: Any, **kwargs: Any) -> Any:
            metrics = kwargs.pop("metrics", None)
            for db in settings.databases:
                harness.pool(db.name)
            components = await real_build_components(
                settings,
                pool_factory=make_pool_factory(harness.pools),
                sql_generator=harness.generator,  # type: ignore[arg-type]
                result_validator=harness.result_validator,  # type: ignore[arg-type]
                metrics=metrics,
                load_schemas=True,
            )
            # Introspection statements are not "user queries": clear them so
            # tests can assert exactly on what the caller asked for.
            for pool in harness.pools.values():
                pool.clear_recordings()
            return components

        self._monkeypatch.setattr("pg_mcp.server.build_components", patched)


@pytest.fixture
def offline_server(monkeypatch: pytest.MonkeyPatch) -> OfflineServer:
    """Provide an installed offline backend for the MCP server.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        OfflineServer: Harness exposing the installed doubles.
    """
    harness = OfflineServer(monkeypatch)
    harness.install()
    return harness


@pytest.fixture
def env_flag() -> Any:
    """Read a boolean environment flag.

    Returns:
        Callable[[str], bool]: Helper returning True when the variable is
        set to a truthy value.
    """

    def _flag(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}

    return _flag
