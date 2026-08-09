"""Offline extractor — produces the raw extraction schema with no LLM.

Two modes, both yielding the *same* evidence-backed schema the Langroid agents
would produce:

1. **Fixture** — a caller-supplied raw payload (already-structured facts, e.g.
   from a prior extraction or a demo fixture).  Validated then returned.
2. **Heuristic** — best-effort regex parse of the OCR text for the fields that
   are reliably machine-readable (SWIFT BICs, AWB numbers/weights, amounts).
   Line-item tables fall back to FIELD_REVIEW rather than being invented.

The deterministic rule layer downstream is identical regardless of which
extractor produced the raw facts.
"""
from __future__ import annotations

import re

from ..domain.enums import DeclaredRole
from ..ocr.base import OcrDocument
from .common_models import (
    AirWaybillExtractionRaw,
    AirWaybillFormRaw,
    BankingExtractionRaw,
    Evidence,
    InvoiceChunkRaw,
    PackingListChunkRaw,
    RawMoney,
    RawNumber,
    ROLE_TO_MODEL,
    RoleValidation,
)

_BIC = re.compile(r"\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b")
_AWB_AIRLINE = re.compile(r"\b(\d{3})-?(\d{8})\b")
_AMOUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})\b")


class OfflineExtractor:
    name = "offline"

    def extract(self, role: DeclaredRole, ocr: OcrDocument, fixture: dict | None) -> tuple[object, list[str]]:
        if fixture is not None:
            model = ROLE_TO_MODEL[role]
            payload = model.model_validate(fixture)
            return payload, list(getattr(payload, "warnings", []) or [])
        # heuristic fallback
        if role == DeclaredRole.BANKING:
            return self._banking(ocr)
        if role == DeclaredRole.AIR_WAYBILL:
            return self._awb(ocr)
        # invoice / packing without a fixture: needs review, do not invent rows
        model = ROLE_TO_MODEL[role]
        rv = RoleValidation(expected_role=role, matches_expected_role=True)
        warn = ["FIELD_REVIEW_REQUIRED: offline heuristic cannot table-parse this role; supply a fixture or enable Mistral+Langroid"]
        payload = model(role_validation=rv, warnings=warn)
        return payload, warn

    # ------------------------------------------------------------------ #
    def _banking(self, ocr: OcrDocument) -> tuple[BankingExtractionRaw, list[str]]:
        text = ocr.full_text()
        bics = _BIC.findall(text)
        sender = bics[0] if bics else None
        amounts = _AMOUNT.findall(text)
        amount = max(amounts, key=lambda a: float(a.replace(",", "")), default=None) if amounts else None
        rv = RoleValidation(expected_role=DeclaredRole.BANKING, matches_expected_role=True)
        payload = BankingExtractionRaw(
            role_validation=rv,
            sender_bic_raw=sender,
            bank_swift_raw=sender,
            amount=RawMoney(amount_raw=amount, currency_raw="USD") if amount else None,
            payment_terms_raw="L/C" if re.search(r"\b(LC|L/C|LETTER OF CREDIT|DOCUMENTARY CREDIT|700)\b", text, re.I) else None,
        )
        return payload, ["heuristic banking extraction — verify before finalising"]

    def _awb(self, ocr: OcrDocument) -> tuple[AirWaybillExtractionRaw, list[str]]:
        text = ocr.full_text()
        m = _AWB_AIRLINE.search(text)
        rv = RoleValidation(expected_role=DeclaredRole.AIR_WAYBILL, matches_expected_role=True)
        form = AirWaybillFormRaw(
            logical_form_id="form-1",
            source_pages=[p.page_no for p in ocr.pages],
            primary_awb_number_raw=f"{m.group(1)}-{m.group(2)}" if m else None,
        )
        payload = AirWaybillExtractionRaw(role_validation=rv, forms=[form])
        return payload, ["heuristic AWB extraction — verify weights/pieces before finalising"]
