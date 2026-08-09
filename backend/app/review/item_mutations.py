"""Reviewer item mutations — add / delete declaration rows without touching evidence.

The extraction evidence (uploads, OCR, raw_extraction) is immutable.  Reviewer
add/delete operations live in a job-level JSON *overlay* (``Job.item_mutations``)
that is re-applied deterministically on every recompute:

* every item carries an immutable, server-controlled ``item_id`` —
  source rows:  ``src:<sha1 of extraction lineage>``  (stable because the
  evidence never changes), manual rows: ``man:<uuid4>`` stored in the overlay;
* deleting a source row writes a *tombstone* (the evidence itself is kept);
  deleting a manual row keeps its record with ``active=false`` — a deleted
  identity is never reactivated, replacements get a new UUID;
* ``ordered_item_ids`` is the authoritative display order — an exact,
  duplicate-free permutation of the active item ids;
* ``revision`` increments on every structural mutation.

Applying the overlay is pure and network-free: filter tombstoned rows,
materialize active manual rows, order, re-sequence 1..N.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from ..domain.errors import ValidationMessage
from ..numbers import q2, q4
from ..rules.models import InvoiceAuthorityResult, WorkItem

SCHEMA_VERSION = 1
_SOURCE_PREFIX = "src:"
_MANUAL_PREFIX = "man:"


class ItemMutationError(Exception):
    """Actionable rejection of an add/delete request (maps to 404/409/422)."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def empty_overlay() -> dict:
    return {"schema": SCHEMA_VERSION, "revision": 0, "ordered_item_ids": [],
            "manual_items": [], "tombstones": [], "hs_selections": {},
            "shipment_override": None, "field_edits": {}, "coo_all": None,
            "bms_edits": {}, "reset_notice": None}


def overlay_of(raw: dict | None) -> dict:
    """Normalize a stored overlay (None / partial dicts from older rows)."""
    base = empty_overlay()
    if raw:
        base.update({k: raw[k] for k in base if k in raw})
    return base


def has_mutations(overlay: dict | None) -> bool:
    # field_edits / coo_all count as structural: they change resolved item
    # values and must run through the same rewrite + recompute path as add/delete
    return bool(overlay and (overlay.get("manual_items") or overlay.get("tombstones")
                             or overlay.get("field_edits") or overlay.get("coo_all")))


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def assign_source_item_ids(items: list[WorkItem]) -> None:
    """Deterministic ids from extraction lineage.  An ``occurrence`` counter
    disambiguates byte-identical lineage keys, and enumeration order is stable
    because the stored extraction is immutable."""
    seen: dict[str, int] = {}
    for it in items:
        if it.item_id:
            continue
        basis = f"{it.source_invoice_number}|{it.source_invoice_date}|{it.source_invoice_item_index}"
        occ = seen.get(basis, 0)
        seen[basis] = occ + 1
        digest = hashlib.sha1(f"{basis}|{occ}".encode()).hexdigest()[:16]
        it.item_id = f"{_SOURCE_PREFIX}{digest}"
        it.item_origin = "source"


def new_manual_item_id(existing_ids: set[str]) -> str:
    while True:
        candidate = f"{_MANUAL_PREFIX}{uuid.uuid4()}"
        if candidate not in existing_ids:
            return candidate


# --------------------------------------------------------------------------- #
# Apply (recompute path — pure, network-free)
# --------------------------------------------------------------------------- #
def _seed_decimal(value, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(value if value not in (None, "") else default))
    except InvalidOperation:
        return Decimal(default)
    return d if d.is_finite() else Decimal(default)


def _materialize(rec: dict, fallback_currency: str) -> WorkItem:
    seed = rec.get("seed") or {}
    invoice = rec.get("invoice") or {}
    qty = _seed_decimal(seed.get("quantity"))
    total = q2(_seed_decimal(seed.get("total_price")))
    unit_price = q2(total / qty) if qty > 0 else Decimal("0")
    return WorkItem(
        xml_item_sequence=0,                      # re-sequenced after ordering
        source_invoice_number=invoice.get("invoice_no") or "",
        source_invoice_date=invoice.get("invoice_date") or "",
        source_invoice_item_index=-1,
        source_invoice_item_no=rec.get("source_line") or f"manual:{rec.get('item_id')}",
        description_raw=(seed.get("description") or "").strip(),
        quantity=qty,
        # blank stays blank — same contract as an extracted row (ITEM_UOM_MISSING)
        invoice_uom_raw=(seed.get("uom") or "").strip().upper(),
        unit_price=unit_price,
        line_total=total,
        currency=invoice.get("currency") or fallback_currency,
        hs_code_raw=(seed.get("final_hs_code") or "").strip() or None,
        country_of_origin_raw=(seed.get("country_of_origin") or "").strip() or None,
        item_id=rec.get("item_id"),
        item_origin="manual",
    )


