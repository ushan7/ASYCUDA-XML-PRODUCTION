"""Derived packing-list view (allocation spec 2026-07-17, section 2).

The packing list is the weight/package authority — never the value authority.
This module assembles the reviewer-facing JSON view of that authority from the
stored RAW packing extraction + the resolved shipment authority + the
invoice-item match.  It is a DERIVED artifact: matching, allocation rules and
validation flags are deterministic outputs, never LLM output.

Two rules this file exists to keep:

* it reports the match the ALLOCATOR made, it does not compute its own.  It
  used to re-implement matching from the same private `_norm`, which meant the
  two could disagree — and the one on screen was not the one in the XML.
* a weight is shown in kilograms only after being converted to kilograms.
  `*_kg` fields were printed straight from `value_raw`, so a row printed in
  grams was displayed as if it were kilograms, while the allocator (which does
  convert) used a value a thousand times smaller.
"""
from __future__ import annotations

from decimal import Decimal

from ..numbers import parse_decimal
from ..rules.packing_match import _norm
from ..units import to_kg

_ZERO = Decimal("0")

# printed package word -> the declaration's package-type code
_PACKAGE_CODES = {
    "CT": "CT", "CTN": "CT", "CTNS": "CT", "CARTON": "CT", "CARTONS": "CT", "CASE": "CT",
    "CASES": "CT", "BX": "BX", "BOX": "BX", "BOXES": "BX", "PALLET": "PX", "PALLETS": "PX",
    "PLT": "PX", "PKG": "PK", "PKGS": "PK", "PACKAGE": "PK", "PACKAGES": "PK", "PACK": "PK",
    "BAG": "BG", "BAGS": "BG", "DRUM": "DR", "DRUMS": "DR", "CRATE": "CR", "CRATES": "CR",
}
# printed quantity unit -> the tariff/XML unit code
_QTY_CODES = {
    "EA": "UNT", "EACH": "UNT", "PC": "UNT", "PCS": "UNT", "PIECE": "UNT", "PIECES": "UNT",
    "NO": "UNT", "NOS": "UNT", "UNIT": "UNT", "UNITS": "UNT", "UNT": "UNT", "SET": "UNT",
    "SETS": "UNT", "KG": "KGM", "KGS": "KGM", "KGM": "KGM", "L": "LTR", "LTR": "LTR",
    "LITRE": "LTR", "LITER": "LTR", "M": "MTR", "MTR": "MTR", "METER": "MTR", "METRE": "MTR",
    "M2": "MTK", "SQM": "MTK", "M3": "MTQ", "CBM": "MTQ",
}


def _code(raw: str | None, table: dict[str, str]) -> str | None:
    if not raw:
        return None
    return table.get("".join(c for c in str(raw).upper() if c.isalnum()))


def _dec(raw_number, locales=None, page=None) -> Decimal | None:
    """The plain printed number (no unit conversion) — quantities and counts."""
    if raw_number is None or not getattr(raw_number, "value_raw", None):
        return None
    loc = (locales or {}).get(page) if page else None
    return parse_decimal(raw_number.value_raw, locale=loc)


def _kg(raw_number, locales=None, page=None) -> Decimal | None:
    """A weight in KILOGRAMS, honouring its printed unit.

    A printed-but-unrecognized unit disqualifies the value here exactly as it
    does in the allocator: showing it anyway would put a number the declaration
    never used on the reviewer's screen.
    """
    value = _dec(raw_number, locales, page)
    if value is None:
        return None
    kg, recognized = to_kg(value, getattr(raw_number, "unit_raw", None))
    return kg if recognized else None


def _s(value) -> str | None:
    return None if value is None else str(value)


