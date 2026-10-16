"""Security posture self-assessment.

Several protections in this service are intentionally optional so that a
single-user stdio deployment keeps working out of the box.  Optional is not
the same as invisible: a deployment that leaves a protection off should say
so loudly at startup instead of failing quietly in production.

This module turns the effective configuration into a short report that is
logged on boot and exposed through the ``health_check`` tool.  It does not
change behaviour -- it is pure observation -- which keeps it safe to run in
every environment, including tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only needed for annotations
    from pg_mcp.config.settings import Settings

logger = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
RISK = "risk"


@dataclass(frozen=True)
class Check:
    """Outcome of a single posture check.

    Attributes:
        name: Short identifier of the check.
        status: One of ``ok``, ``warn`` or ``risk``.
        detail: Human readable explanation of the current state.
        remedy: Concrete action that moves the check to ``ok`` (None when OK).
    """

    name: str
    status: str
    detail: str
    remedy: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Serialise the check.

        Returns:
            dict[str, str | None]: JSON-friendly representation.
        """
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class SecurityPosture:
    """Aggregated result of every posture check.

    Attributes:
        checks: Individual check outcomes.
    """

    checks: list[Check] = field(default_factory=list)

    @property
    def risks(self) -> list[Check]:
        """Checks that leave the deployment materially exposed.

        Returns:
            list[Check]: Checks whose status is ``risk``.
        """
        return [c for c in self.checks if c.status == RISK]

    @property
    def warnings(self) -> list[Check]:
        """Checks that are enabled but could be tightened.

        Returns:
            list[Check]: Checks whose status is ``warn``.
        """
        return [c for c in self.checks if c.status == WARN]

    @property
    def level(self) -> str:
        """Overall posture level.

        Returns:
            str: ``risk`` when any risk exists, else ``warn``, else ``ok``.
        """
        if self.risks:
            return RISK
        if self.warnings:
            return WARN
        return OK

    def to_dict(self) -> dict[str, object]:
        """Serialise the whole report.

        Returns:
            dict[str, object]: Level plus every individual check.
        """
        return {
            "level": self.level,
            "risk_count": len(self.risks),
            "warn_count": len(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
        }


def assess_security_posture(settings: Settings) -> SecurityPosture:
    """Evaluate the effective configuration against a safe baseline.

    Args:
        settings: Loaded application settings.

    Returns:
        SecurityPosture: Report describing every relevant protection.
    """
    checks: list[Check] = []
    db_count = len(settings.databases)
    multi_db = db_count > 1

    auth = settings.auth
    if not auth.enabled:
        status = RISK if multi_db else WARN
        checks.append(
            Check(
                name="authentication",
                status=status,
                detail=(
                    f"authentication is disabled while {db_count} database(s) are exposed"
                    if multi_db
                    else "authentication is disabled (single-user deployment)"
                ),
                remedy="set AUTH__ENABLED=true and configure AUTH__PRINCIPALS",
            )
        )
    else:
        checks.append(
            Check(
                name="authentication",
                status=OK,
                detail=f"{len(auth.principals)} principal(s) configured",
                remedy=None,
            )
        )

    security = settings.security

    checks.append(
        _flag(
            name="system_schema_block",
            enabled=security.block_system_schemas,
            on="pg_catalog / information_schema access is blocked",
            off="system schemas such as pg_catalog are readable",
            remedy="set SECURITY__BLOCK_SYSTEM_SCHEMAS=true",
        )
    )

    checks.append(
        _flag(
            name="row_limit_enforcement",
            enabled=security.enforce_row_limit,
            on=f"row limit ({security.max_rows}) is pushed down into SQL",
            off="result rows are sliced in Python after a full fetch",
            remedy="set SECURITY__ENFORCE_ROW_LIMIT=true",
        )
    )

    if security.blocked_tables:
        checks.append(
            Check(
                name="blocked_tables",
                status=OK,
                detail=f"{len(security.blocked_tables)} table(s) blocked",
                remedy=None,
            )
        )
    else:
        checks.append(
            Check(
                name="blocked_tables",
                status=WARN,
                detail="no table is blocked; every readable table can be queried",
                remedy="set SECURITY__BLOCKED_TABLES to sensitive table names",
            )
        )

    if security.blocked_columns:
        checks.append(
            Check(
                name="blocked_columns",
                status=OK,
                detail=f"{len(security.blocked_columns)} column(s) blocked",
                remedy=None,
            )
        )
    else:
        checks.append(
            Check(
                name="blocked_columns",
                status=WARN,
                detail="no column is blocked; PII columns can be selected",
                remedy="set SECURITY__BLOCKED_COLUMNS to sensitive column names",
            )
        )

    masking = settings.validation.mask_sensitive_data
    checks.append(
        _flag(
            name="llm_data_masking",
            enabled=masking,
            on="sample rows are masked before being sent to the LLM",
            off="sample rows are sent to the LLM verbatim",
            remedy="set VALIDATION__MASK_SENSITIVE_DATA=true",
        )
    )

    if security.readonly_role:
        checks.append(
            Check(
                name="readonly_role",
                status=OK,
                detail=f"queries run as '{security.readonly_role}'",
                remedy=None,
            )
        )
    else:
        checks.append(
            Check(
                name="readonly_role",
                status=WARN,
                detail="no read-only role; queries run with the login role",
                remedy="set SECURITY__READONLY_ROLE to a read-only role",
            )
        )

    if security.allow_explain:
        checks.append(
            Check(
                name="explain_policy",
                status=WARN,
                detail="EXPLAIN is allowed (plan inspection enabled)",
                remedy="set SECURITY__ALLOW_EXPLAIN=false to forbid it",
            )
        )
    else:
        checks.append(
            Check(
                name="explain_policy",
                status=OK,
                detail="EXPLAIN is rejected by default",
                remedy=None,
            )
        )

    return SecurityPosture(checks=checks)


def _flag(
    *,
    name: str,
    enabled: bool,
    on: str,
    off: str,
    remedy: str,
) -> Check:
    """Build a check for a simple boolean protection.

    Args:
        name: Identifier of the check.
        enabled: Whether the protection is turned on.
        on: Detail used when the protection is enabled.
        off: Detail used when the protection is disabled.
        remedy: Action that enables the protection.

    Returns:
        Check: The resulting check outcome.
    """
    return Check(
        name=name,
        status=OK if enabled else RISK,
        detail=on if enabled else off,
        remedy=None if enabled else remedy,
    )


def log_security_posture(posture: SecurityPosture) -> None:
    """Emit the posture report on startup.

    Risks are logged at WARNING level so they are impossible to miss, while
    the full report is always logged at INFO for audit purposes.

    Args:
        posture: Report produced by :func:`assess_security_posture`.
    """
    logger.info(
        "security posture: level=%s risks=%d warnings=%d",
        posture.level,
        len(posture.risks),
        len(posture.warnings),
    )
    for check in posture.checks:
        message = f"security check [{check.status}] {check.name}: {check.detail}"
        if check.status == RISK:
            logger.warning("%s (remedy: %s)", message, check.remedy)
        else:
            logger.info(message)
