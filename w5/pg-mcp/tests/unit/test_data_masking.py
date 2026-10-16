"""Unit tests for the masking helpers used before calling an external LLM."""

import pytest

from pg_mcp.services.data_masking import (
    is_sensitive_column,
    mask_rows,
    mask_value,
    masked_columns,
)


class TestColumnClassification:
    """Which columns count as sensitive."""

    @pytest.mark.parametrize(
        "column",
        ["password", "password_hash", "user_token", "card_number", "email", "ssn"],
    )
    def test_builtin_markers_are_sensitive(self, column: str) -> None:
        """Known markers are recognised without configuration."""
        assert is_sensitive_column(column) is True

    def test_ordinary_columns_are_kept(self) -> None:
        """Business columns keep their values."""
        assert is_sensitive_column("order_total") is False
        assert is_sensitive_column("created_at") is False

    def test_configured_bare_name(self) -> None:
        """A configured bare column name matches anywhere."""
        assert is_sensitive_column("internal_note", ["internal_note"]) is True

    def test_configured_qualified_name_matches_bare(self) -> None:
        """``users.password_hash`` also blocks the bare ``password_hash``."""
        assert is_sensitive_column("password_hash", ["users.password_hash"]) is True

    def test_configured_qualified_name_matches_qualified(self) -> None:
        """A qualified column reference matches too."""
        assert is_sensitive_column("users.password_hash", ["users.password_hash"]) is True


class TestMasking:
    """Values are replaced but types and shapes survive."""

    def test_string_becomes_placeholder(self) -> None:
        """Strings are replaced with a fixed placeholder."""
        assert mask_value("hunter2") == "***"

    def test_number_becomes_zero(self) -> None:
        """Numbers are replaced with zero so aggregates stay well-formed."""
        assert mask_value(4111111111111111) == 0

    def test_none_and_bool_are_preserved(self) -> None:
        """Absence and flags carry no identifying content."""
        assert mask_value(None) is None
        assert mask_value(True) is True

    def test_rows_are_masked_without_mutating_input(self) -> None:
        """The caller's rows are left untouched."""
        rows = [{"id": 1, "email": "a@b.com"}]
        masked = mask_rows(rows)

        assert masked == [{"id": 1, "email": "***"}]
        assert rows == [{"id": 1, "email": "a@b.com"}]

    def test_masked_columns_reports_names(self) -> None:
        """The set of masked columns is reported for logging."""
        rows = [{"id": 1, "email": "a@b.com", "token": "t"}]
        assert masked_columns(rows) == ["email", "token"]
