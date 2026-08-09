"""Invoice authority.

Selects final goods invoices, excludes proforma/charge invoices and duplicate
copies, preserves *uploaded-document order and exact item order* (ADR-001), and
computes the goods total from real item lines.  Charge rows (freight/insurance/
other) are retained separately for the valuation engines but never counted as
goods.

Multi-invoice attachments: one uploaded PDF may bundle several printed
invoices (``chunk.sub_invoices``).  The chunk is split into *virtual invoices*
— contiguous slices of the flat ``rows`` list, cut at each sub-invoice's
``first_page_no`` — so grouping can never reorder items: the global item
sequence is simply the flat row order walked once.  Each virtual invoice gets
its own InvoiceRef and its own printed-totals completeness check.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.enums import RowClassification
from ..domain.errors import BlockingValidationError, ValidationMessage
from ..numbers import parse_decimal, q2
from ..units import to_kg
from .description_clean import clean_description, is_code_only
from .field_allocation import allocate_description
from .models import InvoiceAuthorityResult, InvoiceRef, WorkItem

_NONZERO_DIGIT = re.compile(r"[1-9]")


def _looks_nonzero(raw: str | None) -> bool:
    """A printed amount that carries any non-zero digit (so a real value was
    on the page) — distinguishes a genuine 0.00 / blank from a parse miss."""
    return bool(_NONZERO_DIGIT.search(raw or ""))

_PROFORMA = ("proforma", "performa", "pro forma", "quotation")
_CHARGE_DOC = ("insurance premium", "premium invoice", "freight invoice", "debit note")


def _is_proforma(kind: str | None, title: str | None) -> bool:
    blob = f"{kind or ''} {title or ''}".lower()
    return any(k in blob for k in _PROFORMA)


def _is_charge_doc(kind: str | None, title: str | None) -> bool:
    blob = f"{kind or ''} {title or ''}".lower()
    return any(k in blob for k in _CHARGE_DOC)


@dataclass
class _Virtual:
    """One printed invoice inside a chunk: metadata + its contiguous rows."""
    number: str | None
    date: str | None
    kind: str | None
    title: str | None
    currency: str | None
    totals: object | None                 # InvoiceTotalsRaw | None
    rows: list = field(default_factory=list)


def _virtual_invoices(chunk, warnings: list[ValidationMessage]) -> list[_Virtual]:
    """Split a chunk into per-invoice groups without touching row order.

    Rows are walked once in their stored (printed) order; a row belongs to the
    last sub-invoice that starts on-or-before its page.  A chunk without
    sub_invoices is one virtual invoice built from its header/totals — the
    historical single-invoice behavior, byte for byte.
    """
    hdr = chunk.header
    subs = [s for s in (getattr(chunk, "sub_invoices", None) or []) if s.first_page_no is not None]
    subs.sort(key=lambda s: s.first_page_no)
    for s in (getattr(chunk, "sub_invoices", None) or []):
        if s.first_page_no is None:
            warnings.append(ValidationMessage.warning(
                "SUBINVOICE_UNANCHORED",
                f"Sub-invoice {s.invoice_number_raw or '?'} reports no first page; its "
                f"metadata could not be tied to any rows and was ignored."))

    if not subs:
        return [_Virtual(number=(hdr.invoice_number_raw if hdr else None),
                         date=(hdr.invoice_date_raw if hdr else None),
                         kind=(hdr.invoice_kind_raw if hdr else None),
                         title=(hdr.document_title_raw if hdr else None),
                         currency=(hdr.currency_raw if hdr else None),
                         totals=chunk.totals, rows=list(chunk.rows))]

    single = len(subs) == 1
    starts = [s.first_page_no for s in subs]
    if len(set(starts)) != len(starts):
        shared = sorted({p for p in starts if starts.count(p) > 1})
        warnings.append(ValidationMessage.warning(
            "SUBINVOICE_SHARED_PAGE",
            f"Two invoices start on the same page(s) {shared}; rows on a shared page were "
            f"assigned to the later invoice — verify the boundary rows manually."))

    buckets: list[list] = [[] for _ in subs]
    early_rows = 0
    for row in chunk.rows:                       # flat printed order, walked once
        i = bisect_right(starts, row.source_page_no) - 1
        if i < 0:
            i, early_rows = 0, early_rows + 1
        buckets[i].append(row)
    if early_rows:
        warnings.append(ValidationMessage.warning(
            "SUBINVOICE_ROWS_BEFORE_FIRST",
            f"{early_rows} row(s) print before the first sub-invoice's start page and were "
            f"assigned to it — verify they belong there."))

    out: list[_Virtual] = []
    for s, rows in zip(subs, buckets):
        out.append(_Virtual(
            number=s.invoice_number_raw or ((hdr.invoice_number_raw if hdr else None) if single else None),
            date=s.invoice_date_raw or ((hdr.invoice_date_raw if hdr else None) if single else None),
            kind=s.invoice_kind_raw,
            title=None,
            currency=s.currency_raw or (hdr.currency_raw if hdr else None),
            totals=s.totals if s.totals is not None else (chunk.totals if single else None),
            rows=rows))
    return out


def _goods_target(totals) -> Decimal | None:
    """Printed grand total net of printed charge/discount lines — what the
    goods rows alone must sum to (user rule 2026-07-17)."""
    if totals is None or not totals.grand_total_raw:
        return None
    target = parse_decimal(totals.grand_total_raw)
    if target is None:
        return None
    for charge_raw in (totals.freight_raw, totals.insurance_raw, totals.other_charges_raw):
        charge = parse_decimal(charge_raw)
        if charge:
            target -= charge
    discount = parse_decimal(totals.discount_raw)
    if discount:
        target += abs(discount)
    return target


def _norm_invoice_no(raw: str | None) -> str | None:
    """Invoice-number identity: whitespace-insensitive, case-insensitive — the
    same normalization the extractor uses to unify per-window sub-invoices, so
    `2026-209-1` and `2026-209- 1` are one invoice.  Deliberately does NOT
    strip punctuation: two genuinely distinct invoice numbers may differ only
    by a separator, and a false duplicate blocks a legitimate job."""
    n = re.sub(r"\s+", "", raw or "").upper()
    return n or None


def finalize_invoices(chunks: list, sources: list[str] | None = None,
                      ref=None) -> InvoiceAuthorityResult:
    """`chunks`: list of InvoiceChunkRaw in upload order (already merged per doc).
    `sources`: optional display name per chunk, positionally aligned — used to
    name both documents when one printed invoice arrives from two uploads.
    `ref`: the ReferenceStore, used by the field allocator to validate mined
    countries; None degrades to conservative (label-terminal) allocation."""
    if not chunks:
        raise BlockingValidationError("INVOICE_NO_VALID_SOURCE", "No invoice document was extracted")

    labels = list(sources or [])
    labels += [f"document {i + 1}" for i in range(len(labels), len(chunks))]
    # chunk kept paired with its source label AND its document index: the
    # cross-document duplicate check below compares by index, so two uploads
    # that happen to share a file name are still recognized as two documents.
    finals = [(c, i, labels[i]) for i, c in enumerate(chunks) if not _is_proforma(
        getattr(c.header, "invoice_kind_raw", None) if c.header else None,
        getattr(c.header, "document_title_raw", None) if c.header else None,
    ) and not _is_charge_doc(
        getattr(c.header, "invoice_kind_raw", None) if c.header else None,
        getattr(c.header, "document_title_raw", None) if c.header else None,
    )]

    warnings: list[ValidationMessage] = []
    if not finals:
        finals = [(c, i, labels[i]) for i, c in enumerate(chunks)]
        warnings.append(ValidationMessage.warning(
            "PROFORMA_ONLY", "Only proforma/charge invoices found; using them with caution."))

    items: list[WorkItem] = []
    invoice_refs: list[InvoiceRef] = []
    seen_refs: set[tuple[str, str]] = set()
    goods_total = Decimal("0")
    currency = "USD"
    currencies_seen: list[str] = []
    header0 = next((c.header for c, _i, _s in finals if c.header), None)
    headers = [c.header for c, _i, _s in finals if c.header]

    def _any_header(attr: str):
        """First non-empty value of `attr` across the selected invoices.

        Party/incoterm/currency identity belongs to the FIRST invoice, but the
        shipment-level references (bill of lading, a footer EXIM code) are
        properties of the consignment: a bundle whose second document is the
        one that prints them must not lose them."""
        return next((v for h in headers if (v := (getattr(h, attr, None) or "").strip())), None)

    # (virtual, goods_sum) pairs across all chunks — inputs of the totals checks
    virtual_sums: list[tuple[_Virtual, Decimal]] = []
    # per-chunk printed contribution: chunk-level combined grand, or the subs' sum
    chunk_printed: list[Decimal | None] = []
    # normalized invoice number -> (document index, label) that first carried it,
    # and the collisions found across DIFFERENT documents
    ref_sources: dict[str, tuple[int, str]] = {}
    dup_documents: dict[str, tuple[str, str]] = {}

    seq = 0
    for chunk, doc_i, source in finals:
        virtuals = _virtual_invoices(chunk, warnings)
        if len(virtuals) > 1:
            # a bundle may mix proforma/quotation pages with final invoices —
            # exclude those unless the bundle contains nothing else
            keep = [v for v in virtuals
                    if not _is_proforma(v.kind, v.title) and not _is_charge_doc(v.kind, v.title)]
            for v in virtuals:
                if keep and v not in keep:
                    warnings.append(ValidationMessage.warning(
                        "SUBINVOICE_PROFORMA_SKIPPED",
                        f"Sub-invoice {v.number or '?'} looks like a proforma/charge document; "
                        f"its {len(v.rows)} row(s) were excluded from goods."))
            if keep:
                virtuals = keep

        locales = getattr(chunk, "page_numeric_locales", None) or {}
        for v in virtuals:
            inv_no = v.number or "UNKNOWN"
            inv_dt = v.date or ""
            ref_key = (inv_no, inv_dt)
            # The same printed invoice reaching the declaration from two
            # uploads is the one thing nothing else can see: `seen_refs` below
            # merges the display entry, the row loop still runs for every copy,
            # and each copy's rows reconcile against its own printed total — so
            # the goods total silently doubles.  Detected here by invoice
            # identity (a re-scan has different bytes, so the upload-time
            # sha256 gate cannot catch it) and reported below as BLOCKING.
            # Rows are never dropped: a wrong drop is worse than a blocked job,
            # so the reviewer is told which two documents collide and removes one.
            norm_no = _norm_invoice_no(inv_no) if inv_no != "UNKNOWN" else None
            if norm_no:
                prior = ref_sources.get(norm_no)
                if prior is None:
                    ref_sources[norm_no] = (doc_i, source)
                elif prior[0] != doc_i:
                    dup_documents.setdefault(inv_no, (prior[1], source))
            # `inv_ref`, not `ref`: reusing the name shadowed the ReferenceStore
            # parameter, and every allocate_description() below received the
            # roster entry instead of the store (2026-07-30 live 500).
            inv_ref = None
            if ref_key not in seen_refs and inv_no != "UNKNOWN":
                seen_refs.add(ref_key)
                inv_ref = InvoiceRef(number=inv_no, date=inv_dt,
                                     currency=(v.currency or "").strip().upper())
                invoice_refs.append(inv_ref)
            elif inv_no != "UNKNOWN":
                inv_ref = next((r for r in invoice_refs if (r.number, r.date) == ref_key), None)
            if v.currency:
                currency = v.currency.strip().upper()
                if currency not in currencies_seen:
                    currencies_seen.append(currency)

            v_goods = Decimal("0")
            for idx, row in enumerate(v.rows, start=1):
                loc = locales.get(row.source_page_no)
                if row.row_classification != RowClassification.REAL_GOODS_ITEM:
                    continue
                if row.unit_price_raw is None and row.line_total_raw is None:
                    # continuation fragment noise (batch/serial lines without their
                    # own price) — a real goods row always prints a price or total
                    # (free-of-charge rows print an explicit 0.00)
                    warnings.append(ValidationMessage.warning(
                        "ROW_NO_VALUE_SKIPPED",
                        f"Invoice row p{row.source_page_no}#{row.source_row_index} has neither unit price "
                        f"nor line total (likely a continuation fragment) and was excluded: "
                        f"{(row.description_raw or '')[:60]!r}"))
                    continue
                qty = parse_decimal(row.quantity_raw, locale=loc) or Decimal("0")
                unit_price = parse_decimal(row.unit_price_raw, locale=loc) or Decimal("0")
                # Item price is ALWAYS the printed line total (user rule
                # 2026-07-18): every invoice prints a per-item total, but the
                # per-unit rate is sometimes omitted, so qty x unit_price is
                # never trusted as the price — it is only a last-resort estimate
                # (flagged) when the total is genuinely absent from the row.
                total_text = (row.line_total_raw or "").strip()
                line_total = parse_decimal(row.line_total_raw, locale=loc)
                if line_total is None:
                    line_total = q2(qty * unit_price)
                    if _looks_nonzero(total_text):
                        # a printed amount was there but could not be parsed
                        # (unrecognized currency/notation) — never silent
                        warnings.append(ValidationMessage.warning(
                            "ITEM_TOTAL_UNPARSED",
                            f"Invoice row p{row.source_page_no}#{row.source_row_index} "
                            f"({(row.description_raw or '')[:40]!r}) printed total "
                            f"{total_text!r} could not be parsed; used qty x unit price = "
                            f"{line_total}. Verify / correct the item price."))
                    else:
                        warnings.append(ValidationMessage.warning(
                            "ITEM_TOTAL_ESTIMATED",
                            f"Invoice row p{row.source_page_no}#{row.source_row_index} "
                            f"({(row.description_raw or '')[:40]!r}) had no printed line total; "
                            f"estimated {line_total} from qty x unit price — verify the item value."))
                # Safety net: a goods row whose invoice clearly printed a
                # non-zero amount must NEVER silently ship as 0 (a parse or
                # column-mapping miss). Genuine free-of-charge rows (0.00) and
                # blanks are exempt.
                if line_total == 0 and (_looks_nonzero(total_text)
                                        or _looks_nonzero(row.unit_price_raw)):
                    warnings.append(ValidationMessage.warning(
                        "ITEM_PRICE_ZERO_SUSPECT",
                        f"Invoice row p{row.source_page_no}#{row.source_row_index} "
                        f"({(row.description_raw or '')[:40]!r}) resolved to price 0 but the "
                        f"invoice printed a non-zero amount (total {total_text!r}, unit "
                        f"{row.unit_price_raw!r}). Verify and correct the item price."))
                seq += 1
                # Deterministic FIELD ALLOCATION (live-job root cause,
                # 2026-07-30): vendors fold batch numbers, quantity echoes and
                # the per-row COO into the description cell — split them into
                # their proper fields first, whichever extractor produced the
                # row.  Then the description cleaner (audit 2026-07-20) trims
                # any remaining trailing Batch/Lot/Mfg/Exp/Serial annotation.
                # The ORIGINAL printed text is preserved in
                # evidence_description_raw because the packing list was printed
                # against THAT text — packing-list matching must keep using it,
                # so this cleanup never perturbs weight allocation.
                # `description_printed_raw` is set only when extraction moved
                # this row's name out of a NEIGHBOUR's cell.  The two uses of
                # the raw text then diverge and must not be conflated: the
                # DECLARED description is built from the repaired text, while
                # the EVIDENCE key stays whatever this row itself printed —
                # the packing list was printed against that, so re-keying it
                # would silently move weight allocation.
                original_desc = row.description_raw.strip()
                printed_desc = (getattr(row, "description_printed_raw", None)
                                or row.description_raw).strip()
                alloc = allocate_description(original_desc, ref)
                clean_desc, _trimmed = clean_description(alloc.description)
                removed = " / ".join(x for x in (alloc.annotation, _trimmed) if x)
                # Invoice item weight -> kilograms HERE, at the ingest boundary
                # (spec 2026-07-21).  A unit that is printed but unrecognized
                # disqualifies the source: the value is dropped and allocation
                # falls through to its next net-weight priority, instead of
                # silently reading "500 G" per unit as 500 kg — which is how an
                # entire consignment's net weight ended up above its gross.
                weight_printed = parse_decimal(row.item_weight_raw, locale=loc)
                weight_kg, weight_unit_ok = to_kg(weight_printed, row.item_weight_unit_raw)
                item = WorkItem(
                    xml_item_sequence=seq,
                    source_invoice_number=inv_no,
                    source_invoice_date=inv_dt,
                    source_invoice_item_index=idx,
                    source_invoice_item_no=row.line_no_raw,
                    source_page_no=row.source_page_no,
                    source_document_file=source,
                    description_raw=clean_desc,
                    evidence_description_raw=(printed_desc if clean_desc != printed_desc else None),
                    quantity=qty,
                    # No "PCS" default: an absent unit is a QUESTION for the
                    # reviewer, not a fact.  Defaulting made an unreadable unit
                    # indistinguishable from a printed one, so a wrong column
                    # map shipped 15 rows of "PCS" against an invoice printing
                    # KGM/PRS/MTR with nothing downstream able to tell.
                    invoice_uom_raw=(row.uom_raw or "").strip(),
                    unit_price=unit_price,
                    line_total=line_total,
                    currency=(row.currency_raw or currency).strip().upper(),
                    # export-only brand/model/size raws — resolved later (after
                    # any reviewer edits) by rules.brand_model_size.resolve_all
                    brand_raw=row.brand_raw,
                    model_raw=row.model_raw,
                    size_raw=row.size_raw,
                    hs_code_raw=row.hs_code_raw,
                    # a COO printed inside the description cell counts as the
                    # row's own — but an extractor-captured field always wins
                    country_of_origin_raw=row.country_of_origin_raw or alloc.coo_raw,
                    item_weight_kg=weight_kg,
                    item_weight_scope=row.item_weight_scope,
                )
                if removed:
                    # never silently discarded (description_clean's contract):
                    # the reviewer sees exactly what left the declared text
                    item.warnings.append(ValidationMessage.warning(
                        "DESCRIPTION_ANNOTATION_TRIMMED",
                        f"Item {seq}: annotation text removed from the declared description "
                        f"({removed[:120]!r}); the original wording is kept for packing-list "
                        f"matching. Verify the remaining description.",
                        scope="ITEM", item_sequence=seq, field="description"))
                if not item.invoice_uom_raw:
                    item.warnings.append(ValidationMessage.warning(
                        "ITEM_UOM_MISSING",
                        f"Item {seq}: the invoice printed no readable unit of measurement for "
                        f"this row, so the field is left empty rather than assumed — enter the "
                        f"unit as printed (PCS, KGM, PRS, MTR, …) before finalizing.",
                        scope="ITEM", item_sequence=seq, field="uom"))
                if weight_printed is not None and not weight_unit_ok:
                    item.warnings.append(ValidationMessage.warning(
                        "ITEM_WEIGHT_UNIT_UNKNOWN",
                        f"Item {seq}: invoice weight {row.item_weight_raw!r} is printed in "
                        f"{row.item_weight_unit_raw!r}, which is not a recognized mass unit — "
                        "the invoice weight was ignored and the next weight source is used.",
                        scope="ITEM", item_sequence=seq, field="item_weight"))
                # Mode-B flag: OCR kept only a bare part code and dropped the
                # product name. Never reconstructed (a guessed customs
                # description would be an invented fact) — surfaced for review.
                if is_code_only(clean_desc):
                    item.warnings.append(ValidationMessage.warning(
                        "DESCRIPTION_CODE_ONLY",
                        f"Item {seq} description is only a product code ({clean_desc!r}); the "
                        f"product name may not have been captured — verify and complete it.",
                        scope="ITEM", item_sequence=seq, field="description"))
                items.append(item)
                v_goods += line_total
                goods_total += line_total
                if row.currency_raw:
                    cur = row.currency_raw.strip().upper()
                    if cur and cur not in currencies_seen:
                        currencies_seen.append(cur)
                if inv_ref is not None:
                    inv_ref.item_count += 1
            virtual_sums.append((v, v_goods))

        # printed contribution of this chunk: an explicitly printed combined
        # grand total wins; otherwise the sum of the sub-invoices' own grands
        combined = parse_decimal(chunk.totals.grand_total_raw) if chunk.totals else None
        sub_grands = [parse_decimal(v.totals.grand_total_raw)
                      for v in virtuals if v.totals and v.totals.grand_total_raw]
        sub_grands = [g for g in sub_grands if g is not None]
        if combined is not None and len(virtuals) > 1:
            if len(sub_grands) == len(virtuals) and abs(sum(sub_grands) - combined) > Decimal("0.5"):
                warnings.append(ValidationMessage.warning(
                    "SUBINVOICE_TOTALS_SUM_MISMATCH",
                    f"The printed combined total {combined} differs from the sum of the "
                    f"{len(virtuals)} sub-invoices' own totals {sum(sub_grands)}."))
            chunk_printed.append(combined)
        elif combined is not None:
            chunk_printed.append(combined)
        elif sub_grands and len(sub_grands) == len(virtuals):
            chunk_printed.append(sum(sub_grands, Decimal("0")))
        else:
            chunk_printed.append(None)

    if not items:
        raise BlockingValidationError("INVOICE_NO_VALID_SOURCE", "No real goods lines in the selected invoices")

    if len(currencies_seen) > 1:
        warnings.append(ValidationMessage.warning(
            "MIXED_INVOICE_CURRENCIES",
            f"Multiple invoice currencies seen ({', '.join(currencies_seen)}); XML is blocked until resolved."))

    # Escalated to a hard blocker by validate_declaration (and listed in
    # WARN_MODE_HARD_CODES): a declaration whose customs value and duty are 2x
    # is not an "otherwise complete" XML that warn mode exists to let through.
    for number, (first, second) in dup_documents.items():
        warnings.append(ValidationMessage.warning(
            "INVOICE_DUPLICATE_DOCUMENT",
            f"Invoice {number} was extracted from two uploaded documents ({first} and "
            f"{second}), so its goods rows, quantities and values are counted TWICE in this "
            f"declaration — the goods total and the duty are overstated. Remove one of the "
            f"two documents. No row was dropped automatically.",
            scope="INVOICE"))

    # ---- printed-totals completeness, per printed invoice ------------------- #
    multi = len(virtual_sums) > 1
    for v, v_goods in virtual_sums:
        target = _goods_target(v.totals)
        if target is None or abs(target - v_goods) <= Decimal("0.5"):
            continue
        who = f"invoice {v.number}" if (multi and v.number) else "the invoice"
        if v_goods < target * Decimal("0.8"):
            # The bigger the shortfall the louder the alarm: this is the
            # signature of line items lost during extraction.
            warnings.append(ValidationMessage.warning(
                "ROWS_INCOMPLETE_SUSPECT",
                f"Extracted goods lines sum to {v_goods} but {who} prints {target} "
                f"(net of printed charges) — over 20% of the invoice value is unaccounted for; "
                f"line items were likely lost. Re-run invoice extraction before finalizing."))
        else:  # small residual differences still deserve reviewer eyes
            warnings.append(ValidationMessage.warning(
                "TOTAL_MISMATCH",
                f"Calculated goods total {v_goods} differs from printed total net of charges "
                f"{target}." + (f" ({who})" if multi else "")))

    # document-level printed grand total: only when every uploaded invoice
    # document contributed one (a partial sum would understate the shipment)
    printed_total = None
    if chunk_printed and all(p is not None for p in chunk_printed):
        printed_total = sum(chunk_printed, Decimal("0"))
    elif len(chunk_printed) == 1:
        printed_total = chunk_printed[0]

    exporter = header0.exporter if header0 else None
    consignee = header0.consignee if header0 else None
    # ---- EXIM codes (user rule 2026-08-06: always look for one) ------------- #
    # Party blocks first, then any code printed outside them (footer line), then
    # the other invoices in the bundle.  The importer's EXIM is a hard XML
    # blocker, so an unattributed code is offered to the importer — the party
    # whose code the invoice exists to state — and the reviewer sees where it
    # came from.
    exporter_exim = (exporter.exim_code_raw if exporter else None) or None
    consignee_exim = (consignee.exim_code_raw if consignee else None) or None
    loose_exim = _any_header("exim_code_raw")
    if not consignee_exim and loose_exim and loose_exim != exporter_exim:
        consignee_exim = loose_exim
        warnings.append(ValidationMessage.warning(
            "IMPORTER_EXIM_FROM_DOCUMENT_BODY",
            f"The invoice prints EXIM code {loose_exim!r} outside the consignee block; it is "
            f"used as the importer's EXIM code. Confirm it belongs to the importer."))
    if not exporter_exim and not consignee_exim:
        for h in headers:
            for attr in ("consignee", "exporter"):
                p = getattr(h, attr, None)
                if p and (p.exim_code_raw or "").strip():
                    if attr == "consignee":
                        consignee_exim = p.exim_code_raw.strip()
                    else:
                        exporter_exim = p.exim_code_raw.strip()
    if not consignee_exim:
        warnings.append(ValidationMessage.warning(
            "IMPORTER_EXIM_NOT_ON_INVOICE",
            "No EXIM / IEC code could be found anywhere on the invoice — the importer EXIM "
            "code is a hard XML blocker, so enter it in Critical Review."))
    return InvoiceAuthorityResult(
        items=items,
        goods_total=q2(goods_total),
        currency=currency,
        exporter_name=exporter.name_raw if exporter else None,
        exporter_country_raw=exporter.country_raw if exporter else None,
        consignee_name=consignee.name_raw if consignee else None,
        consignee_code=consignee_exim,
        incoterm=header0.incoterm_raw if header0 else None,
        incoterm_place=header0.incoterm_place_raw if header0 else None,
        payment_terms_raw=header0.payment_terms_raw if header0 else None,
        lc_reference_raw=header0.lc_reference_raw if header0 else None,
        invoice_refs=invoice_refs,
        printed_grand_total=printed_total,
        exporter_address_raw=exporter.address_raw if exporter else None,
        exporter_exim_raw=exporter_exim,
        consignee_address_raw=consignee.address_raw if consignee else None,
        consignee_country_raw=consignee.country_raw if consignee else None,
        bill_of_lading_raw=_any_header("bill_of_lading_number_raw"),
        bill_of_lading_date_raw=_any_header("bill_of_lading_date_raw"),
        currencies_seen=currencies_seen,
        warnings=warnings,
    )