def apply_item_mutations(inv: InvoiceAuthorityResult, raw_overlay: dict | None
                         ) -> list[ValidationMessage]:
    """Rewrite ``inv.items`` through the overlay.  Always assigns source ids;
    with no mutations the list is untouched.  Read-path is lenient (a review
    must stay computable); the strict invariants run at mutation time."""
    assign_source_item_ids(inv.items)
    overlay = overlay_of(raw_overlay)
    if not has_mutations(overlay):
        return []

    dead = {t.get("item_id") for t in overlay.get("tombstones") or []}
    items = [it for it in inv.items if it.item_id not in dead]
    for rec in overlay.get("manual_items") or []:
        if rec.get("active"):
            items.append(_materialize(rec, inv.currency))

    # Bulk "apply COO to all items" (user rule 2026-07-18): a single reviewer
    # COO stamped on every row; a subsequent per-item COO edit (field_edits
    # below) overrides it for that one row.  The stored evidence is untouched.
    coo_all = (overlay.get("coo_all") or "").strip()
    if coo_all:
        for it in items:
            it.country_of_origin_raw = coo_all

    # Reviewer field edits (description / COO / qty / UOM / total price /
    # pinned gross+net weight) — applied by immutable item_id BEFORE HS/COO
    # resolution and allocation so every derived value recomputes from the
    # edited content.  The stored extraction evidence itself is never touched.
    edits = overlay.get("field_edits") or {}
    if edits:
        by_id = {it.item_id: it for it in items}
        for item_id, fields in edits.items():
            it = by_id.get(item_id)
            if it is None:
                continue                          # deleted row — edit inert
            f = fields or {}
            if f.get("description") is not None:
                if it.evidence_description_raw is None:
                    it.evidence_description_raw = it.description_raw
                it.description_raw = str(f["description"])
            if f.get("country_of_origin") is not None:
                it.country_of_origin_raw = str(f["country_of_origin"]) or None
            if f.get("uom") is not None:
                it.invoice_uom_raw = str(f["uom"])
            if f.get("quantity") is not None:
                it.quantity = _seed_decimal(f["quantity"])
            if f.get("total_price") is not None:
                it.line_total = q2(_seed_decimal(f["total_price"]))
            if f.get("gross_weight") is not None:
                it.manual_gross_weight_kg = _seed_decimal(f["gross_weight"])
            if f.get("net_weight") is not None:
                it.manual_net_weight_kg = _seed_decimal(f["net_weight"])
            if f.get("carton_count") is not None:
                it.manual_package_count = _seed_decimal(f["carton_count"])
            if f.get("supplementary_quantity") is not None:
                it.manual_supplementary_quantity = _seed_decimal(f["supplementary_quantity"])
            if it.quantity and it.quantity > 0:
                it.unit_price = q2(it.line_total / it.quantity)
            it.fields_edited = True

    order = {iid: pos for pos, iid in enumerate(overlay.get("ordered_item_ids") or [])}
    items.sort(key=lambda it: (order.get(it.item_id, len(order)), it.xml_item_sequence))
    for seq, it in enumerate(items, start=1):
        it.xml_item_sequence = seq
    inv.items = items

    # Structural mutations stale the derived totals — recompute them here so
    # every consumer (review summary, declaration, fingerprint) agrees.
    inv.goods_total = q2(sum((it.line_total for it in items), Decimal("0")))
    counts: dict[str, int] = {}
    for it in items:
        counts[it.source_invoice_number] = counts.get(it.source_invoice_number, 0) + 1
    for ref in inv.invoice_refs:
        ref.item_count = counts.get(ref.number, 0)

    warnings: list[ValidationMessage] = []
    for it in items:
        if it.item_origin != "manual":
            continue
        missing = [name for name, bad in (
            ("description", not it.description_raw),
            ("quantity", it.quantity <= 0),
        ) if bad]
        if missing:
            warnings.append(ValidationMessage.warning(
                "MANUAL_ITEM_INCOMPLETE",
                f"Item {it.xml_item_sequence}: manually added row is missing "
                f"{', '.join(missing)} — required before XML generation.",
                scope="ITEM", item_sequence=it.xml_item_sequence))
    return warnings


# --------------------------------------------------------------------------- #
# Invariants (mutation path — strict)
# --------------------------------------------------------------------------- #
def validate_overlay_invariants(overlay: dict, active_ids: list[str]) -> None:
    """Active ids unique; ordered_item_ids an exact duplicate-free permutation."""
    if len(set(active_ids)) != len(active_ids):
        raise ItemMutationError(409, "ITEM_ID_DUPLICATE",
                                "Active item ids are not unique — mutation refused.")
    ordered = overlay.get("ordered_item_ids") or []
    if len(set(ordered)) != len(ordered) or set(ordered) != set(active_ids):
        raise ItemMutationError(
            409, "ITEM_ORDER_INVALID",
            "ordered_item_ids is not an exact duplicate-free permutation of the "
            "active items — mutation refused.")


# --------------------------------------------------------------------------- #
# Mutation operations (called under the job lock; persistence is the caller's)
# --------------------------------------------------------------------------- #
def _find_invoice_binding(invoice_id: str, inv: InvoiceAuthorityResult,
                          documents: list) -> dict:
    """Bind to the exact authoritative invoice; retain lineage metadata."""
    ref = next((r for r in inv.invoice_refs if r.number == invoice_id), None)
    if ref is None:
        known = ", ".join(r.number for r in inv.invoice_refs) or "none"
        raise ItemMutationError(
            422, "INVOICE_UNKNOWN",
            f"invoice_id {invoice_id!r} is not an authoritative invoice of this job "
            f"(known: {known}).")
    doc_id, file_name = "", ""
    for d in documents or []:
        if getattr(d, "declared_role", "") != "INVOICE" or not getattr(d, "raw_extraction", None):
            continue
        raw = d.raw_extraction or {}
        numbers = {((raw.get("header") or {}).get("invoice_number_raw") or "").strip()}
        numbers |= {(s.get("invoice_number_raw") or "").strip()
                    for s in raw.get("sub_invoices") or []}
        if ref.number in numbers:
            doc_id, file_name = d.id, d.original_file_name
            break
    return {"invoice_id": ref.number, "invoice_no": ref.number,
            "invoice_date": ref.date, "currency": ref.currency or inv.currency,
            "source_document_id": doc_id, "source_file": file_name,
            "association": "reviewer-selected authoritative invoice"}


