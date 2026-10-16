"""Authentication, per-database authorization and audit logging.

The design review (0003, finding #1) flagged that any MCP client could reach
every configured database with no identity and no audit trail. This module adds
the missing layer:

* :class:`Authenticator` maps an API key to a :class:`Principal`.
* :meth:`Authenticator.authorize` decides whether a principal may query a
  specific database (role-based, database-scoped).
* :class:`AuditLog` emits one structured record per request, successful or not.

Everything is inert until ``AuthConfig.enabled`` is true, so single-user
deployments behave exactly as before.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pg_mcp.models.errors import AuthError, ErrorCode

if TYPE_CHECKING:
    from pg_mcp.config.settings import AuthConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """An authenticated caller."""

    name: str
    roles: tuple[str, ...] = ()
    allowed_databases: tuple[str, ...] = ()

    def may_access(self, database: str | None) -> bool:
        """Report whether this principal may query ``database``.

        Args:
            database: Target database name, or ``None`` when the caller did not
                name one (the orchestrator then picks the only/default one).

        Returns:
            bool: True when access is permitted.
        """
        if not self.allowed_databases:
            return True
        if database is None:
            # An unrestricted principal is required when no database is named.
            return False
        return database in self.allowed_databases


@dataclass
class AuditLog:
    """Structured audit records for every request."""

    enabled: bool = True
    records: list[dict[str, Any]] = field(default_factory=list)

    def record(self, **fields: Any) -> None:
        """Append an audit record and emit it through the logger.

        Args:
            **fields: Audit fields (principal, database, outcome, ...).
        """
        entry = {"timestamp": time.time(), **fields}
        self.records.append(entry)
        if self.enabled:
            logger.info(
                "audit principal=%s database=%s outcome=%s reason=%s request_id=%s",
                entry.get("principal"),
                entry.get("database"),
                entry.get("outcome"),
                entry.get("reason"),
                entry.get("request_id"),
            )


class Authenticator:
    """Resolve API keys to principals and authorize database access."""

    def __init__(self, config: AuthConfig) -> None:
        """Build the lookup table from configuration.

        Args:
            config: Authentication configuration.
        """
        self.enabled = bool(getattr(config, "enabled", False))
        self._by_key: dict[str, Principal] = {}
        for entry in getattr(config, "principals", None) or []:
            secret = entry.api_key.get_secret_value()
            if secret:
                self._by_key[secret] = Principal(
                    name=entry.name,
                    roles=tuple(entry.roles or ()),
                    allowed_databases=tuple(entry.allowed_databases or ()),
                )
        self.audit = AuditLog(
            enabled=bool(getattr(config, "audit_log_enabled", True))
        )

    @property
    def principal_names(self) -> list[str]:
        """Names of configured principals (useful for diagnostics)."""
        return sorted(principal.name for principal in self._by_key.values())

    def authenticate(
        self,
        api_key: str | None,
        *,
        request_id: str | None = None,
        database: str | None = None,
    ) -> Principal | None:
        """Resolve an API key, auditing the attempt.

        Args:
            api_key: Token presented by the caller.
            request_id: Correlation id for the audit record.
            database: Requested database for the audit record.

        Returns:
            Principal | None: The principal, or None when unknown/disabled.

        Raises:
            AuthError: When authentication is enabled but no key was supplied.
        """
        if not self.enabled:
            return None
        if not api_key:
            self.audit.record(
                principal=None,
                database=database,
                outcome="rejected",
                reason="missing_api_key",
                request_id=request_id,
            )
            raise AuthError(
                message="Authentication is enabled but no API key was provided",
                code=ErrorCode.UNAUTHORIZED,
                details={"reason": "missing_api_key"},
            )
        principal = self._by_key.get(api_key)
        if principal is None:
            self.audit.record(
                principal=None,
                database=database,
                outcome="rejected",
                reason="unknown_api_key",
                request_id=request_id,
            )
            raise AuthError(
                message="The provided API key is not recognized",
                code=ErrorCode.UNAUTHORIZED,
                details={"reason": "unknown_api_key"},
            )
        return principal

    def authorize(
        self,
        principal: Principal | None,
        database: str | None,
        *,
        request_id: str | None = None,
    ) -> None:
        """Check that a principal may query ``database``.

        Args:
            principal: Authenticated principal (``None`` when auth is off).
            database: Requested database.
            request_id: Correlation id for the audit record.

        Raises:
            AuthError: When the principal may not access the database.
        """
        if principal is None:
            return
        if not principal.may_access(database):
            self.audit.record(
                principal=principal.name,
                database=database,
                outcome="rejected",
                reason="database_not_permitted",
                request_id=request_id,
            )
            raise AuthError(
                message=(
                    f"Principal '{principal.name}' is not allowed to query "
                    f"database '{database}'"
                ),
                code=ErrorCode.FORBIDDEN,
                details={
                    "reason": "database_not_permitted",
                    "principal": principal.name,
                    "database": database,
                    "allowed_databases": list(principal.allowed_databases),
                },
            )
        self.audit.record(
            principal=principal.name,
            database=database,
            outcome="authorized",
            reason=None,
            request_id=request_id,
        )
