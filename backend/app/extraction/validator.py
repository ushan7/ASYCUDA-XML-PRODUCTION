"""Deterministic validation of raw extraction payloads.

Runs *before* a payload is trusted.  Confirms the role matches, evidence pages
are in scope, evidence quotes actually appear in the OCR, gross-weight evidence
is not mislabelled chargeable/volumetric/net, and numeric tokens parse.  On
failure the (LLM) extractor is asked to repair; the model never authorises its
own output.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from ..numbers import parse_decimal
from ..units import to_kg
from .common_models import (
    AirWaybillExtractionRaw,
    BankingExtractionRaw,
    Evidence,
    InvoiceChunkRaw,
    PackingListChunkRaw,
    RawNumber,
)
from .manifest import _GTIN, _PART
from .manifest import UOM_ALT as _UOM_ALT
from .manifest import uncovered_anchors

_GROSS_FORBIDDEN = ("chargeable", "c.w", "cw", "volumetric", "dimensional", "vol", "net")

# A markdown-table cell pair "| <qty> | <UOM> |" — or the merged single-cell
# variant "| <qty> <UOM> (size) |" some vendors print — marks a goods row on
# the page. Used for a deterministic completeness check: a page whose OCR
# prints such rows must contribute at least one extracted row (any
# classification). The UOM vocabulary is shared with the manifest and table
# parser (manifest.UOM_ALT) so all three layers recognize the same units.
_QTY_UOM_CELLS = re.compile(
    r"\|\s*\d+(?:[.,]\d+)?\s*\|\s*(?:" + _UOM_ALT + r")\s*\|"
    r"|\|\s*\d+(?:[.,]\d+)?\s+(?:" + _UOM_ALT + r")\s*(?:\([^()|]{0,24}\))?\s*\|",
    re.IGNORECASE,
)


def _pages_missing_rows(payload_rows, ocr_pages: dict[int, str]) -> list[str]:
    # Two detectors, either may fire: the raw-text qty|UOM regex, plus the
    # cell-based scan (table_parser.page_prints_goods_rows) that survives OCR
    # bold markers around cells — "| **42021900000** | PCS |" defeated the
    # plain regex and let a whole last page vanish silently (2026-07-19).
    from .table_parser import page_prints_goods_rows

    covered = {row.source_page_no for row in payload_rows}
    errors = []
    for page_no in sorted(set(ocr_pages) - covered):
        text = ocr_pages[page_no] or ""
        if _QTY_UOM_CELLS.search(text) or page_prints_goods_rows(text):
            errors.append(
                f"PAGE_ROWS_MISSING: page {page_no} prints goods-table rows (quantity|UOM cells) but "
                f"no rows were extracted from it; extract EVERY row on that page")
    return errors


def _anchor_errors(rows, ocr_pages: dict[int, str], text_fields: tuple[str, ...]) -> list[str]:
    """Row-level completeness gate: every goods-row anchor the OCR provably
    prints must be covered by an extracted row on the same page."""
    errors = []
    for a in uncovered_anchors(rows, ocr_pages, text_fields):
        errors.append(
            f"ROW_ANCHOR_MISSING: page {a.page_no} prints goods row {a.snippet!r} "
            f"(identity {a.tokens[:3]}) but no extracted row covers it; extract that row")
    return errors

# A page with one of these hints holds house-level consignment values that
# must be extracted as their own form, never merged into the master AWB.
# The HAWB token regex covers the label variants real forwarders print
# (HAWB, H.A.W.B, H/AWB, H-AWB, "H AWB"); "mawb" never matches (no leading h).
_HOUSE_PAGE_HINTS = ("delivery order", "house air waybill", "house airway bill",
                     "house air way bill", "house waybill", "house awb", "house bill")
_HAWB_TOKEN = re.compile(r"\bh\s*[./-]?\s*a\.?\s*w\.?\s*b\b")
_WEIGHT_TOKEN = re.compile(r"\b\d+(?:[.,]\d+)?\s*kgs?\b")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _evidence_in_scope(ev: Evidence, ocr_pages: dict[int, str], errors: list[str], where: str) -> None:
    if ev.page_no not in ocr_pages:
        errors.append(f"{where}: evidence page {ev.page_no} is outside document scope")
        return
    haystack = _norm(ocr_pages[ev.page_no])
    needle = _norm(ev.quote)
    if needle and needle[:40] not in haystack and needle not in haystack:
        # tolerant: require a reasonable token overlap for OCR variance
        toks = [t for t in needle.split() if len(t) > 2]
        hit = sum(1 for t in toks if t in haystack)
        if not toks or hit / len(toks) < 0.5:
            errors.append(f"{where}: evidence quote not found on page {ev.page_no}: {ev.quote[:40]!r}")


def validate_evidence(payload_evidence: list[tuple[str, Evidence]], ocr_pages: dict[int, str]) -> list[str]:
    errors: list[str] = []
    for where, ev in payload_evidence:
        _evidence_in_scope(ev, ocr_pages, errors, where)
    return errors


def _collect(evs: list[Evidence], where: str) -> list[tuple[str, Evidence]]:
    return [(where, e) for e in evs]


def validate_invoice(payload: InvoiceChunkRaw, ocr_pages: dict[int, str]) -> list[str]:
    errors: list[str] = []
    if not payload.role_validation.matches_expected_role:
        errors.append("ROLE_MISMATCH: content does not match the INVOICE upload box")
    pairs: list[tuple[str, Evidence]] = []
    for row in payload.rows:
        if row.quantity_raw and parse_decimal(row.quantity_raw) is None:
            errors.append(f"row {row.source_row_index}: quantity not numeric ({row.quantity_raw!r})")
        if row.line_total_raw and parse_decimal(row.line_total_raw) is None:
            errors.append(f"row {row.source_row_index}: line total not numeric ({row.line_total_raw!r})")
        # Provable model/line-number swap (live failure 2026-07-30): the model
        # cell's GTIN barcode landed in model_raw while the catalogue code
        # landed in line_no_raw.  Fires only on that exact, provable shape —
        # a genuine line number is digits-only and never matches _PART.
        model = (row.model_raw or "").strip()
        line_no = (row.line_no_raw or "").strip()
        if (model and _GTIN.fullmatch(model) and line_no
                and _PART.fullmatch(line_no.upper()) and not _GTIN.fullmatch(line_no)):
            errors.append(
                f"row {row.source_row_index}: model_raw {model!r} is a GTIN/EAN barcode while "
                f"line_no_raw {line_no!r} is a catalogue/part code — put the catalogue code in "
                f"model_raw (the barcode may be omitted) and the row's printed line number, if "
                f"any, in line_no_raw")
        pairs += _collect(row.evidence, f"invoice row {row.source_row_index}")
    for s in (payload.sub_invoices or []):
        pairs += _collect(s.evidence, f"sub-invoice {s.invoice_number_raw or '?'}")
    errors += validate_evidence(pairs, ocr_pages)
    errors += _pages_missing_rows(payload.rows, ocr_pages)
    errors += _anchor_errors(payload.rows, ocr_pages,
                             ("line_no_raw", "description_raw", "brand_raw", "model_raw"))
    return errors


# A packing list states its own totals.  Rows carry 2-3 dp and totals are
# rounded, so an exact match is not required — but past this, a column was read
# wrong, which is a silent declaration error nothing downstream can see.
_PACKING_SUM_TOLERANCE = Decimal("0.02")            # 2 %
# A totals line copied into the row list inflates every allocation basis.
_TOTALS_DESC = re.compile(
    r"^(?:GRAND\s+|SUB[\s-]*)?TOTALS?\b[\s:.\-]*"
    r"(?:(?:GROSS|NET{1,2}|WEIGHT|WT|CTNS?|CARTONS?|PACKAGES?|PKGS?|BOXES|QTY|QUANTITY|PCS|"
    r"PIECES|AMOUNT|VALUE)[\s:.\-]*){0,3}$", re.I)


def _packing_kg(raw, locales: dict, page_no: int | None) -> Decimal | None:
    """A packing RawNumber in kilograms, honouring its printed unit.

    Rows may print grams while the totals print kilograms; comparing the two
    without converting is how a 1000x error passes a tolerance check.
    """
    if raw is None or not raw.value_raw:
        return None
    value = parse_decimal(raw.value_raw, locale=(locales or {}).get(page_no))
    kg, recognized = to_kg(value, raw.unit_raw)
    return kg if recognized else None


def _packing_number(raw, locales: dict, page_no: int | None) -> Decimal | None:
    if raw is None or not raw.value_raw:
        return None
    return parse_decimal(raw.value_raw, locale=(locales or {}).get(page_no))


def _packing_sum_errors(payload: PackingListChunkRaw) -> list[str]:
    """Do the extracted rows add up to what the document says they add up to?

    This is the packing-list analogue of the invoice parser's `qty x price ==
    total` gate, and it is the only check that can catch the failure that
    matters most here: a net-weight column read as gross, or a carton NUMBER
    read as a carton count.  Every row looks individually plausible in both
    cases; only the document's own total disagrees.

    Whole-document only.  A page window holds part of the rows and (sometimes)
    all of the totals, so the sums are partial by construction and a mismatch
    there would mean nothing — and would cost a full repair round to "fix".
    """
    errors: list[str] = []
    locales = getattr(payload, "page_numeric_locales", None) or {}
    if not payload.rows:
        return errors
    checks = (
        ("gross weight", "gross_weight", payload.total_gross_weight, True),
        ("net weight", "net_weight", payload.total_net_weight, True),
        ("package count", "carton_count", payload.total_packages, False),
        ("quantity", None, payload.total_quantity, False),
    )
    for label, attr, printed, is_mass in checks:
        conv = _packing_kg if is_mass else _packing_number
        total = conv(printed, locales, getattr(printed, "evidence", None)
                     and printed.evidence.page_no) if printed is not None else None
        if total is None or total <= 0:
            continue
        got = Decimal("0")
        seen = 0
        for row in payload.rows:
            raw = (getattr(row, attr, None) if attr else
                   (RawNumber(value_raw=row.quantity_raw, unit_raw=None) if row.quantity_raw else None))
            val = conv(raw, locales, row.source_page_no)
            if val is not None:
                got += val
                seen += 1
        if not seen:
            continue
        if abs(got - total) > max(total * _PACKING_SUM_TOLERANCE, Decimal("0.05")):
            errors.append(
                f"PACKING_SUM_MISMATCH: the extracted rows' {label} totals {got} but the packing "
                f"list prints a total of {total} ({seen} of {len(payload.rows)} row(s) carry a "
                f"value). Re-read that column: the usual causes are a net column read as gross, a "
                f"carton NUMBER read as a carton count, or a totals line extracted as a row")
    return errors


def validate_packing(payload: PackingListChunkRaw, ocr_pages: dict[int, str],
                     *, whole_document: bool = False) -> list[str]:
    errors: list[str] = []
    if not payload.role_validation.matches_expected_role:
        errors.append("ROLE_MISMATCH: content does not match the PACKING_LIST upload box")
    pairs: list[tuple[str, Evidence]] = []
    locales = getattr(payload, "page_numeric_locales", None) or {}
    for row in payload.rows:
        pairs += _collect(row.evidence, f"packing row {row.source_row_index}")
        if _TOTALS_DESC.match((row.description_raw or "").strip()):
            errors.append(
                f"PACKING_TOTALS_ROW_EXTRACTED: row {row.source_row_index} on page "
                f"{row.source_page_no} is the document's TOTALS line "
                f"({row.description_raw.strip()!r}), not a goods row — remove it and keep those "
                f"figures in the top-level total_* fields")
        gross = _packing_kg(row.gross_weight, locales, row.source_page_no)
        net = _packing_kg(row.net_weight, locales, row.source_page_no)
        if gross is not None and net is not None and net > gross:
            errors.append(
                f"PACKING_ROW_NET_ABOVE_GROSS: row {row.source_row_index} on page "
                f"{row.source_page_no} ({row.description_raw[:40]!r}) has net {net} kg above gross "
                f"{gross} kg — the two columns are swapped or one of them is misread")
    errors += validate_evidence(pairs, ocr_pages)
    errors += _pages_missing_rows(payload.rows, ocr_pages)
    errors += _anchor_errors(payload.rows, ocr_pages, ("line_no_raw", "description_raw"))
    if whole_document:
        errors += _packing_sum_errors(payload)
    return errors


def _awb_charge_errors(form) -> list[str]:
    """Internal consistency of one waybill's charge boxes.

    `freight_amount` is the waybill's GRAND total (Total Prepaid / Collect).
    Handing back the rate line's weight charge instead silently undervalues the
    declaration — EUR 4653.00 weight charge taken while the box printed
    EUR 4708.00 Total Prepaid (real failure 2026-07-21) — so a `freight_amount`
    that contradicts the very boxes the same form reported is sent for repair.
    Purely arithmetic: no OCR layout parsing, so blank boxes never false-fire.
    """
    errors: list[str] = []
    val = {}
    for name in ("freight_amount", "total_prepaid", "total_collect", "weight_charge",
                 "valuation_charge", "tax_charge", "other_charges_total"):
        box = getattr(form, name, None)
        if not box or not box.amount_raw:
            continue
        amount = parse_decimal(box.amount_raw)
        if amount is None:
            errors.append(f"AWB_CHARGE_NOT_NUMERIC: form {form.logical_form_id} {name} "
                          f"{box.amount_raw!r} is not a number")
        else:
            val[name] = amount

    freight = val.get("freight_amount")
    if freight is None:
        return errors
    grand = val.get("total_prepaid") or val.get("total_collect")
    if grand is not None and freight != grand:
        which = "total_prepaid" if val.get("total_prepaid") else "total_collect"
        errors.append(
            f"AWB_FREIGHT_NOT_GRAND_TOTAL: form {form.logical_form_id} freight_amount {freight} "
            f"does not match its own {which} {grand}; freight_amount must be the waybill's "
            f"bottom-line Total Prepaid/Collect box, not the weight charge or rate-line total")
    elif grand is None and val.get("other_charges_total") and freight == val.get("weight_charge"):
        errors.append(
            f"AWB_FREIGHT_EXCLUDES_OTHER_CHARGES: form {form.logical_form_id} freight_amount "
            f"{freight} is only the weight charge while other charges of "
            f"{val['other_charges_total']} are also printed; report the Total Prepaid/Collect box "
            f"(weight charge + valuation + tax + other charges) in freight_amount")
    return errors


def validate_airwaybill(payload: AirWaybillExtractionRaw, ocr_pages: dict[int, str]) -> list[str]:
    errors: list[str] = []
    if not payload.role_validation.matches_expected_role:
        errors.append("ROLE_MISMATCH: content does not match the AIR_WAYBILL upload box")
    for form in payload.forms:
        gw = form.gross_weight
        if gw and gw.evidence and gw.evidence.label:
            lab = _norm(gw.evidence.label)
            if any(bad in lab for bad in _GROSS_FORBIDDEN):
                errors.append(
                    f"GROSS_WEIGHT_LABEL_INVALID: form {form.logical_form_id} gross weight "
                    f"labelled {gw.evidence.label!r}"
                )
        errors += validate_evidence(_collect(form.evidence, f"awb {form.logical_form_id}"), ocr_pages)
        errors += _awb_charge_errors(form)

    # house-level pages (delivery order / HAWB) that print their own weight must
    # be a separate form — merging them into the master silently drops the
    # consignment-level gross/pcs (the real authority values)
    for page_no, text in ocr_pages.items():
        t = _norm(text)
        house_hinted = any(h in t for h in _HOUSE_PAGE_HINTS) or _HAWB_TOKEN.search(t)
        if not house_hinted or not _WEIGHT_TOKEN.search(t):
            continue
        covered = any(
            (f.gross_weight and f.gross_weight.evidence and f.gross_weight.evidence.page_no == page_no)
            or (f.pieces_or_packages and f.pieces_or_packages.evidence
                and f.pieces_or_packages.evidence.page_no == page_no)
            for f in payload.forms)
        if not covered:
            errors.append(
                f"HOUSE_LEVEL_PAGE_NOT_EXTRACTED: page {page_no} looks like a house-level document "
                f"(delivery order / house air waybill) that prints its own pieces and weight; extract "
                f"it as its OWN separate form with that page's pcs and gross weight — do not merge it "
                f"into the master air waybill form")
    return errors


def validate_banking(payload: BankingExtractionRaw, ocr_pages: dict[int, str]) -> list[str]:
    errors: list[str] = []
    if not payload.role_validation.matches_expected_role:
        errors.append("ROLE_MISMATCH: content does not match the BANKING upload box")
    if payload.amount and payload.amount.amount_raw and parse_decimal(payload.amount.amount_raw) is None:
        errors.append(f"banking amount not numeric ({payload.amount.amount_raw!r})")
    return errors