def add_item(raw_overlay: dict | None, current_items: list[WorkItem],
             inv: InvoiceAuthorityResult, documents: list, *,
             insertion_sn: int, invoice_id: str, manual_review_addition: bool,
             seed: dict, reason: str = "") -> tuple[dict, dict]:
    """Return (new overlay, event payload).  Raises ItemMutationError."""
    count = len(current_items)
    if not 1 <= insertion_sn <= count + 1:
        raise ItemMutationError(
            422, "INSERTION_SN_OUT_OF_RANGE",
            f"insertion_sn must be between 1 and {count + 1} (current item count {count}).")
    invoice = _find_invoice_binding(invoice_id.strip(), inv, documents) if invoice_id.strip() else None
    if invoice is None and not manual_review_addition:
        raise ItemMutationError(
            422, "MANUAL_CONFIRMATION_REQUIRED",
            "Select an authoritative invoice or explicitly confirm a reviewed manual "
            "addition without source invoice.")

    overlay = overlay_of(raw_overlay)
    before = [it.item_id for it in current_items]
    item_id = new_manual_item_id(set(before))
    overlay["manual_items"] = list(overlay["manual_items"]) + [{
        "item_id": item_id,
        "item_origin": "manual",
        "line_type": "goods",
        "manual_review_addition": True,
        "active": True,
        "created_at": _now_iso(),
        "deleted_at": None,
        "invoice": invoice,
        "source_line": f"manual:{item_id}",
        "seed": {
            "description": str(seed.get("description") or "").strip()[:400],
            "quantity": str(_seed_decimal(seed.get("quantity"))),
            "uom": (str(seed.get("uom") or "PCS").strip().upper() or "PCS")[:20],
            "total_price": str(q2(_seed_decimal(seed.get("total_price")))),
            "country_of_origin": str(seed.get("country_of_origin") or "").strip()[:80],
            "final_hs_code": str(seed.get("final_hs_code") or "").strip()[:20],
        },
    }]
    after = list(overlay["ordered_item_ids"] or before)
    after.insert(insertion_sn - 1, item_id)
    overlay["ordered_item_ids"] = after
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    validate_overlay_invariants(overlay, before + [item_id])

    event = {"event_id": str(uuid.uuid4()), "item_id": item_id, "at": _now_iso(),
             "old_sn": None, "new_sn": insertion_sn, "reason": reason or "",
             "revision": overlay["revision"], "invoice": invoice,
             "before_ordering": before, "after_ordering": after}
    return overlay, event


def delete_item(raw_overlay: dict | None, current_items: list[WorkItem], *,
                item_id: str, confirmation_sn: str, reason: str = "") -> tuple[dict, dict]:
    """Return (new overlay, event payload).  Raises ItemMutationError."""
    target = next((it for it in current_items if it.item_id == item_id), None)
    if target is None:
        raise ItemMutationError(404, "ITEM_NOT_FOUND",
                                f"No active item with id {item_id!r} (already deleted, "
                                "or the id is unknown).")
    current_sn = target.xml_item_sequence
    if str(confirmation_sn).strip() != str(current_sn):
        raise ItemMutationError(
            409, "CONFIRMATION_SN_MISMATCH",
            f"Item {item_id} is currently SN {current_sn}, not "
            f"{str(confirmation_sn).strip()!r} — the row order changed; re-confirm "
            "against the refreshed review.")
    if len(current_items) <= 1:
        raise ItemMutationError(
            409, "LAST_ITEM_UNDELETABLE",
            "At least one declarable goods item must remain — the last row cannot "
            "be deleted.")

    overlay = overlay_of(raw_overlay)
    now = _now_iso()
    if item_id.startswith(_MANUAL_PREFIX):
        # keep the record; a deleted identity is never reactivated
        overlay["manual_items"] = [
            ({**rec, "active": False, "deleted_at": now} if rec.get("item_id") == item_id else rec)
            for rec in overlay["manual_items"]]
    else:
        overlay["tombstones"] = list(overlay["tombstones"]) + [{
            "item_id": item_id,
            "origin": "source",
            "invoice_no": target.source_invoice_number,
            "invoice_date": target.source_invoice_date,
            "invoice_line_index": target.source_invoice_item_index,
            "invoice_line_no": target.source_invoice_item_no,
            "description": target.description_raw,
            "previous_sn": current_sn,
            "deleted_at": now,
        }]

    before = [it.item_id for it in current_items]
    after = [iid for iid in (overlay["ordered_item_ids"] or before) if iid != item_id]
    overlay["ordered_item_ids"] = after
    # a removed item's identity leaves every reviewer-state map with it
    overlay["hs_selections"] = {k: v for k, v in (overlay.get("hs_selections") or {}).items()
                                if k != item_id}
    overlay["field_edits"] = {k: v for k, v in (overlay.get("field_edits") or {}).items()
                              if k != item_id}
    overlay["bms_edits"] = {k: v for k, v in (overlay.get("bms_edits") or {}).items()
                            if k != item_id}
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    validate_overlay_invariants(overlay, [iid for iid in before if iid != item_id])

    event = {"event_id": str(uuid.uuid4()), "item_id": item_id, "at": now,
             "old_sn": current_sn, "new_sn": None, "reason": reason or "",
             "revision": overlay["revision"],
             "before_ordering": before, "after_ordering": after}
    return overlay, event


# Reviewer-editable item fields.  HS goes through the DB-gated hs-review
# channel; the supplementary unit CODE stays server-derived (it follows the
# tariff unit of the final HS).
# gross_weight / net_weight (kg) and carton_count (CTN) PIN that item's value:
# allocation keeps a pin EXACT and redistributes the remaining authorised
# total across the unpinned items (docs/allocation-spec.md sections 5-6).
# supplementary_quantity is a plain OVERRIDE — there is no shipment authority
# above it, so nothing is redistributed and no derivation rule changes: the
# entered number simply replaces the derived one, all the way into the XML.
EDITABLE_ITEM_FIELDS = ("description", "quantity", "uom", "total_price", "country_of_origin",
                        "gross_weight", "net_weight", "carton_count",
                        "supplementary_quantity")

# The three pin channels.  An EMPTY value CLEARS the pin so the computed value
# returns — the same convention the brand/model/size channel uses.  Without it
# a pin could be changed but never taken back, which made "pin" a one-way door.
PIN_ITEM_FIELDS = ("gross_weight", "net_weight", "carton_count")

