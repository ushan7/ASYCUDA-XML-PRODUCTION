"""Critical Review — the declaration-level human control gate.

Confirms the authoritative invoice, shipment, party, banking, payment, freight,
insurance and document-reference values *before* item-level allocation and XML
generation.  Three kinds of values live here:

* direct XML values   — copied into a specific element (manifest, parties…);
* derived XML values  — inputs to deterministic composition (Field 9, Field 40,
  first-item banking texts, package kind, weight-unit normalization);
* review/audit values — provenance and lock metadata that never reach XML
  (resolution states, mixed-source reason, fingerprint).

The AI extractors only supply evidence; every value written to XML is either a
deterministic rule output or an explicit reviewer entry made on this screen.
Confirming values re-runs allocation only — never OCR or extraction.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from ..dates import fmt_dd_mmm_yyyy
from ..domain.errors import ValidationMessage
from ..numbers import parse_decimal
from ..reference.store import ReferenceStore
from ..rules.airwaybill_authority import derive_field40
from ..rules.models import BankingResolution, InvoiceAuthorityResult, ShipmentAuthority
from ..units import WEIGHT_UNIT_TO_KG, normalize_weight_unit

# --------------------------------------------------------------------------- #
# Deterministic maps (spec: Critical Review content and XML-field relations)
# --------------------------------------------------------------------------- #
PACKAGE_TYPES = {
    "CT": "Carton", "PX": "Pallet", "PK": "Package", "BX": "Box",
    "BG": "Bag", "DR": "Drum", "CR": "Crate",
}
DEFAULT_PACKAGE_TYPE = "CT"

# Weight units live in app.units — the single conversion boundary for mass
# (re-exported here because callers have imported them from this module since
# before that split; there is only one definition).

# payment-term code -> first-item text prefix (spec section 13)
BANK_TEXT_PREFIX = {
    "200": "LC NO", "400": "TT REF", "700": "CAD REF",
    "906": "DA REF", "600": "DAP REF", "999": "FOC",
}
_EXIM_RE = re.compile(r"[A-Za-z0-9]{13,15}")
# C0 control chars XML 1.0 forbids (tab/newline/CR stay) — reviewer Field 9 may
# carry one from a PDF paste; stripped so serialization never fails.
_XML_ILLEGAL_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


__all_units__ = (WEIGHT_UNIT_TO_KG, normalize_weight_unit)   # re-export marker


def bank_prefix(terms_code: str | None) -> str:
    return BANK_TEXT_PREFIX.get((terms_code or "").strip(), "BANK REF")


def _cur_symbol(currency: str | None) -> str:
    return "$" if (currency or "").strip().upper() in ("USD", "US$", "$") else f"{(currency or '').strip().upper()} "


def transport_doc_type(mawb: str, hawb: str, bill_of_lading: str,
                       authority_type: str = "") -> str:
    """"AWB" or "BL" — which transport document this shipment travelled on.

    A bill of lading uploaded INSTEAD OF an air waybill / delivery order makes
    the shipment a B/L shipment, and Field 9 must then print B/L NO / B/L DATE
    rather than MAWB NO / HAWB (user rule 2026-08-06).  The decision is
    deterministic and stated in one place:

    * the selected shipment authority IS a bill of lading            -> BL
    * a B/L number exists and no air waybill number does             -> BL
    * anything else (an AWB number exists)                           -> AWB

    An air shipment whose invoice happens to quote a B/L number therefore stays
    an AWB shipment; the B/L line is still printed underneath.  The reviewer
    can override the choice in Critical Review.
    """
    if (authority_type or "").upper() == "BILL_OF_LADING":
        return "BL"
    if (bill_of_lading or "").strip() and not ((mawb or "").strip() or (hawb or "").strip()):
        return "BL"
    return "AWB"


def compose_field9(mawb: str, hawb: str, invoice_refs: list[tuple[str, str]],
                   bank_reference: str, bank_date: str, terms_code: str | None,
                   bill_of_lading: str = "", bill_of_lading_date: str = "",
                   doc_type: str = "") -> str:
    """Reference-XML style Field 9 (Traders/Financial/Financial_name):

        MAWB NO:160-08566795 HAWB:KCNKTM0057
        INVOICE NO:2026-209-1 DT: 24-FEB-2026
        LC NO:0018283210508FU, DT:24-FEB-2026

    Sea and land shipments carry a Bill of Lading instead of an air waybill,
    and then the first line is the B/L instead of the MAWB/HAWB line:

        B/L NO:HLCUBOM240012345 B/L DATE:24-FEB-2026

    ``doc_type`` ("AWB"/"BL") forces the choice; empty derives it from the
    numbers via :func:`transport_doc_type`.
    """
    lines: list[str] = []
    kind = (doc_type or "").upper() or transport_doc_type(mawb, hawb, bill_of_lading)
    bl_line = ""
    if bill_of_lading:
        bl_line = f"B/L NO:{bill_of_lading}"
        if bill_of_lading_date:
            bl_line += f" B/L DATE:{fmt_dd_mmm_yyyy(bill_of_lading_date)}"
    if kind == "BL" and (bl_line or not (mawb or hawb)):
        # the B/L REPLACES the air-waybill line — never both, or the reviewer
        # cannot tell which document the declaration travelled on.  A "BL"
        # choice with no B/L number but an AWB number left falls through to the
        # air line rather than emitting no transport reference at all.
        if bl_line:
            lines.append(bl_line)
    else:
        if mawb or hawb:
            lines.append(f"MAWB NO:{mawb or ''} HAWB:{hawb or ''}".strip())
        if bl_line:
            lines.append(bl_line)
    for number, date in invoice_refs:
        lines.append(f"INVOICE NO:{number} DT: {fmt_dd_mmm_yyyy(date)}")
    if bank_reference:
        lines.append(f"{bank_prefix(terms_code)}:{bank_reference}, DT:{fmt_dd_mmm_yyyy(bank_date)}")
    return "\n".join(lines)


def compose_first_item_texts(terms_code: str | None, bank_reference: str,
                             bank_amount: Decimal | None, bank_currency: str,
                             bank_date_ddmmyyyy: str) -> tuple[str, str]:
    """Reference-XML style first-item texts -> (Previous_document_reference, Free_text_1):

        DATE:24/02/2026,($7023.17 OF LC)   |   LC NO:0018283210508FU,$7023.17
    """
    if not bank_reference and bank_amount is None:
        return "", ""
    word = bank_prefix(terms_code).split()[0]          # LC / TT / CAD / DA / DAP / FOC / BANK
    sym = _cur_symbol(bank_currency)
    amt = f"{bank_amount:.2f}" if bank_amount is not None else ""
    prev = ""
    if bank_date_ddmmyyyy or amt:
        inner = f"{sym}{amt} OF {word}" if amt else word
        prev = f"DATE:{bank_date_ddmmyyyy},({inner})"
    free = f"{bank_prefix(terms_code)}:{bank_reference},{sym}{amt}".rstrip(",") if bank_reference else ""
    return prev, free


# --------------------------------------------------------------------------- #
# Review payload models
# --------------------------------------------------------------------------- #
class PartyReview(BaseModel):
    name: str = ""
    address: str = ""
    exim_code: str = ""
    country_code: str = ""
    country_resolution_state: str = "ABSENT"   # RESOLVED / INVALID / ABSENT


class RosterEntry(BaseModel):
    number: str
    date: str                                   # normalized DD-MMM-YYYY
    currency: str = ""
    item_count: int = 0
    source_file: str = ""
    pages: list[int] = Field(default_factory=list)


class ShipmentCandidate(BaseModel):
    form_id: str
    decision: str
    awb_number: str = ""
    gross_weight: str = ""
    packages: str = ""
    selected: bool = False
    reasons: list[str] = Field(default_factory=list)


class FreightCandidate(BaseModel):
    """A document-supported freight amount the reviewer can choose (user rule
    2026-07-17: show invoiced, SWIFT and HAWB-weight-proportional amounts;
    default the highest into the freight input)."""
    source: str          # INVOICE / BANKING_SWIFT / AWB
    amount: str          # 2dp string, in `currency`
    currency: str = ""   # as printed on the source document
    # False when the amount is denominated in something other than the invoice
    # currency: it cannot be clicked into the declaration, only converted first
    comparable: bool = True
    detail: str = ""


class EvidenceItem(BaseModel):
    field: str
    normalized_value: str = ""
    raw_value: str = ""
    source_role: str = ""
    source_file: str = ""
    page_number: int | None = None
    quote: str = ""


class ItemDetailRow(BaseModel):
    """One goods row of the Detailed Review table — an item-level preview of
    what finalize will produce.  Weights/cartons/supplementary come from the
    allocation preview run with the review-default authority totals; blanks
    mean the value cannot be derived yet (e.g. no gross authority)."""
    sn: int                                      # invoice order = XML sequence
    item_id: str = ""                            # immutable server-controlled identity
    origin: str = "source"                       # "source" | "manual"
    edited: bool = False                         # reviewer edited invoice fields
    hs_source: str = ""                          # resolution source of final_hs
    hs_confidence: str = ""                      # "1.00" exact/reviewed, lower = auto guess
    hs_low_confidence: bool = False              # resolved but confidence < 1.0
    hs_explicit: bool = False                    # reviewer explicitly chose/confirmed it
    description: str = ""
    coo: str = ""                                # resolved alpha-2, else raw
    invoice_hs: str = ""                         # HS as printed on the invoice
    final_hs: str = ""                           # official 11-digit; "" = manual review
    quantity: str = ""
    uom: str = ""
    total_price: str = ""
    # deterministic per-item BRAND / MODEL / SIZE (post-XML .xls export preview)
    brand: str = "NA"
    model: str = "NA"
    size: str = "NA"
    gross: str = ""
    net: str = ""
    gross_pinned: bool = False                   # reviewer-entered gross (kg)
    net_pinned: bool = False                     # reviewer-entered net (kg)
    ctn: str = ""
    ctn_pinned: bool = False                     # reviewer-entered cartons (CTN)
    sup_unit: str = ""                           # supplementary unit code
    sup_name: str = ""                           # supplementary unit name
    # evidence lineage (document viewer deep-link): the source document's file
    # name and the page this row was printed on; blank for manual rows
    src_file: str = ""
    src_page: int | None = None
    sup_qty: str = ""                            # supplementary quantity
    sup_qty_pinned: bool = False                 # reviewer-entered supplementary qty


class CriticalReview(BaseModel):
    # ---- 1. declaration summary ----------------------------------------- #
    invoice_item_count: int
    total_number_of_forms: int
    calculated_goods_total: str
    goods_currency: str                          # invoice currency only
    gross_weight: str
    gross_weight_source: str
    total_packages: str
    packages_source: str
    package_type: str = DEFAULT_PACKAGE_TYPE
    package_type_name: str = PACKAGE_TYPES[DEFAULT_PACKAGE_TYPE]
    reviewed_packing_weight_unit: str = "KGM"
    weight_unit_evidence: str = ""
    weight_unit_missing: bool = False

    # ---- 2. manifest & declaration identity (per-job, reference-gated) --- #
    manifest_no: str = ""
    # Box 1 — one of the 17 declaration-model LINES (type+code together).
    declaration_type: str = "IM"
    gen_procedure_code: str = "4"
    customs_office_code: str = ""
    customs_office_name: str = ""
    # Box 37 job-level defaults (4000/000 unless the reviewer selects others).
    extended_customs_procedure: str = ""
    national_customs_procedure: str = ""
    # reviewer-selection overlay revision (stales the fingerprint on change)
    regime_revision: int = 0

    # ---- 3. authoritative invoices --------------------------------------- #
    invoice_numbers: list[str] = Field(default_factory=list)
    invoice_dates: list[str] = Field(default_factory=list)
    invoice_roster: list[RosterEntry] = Field(default_factory=list)
    field_9_invoice_transport_document: str = ""

    # ---- 5/6. parties ----------------------------------------------------- #
    exporter: PartyReview = Field(default_factory=PartyReview)
    importer: PartyReview = Field(default_factory=PartyReview)
    importer_exim_valid: bool = False

    # ---- 7. shipment authority -------------------------------------------- #
    hawb_no: str = ""
    mawb_no: str = ""
    # Sea / land shipments: seeded from the bill of lading itself, else from the
    # B/L reference the invoice prints, else a manual reviewer entry.
    bill_of_lading_no: str = ""
    bill_of_lading_date: str = ""                 # normalized DD-MMM-YYYY
    bill_of_lading_source: str = ""               # TRANSPORT_DOCUMENT / INVOICE / ""
    # "AWB" or "BL" — which transport document Field 9 states.  Reviewer-
    # overridable; the reason records how the default was reached.
    transport_doc_type: str = "AWB"
    transport_doc_type_reason: str = ""
    shipment_authority_type: str = "UNKNOWN"
    shipment_candidates: list[ShipmentCandidate] = Field(default_factory=list)
    gross_weight_source_doc: str = ""
    package_count_source_doc: str = ""
    mixed_source: bool = False
    # per-value provenance (document id/type, printed source label, confidence,
    # reasons) for the finalized gross weight and carton count — audit surface
    gross_weight_authority: dict | None = None
    carton_authority: dict | None = None

    # ---- 8. transport ------------------------------------------------------ #
    field_18_transport_identity: str = ""
    field_21_transport_identity: str = ""
    field_40_previous_document: str = ""
    field_40_reason: str = ""
    # the reviewer is ALWAYS asked which number goes into Field 40 (user rule
    # 2026-07-18); these are the document-extracted choices, suggested = the
    # deterministic weight-rule default
    field_40_candidates: list[dict] = Field(default_factory=list)
    # Box 25/26 — DELIBERATELY unseeded ("" = not yet chosen): the reviewer
    # must pick both modes per job, finalize blocks until then (rule 2026-08-01)
    border_mode: str = ""
    inland_mode_of_transport: str = ""
    border_nationality: str = "NP"
    # border office defaults to the clearance office; differs only for cargo
    # moving between Nepali offices through India/China (NECAS Box 29)
    border_office_code: str = ""
    border_office_name: str = ""
    place_of_loading_code: str = ""
    location_of_goods: str = ""
    container_flag: bool = False

    # ---- 10. incoterm ------------------------------------------------------ #
    incoterm: str = ""
    delivery_place: str = ""
    invoice_values_exclude_freight_insurance: bool = False

    # ---- 11/12. bank & payment --------------------------------------------- #
    bank_code: str = ""
    bank_name: str = ""
    swift_code: str = ""
    bank_resolution_state: str = "ABSENT"
    payment_term_code: str = ""
    payment_term_description: str = ""
    payment_term_text: str = ""
    payment_resolution_state: str = "ABSENT"
    mode_of_payment: str = "CASH"

    # ---- 13. banking transaction reference ---------------------------------- #
    bank_reference: str = ""
    bank_amount: str = ""
    bank_currency: str = ""
    bank_date: str = ""                          # normalized DD-MMM-YYYY

    # ---- 14/15. freight & insurance ------------------------------------------ #
    manual_freight_amount: str = ""
    manual_freight_currency: str = ""
    freight_source: str = ""
    # every document-supported freight amount (invoice / SWIFT / AWB incl.
    # HAWB-weight-prorated master totals); default = highest, reviewer chooses
    freight_candidates: list[FreightCandidate] = Field(default_factory=list)
    manual_insurance_amount: str = ""
    # The insurance premium is added to the CIF as a NATIONAL-currency amount
    # (builder: total_cost = external_freight_national + insurance_national) —
    # it is NOT converted at the exchange rate the way freight is.  The input
    # sat unlabelled directly beneath a freight box labelled in the invoice
    # currency, so a reviewer typing the premium in USD understated the customs
    # value by the whole rate.  The currency the field expects is now stated by
    # the server that consumes it.
    insurance_currency: str = ""

    # ---- 17/18/19. evidence, conflicts, lock ---------------------------------- #
    evidence: list[EvidenceItem] = Field(default_factory=list)
    warnings: list[ValidationMessage] = Field(default_factory=list)
    review_fingerprint: str = ""
    # derived packing-authority view (spec 2026-07-17 section 2); None when no
    # packing list was uploaded
    packing_view: dict | None = None
    # item-level Detailed Review rows (allocation/supplementary preview)
    item_details: list[ItemDetailRow] = Field(default_factory=list)
    # reviewer add/delete overlay revision (0 = untouched)
    item_mutation_revision: int = 0


class CriticalReviewConfirmation(BaseModel):
    """Reviewer-confirmed values posted at finalize.  ``None`` = keep the
    computed review default; empty string = explicit blank."""
    confirmed_gross_weight: str | None = None
    confirmed_total_packages: str | None = None
    reported_item_count: int | None = None
    reviewed_packing_weight_unit: str | None = None
    manual_shipment_authority_confirmed: bool = False
    mixed_source_reason: str | None = None

    manifest_no: str | None = None
    package_type: str | None = None
    hawb_no: str | None = None
    mawb_no: str | None = None
    bill_of_lading_no: str | None = None
    bill_of_lading_date: str | None = None
    # "AWB" / "BL" — the reviewer's final word on which transport document this
    # declaration travelled on (drives the first Field 9 line)
    transport_doc_type: str | None = None
    field_18_transport_identity: str | None = None
    field_21_transport_identity: str | None = None
    field_40_previous_document: str | None = None

    # ---- regime & transport selection (validated against the reference) ---- #
    declaration_type: str | None = None          # picked as ONE line with…
    gen_procedure_code: str | None = None        # …this (IM 4, EX 1, …)
    customs_office_code: str | None = None
    customs_office_name: str | None = None       # normally derived from the code
    border_office_code: str | None = None        # explicit "" = same as clearance
    border_office_name: str | None = None
    extended_customs_procedure: str | None = None
    national_customs_procedure: str | None = None
    border_mode: str | None = None
    inland_mode_of_transport: str | None = None
    border_nationality: str | None = None
    place_of_loading_code: str | None = None
    location_of_goods: str | None = None
    container_flag: bool | None = None
    # explicit reviewer decision on Field 40 — the chosen value is final
    field_40_confirmed: bool = False
    # editable Field 9 (Financial_name): when field_9_override is true the
    # reviewer's field_9_text is used VERBATIM; otherwise Field 9 is recomposed
    # deterministically from the reviewed values at finalize.
    field_9_text: str | None = None
    field_9_override: bool = False

    exporter_name: str | None = None
    exporter_address: str | None = None
    exporter_exim_code: str | None = None
    exporter_country_code: str | None = None
    importer_name: str | None = None
    importer_address: str | None = None
    importer_exim_code: str | None = None
    importer_country_code: str | None = None

    incoterm: str | None = None
    delivery_place: str | None = None
    invoice_values_exclude_freight_insurance: bool = False

    bank_code: str | None = None
    bank_name: str | None = None
    swift_code: str | None = None
    payment_term_code: str | None = None
    mode_of_payment: str | None = None

    bank_reference: str | None = None
    bank_amount: str | None = None
    bank_currency: str | None = None
    bank_date: str | None = None

    manual_freight_amount: str | None = None
    manual_insurance_amount: str | None = None

    review_fingerprint: str | None = None        # stale-review detection


@dataclass
class ReviewedValues:
    """Merged review + confirmation — the single deterministic input for
    finalize/build.  Amounts are Decimal; weight already normalized to kg."""
    gross_weight_kg: Decimal
    total_packages: Decimal
    weight_unit: str
    manual_shipment_authority_confirmed: bool
    weight_unit_missing: bool
    mixed_source_reason: str

    manifest_no: str
    package_type_code: str
    package_type_name: str
    hawb_no: str
    mawb_no: str
    bill_of_lading_no: str
    bill_of_lading_date: str
    transport_doc_type: str                      # AWB | BL
    field_18: str
    field_21: str
    field_40: str
    field_40_confirmed: bool

    # regime & transport (reviewer-selected, reference-gated at validation)
    declaration_type: str
    gen_procedure_code: str
    customs_office_code: str
    customs_office_name: str
    border_office_code: str
    border_office_name: str
    extended_customs_procedure: str
    national_customs_procedure: str
    border_mode: str
    inland_mode_of_transport: str
    border_nationality: str
    place_of_loading_code: str
    location_of_goods: str
    container_flag: bool
    field_9_text: str                            # reviewer's verbatim Field 9 (when overridden)
    field_9_override: bool                        # use field_9_text instead of recomposing
    shipment_authority_type: str

    exporter_name: str
    exporter_address: str
    exporter_exim: str
    exporter_country: str
    importer_name: str
    importer_address: str
    importer_exim: str
    importer_country: str

    incoterm: str
    delivery_place: str
    exclude_freight_insurance: bool

    bank_code: str
    bank_name: str
    swift_code: str
    terms_code: str
    mode_of_payment: str

    bank_reference: str
    bank_amount: Decimal | None
    bank_currency: str
    bank_date: str

    freight_amount: Decimal | None               # authoritative reviewed freight
    insurance_amount: Decimal                    # amount only; currency set in ASYCUDA

    invoice_refs: list[tuple[str, str]]          # (number, raw date) — Field 9 input


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def build_review(
    inv: InvoiceAuthorityResult,
    ship: ShipmentAuthority,
    banking: BankingResolution,
    ref: ReferenceStore,
    *,
    settings,
    freight_default: Decimal | None = None,
    freight_source: str = "",
    freight_candidates: list[dict] | None = None,
    documents: list | None = None,
    packing_view: dict | None = None,
    item_details: list[ItemDetailRow] | None = None,
    item_mutation_revision: int = 0,
    extra_warnings: list | None = None,
    review_selections: dict | None = None,
    regime_revision: int = 0,
) -> CriticalReview:
    warnings = (list(inv.warnings) + list(ship.warnings) + list(banking.warnings)
                + list(extra_warnings or []))

    # ---- regime & transport seeds: stored reviewer selection > settings ---- #
    # (document evidence never supplies these — they are declaration identity,
    # not extraction facts; the reviewer's choice is validated against the
    # reference tables at finalize)
    sel = review_selections or {}
    sel_office = str(sel.get("customs_office_code") or "").strip().upper()
    office_code = sel_office or settings.customs_office_code
    office_name = ref.office_by_code.get(office_code) or (
        settings.customs_office_name if office_code == settings.customs_office_code else "")
    border_code = str(sel.get("border_office_code") or "").strip().upper() or office_code
    border_name = ref.office_by_code.get(border_code) or (
        office_name if border_code == office_code else "")
    border_mode_sel = str(sel.get("border_mode") or "").strip()
    inland_mode_sel = str(sel.get("inland_mode_of_transport") or "").strip()
    if not border_mode_sel or not inland_mode_sel:
        missing = [n for n, v in (("border (Box 25)", border_mode_sel),
                                  ("inland (Box 26)", inland_mode_sel)) if not v]
        warnings.append(ValidationMessage.warning(
            "TRANSPORT_MODE_UNSELECTED",
            f"Mode of transport not selected yet: {' and '.join(missing)}. There is no "
            "default — choose the mode(s) from the reference list before finalizing."))
    gw = ship.gross_weight
    pk = ship.packages
    if gw is None:
        warnings.append(ValidationMessage.warning("GROSS_UNKNOWN", "No gross-weight authority; please enter it."))
    if pk is None:
        warnings.append(ValidationMessage.warning("PACKAGES_UNKNOWN", "No package authority; please enter it."))

    # Documents-optional guidance (user rule 2026-07-17: only the invoice is
    # compulsory) — tell the reviewer exactly what manual entry is expected.
    roles_present = {getattr(d, "declared_role", "") for d in documents or []}
    if "AIR_WAYBILL" not in roles_present:
        warnings.append(ValidationMessage.warning(
            "TRANSPORT_DOC_MISSING",
            "No air waybill / bill of lading uploaded — enter the total gross weight, total "
            "cartons and the MAWB / HAWB / Bill of Lading number manually below."))
    if "PACKING_LIST" not in roles_present:
        warnings.append(ValidationMessage.warning(
            "PACKING_DOC_MISSING",
            "No packing list uploaded — item weights will be split from the authorised gross by "
            "quantity share and cartons by weight share at finalize (exact-sum reconciled)."))
    elif any("PACKING_EXTRACTION_OVER_BUDGET" in str(w)
             for d in documents or [] if getattr(d, "declared_role", "") == "PACKING_LIST"
             for w in (getattr(d, "warnings", None) or [])):
        warnings.append(ValidationMessage.warning(
            "PACKING_TIMEOUT_FALLBACK",
            "Packing-list extraction exceeded the time budget — weight and carton allocation "
            "uses the FALLBACK: authorised gross weight split proportional to item quantity, "
            "cartons proportional to weight, totals reconciled exactly. Verify item weights "
            "before finalizing."))
    elif any("PACKING_EXTRACTION_PARTIAL" in str(w)
             for d in documents or [] if getattr(d, "declared_role", "") == "PACKING_LIST"
             for w in (getattr(d, "warnings", None) or [])):
        warnings.append(ValidationMessage.warning(
            "PACKING_PARTIAL_FALLBACK",
            "Packing-list extraction hit the time budget PART-WAY through — the item weights and "
            "cartons it did extract ARE used; the remaining items take their quantity share of "
            "the authorised gross and are named individually below. Verify those items, or re-run "
            "the packing extraction, before finalizing."))
    if "BANKING" not in roles_present:
        warnings.append(ValidationMessage.warning(
            "BANKING_DOC_MISSING",
            "No banking document uploaded — bank, payment-term and transaction-reference fields "
            "below are manual entries."))

    unit = normalize_weight_unit(ship.gross_weight_unit)
    unit_missing = ship.gross_weight_unit is not None and unit is None or (gw is not None and ship.gross_weight_unit is None)
    if unit_missing:
        warnings.append(ValidationMessage.warning(
            "WEIGHT_UNIT_MISSING",
            "The selected shipment authority does not carry a recognizable weight unit. Confirm the "
            "unit (and tick the manual shipment-authority confirmation) before XML."))

    # ---- parties --------------------------------------------------------- #
    exporter = _party(inv.exporter_name, inv.exporter_address_raw, inv.exporter_exim_raw,
                      inv.exporter_country_raw, ref)
    importer = _party(inv.consignee_name, inv.consignee_address_raw, inv.consignee_code,
                      inv.consignee_country_raw, ref)
    exim_valid = bool(_EXIM_RE.fullmatch(importer.exim_code or ""))
    if not exim_valid:
        warnings.append(ValidationMessage.warning(
            "IMPORTER_EXIM_INVALID",
            f"Importer EXIM code {importer.exim_code!r} is not 13–15 alphanumeric characters — "
            "hard XML blocker until corrected."))
    if exporter.country_resolution_state != "RESOLVED":
        warnings.append(ValidationMessage.warning(
            "EXPORTER_COUNTRY_UNRESOLVED", "Exporter country could not be validated against the country reference."))

    # ---- invoices / roster -------------------------------------------------- #
    roster = _roster(inv, documents)
    if not inv.currency:
        warnings.append(ValidationMessage.warning("INVOICE_CURRENCY_MISSING", "No invoice currency found."))
    # Only fires for two refs sharing a number but printing different dates.
    # It is NOT the guard against one invoice being declared twice: a repeat of
    # the same (number, date) is merged into a single ref by finalize_invoices,
    # so this comparison is 1 != 1.  That case is INVOICE_DUPLICATE_DOCUMENT,
    # raised there and escalated to a blocker by validate_declaration.
    if len({r.number for r in inv.invoice_refs}) != len(inv.invoice_refs):
        warnings.append(ValidationMessage.warning("INVOICE_REF_DUPLICATE", "Duplicate invoice number/date pairs."))

    # ---- shipment candidates + field 40 ----------------------------------- #
    candidates = [ShipmentCandidate(
        form_id=c.logical_form_id, decision=c.decision, awb_number=c.awb_number or "",
        gross_weight=(f"{c.gross_weight}" if c.gross_weight is not None else ""),
        packages=(f"{c.packages}" if c.packages is not None else ""),
        selected=(c.logical_form_id == ship.selected_form_id), reasons=c.reasons,
    ) for c in ship.classifications]
    # ---- transport document: air waybill or bill of lading? ---------------- #
    # A B/L uploaded INSTEAD OF an air waybill / delivery order changes what
    # Field 9 prints (user rule 2026-08-06).  The number comes from the
    # transport document when one was uploaded; otherwise from the invoice,
    # which states it on most sea/land shipments.
    bl_no = (ship.bill_of_lading_number or "").strip()
    bl_date_raw = (ship.bill_of_lading_date or "").strip()
    bl_source = "TRANSPORT_DOCUMENT" if bl_no else ""
    if not bl_no and (inv.bill_of_lading_raw or "").strip():
        bl_no = inv.bill_of_lading_raw.strip()
        bl_date_raw = bl_date_raw or (inv.bill_of_lading_date_raw or "").strip()
        bl_source = "INVOICE"
        warnings.append(ValidationMessage.warning(
            "BL_FROM_INVOICE",
            f"No bill of lading was uploaded, but the invoice states B/L {bl_no!r}" +
            (f" dated {fmt_dd_mmm_yyyy(bl_date_raw)}" if bl_date_raw else "") +
            ". It is used as the transport-document reference — verify it against the "
            "carrier's B/L."))
    doc_type = transport_doc_type(ship.mawb_number or "", ship.hawb_number or "", bl_no,
                                  ship.selected_authority_type)
    if doc_type == "BL":
        doc_type_reason = (
            f"Bill of lading {bl_no or '(number not read)'} is the transport document"
            + (" (it is the selected shipment authority)"
               if ship.selected_authority_type == "BILL_OF_LADING"
               else " and no air waybill number was found")
            + " — Field 9 prints B/L NO / B/L DATE instead of MAWB NO / HAWB.")
        if not bl_no:
            warnings.append(ValidationMessage.warning(
                "BL_NUMBER_MISSING",
                "This is a bill-of-lading shipment but no B/L number could be read — enter it "
                "below; Field 9 and Field 40 both need it."))
    else:
        doc_type_reason = ("Air waybill numbers found — Field 9 prints MAWB NO / HAWB"
                           + (" (the B/L reference is printed underneath)" if bl_no else "") + ".")

    field40, field40_reason = derive_field40(ship)
    if not field40 and bl_no:
        field40, field40_reason = bl_no, (
            "Bill of Lading shipment — the B/L number is used.")
    if not field40 and ship.selected_authority_type not in ("UNKNOWN",) and (ship.hawb_number or ship.mawb_number):
        warnings.append(ValidationMessage.warning(
            "FIELD_40_EMPTY", "Field 40 could not be derived from the air waybills; enter it manually."))
    # the reviewer is always asked which extracted number becomes Field 40
    field_40_candidates = [
        {"source": src, "value": num, "suggested": num == field40}
        for src, num in (("HAWB", ship.hawb_number), ("MAWB", ship.mawb_number),
                         ("B/L", bl_no)) if num]

    # ---- banking ------------------------------------------------------------ #
    bank_date_norm = fmt_dd_mmm_yyyy(banking.value_date_raw)
    if banking.reference_number and (banking.amount is None or not banking.value_date_raw):
        warnings.append(ValidationMessage.warning(
            "BANK_REFERENCE_INCOMPLETE",
            "Banking transaction reference is missing its amount or date; first-item texts will be partial."))

    # ---- freight ------------------------------------------------------------- #
    if freight_default is None:
        warnings.append(ValidationMessage.warning(
            "FREIGHT_MISSING", "No freight evidence found; enter the reviewed freight amount (0.00 must be explicit)."))

    field9 = compose_field9(
        ship.mawb_number or "", ship.hawb_number or "",
        [(r.number, r.date) for r in inv.invoice_refs],
        banking.reference_number or "", banking.value_date_raw or "", banking.terms_code,
        bill_of_lading=bl_no, bill_of_lading_date=bl_date_raw, doc_type=doc_type)

    review = CriticalReview(
        invoice_item_count=len(inv.items),
        total_number_of_forms=1 + -(-max(len(inv.items) - 1, 0) // 3),
        calculated_goods_total=f"{inv.goods_total:.2f}",
        goods_currency=inv.currency,
        gross_weight=(f"{gw:.4f}" if gw is not None else ""),
        gross_weight_source=ship.selected_authority_type,
        total_packages=(f"{pk:.2f}" if pk is not None else ""),
        packages_source=ship.selected_authority_type,
        package_type=settings.package_type_default,
        package_type_name=PACKAGE_TYPES.get(settings.package_type_default, ""),
        reviewed_packing_weight_unit=unit or "KGM",
        weight_unit_evidence=ship.gross_weight_unit or "",
        weight_unit_missing=bool(unit_missing),
        manifest_no="",
        declaration_type=str(sel.get("declaration_type") or settings.declaration_type).upper(),
        gen_procedure_code=str(sel.get("gen_procedure_code")
                               or settings.declaration_gen_procedure_code),
        customs_office_code=office_code,
        customs_office_name=office_name,
        border_office_code=border_code,
        border_office_name=border_name,
        extended_customs_procedure=str(sel.get("extended_customs_procedure")
                                       or settings.extended_customs_procedure),
        national_customs_procedure=str(sel.get("national_customs_procedure")
                                       or settings.national_customs_procedure),
        border_mode=border_mode_sel,
        inland_mode_of_transport=inland_mode_sel,
        border_nationality=str(sel.get("border_nationality")
                               or settings.border_nationality).upper(),
        place_of_loading_code=str(sel.get("place_of_loading_code")
                                  or settings.place_of_loading_code).upper(),
        location_of_goods=str(sel.get("location_of_goods") or settings.location_of_goods),
        container_flag=bool(sel.get("container_flag", False)),
        regime_revision=regime_revision,
        invoice_numbers=[r.number for r in inv.invoice_refs],
        invoice_dates=[fmt_dd_mmm_yyyy(r.date) for r in inv.invoice_refs],
        invoice_roster=roster,
        field_9_invoice_transport_document=field9,
        exporter=exporter,
        importer=importer,
        importer_exim_valid=exim_valid,
        hawb_no=ship.hawb_number or "",
        mawb_no=ship.mawb_number or "",
        bill_of_lading_no=bl_no,
        bill_of_lading_date=fmt_dd_mmm_yyyy(bl_date_raw) if bl_date_raw else "",
        bill_of_lading_source=bl_source,
        transport_doc_type=doc_type,
        transport_doc_type_reason=doc_type_reason,
        shipment_authority_type=ship.selected_authority_type,
        shipment_candidates=candidates,
        gross_weight_source_doc=ship.selected_form_id or ship.selected_authority_type,
        package_count_source_doc=ship.selected_form_id or ship.selected_authority_type,
        mixed_source=False,
        gross_weight_authority=(ship.gross_weight_authority.as_payload()
                                if ship.gross_weight_authority else None),
        carton_authority=(ship.carton_authority.as_payload()
                          if ship.carton_authority else None),
        field_40_previous_document=field40,
        field_40_reason=field40_reason,
        field_40_candidates=field_40_candidates,
        incoterm=inv.incoterm or "",
        delivery_place=inv.incoterm_place or settings.place_of_loading_name,
        bank_code=banking.bank_code or "",
        bank_name=banking.bank_name or "",
        swift_code=(banking.swift_raw or "").strip().upper(),
        bank_resolution_state=banking.bank_resolution_state,
        payment_term_code=banking.terms_code or "",
        payment_term_description=banking.terms_description or "",
        payment_term_text=banking.payment_terms_raw or inv.payment_terms_raw or "",
        payment_resolution_state=banking.payment_resolution_state,
        mode_of_payment="CASH",
        bank_reference=banking.reference_number or (inv.lc_reference_raw or ""),
        bank_amount=(f"{banking.amount:.2f}" if banking.amount is not None else ""),
        bank_currency=(banking.currency or "").strip().upper(),
        bank_date=bank_date_norm,
        manual_freight_amount=(f"{freight_default:.2f}" if freight_default is not None else ""),
        manual_freight_currency=inv.currency,
        freight_source=freight_source,
        freight_candidates=[FreightCandidate.model_validate(c) for c in (freight_candidates or [])],
        manual_insurance_amount="",
        insurance_currency=settings.national_currency,
        evidence=_evidence(documents),
        warnings=warnings,
        packing_view=packing_view,
        item_details=item_details or [],
        item_mutation_revision=item_mutation_revision,
    )
    review.review_fingerprint = _fingerprint(review)
    return review


def _party(name, address, exim, country_raw, ref: ReferenceStore) -> PartyReview:
    code = ref.normalize_country(country_raw)
    state = "RESOLVED" if code else ("INVALID" if (country_raw or "").strip() else "ABSENT")
    return PartyReview(name=(name or "").strip(), address=(address or "").strip(),
                       exim_code=(exim or "").strip(), country_code=code or "",
                       country_resolution_state=state)


def _roster(inv: InvoiceAuthorityResult, documents: list | None) -> list[RosterEntry]:
    files: dict[str, tuple[str, list[int]]] = {}
    for d in documents or []:
        if getattr(d, "declared_role", "") == "INVOICE" and getattr(d, "raw_extraction", None):
            raw = d.raw_extraction or {}
            all_pages = raw.get("page_numbers") or []
            hdr = raw.get("header") or {}
            num = (hdr.get("invoice_number_raw") or "").strip()
            if num:
                files[num] = (d.original_file_name, all_pages)
            # multi-invoice attachment: each bundled invoice maps to its own
            # page span (start page → the page before the next invoice starts)
            subs = sorted((s for s in (raw.get("sub_invoices") or []) if s.get("first_page_no")),
                          key=lambda s: s["first_page_no"])
            for i, s in enumerate(subs):
                s_num = (s.get("invoice_number_raw") or "").strip()
                if not s_num:
                    continue
                start = s["first_page_no"]
                end = subs[i + 1]["first_page_no"] - 1 if i + 1 < len(subs) else (max(all_pages) if all_pages else start)
                span = [p for p in all_pages if start <= p <= end] or list(range(start, end + 1))
                files[s_num] = (d.original_file_name, span)
    return [RosterEntry(
        number=r.number, date=fmt_dd_mmm_yyyy(r.date), currency=r.currency or inv.currency,
        item_count=r.item_count, source_file=files.get(r.number, ("", []))[0],
        pages=files.get(r.number, ("", []))[1],
    ) for r in inv.invoice_refs]


def _evidence(documents: list | None) -> list[EvidenceItem]:
    """Provenance panel: prove the XML-driving values came from documents.
    Read from the stored raw extractions — never re-OCR."""
    out: list[EvidenceItem] = []
    for d in documents or []:
        raw = getattr(d, "raw_extraction", None)
        if not raw:
            continue
        role, fname = d.declared_role, d.original_file_name

        def add(field_name, value, page=None, quote=""):
            if value:
                out.append(EvidenceItem(field=field_name, normalized_value=str(value).strip(),
                                        raw_value=str(value).strip(), source_role=role,
                                        source_file=fname, page_number=page, quote=quote or ""))

        if role == "INVOICE":
            hdr = raw.get("header") or {}
            pages = raw.get("page_numbers") or []
            p0 = pages[0] if pages else None
            add("invoice_number", hdr.get("invoice_number_raw"), p0)
            add("invoice_date", hdr.get("invoice_date_raw"), p0)
            add("invoice_currency", hdr.get("currency_raw"), p0)
            add("incoterm", hdr.get("incoterm_raw"), p0)
            add("payment_terms", hdr.get("payment_terms_raw"), p0)
            # the two references the invoice is always searched for
            add("exporter_exim_code", (hdr.get("exporter") or {}).get("exim_code_raw"), p0)
            add("importer_exim_code", (hdr.get("consignee") or {}).get("exim_code_raw")
                or hdr.get("exim_code_raw"), p0)
            add("bill_of_lading_no", hdr.get("bill_of_lading_number_raw"), p0)
            add("bill_of_lading_date", hdr.get("bill_of_lading_date_raw"), p0)
            hdr_num = (hdr.get("invoice_number_raw") or "").strip()
            for s in raw.get("sub_invoices") or []:
                s_num = (s.get("invoice_number_raw") or "").strip()
                if not s_num or s_num == hdr_num:
                    continue  # first bundled invoice usually duplicates the header
                sp = s.get("first_page_no")
                add("invoice_number", s_num, sp)
                add("invoice_date", s.get("invoice_date_raw"), sp)
        elif role == "AIR_WAYBILL":
            for f in raw.get("forms") or []:
                pages = f.get("source_pages") or []
                p0 = pages[0] if pages else None
                ev = (f.get("evidence") or [{}])[0]
                add("hawb_no", f.get("hawb_number_raw"), p0, ev.get("quote", ""))
                add("mawb_no", f.get("mawb_number_raw"), p0, ev.get("quote", ""))
                add("bill_of_lading_no", f.get("bill_of_lading_number_raw"), p0, ev.get("quote", ""))
                add("bill_of_lading_date", f.get("bill_of_lading_date_raw"), p0, ev.get("quote", ""))
                gwr = (f.get("gross_weight") or {})
                add(f"gross_weight[{f.get('logical_form_id', '')}]",
                    f"{gwr.get('value_raw') or ''} {gwr.get('unit_raw') or ''}".strip(), p0)
        elif role == "BANKING":
            add("bank_swift", raw.get("bank_swift_raw") or raw.get("sender_bic_raw"))
            add("bank_name", raw.get("issuing_or_applicant_bank_name_raw"))
            add("bank_reference", raw.get("reference_number_raw"))
            add("bank_date", raw.get("issue_or_value_date_raw"))
            amt = raw.get("amount") or {}
            add("bank_amount", f"{amt.get('amount_raw') or ''} {amt.get('currency_raw') or ''}".strip())
            add("payment_terms", raw.get("payment_terms_raw"))
    return out


def _fingerprint(review: CriticalReview) -> str:
    basis = {
        "items": review.invoice_item_count,
        "total": review.calculated_goods_total,
        "currency": review.goods_currency,
        "invoices": [f"{n}|{d}" for n, d in zip(review.invoice_numbers, review.invoice_dates)],
        "gross": review.gross_weight, "packages": review.total_packages,
        "hawb": review.hawb_no, "mawb": review.mawb_no, "bl": review.bill_of_lading_no,
        "bl_date": review.bill_of_lading_date, "doc_type": review.transport_doc_type,
        "authority": review.shipment_authority_type,
        "bank": [review.bank_code, review.bank_name, review.swift_code],
        "terms": review.payment_term_code,
        "bank_ref": [review.bank_reference, review.bank_amount, review.bank_date],
        "field40": review.field_40_previous_document,
        "freight": review.manual_freight_amount,
        # reviewer add/delete revision: count/total-neutral swaps (delete a row,
        # add a manual one with the same price) must still stale old reviews
        "item_revision": review.item_mutation_revision,
        # regime-selection revision: an office/procedure change after this
        # review was computed must force a re-review, same as item mutations
        "regime_revision": review.regime_revision,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Merge (reviewer confirmation wins; blanks are explicit)
# --------------------------------------------------------------------------- #
def _doc_type_choice(review: CriticalReview, conf: CriticalReviewConfirmation) -> str:
    """AWB / BL for the merged values: the reviewer's explicit pick, else the
    deterministic rule applied to the reviewed numbers."""
    explicit = (conf.transport_doc_type or "").strip().upper()
    if explicit in ("AWB", "BL"):
        return explicit
    mawb = review.mawb_no if conf.mawb_no is None else conf.mawb_no.strip()
    hawb = review.hawb_no if conf.hawb_no is None else conf.hawb_no.strip()
    bl = (review.bill_of_lading_no if conf.bill_of_lading_no is None
          else conf.bill_of_lading_no.strip())
    return transport_doc_type(mawb, hawb, bl, review.shipment_authority_type)


def merge_confirmation(review: CriticalReview, conf: CriticalReviewConfirmation
                       ) -> tuple[ReviewedValues, bool]:
    """Return (reviewed values, item_count_mismatch)."""
    def pick(conf_val: str | None, default: str) -> str:
        return default if conf_val is None else str(conf_val).strip()

    mismatch = conf.reported_item_count is not None and conf.reported_item_count != review.invoice_item_count

    unit = (pick(conf.reviewed_packing_weight_unit, review.reviewed_packing_weight_unit) or "KGM").upper()
    factor = WEIGHT_UNIT_TO_KG.get(unit, Decimal("1"))
    gross_raw = parse_decimal(pick(conf.confirmed_gross_weight, review.gross_weight) or "0") or Decimal("0")
    packages = parse_decimal(pick(conf.confirmed_total_packages, review.total_packages) or "0") or Decimal("0")

    pkg_code = (pick(conf.package_type, review.package_type) or DEFAULT_PACKAGE_TYPE).upper()

    # ---- regime & offices -------------------------------------------------- #
    # Office NAMES are derived from the reference by code — the client never
    # dictates a name for a code it did not change (explicit name only as the
    # escape hatch for a code missing from the reference).
    from ..reference.store import get_reference   # no cycle: store imports config only
    ref = get_reference()
    office_code = (pick(conf.customs_office_code, review.customs_office_code)).upper()
    if conf.customs_office_name is not None:
        office_name = conf.customs_office_name.strip()
    elif office_code == review.customs_office_code:
        office_name = review.customs_office_name
    else:
        office_name = ref.office_by_code.get(office_code, "")
    # border office follows the clearance office unless explicitly different;
    # an explicit blank means "same as clearance"
    default_border = review.border_office_code or review.customs_office_code
    if default_border == review.customs_office_code:
        default_border = office_code
    border_code = (default_border if conf.border_office_code is None
                   else (conf.border_office_code.strip().upper() or office_code))
    if conf.border_office_name is not None:
        border_name = conf.border_office_name.strip()
    elif border_code == office_code:
        border_name = office_name
    elif border_code == review.border_office_code:
        border_name = review.border_office_name
    else:
        border_name = ref.office_by_code.get(border_code, "")

    freight_txt = pick(conf.manual_freight_amount, review.manual_freight_amount)
    insurance_txt = pick(conf.manual_insurance_amount, review.manual_insurance_amount)
    bank_amount_txt = pick(conf.bank_amount, review.bank_amount)
    # strip XML-illegal control chars, then trim: a blank-after-clean override
    # degrades to deterministic recomposition
    _field9_clean = _XML_ILLEGAL_CTRL.sub("", conf.field_9_text or "").strip()

    values = ReviewedValues(
        gross_weight_kg=gross_raw * factor,
        total_packages=packages,
        weight_unit=unit,
        manual_shipment_authority_confirmed=bool(conf.manual_shipment_authority_confirmed),
        weight_unit_missing=review.weight_unit_missing,
        mixed_source_reason=(conf.mixed_source_reason or "").strip(),
        manifest_no=pick(conf.manifest_no, review.manifest_no),
        package_type_code=pkg_code,
        package_type_name=PACKAGE_TYPES.get(pkg_code, ""),
        hawb_no=pick(conf.hawb_no, review.hawb_no),
        mawb_no=pick(conf.mawb_no, review.mawb_no),
        bill_of_lading_no=pick(conf.bill_of_lading_no, review.bill_of_lading_no),
        bill_of_lading_date=pick(conf.bill_of_lading_date, review.bill_of_lading_date),
        # The reviewer's explicit choice is final; with none, the type is
        # re-derived from the numbers as they stand AFTER their own edits (a
        # reviewer who typed a B/L number over an empty AWB pair gets the B/L
        # Field 9 without having to say so twice).
        transport_doc_type=_doc_type_choice(review, conf),
        field_18=pick(conf.field_18_transport_identity, review.field_18_transport_identity),
        field_21=pick(conf.field_21_transport_identity, review.field_21_transport_identity),
        field_40=pick(conf.field_40_previous_document, review.field_40_previous_document),
        field_40_confirmed=bool(conf.field_40_confirmed),
        # Field 9: verbatim reviewer text only when explicitly overridden AND
        # non-empty AFTER stripping XML-illegal control chars (a PDF paste can
        # carry a form-feed/NUL that would crash serialization) — an accidental
        # blank falls back to recomposition so the MAWB/HAWB/invoice/LC
        # references are never silently dropped.
        field_9_text=_field9_clean,
        field_9_override=bool(conf.field_9_override) and bool(_field9_clean),
        shipment_authority_type=review.shipment_authority_type,
        declaration_type=pick(conf.declaration_type, review.declaration_type).upper(),
        gen_procedure_code=pick(conf.gen_procedure_code, review.gen_procedure_code),
        customs_office_code=office_code,
        customs_office_name=office_name,
        border_office_code=border_code,
        border_office_name=border_name,
        extended_customs_procedure=pick(conf.extended_customs_procedure,
                                        review.extended_customs_procedure),
        national_customs_procedure=pick(conf.national_customs_procedure,
                                        review.national_customs_procedure),
        border_mode=pick(conf.border_mode, review.border_mode),
        inland_mode_of_transport=pick(conf.inland_mode_of_transport,
                                      review.inland_mode_of_transport),
        border_nationality=pick(conf.border_nationality, review.border_nationality).upper(),
        place_of_loading_code=pick(conf.place_of_loading_code,
                                   review.place_of_loading_code).upper(),
        location_of_goods=pick(conf.location_of_goods, review.location_of_goods),
        container_flag=(review.container_flag if conf.container_flag is None
                        else bool(conf.container_flag)),
        exporter_name=pick(conf.exporter_name, review.exporter.name),
        exporter_address=pick(conf.exporter_address, review.exporter.address),
        exporter_exim=pick(conf.exporter_exim_code, review.exporter.exim_code),
        exporter_country=pick(conf.exporter_country_code, review.exporter.country_code).upper(),
        importer_name=pick(conf.importer_name, review.importer.name),
        importer_address=pick(conf.importer_address, review.importer.address),
        importer_exim=pick(conf.importer_exim_code, review.importer.exim_code),
        importer_country=pick(conf.importer_country_code, review.importer.country_code).upper(),
        incoterm=pick(conf.incoterm, review.incoterm),
        delivery_place=pick(conf.delivery_place, review.delivery_place),
        exclude_freight_insurance=bool(conf.invoice_values_exclude_freight_insurance),
        bank_code=pick(conf.bank_code, review.bank_code),
        bank_name=pick(conf.bank_name, review.bank_name),
        swift_code=pick(conf.swift_code, review.swift_code).upper(),
        terms_code=pick(conf.payment_term_code, review.payment_term_code),
        mode_of_payment=pick(conf.mode_of_payment, review.mode_of_payment) or "CASH",
        bank_reference=pick(conf.bank_reference, review.bank_reference),
        bank_amount=parse_decimal(bank_amount_txt) if bank_amount_txt else None,
        bank_currency=pick(conf.bank_currency, review.bank_currency).upper(),
        bank_date=pick(conf.bank_date, review.bank_date),
        freight_amount=parse_decimal(freight_txt) if freight_txt != "" else None,
        insurance_amount=parse_decimal(insurance_txt) or Decimal("0") if insurance_txt else Decimal("0"),
        invoice_refs=list(zip(review.invoice_numbers, review.invoice_dates)),
    )
    return values, mismatch
