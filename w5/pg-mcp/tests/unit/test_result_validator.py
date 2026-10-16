"""Unit tests for the result validator (test-plan review 0008, section 2.3).

Covers the behaviour the review listed as missing: successful validation, low
confidence, the disabled path, timeouts, and -- added for design review 0003
finding #3 -- proof that sensitive values never reach the external LLM.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from pg_mcp.config.settings import OpenAIConfig, SecurityConfig, ValidationConfig
from pg_mcp.models.errors import LLMError, LLMTimeoutError, LLMUnavailableError
from pg_mcp.services.result_validator import ResultValidator


def _response(payload: dict, tokens: int = 42) -> MagicMock:
    """Build a minimal OpenAI chat completion mock.

    Args:
        payload: JSON body the model should "return".
        tokens: Reported total token usage.

    Returns:
        MagicMock: Completion object usable by the validator.
    """
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    completion.usage = MagicMock(total_tokens=tokens)
    return completion


@pytest.fixture
def validator() -> ResultValidator:
    """Validator with masking enabled and a sensitive column configured."""
    return ResultValidator(
        OpenAIConfig(api_key=SecretStr("sk-test-key"), model="gpt-4o-mini"),
        ValidationConfig(enabled=True, confidence_threshold=70, sample_rows=10),
        SecurityConfig(blocked_columns=["users.password_hash"]),
    )


class TestValidationOutcomes:
    """Confidence handling for each LLM response shape."""

    @pytest.mark.asyncio
    async def test_validation_success(self, validator: ResultValidator) -> None:
        """A confident answer is accepted and tokens are reported."""
        completion = _response(
            {"confidence": 95, "explanation": "The count matches the question."}
        )
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            result = await validator.validate(
                question="How many users?",
                sql="SELECT COUNT(*) FROM users;",
                results=[{"count": 7}],
                row_count=1,
            )

        assert result.confidence == 95
        assert result.is_acceptable is True
        assert validator.last_tokens_used == 42

    @pytest.mark.asyncio
    async def test_validation_low_confidence(self, validator: ResultValidator) -> None:
        """A score below the threshold is reported as unacceptable."""
        completion = _response({"confidence": 20, "explanation": "Wrong column."})
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            result = await validator.validate(
                question="How many users?",
                sql="SELECT COUNT(*) FROM users;",
                results=[{"count": 7}],
                row_count=1,
            )

        assert result.confidence == 20
        assert result.is_acceptable is False

    @pytest.mark.asyncio
    async def test_confidence_is_clamped_to_range(self, validator: ResultValidator) -> None:
        """An out-of-range score is clamped instead of propagating."""
        completion = _response({"confidence": 500, "explanation": "nonsense"})
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ):
            result = await validator.validate(
                question="q", sql="SELECT 1;", results=[{"a": 1}], row_count=1
            )

        assert result.confidence == 100

    @pytest.mark.asyncio
    async def test_validation_disabled(self, validator: ResultValidator) -> None:
        """Disabling validation short-circuits without calling the LLM."""
        validator.validation_config.enabled = False
        with patch.object(
            validator.client.chat.completions, "create", new=AsyncMock()
        ) as create:
            result = await validator.validate(
                question="q", sql="SELECT 1;", results=[{"a": 1}], row_count=1
            )

        assert result.confidence == 100
        assert result.is_acceptable is True
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_validation_timeout(self, validator: ResultValidator) -> None:
        """A timeout surfaces as LLMTimeoutError."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=TimeoutError("too slow")),
        ), pytest.raises(LLMTimeoutError):
            await validator.validate(
                question="q", sql="SELECT 1;", results=[{"a": 1}], row_count=1
            )

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, validator: ResultValidator) -> None:
        """An empty choice list is an error, not a confident result."""
        completion = MagicMock()
        completion.choices = []
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(return_value=completion),
        ), pytest.raises(LLMError):
            await validator.validate(
                question="q", sql="SELECT 1;", results=[{"a": 1}], row_count=1
            )

    @pytest.mark.asyncio
    async def test_auth_failure_is_unavailable(self, validator: ResultValidator) -> None:
        """An authentication error is classified as unavailable."""
        with patch.object(
            validator.client.chat.completions,
            "create",
            new=AsyncMock(side_effect=RuntimeError("incorrect api_key provided")),
        ), pytest.raises(LLMUnavailableError):
            await validator.validate(
                question="q", sql="SELECT 1;", results=[{"a": 1}], row_count=1
            )


class TestSensitiveDataMasking:
    """Design review 0003 finding #3: no raw PII may leave the process."""

    @pytest.mark.asyncio
    async def test_configured_sensitive_column_is_masked(
        self, validator: ResultValidator
    ) -> None:
        """A column listed in blocked_columns is replaced before the request."""
        captured: dict = {}
        completion = _response({"confidence": 90, "explanation": "ok"})

        async def _capture(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return completion

        with patch.object(
            validator.client.chat.completions, "create", new=AsyncMock(side_effect=_capture)
        ):
            await validator.validate(
                question="List users",
                sql="SELECT id, password_hash FROM users;",
                results=[{"id": 1, "password_hash": "$2b$12$supersecret"}],
                row_count=1,
            )

        prompt = captured["messages"][1]["content"]
        assert "$2b$12$supersecret" not in prompt
        assert "***" in prompt
        assert validator.last_masked_columns == ["password_hash"]

    @pytest.mark.asyncio
    async def test_heuristic_markers_are_masked(self, validator: ResultValidator) -> None:
        """Email/card-like columns are masked even without configuration."""
        captured: dict = {}

        async def _capture(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return _response({"confidence": 88, "explanation": "ok"})

        with patch.object(
            validator.client.chat.completions, "create", new=AsyncMock(side_effect=_capture)
        ):
            await validator.validate(
                question="List customers",
                sql="SELECT email, card_number FROM customers;",
                results=[{"email": "a@b.com", "card_number": 4111111111111111}],
                row_count=1,
            )

        prompt = captured["messages"][1]["content"]
        assert "a@b.com" not in prompt
        assert "4111111111111111" not in prompt

    @pytest.mark.asyncio
    async def test_masking_can_be_turned_off(self) -> None:
        """Operators who accept the risk can disable masking explicitly."""
        validator = ResultValidator(
            OpenAIConfig(api_key=SecretStr("sk-test-key")),
            ValidationConfig(enabled=True, mask_sensitive_data=False),
            SecurityConfig(),
        )
        captured: dict = {}

        async def _capture(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return _response({"confidence": 80, "explanation": "ok"})

        with patch.object(
            validator.client.chat.completions, "create", new=AsyncMock(side_effect=_capture)
        ):
            await validator.validate(
                question="q",
                sql="SELECT email FROM users;",
                results=[{"email": "a@b.com"}],
                row_count=1,
            )

        assert "a@b.com" in captured["messages"][1]["content"]