# Every field whose EMPTY value means "drop my override, give me the computed
# value back".  The supplementary override joins the pins here and ONLY here:
# it is deliberately not a member of PIN_ITEM_FIELDS, because that tuple also
# decides what a packing/airwaybill re-extraction discards, and this override
# is not reconciled against the shipment authority those documents set.
CLEARABLE_ITEM_FIELDS = PIN_ITEM_FIELDS + ("supplementary_quantity",)


def edit_item_fields(raw_overlay: dict | None, current_items: list[WorkItem], *,
                     item_id: str, fields: dict) -> tuple[dict, dict]:
    """Store reviewer edits for one item's invoice fields, keyed by immutable
    item_id.  Partial edits merge with earlier ones; validation is strict at
    write time (the read path re-derives everything downstream).
    Returns (new overlay, event payload); raises ItemMutationError."""
    target = next((it for it in current_items if it.item_id == item_id), None)
    if target is None:
        raise ItemMutationError(404, "ITEM_NOT_FOUND",
                                f"No active item with id {item_id!r}.")
    supplied = {k: v for k, v in (fields or {}).items() if v is not None}
    unknown = set(supplied) - set(EDITABLE_ITEM_FIELDS)
    if unknown:
        raise ItemMutationError(
            422, "ITEM_FIELD_NOT_EDITABLE",
            f"Field(s) {', '.join(sorted(unknown))} cannot be edited here — editable: "
            f"{', '.join(EDITABLE_ITEM_FIELDS)} (HS via the HS-search channel; the "
            "supplementary unit code follows the tariff unit of that HS).")
    if not supplied:
        raise ItemMutationError(422, "ITEM_EDIT_EMPTY", "No editable fields supplied.")

    clean: dict = {}
    if "description" in supplied:
        desc = str(supplied["description"]).strip()[:400]
        if not desc:
            raise ItemMutationError(422, "DESCRIPTION_REQUIRED",
                                    "The item description cannot be edited to empty.")
        clean["description"] = desc
    if "quantity" in supplied:
        try:
            qty = Decimal(str(supplied["quantity"]))
        except InvalidOperation:
            qty = Decimal("NaN")
        if not qty.is_finite() or qty <= 0:
            raise ItemMutationError(422, "QUANTITY_INVALID",
                                    "Quantity must be a finite number greater than zero.")
        clean["quantity"] = str(qty)
    if "total_price" in supplied:
        try:
            price = Decimal(str(supplied["total_price"]))
        except InvalidOperation:
            price = Decimal("NaN")
        if not price.is_finite() or price < 0:
            raise ItemMutationError(422, "TOTAL_PRICE_INVALID",
                                    "Total price must be a finite non-negative number.")
        clean["total_price"] = str(q2(price))
    if "uom" in supplied:
        clean["uom"] = (str(supplied["uom"]).strip().upper() or "PCS")[:20]
    if "country_of_origin" in supplied:
        clean["country_of_origin"] = str(supplied["country_of_origin"]).strip()[:80]
    for wf, code, label in (("gross_weight", "GROSS_WEIGHT_INVALID", "Gross weight"),
                            ("net_weight", "NET_WEIGHT_INVALID", "Net weight")):
        if wf in supplied:
            if str(supplied[wf]).strip() == "":
                clean[wf] = ""                    # CLEAR the pin (see PIN_ITEM_FIELDS)
                continue
            try:
                wv = Decimal(str(supplied[wf]))
            except InvalidOperation:
                wv = Decimal("NaN")
            if not wv.is_finite() or wv <= 0:
                raise ItemMutationError(
                    422, code, f"{label} must be a finite number greater than zero (kg).")
            clean[wf] = str(q4(wv))
    if "carton_count" in supplied:
        # The carton MUST rule (user rule 2026-08-03, spec section 7): every
        # declared CTN is at or above 0.01 AND an exact multiple of 0.01.  A
        # value off that lattice is REJECTED rather than quietly quantized —
        # rounding 3.333 to 3.33 and then reconciling the whole shipment around
        # the difference produces a number the reviewer never typed and cannot
        # account for.
        if str(supplied["carton_count"]).strip() == "":
            clean["carton_count"] = ""            # CLEAR the pin
        else:
            try:
                cv = Decimal(str(supplied["carton_count"]))
            except InvalidOperation:
                cv = Decimal("NaN")
            if not cv.is_finite() or cv < Decimal("0.01"):
                raise ItemMutationError(
                    422, "CARTON_COUNT_INVALID",
                    "Carton count must be a finite number of at least 0.01 CTN — every "
                    "declared item carries at least one hundredth of a carton.")
            if cv % Decimal("0.01") != 0:
                raise ItemMutationError(
                    422, "CARTON_COUNT_OFF_LATTICE",
                    f"Carton count {cv} is not a whole multiple of 0.01 CTN. Carton counts are "
                    "carried at two decimal places, so enter one (e.g. 3, 3.5 or 3.33).")
            clean["carton_count"] = str(q2(cv))
    if "supplementary_quantity" in supplied:
        # A plain override of the derived quantity: kept at four decimals (the
        # finest place the derivation itself uses, KGM's 0.0001) so no unit's
        # precision is lost and no unit's formula is second-guessed here.
        if str(supplied["supplementary_quantity"]).strip() == "":
            clean["supplementary_quantity"] = ""       # CLEAR the override
        else:
            try:
                sv = Decimal(str(supplied["supplementary_quantity"]))
            except InvalidOperation:
                sv = Decimal("NaN")
            if not sv.is_finite() or sv <= 0:
                raise ItemMutationError(
                    422, "SUPPLEMENTARY_QUANTITY_INVALID",
                    "Supplementary quantity must be a finite number greater than zero "
                    "— every declared item carries a positive supplementary quantity.")
            clean["supplementary_quantity"] = str(q4(sv))

    overlay = overlay_of(raw_overlay)
    previous = dict((overlay.get("field_edits") or {}).get(item_id) or {})
    merged = {**previous, **clean, "edited_at": _now_iso()}
    for pin in CLEARABLE_ITEM_FIELDS:             # an empty value un-pins the field
        if merged.get(pin) == "":
            merged.pop(pin, None)
    # pair sanity across the MERGED edit (an earlier pinned value counts):
    # every item's net must stay strictly below its gross
    mg, mn = merged.get("gross_weight"), merged.get("net_weight")
    if mg and mn and Decimal(str(mn)) >= Decimal(str(mg)):
        raise ItemMutationError(
            422, "NET_NOT_BELOW_GROSS",
            f"Net weight ({mn} kg) must be less than gross weight ({mg} kg).")
    overlay["field_edits"] = {**(overlay.get("field_edits") or {}), item_id: merged}
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    event = {"event_id": str(uuid.uuid4()), "item_id": item_id, "at": _now_iso(),
             "sn": target.xml_item_sequence, "revision": overlay["revision"],
             "changed": clean, "previous_edits": previous or None,
             "cleared": [f for f in CLEARABLE_ITEM_FIELDS if clean.get(f) == ""] or None,
             "before": {"description": target.description_raw,
                        "quantity": str(target.quantity),
                        "uom": target.invoice_uom_raw,
                        "total_price": str(target.line_total),
                        "country_of_origin": target.country_of_origin_raw or "",
                        "gross_weight": str(target.gross_weight_kg or ""),
                        "net_weight": str(target.net_weight_kg or ""),
                        "carton_count": str(target.package_count or ""),
                        "supplementary_quantity": str(target.supplementary_quantity or "")}}
    return overlay, event