def _dimension(dim) -> dict | None:
    if dim is None:
        return None
    return {
        "length_cm": getattr(dim, "length_raw", None),
        "width_cm": getattr(dim, "width_raw", None),
        "height_cm": getattr(dim, "height_raw", None),
        "unit_raw": getattr(dim, "unit_raw", None),
        "volume_cbm": getattr(dim, "volume_cbm_raw", None),
        "package_count": getattr(dim, "package_count_raw", None),
    }


def _party(p) -> str | None:
    return getattr(p, "name_raw", None) if p is not None else None


def build_packing_view(packing_payloads: list, ship, items: list, documents: list | None,
                       *, over_budget: bool, settings, partial: bool = False,
                       packing_evidence: dict | None = None) -> dict | None:
    """Spec-shaped packing JSON, or None when no packing list was uploaded."""
    if not packing_payloads:
        return None
    payload = packing_payloads[0]
    locales = getattr(payload, "page_numeric_locales", None) or {}

    pages_used = sorted({r.source_page_no for p in packing_payloads for r in p.rows})
    files = [{"file_name": getattr(d, "original_file_name", ""),
              "pages_used": pages_used, "extraction_method": "ocr_llm_rules"}
             for d in documents or [] if getattr(d, "declared_role", "") == "PACKING_LIST"]

    # The ALLOCATOR's match, echoed — not a second opinion computed here.
    # PackingEvidence.matched_name is the packing-side product name the group
    # was built from, so a row belongs to whichever invoice items name it.
    evidence = packing_evidence or {}
    by_matched_name: dict[str, list] = {}
    for it in items:
        ev = evidence.get(it.xml_item_sequence)
        if ev is not None and getattr(ev, "matched_name", None):
            by_matched_name.setdefault(_norm(ev.matched_name), []).append((it, ev))
    # Fallback for callers that pass no evidence (display-only contexts): the
    # printed text the packing list was matched against.
    by_key: dict[str, list] = {}
    for it in items:
        by_key.setdefault(_norm(it.evidence_description_raw or it.description_raw), []).append(it)

    total_gross = _kg(payload.total_gross_weight)
    total_net = _kg(payload.total_net_weight)
    total_pkgs = _dec(payload.total_packages)
    total_qty = _dec(getattr(payload, "total_quantity", None))
    ship_gross = getattr(ship, "gross_weight", None)
    ship_pkgs = getattr(ship, "packages", None)

    view_items = []
    sum_qty = _ZERO
    sum_gross = _ZERO
    sum_net = _ZERO
    sum_pkgs = _ZERO
    for p in packing_payloads:
        p_locales = getattr(p, "page_numeric_locales", None) or {}
        for i, row in enumerate(p.rows, start=1):
            page = row.source_page_no
            qty = parse_decimal(row.quantity_raw, locale=p_locales.get(page)) \
                if row.quantity_raw else None
            gross = _kg(row.gross_weight, p_locales, page)
            net = _kg(row.net_weight, p_locales, page)
            pkgs = _dec(row.carton_count, p_locales, page)
            sum_qty += qty or _ZERO
            sum_gross += gross or _ZERO
            sum_net += net or _ZERO
            sum_pkgs += pkgs or _ZERO
            pairs = by_matched_name.get(_norm(row.description_raw)) or []
            if pairs:
                matched_item, matched_ev = pairs[0]
                confidence = getattr(matched_ev, "match_confidence", None)
                method = getattr(matched_ev, "match_method", None)
            else:
                fallback = by_key.get(_norm(row.description_raw)) or []
                matched_item = fallback[0] if fallback else None
                confidence = Decimal("1") if fallback else None
                method = "exact description" if fallback else None
            view_items.append({
                "line_no": i,
                "matched_invoice_line_no": (matched_item.xml_item_sequence
                                            if matched_item is not None else None),
                "match_confidence": _s(confidence),
                "match_method": method,
                "description_raw": row.description_raw,
                "brand": getattr(row, "brand_raw", None),
                "model": getattr(row, "model_raw", None),
                "size": getattr(row, "size_raw", None),
                "item_code": getattr(row, "item_code_raw", None),
                "batch_no": getattr(row, "batch_no_raw", None),
                "lot_no": getattr(row, "lot_no_raw", None),
                "serial_no": getattr(row, "serial_no_raw", None),
                "manufacture_date": getattr(row, "manufacture_date_raw", None),
                "expiry_date": getattr(row, "expiry_date_raw", None),
                "quantity": _s(qty),
                "uom_raw": row.uom_raw,
                "uom_normalized": _code(row.uom_raw, _QTY_CODES),
                "gross_weight_kg": _s(gross),
                "net_weight_kg": _s(net),
                "declared_item_weight_kg": _s(_kg(getattr(row, "declared_weight", None),
                                                  p_locales, page)),
                "weight_type_detected": getattr(row, "weight_type_raw", None),
                "package_count": _s(pkgs),
                "package_type": (_code(getattr(row, "package_type_raw", None), _PACKAGE_CODES)
                                 or settings.package_type_default),
                "carton_no": getattr(row, "carton_no_raw", None),
                "shared_carton_group": row.shared_carton_group_raw,
                "dimension": _dimension(getattr(row, "dimension", None)),
                "hs_code_raw": row.hs_code_raw,
                "coo_raw": row.country_of_origin_raw,
                "source_trace": {
                    "quantity_source": "packing_list",
                    "weight_source": ("packing_list" if (gross or net) else "allocation"),
                    "package_source": ("packing_list" if pkgs else "allocation"),
                    "batch_source": "packing_list",
                    "serial_source": "packing_list",
                    "expiry_source": "packing_list",
                },
            })

    matched = [v for v in view_items if v["matched_invoice_line_no"] is not None]

    def _close(a, b, tol) -> bool:
        return a is not None and b is not None and abs(a - b) <= tol

    def _sum_ok(total, got, tol) -> bool:
        # "no rows carry this value" is not a mismatch — it is silence.
        return True if (total is None or got == _ZERO) else abs(got - total) <= tol

    gross_tol = max((total_gross or _ZERO) * Decimal("0.02"), Decimal("0.05"))
    net_tol = max((total_net or _ZERO) * Decimal("0.02"), Decimal("0.05"))
    pkg_tol = max((total_pkgs or _ZERO) * Decimal("0.02"), Decimal("0.01"))

    blocking: list[str] = []
    notes: list[str] = []
    if over_budget:
        blocking.append("Packing extraction exceeded its time budget and produced no usable rows; "
                        "weight and carton allocation uses the quantity-share fallback.")
    if partial:
        notes.append("Packing extraction stopped at its time budget part-way through; the rows "
                     "below ARE used, and invoice items not among them take their quantity share.")
    if total_net is not None and total_gross is not None and total_net >= total_gross:
        blocking.append(f"Total net weight {total_net} is not below total gross {total_gross}.")
    if not _sum_ok(total_gross, sum_gross, gross_tol):
        notes.append(f"Row gross weights total {sum_gross} against a printed total of {total_gross}.")
    if not _sum_ok(total_net, sum_net, net_tol):
        notes.append(f"Row net weights total {sum_net} against a printed total of {total_net}.")
    if not _sum_ok(total_pkgs, sum_pkgs, pkg_tol):
        notes.append(f"Row package counts total {sum_pkgs} against a printed total of {total_pkgs}.")
    if view_items and not matched:
        notes.append("No packing row matched an invoice item by name or code.")

    return {
        "document_type": "packing_list",
        "document_role": "packing_list",
        "document_confidence": ("0.50" if over_budget else "0.80" if partial else "0.95"),
        "source_files": files,
        "packing_header": {
            "packing_list_no": payload.packing_list_number_raw,
            "packing_list_date": getattr(payload, "packing_list_date_raw", None),
            "invoice_references": list(payload.invoice_references_raw or []),
            "invoice_date": getattr(payload, "invoice_date_raw", None),
            "lc_no": getattr(payload, "lc_reference_raw", None),
            "lc_date": getattr(payload, "lc_date_raw", None),
            "exporter_name": _party(getattr(payload, "exporter", None)),
            "importer_name": _party(getattr(payload, "importer", None)),
            "country_of_final_destination": getattr(
                payload, "country_of_final_destination_raw", None),
        },
        "shipment_totals": {
            "total_packages": _s(total_pkgs),
            "package_type_raw": getattr(payload.total_packages, "unit_raw", None)
            if payload.total_packages else None,
            "package_type_normalized": (
                _code(getattr(payload.total_packages, "unit_raw", None), _PACKAGE_CODES)
                if payload.total_packages else None) or settings.package_type_default,
            "total_quantity": _s(total_qty),
            "quantity_uom_raw": getattr(getattr(payload, "total_quantity", None), "unit_raw", None),
            "quantity_uom_normalized": _code(
                getattr(getattr(payload, "total_quantity", None), "unit_raw", None), _QTY_CODES),
            "gross_weight_kg": _s(total_gross),
            "net_weight_kg": _s(total_net),
            "volume_cbm": _s(_dec(getattr(payload, "total_volume", None))),
            "dimensions": [_dimension(d) for d in (getattr(payload, "dimensions", None) or [])],
            "weight_source_priority": getattr(ship, "selected_authority_type", "UNKNOWN"),
            "package_source_priority": getattr(ship, "selected_authority_type", "UNKNOWN"),
        },
        "transport_cross_reference": {
            "mawb_no": getattr(ship, "mawb_number", None) or None,
            "hawb_no": getattr(ship, "hawb_number", None) or None,
            "awb_gross_weight_kg": _s(ship_gross),
            "awb_package_count": _s(ship_pkgs),
            "match_with_packing_list": _close(total_gross, ship_gross, Decimal("0.5")),
        },
        "items": view_items,
        "allocation_rules_applied": {
            "weight_authority": "authority ladder (HAWB > DO > tracking > AWB > packing totals)",
            "item_weight_method": ("quantity_proportional_fallback (extraction over budget)"
                                   if over_budget else
                                   "packing_item_weight_where_extracted_else_quantity_proportional"
                                   if partial else
                                   "packing_item_weight_if_available_else_carton_or_value_proportional"),
            "item_package_method": "packing_item_carton_if_available_else_weight_proportional",
            "net_weight_method": (f"reviewer_pin > invoice_weight > description_units > "
                                  f"packing_net > {settings.default_net_to_gross_ratio} x gross"),
            "duplicate_item_handling": "sum_duplicate_packing_rows_before_matching_invoice",
            "shared_carton_handling": "divide_shared_carton_total_without_duplication",
            "match_method": "exact_name > product_code > scored_description_similarity",
            "minimum_package_per_item": "0.01",
            "preserve_invoice_order": True,
        },
        "validation": {
            "extraction_over_budget": over_budget,
            "extraction_partial": partial,
            "rows_total": len(view_items),
            "rows_matched_to_invoice": len(matched),
            "total_quantity": str(sum_qty),
            "gross_weight_matches_awb": _close(total_gross, ship_gross, Decimal("0.5")),
            "net_weight_less_than_gross": (
                total_gross is None or total_net is None or total_net < total_gross),
            "package_count_matches_awb": _close(total_pkgs, ship_pkgs, Decimal("0.01")),
            "sum_item_gross_equals_total_gross": _sum_ok(total_gross, sum_gross, gross_tol),
            "sum_item_net_equals_total_net": _sum_ok(total_net, sum_net, net_tol),
            "sum_item_packages_equals_total_packages": _sum_ok(total_pkgs, sum_pkgs, pkg_tol),
            "ready_for_invoice_merge": not blocking,
            "blocking_errors": blocking,
            "warnings": notes,
        },
    }
