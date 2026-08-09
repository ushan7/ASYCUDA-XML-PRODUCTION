"""Shared resolved-data structures used across the deterministic rule engines.

A :class:`WorkItem` starts as a raw invoice goods row and accumulates resolved
values (HS, COO, weights, cartons, supplementary unit, freight/insurance) as it
flows through the pipeline — always preserving invoice order.  Money/weights are
Decimal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..domain.errors import ValidationMessage


@dataclass
class WorkItem:
    xml_item_sequence: int
    source_invoice_number: str
    source_invoice_date: str
    source_invoice_item_index: int
    source_invoice_item_no: str | None

    description_raw: str
    quantity: Decimal
    invoice_uom_raw: str
    unit_price: Decimal
    line_total: Decimal
    currency: str

    # Raw per-row brand/model/size cells as printed (carried verbatim from the
    # invoice row; None when the column was absent).  The deterministic
    # brand_model_size resolver turns these + the description + the exporter
    # into the final display values below.  Export-only: never written to the
    # ASYCUDA XML.
    brand_raw: str | None = None
    model_raw: str | None = None
    size_raw: str | None = None
    # Resolved per-item BRAND / MODEL / SIZE for the post-XML .xls export.
    brand: str | None = None
    model: str | None = None
    size: str | None = None

    # Evidence lineage for the review's document viewer: the page of the source
    # document this row was printed on, and that document's file name.  Display
    # only — never read by allocation, HS/COO resolution or the XML builder.
    source_page_no: int | None = None
    source_document_file: str | None = None

    hs_code_raw: str | None = None
    country_of_origin_raw: str | None = None
    # Invoice-printed item weight, ALREADY CONVERTED TO KILOGRAMS at the
    # invoice-authority ingest boundary (app.units.to_kg).  None when the
    # invoice printed no weight, or printed one in an unrecognized unit — an
    # unrecognized unit disqualifies the source rather than defaulting to kg.
    item_weight_kg: Decimal | None = None
    item_weight_scope: str = "UNKNOWN"

    # Immutable server-controlled identity for reviewer add/delete operations.
    # Source rows get a deterministic id derived from extraction lineage (the
    # evidence is immutable, so the id is stable across recomputes); manual
    # rows get a stored UUID.  Assigned in review.item_mutations.
    item_id: str | None = None
    item_origin: str = "source"                  # "source" | "manual"
    fields_edited: bool = False                  # reviewer edited invoice fields
    # Reviewer-pinned per-item weights (Detailed Review edit, always kg).
    # Allocation keeps a pinned value EXACT and redistributes the remaining
    # authorised total across the unpinned items (exact-sum reconciled).
    manual_gross_weight_kg: Decimal | None = None
    manual_net_weight_kg: Decimal | None = None
    # Reviewer-pinned carton count (CTN, 2dp lattice).  Same contract as the
    # weights: the entered value is kept EXACTLY and the remaining authorised
    # cartons are absorbed by the unpinned items — estimates first, packing
    # evidence only when the estimates cannot cover it (docs/allocation-spec
    # section 6).
    manual_package_count: Decimal | None = None
    # Reviewer-pinned supplementary quantity (Detailed Review edit).  Unlike the
    # weight/carton pins there is nothing to redistribute — the supplementary
    # quantity is per-item with no shipment authority above it — so this pin is
    # a plain override: it REPLACES the derived number and changes nothing else.
    # The unit code/name still follow the official tariff unit of the final HS.
    manual_supplementary_quantity: Decimal | None = None
    # extraction-time description, preserved when the reviewer renames the
    # item: packing rows were printed against THIS text, so evidence-to-
    # evidence matching must keep using it (the edit is declaration-level)
    evidence_description_raw: str | None = None

    # ---- resolved (filled by engines) ----
    final_hs_code_11: str | None = None
    hs_official_description: str | None = None
    hs_tariff_unit: str | None = None
    hs_source: str | None = None
    # deterministic confidence of the HS proposal (1.0 = exact/reviewed;
    # < 1.0 = auto-completed guess -> AUTO_LOW_CONFIDENCE badge in review)
    hs_confidence: float | None = None
    hs_selection_explicit: bool = False          # reviewer explicitly chose it

    coo_alpha2: str | None = None
    coo_source: str | None = None

    gross_weight_kg: Decimal | None = None
    net_weight_kg: Decimal | None = None
    package_count: Decimal | None = None

    supplementary_unit_code: str | None = None
    supplementary_unit_name: str | None = None
    supplementary_quantity: Decimal | None = None

    item_external_freight: Decimal = Decimal("0")
    item_insurance: Decimal = Decimal("0")
    item_other_cost: Decimal = Decimal("0")

    # allocation audit trail (spec 2026-07-17): sources + final values for
    # carton / gross / net, kept per item and surfaced on the declaration
    allocation_audit: dict | None = None

    warnings: list[ValidationMessage] = field(default_factory=list)

    @property
    def commercial_description(self) -> str:
        # Sample XML style: "Adaptar 11 PCS" (description + qty + uom, no hyphen).
        qty = self.quantity.normalize()
        qty_txt = f"{qty:f}".rstrip("0").rstrip(".") if "." in f"{qty:f}" else f"{qty:f}"
        # An empty UOM prints nothing rather than an assumed "PCS": the field is
        # empty precisely because the invoice did not state a readable unit, and
        # ITEM_UOM_MISSING asks the reviewer to fill it in.
        uom = (self.invoice_uom_raw or "").strip().upper()
        return f"{self.description_raw} {qty_txt} {uom}".strip()


@dataclass
class InvoiceRef:
    number: str
    date: str
    currency: str = ""
    item_count: int = 0
    kind: str = "FINAL"          # FINAL / PROFORMA (roster display)


@dataclass
class InvoiceAuthorityResult:
    items: list[WorkItem]
    goods_total: Decimal
    currency: str
    exporter_name: str | None
    exporter_country_raw: str | None
    consignee_name: str | None
    consignee_code: str | None
    incoterm: str | None
    incoterm_place: str | None
    payment_terms_raw: str | None
    lc_reference_raw: str | None
    invoice_refs: list[InvoiceRef]
    printed_grand_total: Decimal | None
    exporter_address_raw: str | None = None
    exporter_exim_raw: str | None = None
    consignee_address_raw: str | None = None
    consignee_country_raw: str | None = None
    # The invoice states the shipment's bill-of-lading reference far more often
    # than not — for a sea/land job it is frequently the ONLY place it prints
    # (the transport document may not even have been uploaded).
    bill_of_lading_raw: str | None = None
    bill_of_lading_date_raw: str | None = None
    currencies_seen: list[str] = field(default_factory=list)
    warnings: list[ValidationMessage] = field(default_factory=list)


@dataclass
class AwbClassification:
    logical_form_id: str
    decision: str          # HAWB / MAWB / UNKNOWN_AWB / TRUE_DO / MIXED_DO_PACKING / TRACKING
    hawb_score: int
    mawb_score: int
    gross_weight: Decimal | None
    chargeable_weight: Decimal | None
    packages: Decimal | None
    awb_number: str | None
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class ValueAuthority:
    """Per-value provenance for one finalized shipment number (gross weight or
    carton/pieces): which document supplied it, from which printed label, and
    why — never silently overridden (design rule catalog, output/audit)."""
    document_id: str | None
    document_type: str | None      # HAWB / TRUE_DO / TRACKING / SINGLE_AWB / PACKING_LIST / REVIEWER_OVERRIDE
    value: Decimal | None
    unit: str | None
    source_label: str | None
    confidence: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_payload(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_type": self.document_type,
            "value": (f"{self.value}" if self.value is not None else None),
            "unit": self.unit,
            "source_label": self.source_label,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
        }


@dataclass
class ShipmentAuthority:
    # HAWB / BILL_OF_LADING / TRUE_DO / TRACKING / SINGLE_AWB / PACKING_LIST / UNKNOWN
    selected_authority_type: str
    gross_weight: Decimal | None
    packages: Decimal | None
    hawb_number: str | None
    mawb_number: str | None
    classifications: list[AwbClassification] = field(default_factory=list)
    warnings: list[ValidationMessage] = field(default_factory=list)
    selected_form_id: str | None = None
    # Sea / land shipments: the bill of lading replaces the air waybill as the
    # transport document, and Field 9 prints B/L NO / B/L DATE instead of
    # MAWB NO / HAWB (user rule 2026-08-06).
    bill_of_lading_number: str | None = None
    bill_of_lading_date: str | None = None
    gross_weight_unit: str | None = None
    package_source_label: str | None = None
    gross_weight_authority: ValueAuthority | None = None
    carton_authority: ValueAuthority | None = None


@dataclass
class BankingResolution:
    bank_code: str | None
    bank_name: str | None
    terms_code: str | None
    terms_description: str | None
    amount: Decimal | None
    currency: str | None
    reference_number: str | None
    draft_tenor_raw: str | None
    invoice_references: list[str] = field(default_factory=list)
    swift_raw: str | None = None
    value_date_raw: str | None = None
    payment_terms_raw: str | None = None
    bank_resolution_state: str = "ABSENT"      # RESOLVED / AMBIGUOUS / INVALID / ABSENT
    payment_resolution_state: str = "ABSENT"   # RESOLVED / REVIEW_REQUIRED / ABSENT
    warnings: list[ValidationMessage] = field(default_factory=list)


@dataclass
class FreightResult:
    effective_freight_foreign: Decimal
    currency: str
    source: str
    warnings: list[ValidationMessage] = field(default_factory=list)