# Reviewer-editable BRAND / MODEL / SIZE — the export-only columns of the
# sibling brand-model-size.xls.  They never appear in the customs XML, so they
# live in their OWN overlay channel and deliberately do NOT bump ``revision``:
# a correction here must not change the review fingerprint, stale the
# declaration, or invalidate a generated XML (user rule 2026-07-21).
BMS_EDITABLE_FIELDS = ("brand", "model", "size")
_BMS_MAX_LEN = 120
_BMS_MAX_EDITS = 5000


def edit_bms_fields(raw_overlay: dict | None, current_items: list[WorkItem], *,
                    edits: list) -> tuple[dict, dict]:
    """Store reviewer brand/model/size overrides for one or many items.

    ``edits`` is a list of ``{"item_id": ..., "brand"/"model"/"size": value}`` —
    a single-cell edit is a list of one, a spreadsheet block paste is a list of
    many.  An EMPTY value clears that override so the deterministic value
    returns.  Returns (new overlay, event payload); raises ItemMutationError.
    """
    if not isinstance(edits, list) or not edits:
        raise ItemMutationError(422, "BMS_EDIT_EMPTY", "No brand/model/size edits supplied.")
    if len(edits) > _BMS_MAX_EDITS:
        raise ItemMutationError(
            422, "BMS_EDIT_TOO_LARGE",
            f"{len(edits)} rows in one request exceeds the {_BMS_MAX_EDITS}-row limit.")
    by_id = {it.item_id: it for it in current_items}
    overlay = overlay_of(raw_overlay)
    store = {k: dict(v) for k, v in (overlay.get("bms_edits") or {}).items()}
    changed: dict[str, dict] = {}
    cleared: dict[str, list] = {}
    for entry in edits:
        if not isinstance(entry, dict):
            raise ItemMutationError(422, "BMS_EDIT_INVALID", "Each edit must be an object.")
        item_id = str(entry.get("item_id") or "")
        if item_id not in by_id:
            raise ItemMutationError(404, "ITEM_NOT_FOUND",
                                    f"No active item with id {item_id!r}.")
        supplied = {f: entry[f] for f in BMS_EDITABLE_FIELDS
                    if f in entry and entry[f] is not None}
        if not supplied:
            raise ItemMutationError(
                422, "BMS_EDIT_EMPTY",
                f"Item {item_id}: supply at least one of {', '.join(BMS_EDITABLE_FIELDS)}.")
        rec = dict(store.get(item_id) or {})
        for field, value in supplied.items():
            text = str(value).strip()[:_BMS_MAX_LEN]
            if text:
                rec[field] = text
                changed.setdefault(item_id, {})[field] = text
            else:
                rec.pop(field, None)          # cleared -> deterministic value returns
                cleared.setdefault(item_id, []).append(field)
        if rec:
            store[item_id] = rec
        else:
            store.pop(item_id, None)
    overlay["bms_edits"] = store
    # NOTE: `revision` is intentionally NOT bumped — see the comment above.
    event = {"event_id": str(uuid.uuid4()), "at": _now_iso(),
             "changed": changed, "cleared": cleared,
             "edited_items": len(set(list(changed) + list(cleared)))}
    return overlay, event


def set_all_coo(raw_overlay: dict | None, ref, *, country_of_origin: str
                ) -> tuple[dict, dict]:
    """Stamp one reviewer COO on EVERY item (user rule 2026-07-18).  The value
    must resolve to a canonical Alpha-2 via the country reference.  Any prior
    per-item COO edit is cleared so the value truly appears on all rows; a
    later per-item edit can still override one row.  Returns (overlay, event)."""
    raw = str(country_of_origin or "").strip()
    code = ref.normalize_country(raw)
    if not code:
        raise ItemMutationError(
            422, "COO_INVALID",
            f"Country of origin {raw!r} does not resolve to a canonical Alpha-2 country code.")
    overlay = overlay_of(raw_overlay)
    previous = overlay.get("coo_all")
    overlay["coo_all"] = code
    # drop stale per-item COO edits so the bulk value shows on every row
    overlay["field_edits"] = {
        iid: {k: v for k, v in (fe or {}).items() if k != "country_of_origin"}
        for iid, fe in (overlay.get("field_edits") or {}).items()}
    overlay["field_edits"] = {iid: fe for iid, fe in overlay["field_edits"].items()
                              if any(k != "edited_at" for k in fe)}
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    event = {"event_id": str(uuid.uuid4()), "at": _now_iso(),
             "country_of_origin": code, "raw_input": raw,
             "previous": previous, "revision": overlay["revision"]}
    return overlay, event


