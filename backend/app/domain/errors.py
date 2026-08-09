"""Domain error types and the machine-readable validation message model."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .enums import Severity


class ValidationMessage(BaseModel):
    code: str
    severity: Severity
    scope: Literal["JOB", "DOCUMENT", "INVOICE", "ITEM", "XML_FIELD"] = "JOB"
    document_id: str | None = None
    item_sequence: int | None = None
    field: str | None = None
    message: str
    remediation: str | None = None

    @classmethod
    def blocking(cls, code: str, message: str, **kw) -> "ValidationMessage":
        return cls(code=code, severity=Severity.BLOCKING, message=message, **kw)

    @classmethod
    def warning(cls, code: str, message: str, **kw) -> "ValidationMessage":
        return cls(code=code, severity=Severity.WARNING, message=message, **kw)


class BlockingValidationError(Exception):
    """Raised when a deterministic rule cannot produce a legal value."""

    def __init__(self, code: str, message: str = "", **kw):
        self.message = ValidationMessage.blocking(code, message or code, **kw)
        super().__init__(f"{code}: {message}")


class ExtractionFailure(Exception):
    pass
