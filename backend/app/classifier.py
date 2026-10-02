from typing import Literal
from pydantic import BaseModel, Field, field_validator
from langchain_groq import ChatGroq
from backend.app.config import settings

class ClassificationError(Exception):
    """Raised when classification fails or produces invalid results."""
    pass

class FailureClassification(BaseModel):
    category: Literal[
        "insufficient_funds",
        "bank_declined_soft",
        "bank_declined_hard",
        "timeout",
        "user_cancelled",
        "other"
    ] = Field(description="The category of the payment failure")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")
    reason: str = Field(description="Short explanation for the classification")

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("Confidence must be between 0.0 and 1.0")
        return v

def classify_failure(
    error_code: str | None,
    error_reason: str | None,
    error_source: str | None,
    error_step: str | None
) -> FailureClassification:
    """
    Classifies a payment failure using Groq LLM based on error details.
    """
    # 5. If all four fields are empty, return category "other" with reason "no error information"
    if not any([error_code, error_reason, error_source, error_step]):
        return FailureClassification(
            category="other",
            confidence=1.0,
            reason="no error information"
        )

    llm = ChatGroq(
        model=settings.GROQ_MODEL_NAME,
        temperature=0,
        groq_api_key=settings.GROQ_API_KEY
    )

    structured_llm = llm.with_structured_output(
        FailureClassification,
        method="json_schema",
        strict=True
    )

    # Categories based on spec.md section 5 (simulated here as prompts)
    # insufficient_funds: Customer has no money
    # bank_declined_soft: Temporary bank issue, retry likely to succeed
    # bank_declined_hard: Permanent bank rejection, retry unlikely
    # timeout: Connection or processing timeout
    # user_cancelled: User stopped the payment flow
    # other: Any other failure or unsure

    prompt = (
        f"Classify this payment failure:\n"
        f"Error Code: {error_code}\n"
        f"Error Reason: {error_reason}\n"
        f"Error Source: {error_source}\n"
        f"Error Step: {error_step}\n\n"
        f"Categories:\n"
        f"- insufficient_funds: Customer has insufficient funds in account.\n"
        f"- bank_declined_soft: Temporary bank decline; likely to succeed on retry.\n"
        f"- bank_declined_hard: Permanent bank decline; unlikely to succeed on retry.\n"
        f"- timeout: Payment timed out during processing.\n"
        f"- user_cancelled: User cancelled the transaction.\n"
        f"- other: Any other failure or if unsure.\n\n"
        f"Instructions: Use only the provided information. Pick 'other' if unsure. "
        f"Do not decide timing or any action."
    )

    try:
        result = structured_llm.invoke(prompt)
        if not isinstance(result, FailureClassification):
            raise ClassificationError(f"Invalid result type: {type(result)}")

        # Pydantic validation is handled by with_structured_output + the class definition
        return result
    except Exception as e:
        if isinstance(e, ClassificationError):
            raise e
        raise ClassificationError(f"LLM classification failed: {str(e)}") from e