def set_shipment_totals(raw_overlay: dict | None, *, gross_weight, weight_unit: str,
                        total_packages) -> tuple[dict, dict]:
    """Reviewer-corrected shipment totals (user rule 2026-07-18): once the
    reviewer corrects gross weight / cartons, those values are the FINAL
    shipment authority — every recompute (review summary, Detailed-Review
    allocation preview, finalize, XML) reconciles item sums exactly to them.
    Returns (new overlay, event payload); raises ItemMutationError."""
    from ..review.critical_review import WEIGHT_UNIT_TO_KG

    unit = (str(weight_unit or "KGM").strip().upper() or "KGM")
    if unit not in WEIGHT_UNIT_TO_KG:
        raise ItemMutationError(
            422, "WEIGHT_UNIT_INVALID",
            f"weight_unit {weight_unit!r} is not one of {', '.join(sorted(WEIGHT_UNIT_TO_KG))}.")
    try:
        gross = Decimal(str(gross_weight))
        packages = Decimal(str(total_packages))
    except InvalidOperation:
        raise ItemMutationError(422, "SHIPMENT_TOTALS_INVALID",
                                "Gross weight and total packages must be numbers.")
    if not (gross.is_finite() and packages.is_finite() and gross > 0 and packages > 0):
        raise ItemMutationError(
            422, "SHIPMENT_TOTALS_INVALID",
            "Reviewer shipment totals must be finite numbers greater than zero.")
    # The carton MUST rule reaches the authority too: item cartons are whole
    # multiples of 0.01, so a total that is not one could never be matched by
    # their sum.  Rejecting beats quantizing silently — the reviewer would
    # otherwise reconcile against a number they did not enter.
    if packages % Decimal("0.01") != 0:
        raise ItemMutationError(
            422, "TOTAL_PACKAGES_OFF_LATTICE",
            f"Total packages {packages} is not a whole multiple of 0.01. Carton counts are "
            "carried at two decimal places, and item cartons must sum to this figure exactly.")

    overlay = overlay_of(raw_overlay)
    previous = overlay.get("shipment_override")
    overlay["shipment_override"] = {
        "gross_weight": str(gross), "weight_unit": unit,
        "total_packages": str(packages), "set_at": _now_iso(),
    }
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    event = {"event_id": str(uuid.uuid4()), "at": _now_iso(),
             "gross_weight": str(gross), "weight_unit": unit,
             "total_packages": str(packages), "previous": previous,
             "revision": overlay["revision"]}
    return overlay, event


def apply_shipment_override(ship, raw_overlay: dict | None) -> None:
    """Rewrite the resolved shipment authority with the reviewer's stored
    totals (read path — lenient; the strict validation ran at write time)."""
    override = overlay_of(raw_overlay).get("shipment_override") or None
    if not override:
        return
    from ..numbers import parse_decimal

    gross = parse_decimal(str(override.get("gross_weight") or ""))
    packages = parse_decimal(str(override.get("total_packages") or ""))
    if gross is None or packages is None or gross <= 0 or packages <= 0:
        return                                    # corrupt store — keep document authority
    from ..rules.models import ValueAuthority

    ship.gross_weight = gross
    ship.packages = packages
    ship.gross_weight_unit = override.get("weight_unit") or "KGM"
    ship.selected_authority_type = "REVIEWER_OVERRIDE"
    reviewed = ["reviewer-corrected totals are the final shipment authority"]
    ship.gross_weight_authority = ValueAuthority(
        None, "REVIEWER_OVERRIDE", gross, ship.gross_weight_unit,
        "reviewer-entered", 100, list(reviewed))
    ship.carton_authority = ValueAuthority(
        None, "REVIEWER_OVERRIDE", packages, None,
        "reviewer-entered", 100, list(reviewed))
    ship.warnings.append(ValidationMessage.warning(
        "SHIPMENT_TOTALS_REVIEWED",
        f"Shipment totals were corrected by the reviewer ({gross} "
        f"{ship.gross_weight_unit} / {packages} pkgs) and are the final authority — "
        "item weights and cartons reconcile exactly to these values."))


# --------------------------------------------------------------------------- #
# Evidence-change reset (user rule 2026-08-02)
# --------------------------------------------------------------------------- #
# Roles whose evidence feeds the item list vs the shipment authority — the
# scope of the post-extraction overlay reset.  BANKING feeds neither (the
# reviewed freight amount travels in the finalize request, never the overlay).
_ITEM_EVIDENCE_ROLES = {"INVOICE"}
_SHIPMENT_EVIDENCE_ROLES = {"AIR_WAYBILL", "PACKING_LIST"}


