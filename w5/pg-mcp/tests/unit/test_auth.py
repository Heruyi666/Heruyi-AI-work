"""Unit tests for authentication / authorization (design review 0003, finding #1)."""

import pytest
from pydantic import SecretStr

from pg_mcp.config.settings import AuthConfig, AuthPrincipal
from pg_mcp.models.errors import AuthError, ErrorCode
from pg_mcp.security.auth import Authenticator, Principal


def _config(*principals: AuthPrincipal, **kwargs: object) -> AuthConfig:
    """Build an AuthConfig from principals.

    Args:
        *principals: Principals to configure.
        **kwargs: Extra AuthConfig fields (e.g. ``enabled``).

    Returns:
        AuthConfig: The configuration.
    """
    payload: dict = {"principals": list(principals), "enabled": True}
    payload.update(kwargs)
    return AuthConfig(**payload)  # type: ignore[arg-type]


@pytest.fixture
def authenticator() -> Authenticator:
    """An enabled authenticator with a scoped and an unrestricted caller."""
    return Authenticator(
        _config(
            AuthPrincipal(
                name="analyst",
                api_key=SecretStr("key-analyst"),
                roles=["reader"],
                allowed_databases=["analytics"],
            ),
            AuthPrincipal(
                name="admin",
                api_key=SecretStr("key-admin"),
                roles=["admin"],
            ),
        )
    )


class TestAuthentication:
    """API keys resolve to principals, or fail loudly."""

    def test_unknown_key_is_rejected(self, authenticator: Authenticator) -> None:
        """An unknown key raises UNAUTHORIZED and is audited."""
        with pytest.raises(AuthError) as exc:
            authenticator.authenticate("key-nope", request_id="r1")

        assert exc.value.code == ErrorCode.UNAUTHORIZED
        assert exc.value.details["reason"] == "unknown_api_key"
        assert authenticator.audit.records[-1]["outcome"] == "rejected"

    def test_missing_key_is_rejected(self, authenticator: Authenticator) -> None:
        """A missing key raises UNAUTHORIZED."""
        with pytest.raises(AuthError) as exc:
            authenticator.authenticate(None, request_id="r2")

        assert exc.value.details["reason"] == "missing_api_key"

    def test_valid_key_resolves_principal(self, authenticator: Authenticator) -> None:
        """A known key resolves to its principal with roles attached."""
        principal = authenticator.authenticate("key-analyst")

        assert principal is not None
        assert principal.name == "analyst"
        assert principal.roles == ("reader",)

    def test_disabled_authenticator_is_transparent(self) -> None:
        """With auth disabled nothing is enforced (backward compatibility)."""
        auth = Authenticator(AuthConfig(enabled=False))

        assert auth.authenticate(None) is None
        auth.authorize(None, "anything")


class TestAuthorization:
    """Access is scoped per database."""

    def test_scoped_principal_may_access_its_database(
        self, authenticator: Authenticator
    ) -> None:
        """The analyst reaches the database it is scoped to."""
        principal = authenticator.authenticate("key-analyst")
        authenticator.authorize(principal, "analytics", request_id="r3")

        assert authenticator.audit.records[-1]["outcome"] == "authorized"

    def test_scoped_principal_cannot_cross_databases(
        self, authenticator: Authenticator
    ) -> None:
        """A request for another database is FORBIDDEN, not silently allowed."""
        principal = authenticator.authenticate("key-analyst")

        with pytest.raises(AuthError) as exc:
            authenticator.authorize(principal, "billing", request_id="r4")

        assert exc.value.code == ErrorCode.FORBIDDEN
        assert exc.value.details["reason"] == "database_not_permitted"
        assert exc.value.details["allowed_databases"] == ["analytics"]

    def test_unrestricted_principal_reaches_everything(
        self, authenticator: Authenticator
    ) -> None:
        """A principal with no restriction list can query any database."""
        principal = authenticator.authenticate("key-admin")
        authenticator.authorize(principal, "billing")
        authenticator.authorize(principal, "analytics")

    def test_unnamed_database_requires_unrestricted_principal(
        self, authenticator: Authenticator
    ) -> None:
        """Without a database name, only unrestricted callers may proceed."""
        scoped = authenticator.authenticate("key-analyst")
        with pytest.raises(AuthError):
            authenticator.authorize(scoped, None)


class TestPrincipal:
    """The access rule itself."""

    def test_empty_allow_list_means_all(self) -> None:
        """No configured databases means unrestricted."""
        assert Principal(name="p").may_access("anything") is True

    def test_allow_list_is_enforced(self) -> None:
        """Only listed databases are reachable."""
        principal = Principal(name="p", allowed_databases=("a", "b"))
        assert principal.may_access("a") is True
        assert principal.may_access("c") is False


class TestAuditLog:
    """Every decision leaves a record."""

    def test_records_carry_correlation_id(self, authenticator: Authenticator) -> None:
        """Audit records are linked to the request id."""
        principal = authenticator.authenticate("key-analyst", request_id="req-42")
        authenticator.authorize(principal, "analytics", request_id="req-42")

        assert {record["request_id"] for record in authenticator.audit.records} == {"req-42"}

    def test_audit_can_be_silenced(self) -> None:
        """Disabling the log keeps records but stops the logger."""
        auth = Authenticator(_config(audit_log_enabled=False))
        assert auth.audit.enabled is False
