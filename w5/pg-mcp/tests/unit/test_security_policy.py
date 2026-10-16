"""Tests for the security policies that are now enforced from configuration.

Covers review items:
- "Sensitive Resource Blocking Cannot Be Configured"
- "Input Length Guard Missing"
- "SQL Validation Feedback" / "Result Confidence Thresholds"
"""

import pytest

from pg_mcp.app import build_components
from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.query import QueryRequest
from pg_mcp.services.sql_validator import SQLValidator
from pg_mcp.testing.fakes import (
    DEFAULT_TABLES,
    FakePool,
    StubResultValidator,
    StubSQLGenerator,
    build_offline_settings,
    make_pool_factory,
    offline_pg_handler,
)


async def build_validator(settings: object, *script: str) -> object:
    """Build an orchestrator with offline doubles and a scripted SQL generator.

    Args:
        settings: Settings object to build from.
        *script: SQL answers for the stub generator.

    Returns:
        object: The wired components (``AppComponents``).
    """
    names = [db.name for db in settings.databases]  # type: ignore[attr-defined]
    handler = offline_pg_handler(DEFAULT_TABLES)
    pools = {name: FakePool(name, handler=handler) for name in names}
    return await build_components(
        settings,  # type: ignore[arg-type]
        pool_factory=make_pool_factory(pools),
        sql_generator=StubSQLGenerator(list(script) or ["SELECT 1;"]),  # type: ignore[arg-type]
        result_validator=StubResultValidator(),  # type: ignore[arg-type]
        load_schemas=False,
    )


class TestBlockedTables:
    """blocked_tables from settings must be enforced."""

    def test_validator_accepts_schema_qualified_entries(self) -> None:
        """Schema-qualified entries block both qualified and bare references."""
        validator = SQLValidator(SecurityConfig(), blocked_tables=["public.secrets"])
        with pytest.raises(Exception) as exc_info:
            validator.validate_or_raise("SELECT * FROM public.secrets")
        assert "secrets" in str(exc_info.value).lower()

        with pytest.raises(Exception) as exc_info:
            validator.validate_or_raise("SELECT * FROM secrets")
        assert "secrets" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_config_driven_blocking_end_to_end(self) -> None:
        """A configured blocked table is rejected through the full pipeline."""
        settings = build_offline_settings(
            databases=("analytics",), blocked_tables=("payments",)
        )
        components = await build_validator(settings, "SELECT id FROM payments;")

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="show payments", database="analytics")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.details is not None
        assert response.error.details["reason"] == "blocked_table"
        await components.shutdown()  # type: ignore[attr-defined]


class TestBlockedColumns:
    """blocked_columns from settings must be enforced."""

    def test_qualified_and_wildcard_entries(self) -> None:
        """Qualified names and ``table.*`` wildcards are both supported."""
        validator = SQLValidator(
            SecurityConfig(), blocked_columns=["users.password_hash", "payments.*"]
        )
        for sql in (
            "SELECT password_hash FROM users",
            "SELECT users.password_hash FROM users",
            "SELECT * FROM payments",
            "SELECT p.card_number FROM payments p",
        ):
            with pytest.raises(Exception) as exc_info:
                validator.validate_or_raise(sql)
            assert "not allowed" in str(exc_info.value)

    def test_benign_columns_pass(self) -> None:
        """Unrelated columns are not over-blocked."""
        validator = SQLValidator(SecurityConfig(), blocked_columns=["users.password_hash"])
        validator.validate_or_raise("SELECT id, email FROM users")

    @pytest.mark.asyncio
    async def test_config_driven_column_blocking_end_to_end(self) -> None:
        """Configured sensitive columns are rejected through the pipeline."""
        settings = build_offline_settings(
            databases=("analytics",), blocked_columns=("users.password_hash",)
        )
        components = await build_validator(
            settings, "SELECT id, password_hash FROM users;"
        )

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="show password hashes", database="analytics")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.details is not None
        assert response.error.details["reason"] == "blocked_column"
        await components.shutdown()  # type: ignore[attr-defined]


class TestExplainPolicy:
    """The EXPLAIN policy must follow SECURITY_ALLOW_EXPLAIN."""

    def test_rejected_by_default(self) -> None:
        """EXPLAIN is rejected by default with an actionable reason."""
        validator = SQLValidator(SecurityConfig())
        with pytest.raises(Exception) as exc_info:
            validator.validate_or_raise("EXPLAIN SELECT * FROM users")
        assert getattr(exc_info.value, "details", {})["reason"] == "explain_not_allowed"

    def test_allowed_when_enabled(self) -> None:
        """EXPLAIN is allowed when configured."""
        validator = SQLValidator(SecurityConfig(allow_explain=True))
        validator.validate_or_raise("EXPLAIN ANALYZE SELECT * FROM users")

    def test_config_parses_allow_explain(self) -> None:
        """allow_explain is part of the security configuration schema."""
        assert SecurityConfig().allow_explain is False
        assert SecurityConfig(allow_explain=True).allow_explain is True