def reset_for_evidence_change(raw_overlay: dict | None, changed_role: str,
                              cause: str = "") -> tuple[dict | None, dict, dict]:
    """Discard reviewer overlay state derived from evidence that just changed.

    Critical/Detailed Review must recompute FRESH after any document
    (re)extraction — prior reviewer values (COO especially) are never blindly
    re-applied to evidence they were not made against (user rule 2026-08-02).
    The overlay's item channels are keyed by positional ``src:`` ids, so after
    the item list changes they can silently re-bind to different physical rows.

    Role-scoped: INVOICE evidence feeds the item list, so every item channel is
    discarded (per-item field edits incl. COO, bulk COO, tombstones, ordering,
    brand/model/size edits) and manual rows are deactivated (kept inactive for
    the audit trail — the re-extraction may now supply them).  AWB / packing
    evidence feeds the shipment authority, so the reviewer shipment-totals
    override is discarded together with the per-item WEIGHT and CARTON pins:
    those are reconciled against the authority this evidence sets, so a pin
    made against the superseded document is the same blind carryover in a
    different column.  Their siblings in ``field_edits`` (description / COO /
    quantity / price) survive — the invoice did not change.

    The two sanctioned exceptions survive: regime /
    office selections live outside this overlay entirely, and explicit HS
    selections are FOLDED into content-keyed history (normalized item name ->
    code, re-proposed at HISTORY confidence) instead of re-applied by position.

    Returns ``(new_overlay | None, hs_history_fold, discarded)`` — ``None``
    when nothing needed resetting (no revision bump, no audit noise).
    """
    role = str(changed_role or "").strip().upper()
    overlay = overlay_of(raw_overlay)
    discarded: dict = {}
    hs_fold: dict = {}
    now = _now_iso()

    if role in _SHIPMENT_EVIDENCE_ROLES:
        if overlay.get("shipment_override"):
            discarded["shipment_override"] = overlay["shipment_override"]
            overlay["shipment_override"] = None
        # Weight / carton PINS were made against the packing or airwaybill
        # evidence that just changed, and they are reconciled against the
        # authority that evidence sets — so a surviving pin is exactly the
        # blind carryover this rule exists to stop.  Only the pin channels go;
        # description / COO / quantity / price edits were made against the
        # INVOICE, which did not change, and must not be collateral damage.
        edits = overlay.get("field_edits") or {}
        kept: dict = {}
        stripped = 0
        for iid, fields in edits.items():
            survivors = {k: v for k, v in (fields or {}).items() if k not in PIN_ITEM_FIELDS}
            if len(survivors) != len(fields or {}):
                stripped += 1
            if any(k != "edited_at" for k in survivors):
                kept[iid] = survivors
        if stripped:
            discarded["weight_carton_pins"] = stripped
            overlay["field_edits"] = kept
    elif role in _ITEM_EVIDENCE_ROLES:
        if overlay.get("coo_all"):
            discarded["coo_all"] = overlay["coo_all"]
            overlay["coo_all"] = None
        if overlay.get("field_edits"):
            discarded["field_edits"] = len(overlay["field_edits"])
            overlay["field_edits"] = {}
        if overlay.get("tombstones"):
            discarded["tombstones"] = len(overlay["tombstones"])
            overlay["tombstones"] = []
        if overlay.get("bms_edits"):
            discarded["bms_edits"] = len(overlay["bms_edits"])
            overlay["bms_edits"] = {}
        if overlay.get("ordered_item_ids"):
            discarded["ordering"] = True
            overlay["ordered_item_ids"] = []
        active_manual = [r for r in overlay.get("manual_items") or [] if r.get("active")]
        if active_manual:
            discarded["manual_items"] = len(active_manual)
            overlay["manual_items"] = [
                ({**r, "active": False, "deleted_at": now,
                  "superseded_by_extraction": cause or role} if r.get("active") else r)
                for r in overlay["manual_items"]]
        selections = overlay.get("hs_selections") or {}
        if selections:
            unfoldable = 0
            for sel in selections.values():
                key = str((sel or {}).get("description_key") or "")
                code = str((sel or {}).get("final_hs_code") or "")
                if key and code and (sel or {}).get("explicit"):
                    hs_fold[key] = {"code": code,
                                    "source": str((sel or {}).get("hs_review_source") or ""),
                                    "at": now}
                else:
                    unfoldable += 1              # legacy record without a content key
            discarded["hs_selections"] = len(selections)
            if unfoldable:
                discarded["hs_selections_unfoldable"] = unfoldable
            overlay["hs_selections"] = {}
    else:
        return None, {}, {}

    if not discarded:
        return None, {}, {}
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    # The notice records the revision it was written at: the review shows it
    # only while the revision is unchanged, so the reviewer's next mutation
    # retires it without anyone having to clear it.
    overlay["reset_notice"] = {
        "revision": overlay["revision"], "role": role, "cause": cause or "", "at": now,
        "discarded": {k: (v if isinstance(v, (int, str, bool)) else True)
                      for k, v in discarded.items()},
        "hs_folded": len(hs_fold),
    }
    return overlay, hs_fold, discarded


def _validate_reviewed_hs(ref, final_hs_code: str, hs_review_source: str
                          ) -> tuple[str, str, object]:
    """Shared write-time gate for reviewed HS selections: the source must be
    allowlisted and the code must normalize to exactly 11 digits and exist
    verbatim in the supplied HS database (leading zeros preserved — no zfill,
    no prefix completion).  Returns (code, source, db record)."""
    from ..rules.hs_resolver import HS_REVIEW_SOURCES, _HS11_RE
    from ..reference.store import digits_only as _digits

    source = str(hs_review_source or "").strip()
    if source not in HS_REVIEW_SOURCES:
        raise ItemMutationError(
            422, "HS_REVIEW_SOURCE_INVALID",
            f"hs_review_source {source!r} is not allowlisted "
            f"({', '.join(sorted(HS_REVIEW_SOURCES))}).")
    code = _digits(str(final_hs_code or ""))
    if not _HS11_RE.fullmatch(code):
        raise ItemMutationError(
            422, "HS_NOT_11_DIGITS",
            f"Reviewed HS {final_hs_code!r} does not normalize to exactly 11 digits — "
            "partial HS4/HS6/HS8 codes can never become final.")
    rec = ref.hs_by_11.get(code)
    if rec is None:
        raise ItemMutationError(
            422, "HS_NOT_IN_DATABASE",
            f"HS {code} is not an exact code in the supplied official HS database — "
            "only exact database HS11 codes may become final.")
    return code, source, rec


