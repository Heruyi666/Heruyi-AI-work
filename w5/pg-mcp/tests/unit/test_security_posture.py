"""Tests for the security posture self-assessment.

The posture report is what turns "a protection is optional" into "a
protection that is off is announced at startup".  These tests pin that
behaviour, including the multi-database rule that escalates a disabled
authentication layer from a warning to a risk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest  # noqa: TC002 - required at runtime by the parametrize decorators

if TYPE_CHECKING:  # pragma: no cover - import only needed for annotations
    from pg_mcp.config.settings import Settings

from pg_mcp.security.posture import (
    RISK,
    WARN,
    SecurityPosture,
    assess_security_posture,
    log_security_posture,
)
from pg_mcp.testing.fakes import build_offline_settings


def _settings(**overrides: object) -> Settings:
    """Build offline settings with overrides applied to nested sections.

    Args:
        **overrides: Mapping of ``section__field`` to value.

    Returns:
        Settings: Configured settings object.
    """
    settings = build_offline_settings(databases=("analytics",))
    for key, value in overrides.items():
        section, field = key.split("__", 1)
        setattr(getattr(settings, section), field, value)
    return settings


def _by_name(posture: SecurityPosture, name: str) -> str:
    """Look up the status of one check.

    Args:
        posture: The posture report.
        name: Check identifier.

    Returns:
        str: Status of that check.
    """
    check = next(c for c in posture.checks if c.name == name)
    return check.status


class TestAuthenticationEscalation:
    """Disabled authentication is graded by how much it exposes."""

    def test_single_database_is_only_a_warning(self) -> None:
        """One database with auth off is acceptable but worth flagging."""
        posture = assess_security_posture(_settings())
        assert _by_name(posture, "authentication") == WARN
        assert posture.level == WARN

    def test_multiple_databases_make_it_a_risk(self) -> None:
        """Several databases with auth off means any caller reaches all of them."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        posture = assess_security_posture(settings)
        assert _by_name(posture, "authentication") == RISK
        assert posture.level == RISK

    def test_enabled_authentication_is_ok(self) -> None:
        """Turning auth on clears the finding."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        settings.auth.enabled = True
        posture = assess_security_posture(settings)
        assert _by_name(posture, "authentication") == "ok"


class TestProtectionFlags:
    """Each optional protection maps to the expected finding."""

    def test_system_schema_block_off_is_a_risk(self) -> None:
        """Readable pg_catalog leaks server internals."""
        posture = assess_security_posture(_settings(security__block_system_schemas=False))
        assert _by_name(posture, "blocked_tables") in {"ok", WARN}
        assert _by_name(posture, "system_schema_block") == RISK

    def test_row_limit_off_is_a_risk(self) -> None:
        """Fetching everything before slicing is the DoS path."""
        posture = assess_security_posture(_settings(security__enforce_row_limit=False))
        assert _by_name(posture, "row_limit_enforcement") == RISK

    def test_masking_off_is_a_risk(self) -> None:
        """Unmasked sample rows send PII to the LLM."""
        posture = assess_security_posture(_settings(validation__mask_sensitive_data=False))
        assert _by_name(posture, "llm_data_masking") == RISK

    def test_explain_allowed_is_only_a_warning(self) -> None:
        """EXPLAIN is read-only, so it downgrades to a warning."""
        posture = assess_security_posture(_settings(security__allow_explain=True))
        assert _by_name(posture, "explain_policy") == WARN


class TestFullyHardenedConfiguration:
    """A deployment that follows every recommendation reports clean."""

    def test_all_checks_pass(self) -> None:
        """Nothing is flagged when every protection is configured."""
        settings = build_offline_settings(
            databases=("analytics",),
            blocked_tables=("payments",),
            blocked_columns=("users.password_hash",),
        )
        settings.auth.enabled = True
        settings.security.block_system_schemas = True
        settings.security.enforce_row_limit = True
        settings.security.readonly_role = "readonly_user"
        settings.security.allow_explain = False
        settings.validation.mask_sensitive_data = True

        posture = assess_security_posture(settings)

        assert posture.level == "ok", [c.to_dict() for c in posture.checks]
        assert posture.risks == []
        assert posture.warnings == []


class TestReporting:
    """The report is serialisable and announced at the right log level."""

    def test_to_dict_shape(self) -> None:
        """The tool payload carries level, counts and every check."""
        payload = assess_security_posture(_settings()).to_dict()
        assert payload["level"] in {"ok", WARN, RISK}
        assert isinstance(payload["risk_count"], int)
        assert isinstance(payload["warn_count"], int)
        assert payload["checks"]
        assert {"name", "status", "detail", "remedy"} <= set(payload["checks"][0])

    def test_risks_are_logged_at_warning_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A risk must not hide in an INFO line."""
        settings = build_offline_settings(databases=("analytics", "billing"))
        posture = assess_security_posture(settings)

        with caplog.at_level(logging.INFO, logger="pg_mcp.security.posture"):
            log_security_posture(posture)

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a risk produced no WARNING record"
        assert any("authentication" in r.getMessage() for r in warnings)

    def test_clean_posture_logs_no_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """A clean report stays at INFO so it does not cry wolf."""
        posture = SecurityPosture(checks=[])
        with caplog.at_level(logging.INFO, logger="pg_mcp.security.posture"):
            log_security_posture(posture)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_every_check_has_a_remedy_when_not_ok(self) -> None:
        """An actionable report tells the operator how to fix each finding."""
        posture = assess_security_posture(_settings())
        for check in posture.checks:
            if check.status == "ok":
                assert check.remedy is None
            else:
                assert check.remedy, f"{check.name} has no remedy"
