"""Immutable OCR envelope + provider protocol.

OCR output is *untrusted document data*.  It is versioned, never overwritten in
place, and only the pages required for a role are handed to an extractor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from pydantic import BaseModel, Field

from ..domain.enums import DeclaredRole


class OcrBlock(BaseModel):
    block_id: str
    block_type: str = "text"
    text: str = ""
    bbox: tuple[float, float, float, float] | None = None


class OcrPage(BaseModel):
    page_no: int = Field(ge=1)
    plain_text: str = ""
    markdown: str = ""
    blocks: list[OcrBlock] = Field(default_factory=list)


class OcrDocument(BaseModel):
    document_id: str
    declared_role: DeclaredRole
    source_sha256: str = ""
    ocr_provider: str = "offline"
    ocr_model: str = "pypdf-textlayer"
    ocr_schema_version: str = "ocr-v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    pages: list[OcrPage] = Field(default_factory=list)

    def page_text_map(self) -> dict[int, str]:
        return {p.page_no: p.plain_text for p in self.pages}

    def full_text(self) -> str:
        return "\n".join(p.plain_text for p in self.pages)


class OcrProvider(Protocol):
    name: str

    def run(self, *, document_id: str, declared_role: DeclaredRole, file_path: str, sha256: str) -> OcrDocument:
        ...