# one printer-style token: a plain SN ("19") or a low-high range ("1-15",
# en-dash tolerated).  The full spec is comma-separated tokens.
_SN_RANGE_TOKEN_RE = re.compile(r"(\d+)(?:\s*[-–]\s*(\d+))?")


def parse_sn_ranges(spec: str, max_sn: int) -> list[int]:
    """Printer-style SN range spec — "1-15, 19, 80" means SNs 1..15, 19 and 80.
    Strict at write time: every token must be a plain SN or low-high range, no
    reversed ranges, every SN within 1..max_sn.  Returns sorted unique SNs."""
    raw = str(spec or "").strip()
    if not raw:
        raise ItemMutationError(
            422, "SN_RANGE_EMPTY",
            'Enter the item range to apply to — e.g. "1-15, 19, 80" or "all".')
    if raw.lower() == "all":
        return list(range(1, max_sn + 1))
    sns: set[int] = set()
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        m = _SN_RANGE_TOKEN_RE.fullmatch(t)
        if not m:
            raise ItemMutationError(
                422, "SN_RANGE_INVALID",
                f"Range token {t!r} is not a serial number or low-high range — "
                'use the printer style, e.g. "1-15, 19, 80".')
        lo = int(m.group(1))
        hi = int(m.group(2) or m.group(1))
        if lo > hi:
            raise ItemMutationError(
                422, "SN_RANGE_INVALID",
                f"Range {t!r} is reversed — write it low-high (e.g. {hi}-{lo}).")
        if lo < 1 or hi > max_sn:
            raise ItemMutationError(
                422, "SN_RANGE_OUT_OF_BOUNDS",
                f"Range {t!r} is outside the current item list (SN 1..{max_sn}).")
        sns.update(range(lo, hi + 1))
    if not sns:
        raise ItemMutationError(
            422, "SN_RANGE_EMPTY",
            'The range resolved to no items — e.g. "1-15, 19, 80" or "all".')
    return sorted(sns)


def select_hs(raw_overlay: dict | None, current_items: list[WorkItem], ref, *,
              item_id: str, final_hs_code: str, hs_review_source: str
              ) -> tuple[dict, dict]:
    """Store a reviewed HS selection for one item (Detailed Review HS search /
    low-confidence confirmation).  Strictly validated at write time via
    ``_validate_reviewed_hs``.  Returns (new overlay, event payload)."""
    target = next((it for it in current_items if it.item_id == item_id), None)
    if target is None:
        raise ItemMutationError(404, "ITEM_NOT_FOUND",
                                f"No active item with id {item_id!r}.")
    code, source, rec = _validate_reviewed_hs(ref, final_hs_code, hs_review_source)

    from ..rules.hs_resolver import _normalized_item_name

    overlay = overlay_of(raw_overlay)
    previous = (overlay.get("hs_selections") or {}).get(item_id)
    overlay["hs_selections"] = {**(overlay.get("hs_selections") or {}), item_id: {
        "final_hs_code": code,
        "hs_review_source": source,
        "explicit": True,
        "selected_at": _now_iso(),
        # content key, stored at decision time: lets the selection be folded
        # into name-keyed HS history when this item's evidence is superseded
        # (the positional item_id must never be re-bound to new evidence)
        "description_key": _normalized_item_name(target.description_raw),
    }}
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    event = {"event_id": str(uuid.uuid4()), "item_id": item_id, "at": _now_iso(),
             "sn": target.xml_item_sequence, "final_hs_code": code,
             "hs_review_source": source, "previous_selection": previous,
             "previous_final_hs": target.final_hs_code_11,
             "official_description": rec.description, "official_unit": rec.unit,
             "revision": overlay["revision"]}
    return overlay, event


def select_hs_range(raw_overlay: dict | None, current_items: list[WorkItem], ref, *,
                    final_hs_code: str, hs_review_source: str, sn_range: str
                    ) -> tuple[dict, dict]:
    """Bulk HS apply (Detailed Review, additional to the per-row selection):
    one DB-validated HS code stamped on every item in a printer-style SN range
    ("1-15, 19, 80" or "all").  Each targeted row gets the same explicit
    ``hs_selections`` record as a per-row pick — a later per-row selection
    still overrides that one row.  Returns (new overlay, event payload)."""
    from ..rules.hs_resolver import _normalized_item_name

    code, source, rec = _validate_reviewed_hs(ref, final_hs_code, hs_review_source)
    sns = parse_sn_ranges(sn_range, len(current_items))
    by_sn = {it.xml_item_sequence: it for it in current_items}
    targets = [by_sn[sn] for sn in sns if sn in by_sn]
    if len(targets) != len(sns):
        missing = [sn for sn in sns if sn not in by_sn]
        raise ItemMutationError(
            409, "SN_RANGE_STALE",
            f"SN(s) {', '.join(map(str, missing))} are not in the current item list — "
            "the row order changed; re-check the range against the refreshed review.")

    overlay = overlay_of(raw_overlay)
    now = _now_iso()
    selections = dict(overlay.get("hs_selections") or {})
    previous = {it.item_id: selections.get(it.item_id) for it in targets}
    for it in targets:
        selections[it.item_id] = {
            "final_hs_code": code,
            "hs_review_source": source,
            "explicit": True,
            "selected_at": now,
            "description_key": _normalized_item_name(it.description_raw),
        }
    overlay["hs_selections"] = selections
    overlay["revision"] = int(overlay.get("revision") or 0) + 1
    event = {"event_id": str(uuid.uuid4()), "at": now,
             "final_hs_code": code, "hs_review_source": source,
             "sn_range": str(sn_range).strip(), "sns": sns,
             "item_ids": [it.item_id for it in targets],
             "previous_selections": {k: v for k, v in previous.items() if v},
             "official_description": rec.description, "official_unit": rec.unit,
             "revision": overlay["revision"]}
    return overlay, event
