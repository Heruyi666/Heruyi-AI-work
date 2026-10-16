"""Masking of sensitive values before they leave the process.

The result validator sends a sample of the query result to an external LLM.
Without masking that sample can carry passwords, card numbers, tokens or other
personal data, which is a compliance problem rather than a styling one
(design review 0003, finding #3).

Masking keeps the *shape* of the result -- column names, row count, value types
-- because that is what the validator needs to judge whether the result answers
the question. Only the values of sensitive columns are replaced.
"""

from __future__ import annotations

from typing import Any

# Column-name fragments treated as sensitive even when not explicitly
# configured. Matching is substring based and case-insensitive.
SENSITIVE_COLUMN_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "card_number",
    "credit_card",
    "cvv",
    "ssn",
    "id_card",
    "idcard",
    "passport",
    "phone",
    "mobile",
    "email",
    "address",
    "salary",
    "iban",
    "private_key",
)

MASKED_STRING = "***"
MASKED_NUMBER = 0


def is_sensitive_column(
    column: str,
    configured: tuple[str, ...] | list[str] = (),
) -> bool:
    """Report whether a column must be masked.

    Args:
        column: Column name as returned by the database.
        configured: Explicitly configured sensitive columns. Both bare names
            (``password_hash``) and qualified names (``users.password_hash``)
            are accepted.

    Returns:
        bool: True when the column value must be replaced.
    """
    lowered = column.lower()
    for entry in configured:
        entry = entry.strip().lower()
        if not entry:
            continue
        # "users.password_hash" also matches the bare column name.
        bare = entry.rsplit(".", 1)[-1]
        if lowered in (entry, bare) or lowered.endswith(f".{bare}"):
            return True
    return any(marker in lowered for marker in SENSITIVE_COLUMN_MARKERS)


def mask_value(value: Any) -> Any:
    """Replace a single sensitive value while keeping its type.

    Args:
        value: Original value.

    Returns:
        Any: A placeholder of the same coarse type (``None`` and booleans are
        left alone because they carry no identifying content).
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return MASKED_NUMBER
    return MASKED_STRING


def mask_rows(
    rows: list[dict[str, Any]],
    configured: tuple[str, ...] | list[str] = (),
) -> list[dict[str, Any]]:
    """Return a copy of ``rows`` with sensitive column values replaced.

    Args:
        rows: Result rows to mask.
        configured: Explicitly configured sensitive columns.

    Returns:
        list[dict[str, Any]]: New list of new dicts; the input is not mutated.

    Example:
        >>> mask_rows([{"id": 1, "password_hash": "abc"}])
        [{'id': 1, 'password_hash': '***'}]
    """
    masked: list[dict[str, Any]] = []
    for row in rows:
        new_row: dict[str, Any] = {}
        for column, value in row.items():
            if is_sensitive_column(str(column), configured):
                new_row[column] = mask_value(value)
            else:
                new_row[column] = value
        masked.append(new_row)
    return masked


def masked_columns(
    rows: list[dict[str, Any]],
    configured: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """List the columns that :func:`mask_rows` would mask.

    Args:
        rows: Result rows to inspect.
        configured: Explicitly configured sensitive columns.

    Returns:
        list[str]: Column names (in first-seen order) that are sensitive.
    """
    seen: list[str] = []
    for row in rows:
        for column in row:
            name = str(column)
            if name not in seen and is_sensitive_column(name, configured):
                seen.append(name)
    return seen
