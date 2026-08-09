"""End-to-end deterministic pipeline.

Resolution runs off the *stored raw extraction* (never re-OCRing), so Critical
Review and Finalize both replay the same deterministic steps in dependency
order:

    invoice authority -> banking -> AWB authority -> packing match
      -> HS -> COO -> [Critical Review gate]
      -> weight/carton allocation -> supplementary -> freight/insurance
      -> merged declaration -> validation -> XML

Confirming gross/packages only re-runs allocation onward — it never touches OCR
or extraction.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import Decimal

from .config import get_settings
from .declaration.builder import build_declaration
from .declaration.models import MergedDeclaration
from .declaration.validator import validate_declaration
from .domain.enums import DeclaredRole, DocumentStatus
from .extraction.common_models import (
    AirWaybillExtractionRaw,
    BankingExtractionRaw,
    InvoiceChunkRaw,
    PackingListChunkRaw,
)
from .domain.errors import ValidationMessage
from .numbers import parse_decimal, q2
from .reference.store import get_reference
from .review.critical_review import (
    WEIGHT_UNIT_TO_KG,
    CriticalReview,
    ItemDetailRow,
    ReviewedValues,
    build_review,
    normalize_weight_unit,
)
from .review.item_mutations import (
    apply_item_mutations,
    apply_shipment_override,
    overlay_of,
)
from .review.packing_view import build_packing_view
from .rules import (
    airwaybill_authority,
    banking as banking_rules,
    brand_model_size as bms_rules,
    coo as coo_rules,
    field_allocation as field_alloc_rules,
    freight as freight_rules,
    hs_resolver,
    invoice_authority,
    packing_match,
    supplementary_unit,
    weight_carton,
)
from .rules.models import BankingResolution, InvoiceAuthorityResult, ShipmentAuthority, WorkItem


@dataclass
class ResolvedContext:
    inv: InvoiceAuthorityResult
    ship: ShipmentAuthority
    banking: BankingResolution
    items: list[WorkItem]                     # HS + COO resolved, order preserved
    packing_evidence: dict
    invoice_freight: Decimal | None
    awb_freight: Decimal | None
    banking_freight: Decimal | None
    exchange_rate: Decimal
    awb_freight_detail: str | None = None     # provenance shown to the reviewer
    # Printed currency of each non-invoice freight candidate ("" = none printed,
    # assumed to be the invoice currency).  Freight is declared in the invoice
    # currency, so a candidate in another one may never be auto-selected.
    awb_freight_currency: str = ""
    banking_freight_currency: str = ""
    packing_present: bool = True              # a PACKING_LIST document was uploaded
    packing_partial: bool = False             # …and its extraction did not finish
    item_mutation_revision: int = 0           # reviewer add/delete overlay revision


def _group_raw(documents: list) -> tuple[dict[DeclaredRole, list], dict[DeclaredRole, list[str]]]:
    """Parse stored raw_extraction JSON back into typed payloads, by role.

    Also returns each payload's source file name, positionally aligned: the
    invoice authority needs it to name BOTH documents when one printed invoice
    was extracted twice (which otherwise doubles the declared customs value).
    """
    out: dict[DeclaredRole, list] = {r: [] for r in DeclaredRole}
    src: dict[DeclaredRole, list[str]] = {r: [] for r in DeclaredRole}
    model = {
        DeclaredRole.INVOICE: InvoiceChunkRaw,
        DeclaredRole.PACKING_LIST: PackingListChunkRaw,
        DeclaredRole.AIR_WAYBILL: AirWaybillExtractionRaw,
        DeclaredRole.BANKING: BankingExtractionRaw,
    }
    for doc in sorted(documents, key=lambda d: (d.declared_role, d.upload_index_within_role)):
        role = DeclaredRole(doc.declared_role)
        # Belt and braces behind services.declarable_documents: a document the
        # reviewer rejected keeps its stored extraction, and this loop's only
        # other condition is "has a raw_extraction" — which is exactly how a
        # role-mismatched document used to be read as if it were declared.
        if getattr(doc, "status", "") == DocumentStatus.ROLE_REJECTED.value:
            continue
        if role in model and doc.raw_extraction:
            out[role].append(model[role].model_validate(doc.raw_extraction))
            src[role].append(getattr(doc, "original_file_name", None)
                             or f"{role.value} #{getattr(doc, 'upload_index_within_role', 0)}")
    return out, src


def _money(box) -> Decimal | None:
    """Amount of a RawMoney charge box, or None when the box was blank."""
    return parse_decimal(box.amount_raw) if box and box.amount_raw else None


def _money_currency(form) -> str:
    """The charge currency of an air waybill form: the AWB prints ONE currency
    for every charge box, so the first one any box carries is it."""
    for box in (form.total_prepaid, form.total_collect, form.freight_amount,
                form.weight_charge, form.other_charges_total,
                form.valuation_charge, form.tax_charge):
        cur = (getattr(box, "currency_raw", None) or "").strip().upper() if box else ""
        if cur:
            return cur
    return ""


def awb_freight_candidate(awb_payloads: list, authority_gross: Decimal | None,
                          warnings_out: list) -> tuple[Decimal | None, str | None, str]:
    """This consignment's air-waybill freight candidate, its provenance and its
    CURRENCY.

    Per waybill form: take the whole printed charge (Total Prepaid / Collect,
    never the bare weight charge — user rule 2026-07-21) and scale it to the
    shipment authority's gross-weight share (user rule 2026-07-17).

    The currency is returned, not merely printed into ``detail``: a waybill is
    billed in whatever money the carrier chose, and selection must be able to
    tell whether this amount is comparable with the invoice's.
    """
    amount = detail = None
    currency = ""
    for p in awb_payloads:
        for f in p.forms:
            total, charge_detail, charge_warnings = freight_rules.awb_charge_total(
                total_prepaid=_money(f.total_prepaid),
                total_collect=_money(f.total_collect),
                weight_charge=_money(f.weight_charge),
                valuation_charge=_money(f.valuation_charge),
                tax_charge=_money(f.tax_charge),
                other_charges=_money(f.other_charges_total),
                printed_freight=_money(f.freight_amount))
            if total is None:
                continue
            warnings_out.extend(charge_warnings)
            form_gross = parse_decimal(f.gross_weight.value_raw) if f.gross_weight else None
            amount, prorate_note = freight_rules.prorate_to_authority(
                total, form_gross, authority_gross)
            currency = _money_currency(f)
            detail = " ".join(x for x in (
                f"{f.document_kind_raw or 'AWB'} {charge_detail} {currency}".rstrip(),
                prorate_note) if x)
    return amount, detail, currency


def _reset_notice_warning(overlay: dict) -> ValidationMessage | None:
    """The reviewer-visible trace of an evidence-change overlay reset.

    Shown only while the overlay revision still equals the one the notice was
    written at — the reviewer's next mutation retires it.  Without this the
    reset reads as silent data loss: edits made against the old evidence would
    simply be gone from the recomputed review.
    """
    notice = overlay.get("reset_notice") or None
    if not notice or int(notice.get("revision") or -1) != int(overlay.get("revision") or 0):
        return None
    d = notice.get("discarded") or {}
    parts = []
    if "coo_all" in d:
        parts.append(f"the bulk COO ({d['coo_all']})")
    if "field_edits" in d:
        parts.append(f"{d['field_edits']} item field edit(s)")
    if "tombstones" in d:
        parts.append(f"{d['tombstones']} deleted-row mark(s)")
    if "manual_items" in d:
        parts.append(f"{d['manual_items']} manually added row(s)")
    if "hs_selections" in d:
        folded = int(notice.get("hs_folded") or 0)
        parts.append(f"{d['hs_selections']} HS selection(s)"
                     + (f" ({folded} re-proposed from item-name history)" if folded else ""))
    if "bms_edits" in d:
        parts.append(f"{d['bms_edits']} brand/model/size edit(s)")
    if "shipment_override" in d:
        parts.append("the reviewer shipment-totals correction")
    return ValidationMessage.warning(
        "REVIEW_STATE_RESET",
        f"{notice.get('cause') or 'The evidence'} — reviewer entries made against the "
        f"previous evidence were set aside ({', '.join(parts) or 'reviewer edits'}) and "
        f"this review is recomputed fresh from the current documents. Re-check Critical "
        f"and Detailed Review. Customs office/regime selections are kept.")


def resolve_context(documents: list, exchange_rate: Decimal | None = None,
                    item_mutations: dict | None = None,
                    hs_history: dict | None = None) -> ResolvedContext:
    settings = get_settings()
    ref = get_reference()
    # `is not None`, not `or`: Decimal("0") is falsy, so `or` silently replaced
    # a caller's stated rate with the default instead of using or rejecting it.
    # Paired with services._validated_rate_override, which refuses <= 0 at the
    # boundary, nothing unusable reaches here.
    rate = exchange_rate if exchange_rate is not None else settings.default_exchange_rate
    raw, raw_sources = _group_raw(documents)

    inv = invoice_authority.finalize_invoices(raw[DeclaredRole.INVOICE],
                                              raw_sources[DeclaredRole.INVOICE], ref=ref)
    banking = banking_rules.resolve_banking(
        raw[DeclaredRole.BANKING][0] if raw[DeclaredRole.BANKING] else None, ref)
    ship = airwaybill_authority.resolve_shipment_authority(
        raw[DeclaredRole.AIR_WAYBILL], raw[DeclaredRole.PACKING_LIST],
        invoice_numbers={r.number for r in inv.invoice_refs})
    # Reviewer-corrected shipment totals are the FINAL authority (user rule
    # 2026-07-18): every consumer — review summary, item-details allocation
    # preview, finalize, XML — reconciles to them from here on.
    apply_shipment_override(ship, item_mutations)

    # Reviewer add/delete overlay — applied BEFORE packing match / HS / COO so
    # every downstream engine sees the mutated, re-sequenced item list.
    inv.warnings.extend(apply_item_mutations(inv, item_mutations))
    notice_msg = _reset_notice_warning(overlay_of(item_mutations))
    if notice_msg:
        inv.warnings.append(notice_msg)
    items = inv.items
    # Packing extraction over the time budget is not trusted for per-item
    # allocation (user rule 2026-07-17): its evidence is dropped and the
    # quantity-proportional fallback runs; the reviewer sees a warning.
    def _packing_flag(marker: str) -> bool:
        return any(marker in str(w)
                   for d in documents
                   if getattr(d, "declared_role", "") == DeclaredRole.PACKING_LIST.value
                   for w in (getattr(d, "warnings", None) or []))

    packing_overbudget = _packing_flag("PACKING_EXTRACTION_OVER_BUDGET")
    # PARTIAL is a THIRD state, not a milder over-budget: the rows that were
    # extracted are real evidence and are used, while everything else falls
    # back to the quantity share.  Collapsing it into either "present" or
    # "absent" is what makes it dangerous — see weight_carton's fallback shape.
    packing_partial = _packing_flag("PACKING_EXTRACTION_PARTIAL")
    packing_evidence = ({} if packing_overbudget
                        else packing_match.match_packing(
                            items, raw[DeclaredRole.PACKING_LIST],
                            warnings_out=inv.warnings))
    if packing_partial and getattr(ship, "selected_authority_type", "") == "PACKING_LIST":
        ship.warnings.append(ValidationMessage.warning(
            "PACKING_AUTHORITY_PARTIAL",
            "The shipment gross weight and package count come from a packing list whose "
            "extraction did not finish — the figure read as the document total may be an "
            "interior subtotal. Confirm the shipment totals in Critical Review."))
    # Content-keyed HS history (user rule 2026-08-02): normalized item name ->
    # previously reviewer-confirmed code, folded from selections superseded by
    # an evidence change.  Re-proposed at HISTORY confidence for the reviewer
    # to re-confirm — never re-applied blindly as final.
    hist = {k: (v.get("code") if isinstance(v, dict) else str(v))
            for k, v in (hs_history or {}).items()}
    items = hs_resolver.resolve_hs_all(items, ref, history={k: c for k, c in hist.items() if c})
    # reviewer HS selections (Detailed Review search / low-confidence confirm)
    # override the cascade — DB-gated again at apply time, keyed by item_id
    inv.warnings.extend(hs_resolver.apply_hs_reviews(
        items, overlay_of(item_mutations).get("hs_selections") or {}, ref))
    items = coo_rules.resolve_coo_all(items, inv.exporter_country_raw, ref,
                                      exporter_name=inv.exporter_name)
    # Deterministic per-item BRAND / MODEL / SIZE for the post-XML .xls export.
    # Runs last (after mutations + COO) so a reviewer-edited description is
    # reflected in the parsed SIZE; never touches any XML value.  Reviewer
    # overrides are applied FIRST so they win over the deterministic values.
    bms_rules.apply_edits(items, overlay_of(item_mutations).get("bms_edits") or {})
    bms_rules.resolve_all(items, inv.exporter_name)
    # Field-allocation gates: source-agnostic per-item checks (barcode-only
    # MODEL, annotation text left in the declared description) — warnings for
    # the reviewer, after every resolver including reviewer edits.
    field_alloc_rules.audit_items(items)

    # freight source candidates (foreign currency)
    inv_freight = None
    for chunk in raw[DeclaredRole.INVOICE]:
        if chunk.totals and chunk.totals.freight_raw:
            inv_freight = parse_decimal(chunk.totals.freight_raw)
        else:
            # multi-invoice attachment: each bundled invoice may bill its own
            # freight line — the shipment-level candidate is their sum
            sub_freights = [parse_decimal(s.totals.freight_raw)
                            for s in (getattr(chunk, "sub_invoices", None) or [])
                            if s.totals and s.totals.freight_raw]
            sub_freights = [f for f in sub_freights if f is not None]
            if sub_freights:
                inv_freight = sum(sub_freights, Decimal("0"))
    awb_freight, awb_freight_detail, awb_freight_currency = awb_freight_candidate(
        raw[DeclaredRole.AIR_WAYBILL], ship.gross_weight, inv.warnings)
    bank_freight = None
    bank_freight_currency = ""
    if raw[DeclaredRole.BANKING]:
        mentions = [m for m in raw[DeclaredRole.BANKING][0].freight_mentions if m.amount_raw]
        vals = [(parse_decimal(m.amount_raw), (m.currency_raw or "").strip().upper())
                for m in mentions]
        vals = [(v, c) for v, c in vals if v is not None]
        if vals:
            bank_freight = sum((v for v, _ in vals), Decimal("0"))
            # A SWIFT message settles in one currency; if the mentions somehow
            # disagree, treat the sum as un-denominated rather than claiming a
            # currency it does not have.
            seen = {c for _, c in vals if c}
            bank_freight_currency = seen.pop() if len(seen) == 1 else ""

    return ResolvedContext(
        inv=inv, ship=ship, banking=banking, items=items, packing_evidence=packing_evidence,
        invoice_freight=inv_freight, awb_freight=awb_freight, banking_freight=bank_freight,
        exchange_rate=rate, awb_freight_detail=awb_freight_detail,
        awb_freight_currency=awb_freight_currency,
        banking_freight_currency=bank_freight_currency,
        packing_present=bool(raw[DeclaredRole.PACKING_LIST]) and not packing_overbudget,
        packing_partial=packing_partial and not packing_overbudget,
        item_mutation_revision=int(overlay_of(item_mutations).get("revision") or 0),
    )


def freight_candidates(ctx: ResolvedContext) -> list[dict]:
    """Every freight amount the documents support, for the reviewer to choose
    from (user rule 2026-07-17): invoiced freight, SWIFT/banking freight, and
    the HAWB-weight-proportional share of a consolidated master AWB's total.
    The XML default is the HIGHEST candidate; the reviewer may pick any or
    enter a manual amount."""
    declared = (ctx.inv.currency or "").strip().upper()

    def _entry(source: str, amount: Decimal, currency: str, detail: str) -> dict:
        cur = (currency or "").strip().upper() or declared
        return {"source": source, "amount": f"{q2(amount):.2f}", "currency": cur,
                # a chip in another currency is a value the reviewer must
                # convert, not one they can click into the declaration
                "comparable": cur == declared, "detail": detail}

    out = []
    if ctx.invoice_freight is not None:
        out.append(_entry("INVOICE", ctx.invoice_freight, declared,
                          "freight printed on the invoice totals"))
    if ctx.banking_freight is not None:
        out.append(_entry("BANKING_SWIFT", ctx.banking_freight, ctx.banking_freight_currency,
                          "freight mentioned in the bank/SWIFT document"))
    if ctx.awb_freight is not None:
        out.append(_entry("AWB", ctx.awb_freight, ctx.awb_freight_currency,
                          ctx.awb_freight_detail or "air-waybill freight"))
    return out


def _trim_num(value: Decimal | None, places: int = 4) -> str:
    """Compact display number: fixed places, trailing zeros trimmed."""
    if value is None:
        return ""
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def _grouped_item_messages(before: list[int], items: list) -> list[ValidationMessage]:
    """One message per distinct code for the item-scope messages the preview
    just produced, naming every affected SN.

    ``before[i]`` is how many warnings item ``i`` carried before the preview
    ran, so pre-existing item messages (HS, COO, …) are not re-reported here.
    Grouping keeps a 200-row invoice from emitting 200 identical lines while
    still naming the rows the reviewer has to look at.
    """
    grouped: dict[str, tuple[ValidationMessage, list[int]]] = {}
    for i, it in enumerate(items):
        for msg in it.warnings[before[i]:]:
            entry = grouped.setdefault(msg.code, (msg, []))
            entry[1].append(it.xml_item_sequence)
    out = []
    for first, sns in grouped.values():
        if len(sns) == 1:
            out.append(first)                       # keep the item anchor
            continue
        shown = ", ".join(str(s) for s in sns[:20]) + (" …" if len(sns) > 20 else "")
        out.append(first.model_copy(update={
            "scope": "JOB", "item_sequence": None,
            "message": f"{len(sns)} item(s) — SN {shown}. {first.message}"}))
    return out


def item_details_preview(ctx: ResolvedContext,
                         warnings_out: list | None = None) -> list[ItemDetailRow]:
    """Item rows for the Detailed Review table.

    Allocation + supplementary units are previewed on deep COPIES of the
    resolved items using the review-default authority totals, so ``ctx`` is
    never mutated — finalize re-runs the real allocation with the
    reviewer-confirmed values.  With no gross authority the weight columns
    stay blank instead of showing a meaningless basis-as-kg split.

    EVERY message the preview produces is appended to ``warnings_out``, both
    shipment- and item-scope.  This used to be an allow-list of three pin
    codes, which meant a blank cell had no stated reason: a misread
    description made the apportionment infeasible, the blocking
    GROSS_ALLOCATION_IMPOSSIBLE that said so was filtered out, and the
    reviewer was left looking at empty Gross / Net / Sup_U / Sup_qty columns
    with five unrelated warnings above them.  A blank cell must always come
    with the reason it is blank.
    """
    items = copy.deepcopy(ctx.items)
    unit = normalize_weight_unit(ctx.ship.gross_weight_unit) or "KGM"
    factor = WEIGHT_UNIT_TO_KG.get(unit, Decimal("1"))
    gross_kg = (ctx.ship.gross_weight or Decimal("0")) * factor
    before = [len(it.warnings) for it in items]
    if gross_kg > 0:
        msgs = weight_carton.allocate_weights_and_cartons(
            items, ctx.packing_evidence, gross_kg, ctx.ship.packages or Decimal("0"),
            packing_present=ctx.packing_present, packing_partial=ctx.packing_partial)
        if warnings_out is not None:
            warnings_out.extend(msgs)
    supplementary_unit.resolve_supplementary_all(items)
    if warnings_out is not None:
        warnings_out.extend(_grouped_item_messages(before, items))
    return [ItemDetailRow(
        sn=it.xml_item_sequence,
        item_id=it.item_id or "",
        origin=it.item_origin,
        edited=it.fields_edited,
        hs_source=it.hs_source or "",
        hs_confidence=(f"{it.hs_confidence:.2f}" if it.hs_confidence is not None else ""),
        hs_low_confidence=bool(it.final_hs_code_11
                               and (it.hs_confidence or 0) < 1.0),
        hs_explicit=bool(it.hs_selection_explicit),
        description=it.description_raw,
        coo=it.coo_alpha2 or (it.country_of_origin_raw or "").strip(),
        invoice_hs=(it.hs_code_raw or "").strip(),
        final_hs=it.final_hs_code_11 or "",
        quantity=_trim_num(it.quantity),
        uom=(it.invoice_uom_raw or "").strip().upper(),
        total_price=f"{it.line_total:.2f}",
        brand=it.brand or "NA",
        model=it.model or "NA",
        size=it.size or "NA",
        gross=_trim_num(it.gross_weight_kg),
        net=_trim_num(it.net_weight_kg),
        gross_pinned=bool(it.manual_gross_weight_kg and it.manual_gross_weight_kg > 0),
        net_pinned=bool(it.manual_net_weight_kg and it.manual_net_weight_kg > 0),
        ctn=_trim_num(it.package_count, 2),
        ctn_pinned=bool(it.manual_package_count and it.manual_package_count > 0),
        sup_unit=it.supplementary_unit_code or "",
        sup_name=it.supplementary_unit_name or "",
        sup_qty=_trim_num(it.supplementary_quantity),
        sup_qty_pinned=bool(it.manual_supplementary_quantity
                            and it.manual_supplementary_quantity > 0),
        src_file=it.source_document_file or "",
        src_page=it.source_page_no,
    ) for it in items]


def to_critical_review(ctx: ResolvedContext, documents: list | None = None,
                       review_selections: dict | None = None) -> CriticalReview:
    # The review shows the deterministic freight selection as the editable
    # default; a reviewed amount then has highest authority at finalize.
    preview = freight_rules.select_freight(
        ctx.invoice_freight, ctx.awb_freight, ctx.banking_freight, None, ctx.inv.currency,
        awb_currency=ctx.awb_freight_currency,
        banking_currency=ctx.banking_freight_currency)
    freight_default = None if preview.source == "MISSING_ZERO" else preview.effective_freight_foreign

    # derived packing-authority view (spec 2026-07-17 section 2)
    packing_payloads = [PackingListChunkRaw.model_validate(d.raw_extraction)
                        for d in documents or []
                        if getattr(d, "declared_role", "") == DeclaredRole.PACKING_LIST.value
                        and getattr(d, "raw_extraction", None)]
    view = build_packing_view(
        packing_payloads, ctx.ship, ctx.items, documents,
        over_budget=bool(packing_payloads) and not ctx.packing_present,
        partial=ctx.packing_partial,
        packing_evidence=ctx.packing_evidence,
        settings=get_settings())

    preview_warnings: list = []
    details = item_details_preview(ctx, warnings_out=preview_warnings)
    return build_review(
        ctx.inv, ctx.ship, ctx.banking, get_reference(),
        settings=get_settings(),
        freight_default=freight_default,
        freight_source=preview.source,
        freight_candidates=freight_candidates(ctx),
        documents=documents,
        packing_view=view,
        item_details=details,
        item_mutation_revision=ctx.item_mutation_revision,
        extra_warnings=preview_warnings,
        review_selections=(review_selections or {}).get("values"),
        regime_revision=int((review_selections or {}).get("revision") or 0),
    )


def finalize(
    ctx: ResolvedContext,
    reviewed: ReviewedValues,
    *,
    hs_overrides: dict | None = None,
) -> MergedDeclaration:
    # Manual HS entries from the review UI (DB-gated) must land before the
    # supplementary-unit step, which derives from the official tariff unit.
    manual_hs_warnings = hs_resolver.apply_manual_hs(ctx.items, hs_overrides or {}, get_reference())

    # allocation (does NOT re-run OCR/extraction)
    alloc_msgs = weight_carton.allocate_weights_and_cartons(
        ctx.items, ctx.packing_evidence, reviewed.gross_weight_kg, reviewed.total_packages,
        packing_present=ctx.packing_present, packing_partial=ctx.packing_partial)
    supplementary_unit.resolve_supplementary_all(ctx.items)

    # The reviewed freight amount is the canonical value (explicit 0.00 wins too).
    freight = freight_rules.select_freight(
        ctx.invoice_freight, ctx.awb_freight, ctx.banking_freight,
        reviewed.freight_amount, ctx.inv.currency,
        awb_currency=ctx.awb_freight_currency,
        banking_currency=ctx.banking_freight_currency)
    freight_rules.allocate_cost(ctx.items, freight.effective_freight_foreign, "item_external_freight")
    freight_rules.allocate_cost(ctx.items, reviewed.insurance_amount, "item_insurance")

    decl = build_declaration(
        job_id="",
        inv=ctx.inv, ship=ctx.ship, banking=ctx.banking, items=ctx.items,
        freight=freight, reviewed=reviewed, exchange_rate=ctx.exchange_rate,
    )
    # collect engine-level warnings; allocation-spec impossibilities are
    # blocking ("stop and raise a validation error")
    from .domain.enums import Severity
    decl.warnings.extend(ctx.inv.warnings + ctx.ship.warnings + ctx.banking.warnings
                         + freight.warnings + manual_hs_warnings
                         + [m for m in alloc_msgs if m.severity != Severity.BLOCKING])
    decl.allocation_audit = [it.allocation_audit for it in ctx.items if it.allocation_audit]
    decl = validate_declaration(decl)
    for m in alloc_msgs:
        if m.severity == Severity.BLOCKING:
            decl.blocking_errors.append(m)
            decl.ready_for_xml = False

    # Weight-unit gate: unresolved source unit needs the reviewer's explicit
    # shipment-authority confirmation (spec: manual_shipment_authority_confirmed).
    if reviewed.weight_unit_missing and not reviewed.manual_shipment_authority_confirmed:
        decl.blocking_errors.append(ValidationMessage.blocking(
            "WEIGHT_UNIT_UNCONFIRMED",
            "The shipment authority's weight unit could not be resolved from evidence; confirm the "
            "reviewed unit and tick the manual shipment-authority confirmation."))
        decl.ready_for_xml = False

    # Documents-optional gates (user rule 2026-07-17): with no transport /
    # packing authority the reviewer MUST supply the shipment totals and a
    # transport-document reference before XML.
    if not reviewed.gross_weight_kg or reviewed.gross_weight_kg <= 0:
        decl.blocking_errors.append(ValidationMessage.blocking(
            "SHIPMENT_GROSS_REQUIRED",
            "No gross-weight authority (no usable AWB / packing list). Enter the total gross "
            "weight in Critical Review."))
        decl.ready_for_xml = False
    if not reviewed.total_packages or reviewed.total_packages <= 0:
        decl.blocking_errors.append(ValidationMessage.blocking(
            "SHIPMENT_PACKAGES_REQUIRED",
            "No package-count authority (no usable AWB / packing list). Enter the total cartons "
            "in Critical Review."))
        decl.ready_for_xml = False
    if not (reviewed.mawb_no or reviewed.hawb_no or reviewed.bill_of_lading_no):
        decl.blocking_errors.append(ValidationMessage.blocking(
            "TRANSPORT_REFERENCE_REQUIRED",
            "No transport document reference. Enter the MAWB, HAWB or Bill of Lading number "
            "in Critical Review."))
        decl.ready_for_xml = False

    # Field 40 is stamped on EVERY item, so the reviewer must explicitly
    # choose/confirm which AWB number it carries (user rule 2026-07-18: the
    # system suggests, the user decides — their choice is final).
    if ((reviewed.hawb_no or reviewed.mawb_no or reviewed.bill_of_lading_no)
            and not reviewed.field_40_confirmed):
        which = "HAWB/MAWB/B/L" if reviewed.bill_of_lading_no else "HAWB/MAWB"
        decl.blocking_errors.append(ValidationMessage.blocking(
            "FIELD_40_UNCONFIRMED",
            f"Field 40 (Summary_declaration on every item) is set to "
            f"{reviewed.field_40 or '(empty)'} — confirm the {which} choice in "
            "Critical Review before the declaration is final."))
        decl.ready_for_xml = False

    # An HS code the machine guessed from the item description is not a
    # resolution — it is a proposal at 0.3 confidence.  Because the cascade
    # always finalizes SOMETHING, and validate_declaration only checks that the
    # code is 11 digits, such a guess was indistinguishable from an
    # invoice-printed exact match by the time it reached the XML.  HS sets the
    # duty rate, so it must be confirmed by a human, exactly like an unresolved
    # HS.  Blocking, but deliberately NOT in WARN_MODE_HARD_CODES: warn mode
    # still lets the reviewer take the file to ASYCUDA to test it.
    unconfirmed = [it for it in ctx.items
                   if it.hs_source == "SEMANTIC_DESCRIPTION" and not it.hs_selection_explicit]
    for it in unconfirmed:
        decl.blocking_errors.append(ValidationMessage.blocking(
            "HS_GUESS_UNCONFIRMED",
            f"Item {it.xml_item_sequence} ({it.description_raw[:40]!r}): HS "
            f"{it.final_hs_code_11} was auto-selected by matching the description against the "
            f"official database — the invoice printed no HS code. Confirm or correct it in "
            f"Detailed Review before this declaration is final.",
            scope="ITEM", item_sequence=it.xml_item_sequence, field="hs_code"))
    if unconfirmed:
        decl.ready_for_xml = False

    # Reviewer-added rows may be saved incomplete, but XML never ships them
    # half-filled (MANUAL_ITEM_INCOMPLETE is a warning at review, a blocker here).
    for it in ctx.items:
        if it.item_origin != "manual":
            continue
        missing = [name for name, bad in (
            ("description", not (it.description_raw or "").strip()),
            ("quantity", not it.quantity or it.quantity <= 0),
        ) if bad]
        if missing:
            decl.blocking_errors.append(ValidationMessage.blocking(
                "MANUAL_ITEM_INCOMPLETE",
                f"Item {it.xml_item_sequence}: manually added row is missing "
                f"{', '.join(missing)} — complete it before XML.",
                scope="ITEM", item_sequence=it.xml_item_sequence))
            decl.ready_for_xml = False
    return decl
