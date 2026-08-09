"""Mistral OCR provider (real integration).

Uploads the file, gets a signed URL, and calls ``client.ocr.process`` with
``mistral-ocr-latest``.  Produces the same immutable :class:`OcrDocument`
envelope as the offline provider so nothing downstream changes.

SDK: ``pip install mistralai``.  Key from ``EASYCUSTOMS_MISTRAL_API_KEY`` or the
standard ``MISTRAL_API_KEY``.
"""
from __future__ import annotations

import logging

from ..config import get_settings
from ..domain.enums import DeclaredRole
from .base import OcrBlock, OcrDocument, OcrPage

log = logging.getLogger("easycustoms.ocr")


class MistralOcrProvider:
    name = "mistral"

    def __init__(self) -> None:
        settings = get_settings()
        key = settings.resolved_mistral_key()
        if not key:
            raise RuntimeError("Mistral OCR requires EASYCUSTOMS_MISTRAL_API_KEY or MISTRAL_API_KEY")
        from mistralai import Mistral  # lazy: not installed in offline mode

        # timeout_ms bounds every HTTP call (upload / signed-url / process) —
        # without it a stalled connection hangs the whole extraction silently
        self._client = Mistral(api_key=key,
                               timeout_ms=int(settings.mistral_ocr_timeout_seconds * 1000))
        self._model = settings.mistral_ocr_model
        self._include_image_base64 = settings.mistral_ocr_include_image_base64

    def run(self, *, document_id: str, declared_role: DeclaredRole, file_path: str, sha256: str) -> OcrDocument:
        # The vendor is told the DOCUMENT ID, not the path.  file_path is this
        # server's absolute storage path, so sending it disclosed the deployment's
        # directory layout and the job UUID to a third party for no benefit — the
        # name is only a label on their side.
        with open(file_path, "rb") as fh:
            uploaded = self._client.files.upload(
                file={"file_name": f"{document_id}.pdf", "content": fh.read()},
                purpose="ocr",
            )
        try:
            signed = self._client.files.get_signed_url(file_id=uploaded.id)
            resp = self._client.ocr.process(
                model=self._model,
                document={"type": "document_url", "document_url": signed.url},
                include_image_base64=self._include_image_base64,
            )
        finally:
            # The upload PERSISTS in the vendor's file store; the signed URL is
            # only needed for the one process() call above.  Without this, every
            # invoice, packing list and BANKING document (bank reference, SWIFT
            # text, payment amounts) accumulated there forever, outliving any
            # deletion the operator performs in this app — and one leaked OCR key
            # would yield the whole historical corpus rather than just API credit.
            # Best effort in a finally: a failed cleanup must not fail an
            # extraction whose OCR has already been paid for.
            try:
                self._client.files.delete(file_id=uploaded.id)
            except Exception as e:                       # noqa: BLE001
                log.warning("could not delete document %s from the OCR vendor's file store "
                            "(%s: %s) — it remains in the Mistral account and should be "
                            "removed there", document_id, type(e).__name__, e)
        pages: list[OcrPage] = []
        for i, page in enumerate(resp.pages, start=1):
            md = getattr(page, "markdown", "") or ""
            blocks = [OcrBlock(block_id=f"p{i}b{j}", text=ln) for j, ln in enumerate(md.splitlines()) if ln.strip()]
            pages.append(OcrPage(page_no=i, plain_text=md, markdown=md, blocks=blocks))
        return OcrDocument(
            document_id=document_id,
            declared_role=declared_role,
            source_sha256=sha256,
            ocr_provider=self.name,
            ocr_model=self._model,
            pages=pages,
        )