class TestRejectionReasons:
    """Rejections carry machine-readable reasons for observability."""

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            ("DELETE FROM users", "forbidden_statement"),
            ("SELECT 1; DROP TABLE users", "multiple_statements"),
            ("SELECT pg_sleep(10)", "blocked_function"),
            ("VACUUM ANALYZE users", "disallowed_command"),
        ],
    )
    def test_reason_codes(self, sql: str, reason: str) -> None:
        """Each rejection path reports a distinct reason code."""
        validator = SQLValidator(SecurityConfig())
        with pytest.raises(Exception) as exc_info:
            validator.validate_or_raise(sql)
        assert getattr(exc_info.value, "details", {}).get("reason") == reason

    def test_build_validation_result_reports_real_outcome(self) -> None:
        """The validation result reflects the real checks (not a hardcoded True)."""
        validator = SQLValidator(SecurityConfig(), blocked_tables=["payments"])

        ok = validator.build_validation_result("SELECT id FROM users")
        assert ok.is_valid is True
        assert ok.is_select is True
        assert ok.allows_data_modification is False

        blocked = validator.build_validation_result("SELECT id FROM payments")
        assert blocked.is_valid is False
        assert "not allowed" in (blocked.error_message or "")

        write = validator.build_validation_result("DELETE FROM users")
        assert write.is_valid is False
        assert write.allows_data_modification is True

        blocked_fn = validator.build_validation_result("SELECT pg_sleep(1)")
        assert blocked_fn.uses_blocked_functions == ["pg_sleep"]


class TestQuestionLengthGuard:
    """ValidationConfig.max_question_length must be enforced before the LLM call."""

    @pytest.mark.asyncio
    async def test_long_question_rejected_without_llm_call(self) -> None:
        """An over-long question is rejected before any LLM invocation."""
        settings = build_offline_settings(databases=("analytics",), max_question_length=20)
        components = await build_validator(settings, "SELECT 1;")

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="x" * 21, database="analytics")
        )

        assert response.success is False
        assert response.error is not None
        assert response.error.code == "question_too_long"
        assert components.sql_generator.calls == []  # type: ignore[attr-defined]
        await components.shutdown()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_question_within_limit_is_processed(self) -> None:
        """A question at the limit still goes through."""
        settings = build_offline_settings(databases=("analytics",), max_question_length=20)
        components = await build_validator(settings, "SELECT 1;")

        response = await components.orchestrator.execute_query(  # type: ignore[attr-defined]
            QueryRequest(question="x" * 20, database="analytics")
        )

        assert response.success is True
        await components.shutdown()  # type: ignore[attr-defined]


class TestConfidenceThreshold:
    """ValidationConfig.min_confidence_score must be applied."""

    @pytest.mark.asyncio
    async def test_low_confidence_produces_warning(self) -> None:
        """A low confidence score is surfaced instead of being silently accepted."""
        settings = build_offline_settings(databases=("analytics",), min_confidence_score=80)
        names = ["analytics"]
        handler = offline_pg_handler(DEFAULT_TABLES)
        pools = {name: FakePool(name, handler=handler) for name in names}
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT COUNT(*) FROM users;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(confidence=55, explanation="partial match"),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="count users", database="analytics")
        )

        assert response.success is True
        assert response.confidence == 55
        assert response.warning is not None
        assert "below the configured threshold" in response.warning
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_high_confidence_has_no_warning(self) -> None:
        """A confident result carries no warning."""
        settings = build_offline_settings(databases=("analytics",), min_confidence_score=70)
        handler = offline_pg_handler(DEFAULT_TABLES)
        pools = {"analytics": FakePool("analytics", handler=handler)}
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT COUNT(*) FROM users;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(confidence=95),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="count users", database="analytics")
        )

        assert response.warning is None
        assert response.confidence == 95
        await components.shutdown()

    @pytest.mark.asyncio
    async def test_validation_failure_no_longer_reports_perfect_confidence(self) -> None:
        """A failing validator reports unverified confidence instead of 100."""
        settings = build_offline_settings(databases=("analytics",))
        handler = offline_pg_handler(DEFAULT_TABLES)
        pools = {"analytics": FakePool("analytics", handler=handler)}
        components = await build_components(
            settings,
            pool_factory=make_pool_factory(pools),
            sql_generator=StubSQLGenerator(["SELECT COUNT(*) FROM users;"]),  # type: ignore[arg-type]
            result_validator=StubResultValidator(error=RuntimeError("validator down")),  # type: ignore[arg-type]
            load_schemas=False,
        )

        response = await components.orchestrator.execute_query(
            QueryRequest(question="count users", database="analytics")
        )

        assert response.success is True
        assert response.confidence == 0
        assert response.warning is not None
        assert "unverified" in response.warning
        await components.shutdown()
