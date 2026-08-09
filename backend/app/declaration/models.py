"""Canonical merged declaration.

The only input to final validation and the XML composer.  Fully independent of
model conversation history: authoritative values, item order, valuation, rule
versions and warnings.  All money is stored as strings already quantized to the
ASYCUDA presentation so serialization is a pure mapping.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.errors import ValidationMessage


class DeclarationItem(BaseModel):
    xml_item_sequence: int
    source_invoice_number: str
    source_invoice_date: str
    source_invoice_item_index: int

    commercial_description: str
    goods_description: str            # official HS description
    quantity: str
    invoice_uom: str

    # Per-item BRAND / MODEL / SIZE for the sibling .xls export (frozen onto the
    # declaration so the workbook can never drift from the XML it accompanies).
    # Export-only: these are NOT serialized into the ASYCUDA XML.  "NA" when a
    # value could not be resolved.
    brand: str = "NA"
    model: str = "NA"
    size: str = "NA"

    commodity_code: str               # first 8 digits
    precision_1: str                  # remaining 3 digits
    hs_code_11: str
    hs_source: str

    coo_alpha2: str
    coo_source: str

    gross_weight_kg: str
    net_weight_kg: str
    package_count: str

    supplementary_unit_code: str
    supplementary_unit_name: str
    supplementary_quantity: str

    item_price_foreign: str
    item_invoice_national: str
    item_invoice_foreign: str
    item_external_freight_national: str
    item_external_freight_foreign: str
    item_insurance_national: str
    item_insurance_foreign: str
    total_cost_itm: str
    total_cif_itm: str
    statistical_value: str
    alpha_coefficient: str
    value_item: str

    warnings: list[ValidationMessage] = Field(default_factory=list)


class DeclarationParties(BaseModel):
    exporter_name: str = ""                # name + newline + address (Field spec)
    exporter_code: str = ""                # exporter EXIM code
    exporter_country_code: str = ""
    consignee_name: str = ""               # name + newline + address
    consignee_code: str = ""               # importer EXIM (13–15 alphanumeric)
    consignee_country_code: str = ""
    financial_name: str = ""
    country_of_origin_name: str = ""
    trading_country: str = ""


class DeclarationValuation(BaseModel):
    exchange_rate: str
    currency_foreign: str
    total_invoice_foreign: str
    total_invoice_national: str
    external_freight_foreign: str
    external_freight_national: str
    insurance_national: str
    insurance_foreign: str
    total_cost: str
    total_cif: str
    total_weight: str
    value_details: str


class MergedDeclaration(BaseModel):
    declaration_id: str
    job_id: str
    version: int = 1
    schema_version: str = "decl-v1"
    rule_set_version: str = ""

    # header — regime identity (reviewer-selected per job, reference-gated)
    customs_office_code: str
    customs_office_name: str
    declaration_type: str
    gen_procedure_code: str
    # Property/Sad_flow: "E" for export-flow types (EX/PEX), else "I"
    sad_flow: str = "I"
    # Box 37 pair stamped on every item (per-item overrides are a later phase)
    extended_customs_procedure: str = "4000"
    national_customs_procedure: str = "000"
    manifest_number: str = ""
    total_number_of_items: int
    total_number_of_packages: str
    package_type_code: str = "CT"
    package_type_name: str = "Carton"

    # per-item allocation audit trail (spec 2026-07-17): matched packing name
    # and the source of every carton / gross / net value
    allocation_audit: list[dict] = Field(default_factory=list)

    parties: DeclarationParties
    bank_code: str = ""
    bank_name: str = ""
    terms_code: str = ""
    terms_description: str = ""
    mode_of_payment: str = "CASH"

    incoterm_code: str = ""
    incoterm_place: str = ""
    place_of_loading_code: str = ""
    # kept for the Delivery_terms/IncoTerms place fallback chain; the XML
    # Place_of_loading/Name itself is emitted EMPTY (ASYCUDA derives it from
    # the code on import — user rule 2026-08-01)
    place_of_loading_name: str = ""

    # transport (Fields 18/21 identities + reviewer-selected Box 25/26 modes).
    # border_mode has NO default: "" means the reviewer never chose one, which
    # the validator blocks (never silently 01).
    field_18_identity: str = ""
    field_21_identity: str = ""
    border_nationality: str = "NP"
    border_mode: str = ""
    inland_mode_of_transport: str = ""
    # border office defaults to the clearance office (differs only for cargo
    # moving between Nepali offices through India/China)
    border_office_code: str = ""
    border_office_name: str = ""
    location_of_goods: str = ""
    container_flag: bool = False

    hawb_number: str = ""
    mawb_number: str = ""
    # sea / land: the transport document this declaration travelled on
    bill_of_lading_number: str = ""
    bill_of_lading_date: str = ""
    transport_doc_type: str = "AWB"            # AWB | BL — drives Field 9's first line
    field_40_summary_declaration: str = ""     # reviewed Field 40, stamped on every item
    shipment_authority_type: str = ""
    field9_financial_name: str = ""
    first_item_previous_doc_ref: str = ""
    first_item_free_text_1: str = ""

    # review/audit metadata (never written to XML)
    weight_unit_reviewed: str = "KGM"
    exclude_freight_insurance_confirmed: bool = False
    mixed_source_reason: str = ""

    valuation: DeclarationValuation
    items: list[DeclarationItem]

    rule_versions: dict[str, str] = Field(default_factory=dict)
    warnings: list[ValidationMessage] = Field(default_factory=list)
    blocking_errors: list[ValidationMessage] = Field(default_factory=list)
    ready_for_xml: bool = False
    # warn-mode: XML was generated DESPITE blocking cases (ASYCUDA testing)
    xml_built_with_blockers: bool = False
