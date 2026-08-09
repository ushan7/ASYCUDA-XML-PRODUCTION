"""Raw extraction schema — evidence-backed *facts*, never decisions.

These Pydantic models are the single contract between the (LLM/offline)
extractor and the deterministic rule services.  Every numeric value is a raw
string; conversion to Decimal happens later.  Nothing here is a final customs
value (no resolved HS11, COO, gross-weight authority, bank code or XML).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.enums import DeclaredRole, RowClassification

_QUOTE_MAX = 500


class Evidence(BaseModel):
    page_no: int = Field(ge=1)
    label: str | None = None
    quote: str = Field(min_length=1, max_length=_QUOTE_MAX,
                       description="The SHORTEST distinctive fragment of the OCR line that proves "
                                   "the value — about 12 words, never a whole paragraph.")
    block_id: str | None = None

    @field_validator("quote", mode="before")
    @classmethod
    def _truncate_quote(cls, v):
        """An over-long quote is TRIMMED, never rejected.

        `max_length` alone turned a model that quoted a whole table row into a
        schema ValidationError, which costs a full repair round — a resend of
        every row in the window — to fix a value that was already correct.
        Downstream only ever matches the quote's first 40 characters against the
        OCR, so the prefix carries all the evidence the check uses.
        """
        return v[:_QUOTE_MAX] if isinstance(v, str) and len(v) > _QUOTE_MAX else v


class RoleValidation(BaseModel):
    expected_role: DeclaredRole
    matches_expected_role: bool = True
    detected_title_raw: str | None = None
    detected_document_kind: str | None = None
    reason: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class RawNumber(BaseModel):
    value_raw: str | None = None
    unit_raw: str | None = None
    evidence: Evidence | None = None


class RawMoney(BaseModel):
    amount_raw: str | None = None
    currency_raw: str | None = None
    evidence: Evidence | None = None


class PartyRaw(BaseModel):
    name_raw: str | None = None
    address_raw: str | None = None
    country_raw: str | None = None
    pan_raw: str | None = None
    exim_code_raw: str | None = Field(
        default=None,
        description="THIS party's printed EXIM / IEC code, verbatim — the number printed "
                    "against a label such as EXIM CODE / EXIM NO / EXIM REGD NO / IEC / "
                    "IE CODE / Importer-Exporter Code / IEC (PAN based). Never the party's "
                    "PAN/VAT/GST/TIN registration number (that goes in pan_raw), never a "
                    "phone or invoice number.")


# --------------------------------------------------------------------------- #
# Invoice
# --------------------------------------------------------------------------- #
class InvoiceHeaderRaw(BaseModel):
    document_title_raw: str | None = None
    invoice_kind_raw: str | None = None          # FINAL / PROFORMA / CHARGE / COPY ...
    invoice_number_raw: str | None = None
    invoice_date_raw: str | None = None
    exporter: PartyRaw | None = None
    consignee: PartyRaw | None = None
    currency_raw: str | None = None
    incoterm_raw: str | None = None
    incoterm_place_raw: str | None = None
    payment_terms_raw: str | None = None
    lc_reference_raw: str | None = None
    # An invoice almost always prints the EXIM code of at least one party, and
    # very often prints the transport-document reference of the shipment it
    # belongs to — both are declaration values the transport document may not
    # supply (or may not have been uploaded at all).
    exim_code_raw: str | None = Field(
        default=None,
        description="An EXIM / IEC code printed on the invoice that is NOT inside either "
                    "party's address block (e.g. a footer line 'EXIM NO: 1234567890123'). "
                    "When the code prints inside a party block, put it in that party's "
                    "exim_code_raw instead and leave this null.")
    bill_of_lading_number_raw: str | None = Field(
        default=None,
        description="The bill-of-lading number printed on the invoice, verbatim — the value "
                    "against B/L NO / BL NO / BILL OF LADING NO / MBL / HBL / OBL, or a "
                    "land-transport consignment note / lorry receipt (LR) / railway receipt "
                    "(RR) number. Never an air waybill number, never the invoice number.")
    bill_of_lading_date_raw: str | None = Field(
        default=None,
        description="The bill-of-lading date printed on the invoice (B/L DATE / BL DT / "
                    "date of the consignment note), verbatim. Never the invoice date.")


class InvoiceLineRaw(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    source_page_no: int = Field(ge=1)
    source_row_index: int = Field(ge=1)
    line_no_raw: str | None = Field(
        default=None,
        description="The row's printed LINE/serial number from the line-number column "
                    "('10', '140') — never a product/catalogue code.")
    description_raw: str = Field(
        description="The printed goods NAME/description ONLY. Never append batch/lot "
                    "numbers, expiry dates, quantity echoes ('3 EA'), origin statements "
                    "('COO: Ireland') or tariff codes — each belongs in its own field. "
                    "Intrinsic attributes printed as part of the name (material, "
                    "dimensions, pack size like '(10X10)') stay.")
    description_printed_raw: str | None = Field(
        default=None,
        description="Set ONLY by the extraction layer, never by a model: the row's own "
                    "ORIGINAL printed description cell, kept when cross-row attribution "
                    "moved this row's name out of a neighbour's cell. Packing-list "
                    "matching keys on the text the vendor actually printed on this row, "
                    "so it reads this when present.")
    brand_raw: str | None = Field(
        default=None,
        description="The row's printed brand/trademark/manufacturer NAME, when one is "
                    "printed. Never invented, never the exporter's company name.")
    model_raw: str | None = Field(
        default=None,
        description="The row's model/part/catalogue/style/material code exactly as printed "
                    "('RSINT25012X', '01E3120'). When a cell prints both a catalogue code "
                    "and a 13-14 digit GTIN/EAN barcode number, this is the catalogue "
                    "code — never the barcode.")
    size_raw: str | None = Field(
        default=None,
        description="The row's printed size/dimension/capacity specification ('2.50X12', "
                    "'500ML', 'EU 42'). Never a quantity or packaging count.")
    quantity_raw: str | None = None
    uom_raw: str | None = None
    unit_price_raw: str | None = Field(
        default=None,
        description="Per-UNIT price/rate only (columns like Unit Price / Rate / "
                    "Price per unit). Null if the row prints no per-unit rate.")
    line_total_raw: str | None = Field(
        default=None,
        description="The row's TOTAL/EXTENDED amount exactly as printed, keeping "
                    "the digits verbatim (e.g. 'Rs. 1,234.00', '75,000/-'). This "
                    "is the Amount / Total / Total Amt in INR / Value / Assessable "
                    "Value / Ext. Value column — NOT the per-unit rate. Every "
                    "goods row has this; never leave it null when a line amount "
                    "is printed, and never put the per-unit rate here.")
    currency_raw: str | None = None
    hs_code_raw: str | None = None
    country_of_origin_raw: str | None = Field(
        default=None,
        description="THIS row's own printed country of origin, VERBATIM — from an "
                    "Origin/COO/'Made in' column, label or continuation sub-line, as a "
                    "full name or code exactly as printed ('Ireland', 'JP', 'Made in "
                    "China' -> 'China'). Never derived from the exporter's address; a "
                    "document-level origin clause applies only when the document prints "
                    "no per-row origin.")
    # Per-row annotations that vendors fold into the description cell or print
    # as continuation sub-lines.  Their schema home — so the LLM has somewhere
    # to put them OTHER than description_raw (the live misallocation).
    batch_no_raw: str | None = Field(
        default=None, description="The row's printed batch number, if any ('0013032995').")
    lot_no_raw: str | None = Field(
        default=None, description="The row's printed lot number, if any.")
    serial_no_raw: str | None = Field(
        default=None, description="The row's printed serial number, if any.")
    expiry_date_raw: str | None = Field(
        default=None, description="The row's printed expiry date, if any, verbatim.")
    item_weight_raw: str | None = None
    item_weight_unit_raw: str | None = None
    item_weight_scope: Literal["PER_UNIT", "LINE_TOTAL", "UNKNOWN"] = "UNKNOWN"
    row_classification: RowClassification = RowClassification.REAL_GOODS_ITEM
    evidence: list[Evidence] = Field(default_factory=list)


class InvoiceTotalsRaw(BaseModel):
    goods_subtotal_raw: str | None = None
    freight_raw: str | None = None
    insurance_raw: str | None = None
    other_charges_raw: str | None = None
    discount_raw: str | None = None
    grand_total_raw: str | None = None


class SubInvoiceRaw(BaseModel):
    """One printed invoice document inside a multi-invoice upload.

    A single INVOICE attachment may bundle several distinct invoices (each with
    its own number, date and totals).  Goods rows are NOT nested here — they
    stay in the flat top-level ``rows`` list in printed order; a row belongs to
    the sub-invoice whose ``first_page_no`` most recently precedes the row's
    ``source_page_no``.
    """

    invoice_number_raw: str | None = Field(
        default=None, description="This invoice's own printed invoice number.")
    invoice_date_raw: str | None = None
    invoice_kind_raw: str | None = None          # FINAL / PROFORMA / CHARGE / COPY ...
    currency_raw: str | None = None
    first_page_no: int | None = Field(
        default=None, ge=1,
        description="Page where THIS invoice's header/title block prints (the page the "
                    "invoice starts on). Null only when the header page is not in scope.")
    totals: InvoiceTotalsRaw | None = Field(
        default=None, description="THIS invoice's own printed totals (never another invoice's).")
    evidence: list[Evidence] = Field(default_factory=list)


class InvoiceChunkRaw(BaseModel):
    role_validation: RoleValidation
    page_numbers: list[int] = Field(default_factory=list)
    header: InvoiceHeaderRaw | None = None
    rows: list[InvoiceLineRaw] = Field(default_factory=list)
    totals: InvoiceTotalsRaw | None = Field(
        default=None,
        description="Totals printed for the WHOLE document. When the upload bundles several "
                    "invoices, use sub_invoices[*].totals for each invoice's own totals and "
                    "leave this null unless a combined grand total is explicitly printed.")
    sub_invoices: list[SubInvoiceRaw] = Field(
        default_factory=list,
        description="One entry per distinct printed invoice document in the upload, in page "
                    "order. Single-invoice uploads may leave this empty. Never merge "
                    "different invoices' numbers/dates/totals into one entry.")
    page_complete: bool = True
    warnings: list[str] = Field(default_factory=list)
    # Stamped deterministically by the extraction service (never by the LLM):
    # per-page numeric-locale hints ("EU"/"US") from numbers.detect_numeric_locale.
    page_numeric_locales: dict[int, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Packing list
# --------------------------------------------------------------------------- #
class DimensionRaw(BaseModel):
    """A printed package dimension block (per row, or per package group)."""

    length_raw: str | None = None
    width_raw: str | None = None
    height_raw: str | None = None
    unit_raw: str | None = Field(default=None, description="CM / MM / IN / M, exactly as printed.")
    volume_cbm_raw: str | None = None
    package_count_raw: str | None = None


class PackingRowRaw(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    source_page_no: int = Field(ge=1)
    source_row_index: int = Field(ge=1)
    line_no_raw: str | None = None
    description_raw: str = Field(
        description="The printed goods NAME/description ONLY — batch/lot/serial numbers, "
                    "dates, quantity echoes and origin statements belong in their own "
                    "fields, never appended here.")
    brand_raw: str | None = Field(
        default=None,
        description="The row's printed brand/trademark name, when one is printed.")
    model_raw: str | None = Field(
        default=None,
        description="The row's model/part/catalogue code as printed — never a 13-14 digit "
                    "GTIN/EAN barcode number.")
    size_raw: str | None = Field(
        default=None,
        description="The row's printed size/dimension/capacity specification. Never a "
                    "quantity or packaging count.")
    item_code_raw: str | None = Field(
        default=None, description="The row's own product/part/material/catalogue code, if printed.")
    quantity_raw: str | None = None
    uom_raw: str | None = None
    hs_code_raw: str | None = None
    country_of_origin_raw: str | None = Field(
        default=None,
        description="THIS row's own printed country of origin, verbatim (full name or "
                    "code exactly as printed). Never derived from the exporter's address.")
    # ---- the reason a packing list exists: per-row weights and packages ----- #
    gross_weight: RawNumber | None = Field(
        default=None,
        description="THIS row's own gross weight (G.W. / GROSS WT / GRS WT / BRUTTO) exactly as "
                    "printed, with unit_raw set to the printed unit (KG, KGS, G, LBS). Never the "
                    "document's total row, never the net weight.")
    net_weight: RawNumber | None = Field(
        default=None,
        description="THIS row's own net weight (N.W. / NET WT / NETT / NETTO) exactly as printed, "
                    "with unit_raw set to the printed unit. Never the gross weight.")
    declared_weight: RawNumber | None = Field(
        default=None,
        description="Use ONLY when the row prints a single weight column that is not labelled "
                    "gross or net; set weight_type_raw to say which it is (UNKNOWN if the "
                    "document does not say). Never duplicate a value already in gross_weight or "
                    "net_weight here.")
    weight_type_raw: Literal["GROSS", "NET", "UNKNOWN"] | None = Field(
        default=None, description="What declared_weight is, when the document labels it.")
    carton_count: RawNumber | None = Field(
        default=None,
        description="HOW MANY cartons/packages this row occupies — a COUNT, never a carton "
                    "NUMBER. A row marked 'C/NO 1-5' occupies 5 cartons; a row marked 'CTN 7' "
                    "occupies 1. Fractional values are allowed when a carton holds several rows.")
    carton_no_raw: str | None = Field(
        default=None,
        description="The carton NUMBER or range printed against the row ('7', '1-5', 'C/NO 12-18'), "
                    "copied verbatim. This is an identifier, not a count.")
    shared_carton_group_raw: str | None = Field(
        default=None,
        description="Set on EVERY row that shares one carton (or one carton range) with other "
                    "rows, to the same value for all of them — normally the printed carton number "
                    "or range ('1-5'). Rows in a shared group must not each claim the group's "
                    "whole carton count as their own.")
    package_type_raw: str | None = Field(
        default=None, description="CTN / BOX / PALLET / BAG / DRUM, as printed for this row.")
    batch_no_raw: str | None = None
    lot_no_raw: str | None = None
    serial_no_raw: str | None = None
    manufacture_date_raw: str | None = None
    expiry_date_raw: str | None = None
    dimension: DimensionRaw | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class PackingListChunkRaw(BaseModel):
    role_validation: RoleValidation
    packing_list_number_raw: str | None = None
    packing_list_date_raw: str | None = None
    invoice_references_raw: list[str] = Field(default_factory=list)
    invoice_date_raw: str | None = None
    lc_reference_raw: str | None = None
    lc_date_raw: str | None = None
    exporter: PartyRaw | None = None
    importer: PartyRaw | None = None
    country_of_final_destination_raw: str | None = None
    rows: list[PackingRowRaw] = Field(default_factory=list)
    total_gross_weight: RawNumber | None = Field(
        default=None, description="The document's printed TOTAL gross weight, with its unit.")
    total_net_weight: RawNumber | None = Field(
        default=None, description="The document's printed TOTAL net weight, with its unit.")
    total_packages: RawNumber | None = Field(
        default=None,
        description="The document's printed TOTAL number of packages/cartons, with the printed "
                    "package word (CTN/BOX/PALLET) in unit_raw.")
    total_quantity: RawNumber | None = Field(
        default=None, description="The document's printed TOTAL goods quantity, with its UOM.")
    total_volume: RawNumber | None = Field(
        default=None, description="The document's printed total volume (CBM/m3), if any.")
    dimensions: list[DimensionRaw] = Field(
        default_factory=list,
        description="Package dimension blocks printed for the shipment as a whole, if any.")
    page_complete: bool = True
    warnings: list[str] = Field(default_factory=list)
    # Stamped deterministically by the extraction service (never by the LLM).
    page_numeric_locales: dict[int, str] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Air waybill
# --------------------------------------------------------------------------- #
class AirWaybillFormRaw(BaseModel):
    logical_form_id: str
    source_pages: list[int] = Field(default_factory=list)
    document_title_raw: str | None = None
    document_kind_raw: str | None = Field(
        default=None,
        description="Kind of THIS form's own document, judged only from its own pages: "
                    "MASTER_AIR_WAYBILL, HOUSE_AIR_WAYBILL, DELIVERY_ORDER, CARGO_TRACKING, "
                    "BILL_OF_LADING (sea/land: bill of lading, sea waybill, consignment note, "
                    "lorry receipt, railway receipt) or OTHER.")
    primary_awb_number_raw: str | None = None
    hawb_number_raw: str | None = None
    mawb_number_raw: str | None = None
    # Sea and land shipments arrive with a bill of lading instead of an air
    # waybill; it carries the same shipment authority (gross weight, packages)
    # and its number/date is what Field 9 prints for such a shipment.
    bill_of_lading_number_raw: str | None = Field(
        default=None,
        description="THIS form's own bill-of-lading number, verbatim (B/L NO / BILL OF LADING "
                    "NO / MBL / HBL / OBL, or a land consignment-note / lorry-receipt / "
                    "railway-receipt number). Never an air waybill number.")
    bill_of_lading_date_raw: str | None = Field(
        default=None,
        description="THIS form's own bill-of-lading date, verbatim — the issue date (or "
                    "'SHIPPED ON BOARD' date when that is the only date printed).")
    issuer_raw: str | None = None
    carrier_raw: str | None = None
    shipper: PartyRaw | None = None
    consignee: PartyRaw | None = None
    origin_airport_raw: str | None = None
    destination_airport_raw: str | None = None
    flight_number_raw: str | None = None
    gross_weight: RawNumber | None = None
    chargeable_weight: RawNumber | None = None
    pieces_or_packages: RawNumber | None = None
    package_source_label_raw: str | None = None
    # ---- charge boxes ------------------------------------------------------ #
    # An IATA air waybill prints the freight in SEVERAL boxes and only the
    # bottom "Total Prepaid" / "Total Collect" box is the whole freight for the
    # waybill.  Extract each box separately so the deterministic layer never has
    # to guess which one it was handed.
    freight_amount: RawMoney | None = Field(
        default=None,
        description="The air waybill's GRAND TOTAL charge: the amount printed in the 'Total "
                    "Prepaid' box, or in 'Total Collect' when the shipment is freight collect. "
                    "That box already includes the weight charge PLUS valuation charge, tax and "
                    "every 'Other Charges' line (AWC, MYC, SCC, fuel/security surcharges). NEVER "
                    "put the rate line's 'Total' column or the 'Weight Charge' box here when a "
                    "Total Prepaid/Collect box is printed — those exclude the other charges and "
                    "understate the freight.")
    total_prepaid: RawMoney | None = Field(
        default=None, description="The 'Total Prepaid' box, exactly as printed. Null if blank.")
    total_collect: RawMoney | None = Field(
        default=None, description="The 'Total Collect' box, exactly as printed. Null if blank.")
    weight_charge: RawMoney | None = Field(
        default=None,
        description="The 'Weight Charge' box alone (= chargeable weight x rate, i.e. the rate "
                    "line's 'Total' column), excluding other charges. Null if blank.")
    valuation_charge: RawMoney | None = Field(
        default=None, description="The 'Valuation Charge' box alone. Null if blank.")
    tax_charge: RawMoney | None = Field(
        default=None, description="The 'Tax' box alone. Null if blank.")
    other_charges_total: RawMoney | None = Field(
        default=None,
        description="'Total Other Charges Due Agent' plus 'Total Other Charges Due Carrier' — the "
                    "AWC/MYC/SCC/fuel-surcharge lines. If both boxes are printed, give their sum. "
                    "Null if blank.")
    freight_payment_status_raw: str | None = None
    invoice_references_raw: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)


class AirWaybillExtractionRaw(BaseModel):
    role_validation: RoleValidation
    forms: list[AirWaybillFormRaw] = Field(
        default_factory=list,
        description="One form per distinct transport document in the upload: a master air waybill, "
                    "a house air waybill, a bill of lading, a delivery order and a tracking page "
                    "each get their OWN "
                    "form. Never merge different documents into one form; every weight/pieces value "
                    "must come from that form's own pages (a delivery-order page's 'No. of pcs' and "
                    "'weight' belong to the delivery-order form only).")
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Banking
# --------------------------------------------------------------------------- #
class BankingExtractionRaw(BaseModel):
    role_validation: RoleValidation
    document_kind_raw: str | None = None
    swift_message_type_raw: str | None = None
    sender_bic_raw: str | None = None
    receiver_bic_raw: str | None = None
    issuing_or_applicant_bank_name_raw: str | None = None
    bank_swift_raw: str | None = None
    reference_number_raw: str | None = None
    issue_or_value_date_raw: str | None = None
    amount: RawMoney | None = None
    applicant: PartyRaw | None = None
    beneficiary: PartyRaw | None = None
    payment_terms_raw: str | None = None
    draft_tenor_raw: str | None = None
    invoice_references_raw: list[str] = Field(default_factory=list)
    freight_mentions: list[RawMoney] = Field(default_factory=list)
    exchange_rate_mentions_raw: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Insurance (optional)
# --------------------------------------------------------------------------- #
class InsuranceExtractionRaw(BaseModel):
    role_validation: RoleValidation
    policy_number_raw: str | None = None
    insurer_raw: str | None = None
    invoice_value: RawMoney | None = None
    sum_insured: RawMoney | None = None
    incidental_cost: RawMoney | None = None
    premium: RawMoney | None = None
    exchange_rate_raw: str | None = None
    invoice_references_raw: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


ROLE_TO_MODEL = {
    DeclaredRole.INVOICE: InvoiceChunkRaw,
    DeclaredRole.PACKING_LIST: PackingListChunkRaw,
    DeclaredRole.AIR_WAYBILL: AirWaybillExtractionRaw,
    DeclaredRole.BANKING: BankingExtractionRaw,
    DeclaredRole.INSURANCE: InsuranceExtractionRaw,
}
