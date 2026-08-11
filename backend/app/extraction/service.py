"""Extraction service — role-specific, provider-agnostic.

Dispatches to the configured provider and always runs the deterministic
validator afterwards:

* ``openai``   — direct OpenAI structured output (default).
* ``langroid`` — the report's ChatAgent + forced ToolMessage design.
* ``offline``  — pypdf fixtures / heuristics (no keys).

A supplied ``fixture`` (e.g. the bundled demo) short-circuits the LLM entirely.
If a live provider is selected but its key/library is unavailable, we fall back
to the offline extractor with a warning so the app keeps working.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import get_settings
from ..domain.enums import DeclaredRole
from ..numbers import detect_numeric_locale
from ..ocr.base import OcrDocument
from .common_models import ROLE_TO_MODEL
from .document_refs import backfill_document_refs
from .offline_extractor import OfflineExtractor
from .validator import (
    validate_airwaybill,
    validate_banking,
    validate_invoice,
    validate_packing,
)

log = logging.getLogger("easycustoms.extraction")

_VALIDATORS = {
    DeclaredRole.INVOICE: validate_invoice,
    DeclaredRole.PACKING_LIST: validate_packing,
    DeclaredRole.AIR_WAYBILL: validate_airwaybill,
    DeclaredRole.BANKING: validate_banking,
}


@dataclass
class ExtractionResult:
    role: DeclaredRole
    payload: object          # one of the *Raw pydantic models
    provider: str
    role_match: bool
    review_required: bool
    warnings: list[str]
    errors: list[str]
    # Vendor token accounting for THIS extraction, or None when no paid
    # call was made (fixture/offline). Carried out rather than logged and
    # dropped so services can attribute the spend to the job's owner.
    usage: dict | None = None

    def payload_dict(self) -> dict:
        return self.payload.model_dump(mode="json")


def _run_provider(role: DeclaredRole, ocr: OcrDocument, fixture: dict | None,
                  deadline: float | None = None) -> tuple[object, list[str], str, dict | None]:
    settings = get_settings()

    # Fixture (demo / replay) never calls an LLM.
    if fixture is not None:
        payload, warnings = OfflineExtractor().extract(role, ocr, fixture)
        return payload, warnings, "fixture", None

    provider = settings.extraction_provider
    if provider == "openai":
        if settings.resolved_openai_key():
            from .openai_extractor import OpenAIExtractor

            from .openai_extractor import ExtractionDeadlineExceeded

            extractor = OpenAIExtractor()
            try:
                payload, warnings = extractor.extract(role, ocr, None, deadline=deadline)
            except ExtractionDeadlineExceeded as aborted:
                # The windows that completed before the budget ran out were
                # paid for. Dropping their tokens here would make an aborted
                # packing list — the most expensive thing this app does —
                # cost nothing on the report.
                aborted.usage = extractor.usage()
                raise
            return payload, warnings, "openai", extractor.usage()
        log.warning("extraction_provider 'openai' selected but no key — falling back to offline.")

    elif provider == "langroid":
        from .langroid_agents import langroid_available

        if settings.resolved_openai_key() and langroid_available():
            from .langroid_agents import run_langroid_extraction

            payload, warnings = run_langroid_extraction(role, ocr.page_text_map(), ocr.full_text())
            return payload, warnings, "langroid", None
        log.warning("extraction_provider 'langroid' unavailable (key/library) — falling back to offline.")

    payload, warnings = OfflineExtractor().extract(role, ocr, None)
    return payload, warnings, "offline", None


def packing_budget_marker(budget: float) -> str:
    """The over-budget marker the pipeline detects (substring
    PACKING_EXTRACTION_OVER_BUDGET) to switch to the quantity-share fallback."""
    return (f"PACKING_EXTRACTION_OVER_BUDGET: extraction aborted after exceeding the "
            f"{budget:.0f}s budget — weight/carton allocation will use the fallback "
            f"(gross split by item quantity share, cartons by weight share, totals "
            f"reconciled exactly).")


def extract_document(role: DeclaredRole, ocr: OcrDocument, fixture: dict | None = None,
                     deadline: float | None = None) -> ExtractionResult:
    from .common_models import RoleValidation
    from .openai_extractor import ExtractionDeadlineExceeded

    try:
        payload, warnings, provider, usage = _run_provider(role, ocr, fixture, deadline)
    except ExtractionDeadlineExceeded as aborted:
        # Aborted at the time budget (packing list): return an empty payload
        # flagged over-budget instead of running the document to completion and
        # discarding it. The pipeline drops packing evidence and uses the
        # quantity-share allocation fallback; a manual retry can reuse the
        # already-persisted OCR envelope.
        model_cls = ROLE_TO_MODEL[role]
        payload = model_cls(role_validation=RoleValidation(
            expected_role=role, matches_expected_role=True))
        budget = get_settings().packing_extraction_budget_seconds
        return ExtractionResult(
            role=role, payload=payload, provider="openai (aborted at budget)",
            role_match=True, review_required=True,
            warnings=[packing_budget_marker(budget)], errors=[],
            usage=getattr(aborted, "usage", None))

    # Deterministic per-page numeric-locale hints (EU/US) — stamped by code,
    # never by the LLM; downstream parsing uses them to resolve "1.600"-style
    # separator ambiguity.
    if hasattr(payload, "page_numeric_locales"):
        payload.page_numeric_locales = {
            page_no: loc for page_no, text in ocr.page_text_map().items()
            if (loc := detect_numeric_locale(text)) is not None
        }

    # Deterministic labelled-reference scan (user rules 2026-08-06): the
    # parties' EXIM/IEC codes on an invoice, and the bill-of-lading number/date
    # of a sea or land shipment.  Also stamped by code, and only ever into a
    # field the extractor left empty — each fill is reported as a warning so
    # the reviewer can see where the value came from.
    warnings = list(warnings) + backfill_document_refs(role, payload, ocr.page_text_map())

    # The packing sums (rows vs the document's own printed totals) can only be
    # judged on the WHOLE document: a page window holds part of the rows and
    # sometimes all of the totals, so a per-window mismatch means nothing.
    if role is DeclaredRole.PACKING_LIST:
        errors = validate_packing(payload, ocr.page_text_map(), whole_document=True)
    else:
        validator = _VALIDATORS.get(role)
        errors = validator(payload, ocr.page_text_map()) if validator else []
    role_match = getattr(payload.role_validation, "matches_expected_role", True)
    review_required = bool(errors) or not role_match or bool(warnings)
    return ExtractionResult(
        role=role,
        payload=payload,
        provider=provider,
        usage=usage,
        role_match=role_match,
        review_required=review_required,
        warnings=warnings,
        errors=errors,
    )
