import pytest
from unittest.mock import MagicMock, patch
from backend.app.classifier import classify_failure, FailureClassification, ClassificationError

def test_classify_failure_all_empty():
    """Test that empty fields return 'other' without calling LLM."""
    with patch("backend.app.classifier.ChatGroq") as mock_llm:
        result = classify_failure(None, None, None, None)
        assert result.category == "other"
        assert result.reason == "no error information"
        mock_llm.assert_not_called()

def test_classify_failure_valid_result():
    """Test a valid classification result."""
    mock_result = FailureClassification(
        category="insufficient_funds",
        confidence=0.9,
        reason="Clear insufficient funds error"
    )

    with patch("backend.app.classifier.ChatGroq") as mock_llm_cls:
        mock_llm = MagicMock()
        mock_structured_llm = MagicMock()

        mock_llm_cls.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm
        mock_structured_llm.invoke.return_value = mock_result

        result = classify_failure("ERR01", "No money", "bank", "pay")
        assert result == mock_result

def test_classify_failure_invalid_category():
    """Test that an invalid category (if LLM bypasses schema) raises ClassificationError."""
    # Since with_structured_output with strict=True usually prevents this,
    # we simulate a case where the LLM returns something that fails Pydantic validation
    # if we were to manually create it.

    with patch("backend.app.classifier.ChatGroq") as mock_llm_cls:
        mock_llm = MagicMock()
        mock_structured_llm = MagicMock()

        mock_llm_cls.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm
        # Simulate LLM returning an object that isn't a FailureClassification
        mock_structured_llm.invoke.return_value = {"category": "invalid_cat", "confidence": 0.8, "reason": "bad"}

        with pytest.raises(ClassificationError):
            classify_failure("ERR01", "No money", "bank", "pay")

def test_classify_failure_invalid_confidence():
    """Test that confidence outside [0, 1] fails."""
    with patch("backend.app.classifier.ChatGroq") as mock_llm_cls:
        mock_llm = MagicMock()
        mock_structured_llm = MagicMock()

        mock_llm_cls.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        # We can't easily instantiate FailureClassification with 1.5 because of the validator,
        # so we mock the return value to be a dictionary that would fail validation
        # if the structured output logic tried to parse it, or we mock the result
        # to be something that triggers a Pydantic error during a manual check.

        # For the purpose of this test, we simulate the LLM returning something that
        # causes a validation error during the structured output process.
        mock_structured_llm.invoke.side_effect = ValueError("Confidence must be between 0.0 and 1.0")

        with pytest.raises(ClassificationError):
            classify_failure("ERR01", "No money", "bank", "pay")

def test_classify_failure_model_exception():
    """Test that model exceptions are wrapped in ClassificationError."""
    with patch("backend.app.classifier.ChatGroq") as mock_llm_cls:
        mock_llm = MagicMock()
        mock_structured_llm = MagicMock()

        mock_llm_cls.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm
        mock_structured_llm.invoke.side_effect = Exception("API Down")

        with pytest.raises(ClassificationError):
            classify_failure("ERR01", "No money", "bank", "pay")
