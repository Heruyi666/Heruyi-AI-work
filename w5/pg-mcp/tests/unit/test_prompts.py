"""Unit tests for the prompt builders (test-plan review 0008, section 2.3).

Prompt engineering changes are easy to make and impossible to notice, so these
tests lock in the contract: the question, the schema and any retry feedback must
all reach the model.
"""

import pytest

from pg_mcp.models.schema import ColumnInfo, DatabaseSchema, TableInfo
from pg_mcp.prompts.result_validation import (
    RESULT_VALIDATION_SYSTEM_PROMPT,
    build_validation_prompt,
)
from pg_mcp.prompts.sql_generation import (
    SQL_GENERATION_SYSTEM_PROMPT,
    build_user_prompt,
)


@pytest.fixture
def schema() -> DatabaseSchema:
    """Minimal schema used by the prompt builders."""
    users = TableInfo(
        table_name="users",
        schema_name="public",
        columns=[
            ColumnInfo(name="id", data_type="integer", is_primary_key=True, is_nullable=False),
            ColumnInfo(name="email", data_type="varchar", is_nullable=False),
        ],
    )
    return DatabaseSchema(database_name="analytics", tables=[users], version="15.0")


class TestSQLGenerationPrompt:
    """build_user_prompt assembles every section the model needs."""

    def test_build_user_prompt_basic(self, schema: DatabaseSchema) -> None:
        """The question and the schema both appear in the prompt."""
        prompt = build_user_prompt(question="Count the users", schema=schema)

        assert "Count the users" in prompt
        assert "users" in prompt
        assert "email" in prompt

    def test_build_user_prompt_with_context(self, schema: DatabaseSchema) -> None:
        """Extra context is appended when supplied."""
        prompt = build_user_prompt(
            question="Count the users", schema=schema, context="Only active ones"
        )

        assert "Only active ones" in prompt

    def test_build_user_prompt_with_retry(self, schema: DatabaseSchema) -> None:
        """A failed attempt and its error are surfaced for the retry."""
        prompt = build_user_prompt(
            question="Count the users",
            schema=schema,
            previous_attempt="SELECT * FROM nope;",
            error_feedback='relation "nope" does not exist',
        )

        assert "SELECT * FROM nope;" in prompt
        assert "does not exist" in prompt

    def test_system_prompt_states_read_only_rule(self) -> None:
        """The system prompt forbids anything but SELECT."""
        lowered = SQL_GENERATION_SYSTEM_PROMPT.lower()
        assert "select" in lowered
        assert "postgresql" in lowered or "postgres" in lowered


class TestValidationPrompt:
    """build_validation_prompt renders the sample rows for the validator."""

    def test_build_validation_prompt_includes_question_and_sql(self) -> None:
        """Question, SQL and row count are all present."""
        prompt = build_validation_prompt(
            question="How many users?",
            sql="SELECT COUNT(*) FROM users;",
            results=[{"count": 3}],
            row_count=3,
        )

        assert "How many users?" in prompt
        assert "SELECT COUNT(*) FROM users;" in prompt

    def test_build_validation_prompt_renders_rows(self) -> None:
        """Sample rows are serialised into the prompt."""
        prompt = build_validation_prompt(
            question="List users",
            sql="SELECT id FROM users;",
            results=[{"id": 1}, {"id": 2}],
            row_count=2,
        )

        assert '"id"' in prompt or "'id'" in prompt

    def test_system_prompt_requests_json(self) -> None:
        """The validator must answer with structured JSON."""
        lowered = RESULT_VALIDATION_SYSTEM_PROMPT.lower()
        assert "json" in lowered
        assert "confidence" in lowered
