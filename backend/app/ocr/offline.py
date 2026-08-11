"""Offline OCR provider — pypdf text-layer extraction, no API keys.

Good enough for text PDFs (the common case for commercial invoices, packing
lists, AWBs and SWIFT prints).  Scanned/image PDFs yield empty text here and
should be routed to the Mistral provider in production.
"""
from __future__ import annotations

import io

from ..domain.enums import DeclaredRole
from .base import OcrBlock, OcrDocument, OcrPage


class OfflineOcrProvider:
    name = "offline"

    def run(self, *, document_id: str, declared_role: DeclaredRole, data: bytes,
            sha256: str) -> OcrDocument:
        pages: list[OcrPage] = []
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            for i, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                blocks = [
                    OcrBlock(block_id=f"p{i}b{j}", text=line)
                    for j, line in enumerate(text.splitlines())
                    if line.strip()
                ]
                pages.append(OcrPage(page_no=i, plain_text=text, markdown=text, blocks=blocks))
        except Exception:
            # Non-PDF or unreadable — leave a single empty page.
            pages = [OcrPage(page_no=1, plain_text="")]

        return OcrDocument(
            document_id=document_id,
            declared_role=declared_role,
            source_sha256=sha256,
            ocr_provider=self.name,
            ocr_model="pypdf-textlayer",
            pages=pages,
        )
