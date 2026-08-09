"""Deterministic ASYCUDA World SAD XML composer.

A pure serializer built on lxml.  It accepts a *validated* MergedDeclaration and
never calls OCR / Langroid / any LLM.  Structure and field mapping mirror the
reference sample: HS split into Commodity_code(8) + Precision_1(3), three
Supplementary_unit blocks (first populated), eight empty Taxation_line stubs,
the reviewed Field 40 as Summary_declaration on every item, and
Previous_document_reference / Free_text_1 populated on the first item only.
Every header identity value (manifest, Fields 18/21, package kind) comes from
the confirmed Critical Review; the Declarant block is always emitted empty.
XML escaping is the library's job.
"""
from __future__ import annotations

import hashlib
import math
import re

from lxml import etree

from ..declaration.models import MergedDeclaration

TEMPLATE_VERSION = "asycuda-np-sad-v1"

# XML 1.0 forbids most C0 control chars (only tab/newline/CR are legal); lxml
# raises ValueError on them.  Any reviewer-entered text (Field 9, party names,
# …) could carry one from a PDF copy-paste, so strip them here — a warn-mode
# declaration must always serialize, never 500.
#
# The class is the FULL set XML 1.0 rejects, not just C0: lone surrogates
# (U+D800-U+DFFF, which a str can hold after a surrogateescape decode of
# mis-encoded OCR text) and the two non-characters U+FFFE/U+FFFF raise the
# same ValueError.  Missing them meant a document carrying one turned finalize
# into a 500 instead of a declaration with a warning.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _xml_safe(text) -> str:
    return _XML_ILLEGAL.sub("", str(text))


def _el(parent, tag, text=None):
    e = etree.SubElement(parent, tag)
    if text is not None and text != "":
        e.text = _xml_safe(text)
    return e


def _null(parent, tag):
    """Emit ``<tag><null/></tag>`` as ASYCUDA expects for empty structured fields."""
    e = etree.SubElement(parent, tag)
    etree.SubElement(e, "null")
    return e


def _money(parent, tag, national, foreign):
    e = etree.SubElement(parent, tag)
    _el(e, "Amount_national_currency", national)
    _el(e, "Amount_foreign_currency", foreign)
    return e


def build_xml(decl: MergedDeclaration) -> bytes:
    root = etree.Element("ASYCUDA")

    # ---- Export_release -------------------------------------------------
    exr = _el(root, "Export_release")
    _el(exr, "Date_of_exit")
    _el(exr, "Time_of_exit")
    for t in ("Actual_office_of_exit_code", "Actual_office_of_exit_name", "Exit_reference", "Comments"):
        _null(exr, t)

    # ---- Property -------------------------------------------------------
    prop = _el(root, "Property")
    _el(prop, "Sad_flow", decl.sad_flow or "I")
    forms = _el(prop, "Forms")
    _el(forms, "Number_of_the_form", "1")
    # SAD layout: the main form carries 1 item, each continuation sheet 3 more.
    total_forms = 1 + math.ceil(max(len(decl.items) - 1, 0) / 3)
    _el(forms, "Total_number_of_forms", str(total_forms))
    nbers = _el(prop, "Nbers")
    _el(nbers, "Number_of_loading_lists")
    _el(nbers, "Total_number_of_items", str(decl.total_number_of_items))
    _el(nbers, "Total_number_of_packages", decl.total_number_of_packages)
    _null(prop, "Place_of_declaration")
    _el(prop, "Selected_page", "1")

    # ---- Identification -------------------------------------------------
    ident = _el(root, "Identification")
    office = _el(ident, "Office_segment")
    _el(office, "Customs_clearance_office_code", decl.customs_office_code)
    _el(office, "Customs_Clearance_office_name", decl.customs_office_name)
    typ = _el(ident, "Type")
    _el(typ, "Type_of_declaration", decl.declaration_type)
    _el(typ, "Declaration_gen_procedure_code", decl.gen_procedure_code)
    _el(typ, "Type_of_transit_document")
    # Reviewed manifest number (may legitimately be empty — warning, not blocker).
    _el(ident, "Manifest_reference_number", decl.manifest_number)

    # ---- Traders --------------------------------------------------------
    traders = _el(root, "Traders")
    exporter = _el(traders, "Exporter")
    _el(exporter, "Exporter_code", decl.parties.exporter_code)
    _el(exporter, "Exporter_name", decl.parties.exporter_name)
    consignee = _el(traders, "Consignee")
    _el(consignee, "Consignee_code", decl.parties.consignee_code)
    _el(consignee, "Consignee_name", decl.parties.consignee_name)
    fin = _el(traders, "Financial")
    _el(fin, "Financial_code")
    _el(fin, "Financial_name", decl.parties.financial_name)

    # ---- Declarant (intentionally left empty — no code/name/PAN/address) --
    declarant = _el(root, "Declarant")
    _el(declarant, "Declarant_code")
    _el(declarant, "Declarant_name")

    # ---- General_information -------------------------------------------
    gi = _el(root, "General_information")
    country = _el(gi, "Country")
    _el(country, "Country_first_destination", decl.parties.trading_country)
    _el(country, "Trading_country", decl.parties.trading_country)
    exp = _el(country, "Export")
    _el(exp, "Export_country_code", decl.parties.trading_country)
    _el(exp, "Export_country_name", decl.parties.country_of_origin_name)
    _el(country, "Country_of_origin_name", decl.parties.country_of_origin_name)
    _el(gi, "Value_details", decl.valuation.value_details)

    # ---- Transport ------------------------------------------------------
    transport = _el(root, "Transport")
    mot = _el(transport, "Means_of_transport")
    # Field 18 — identity at arrival/departure (reviewed)
    dai = _el(mot, "Departure_arrival_information")
    _el(dai, "Identity", decl.field_18_identity)
    _null(dai, "Nationality")
    # Field 21 — border identity (reviewed) + profile nationality/mode
    bi = _el(mot, "Border_information")
    _el(bi, "Identity", decl.field_21_identity)
    _el(bi, "Nationality", decl.border_nationality)
    _el(bi, "Mode", decl.border_mode)
    _el(mot, "Inland_mode_of_transport", decl.inland_mode_of_transport)
    _el(transport, "Container_flag", "true" if decl.container_flag else "false")
    dt = _el(transport, "Delivery_terms")
    _el(dt, "Code", decl.incoterm_code or "C&F")
    _el(dt, "Place", decl.incoterm_place or decl.place_of_loading_name)
    bo = _el(transport, "Border_office")
    _el(bo, "Code", decl.border_office_code or decl.customs_office_code)
    _el(bo, "Name", decl.border_office_name or decl.customs_office_name)
    pol = _el(transport, "Place_of_loading")
    _el(pol, "Code", decl.place_of_loading_code)
    # Name deliberately EMPTY (user rule 2026-08-01, README ADR-011): ASYCUDA
    # derives the name from the code on import; informational elements in the
    # SAD XML are discarded (UNCTAD XML SAD spec).
    _el(pol, "Name")
    _el(transport, "Location_of_goods", decl.location_of_goods)

    # ---- Financial ------------------------------------------------------
    financial = _el(root, "Financial")
    bank = _el(financial, "Bank")
    _el(bank, "Code", decl.bank_code)
    _el(bank, "Name", decl.bank_name)
    _el(bank, "Branch")
    _el(bank, "Reference")
    terms = _el(financial, "Terms")
    _el(terms, "Code", decl.terms_code)
    _el(terms, "Description", decl.terms_description)
    _el(financial, "Mode_of_payment", decl.mode_of_payment)

    # ---- Valuation (header) --------------------------------------------
    val = _el(root, "Valuation")
    _el(val, "Calculation_working_mode", "0")
    _el(_el(val, "Weight"), "Gross_weight", decl.valuation.total_weight)
    _el(val, "Total_cost", decl.valuation.total_cost)
    _el(val, "Total_CIF", decl.valuation.total_cif)
    _money(val, "Gs_Invoice", decl.valuation.total_invoice_national, decl.valuation.total_invoice_foreign)
    _money(val, "Gs_external_freight", decl.valuation.external_freight_national, decl.valuation.external_freight_foreign)
    _money(val, "Gs_internal_freight", "0.00", "0")
    _money(val, "Gs_insurance", decl.valuation.insurance_national, decl.valuation.insurance_foreign)
    _money(val, "Gs_other_cost", "0.00", "0.0")
    _money(val, "Gs_deduction", "0.00", "0.0")
    tot = _el(val, "Total")
    _el(tot, "Total_invoice", decl.valuation.total_invoice_foreign)
    _el(tot, "Total_weight", decl.valuation.total_weight)

    # ---- Items ----------------------------------------------------------
    for it in decl.items:
        _build_item(root, decl, it)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=False, pretty_print=True)


def _build_item(root, decl: MergedDeclaration, it) -> None:
    first = it.xml_item_sequence == 1
    item = _el(root, "Item")

    pkg = _el(item, "Packages")
    _el(pkg, "Number_of_packages", it.package_count)
    _el(pkg, "Marks1_of_packages")
    _el(pkg, "Marks2_of_packages")
    _el(pkg, "Kind_of_packages_code", decl.package_type_code)
    _el(pkg, "Kind_of_packages_name", decl.package_type_name)

    inc = _el(item, "IncoTerms")
    _el(inc, "Code", decl.incoterm_code or "C&F")
    _el(inc, "Place", decl.incoterm_place or decl.place_of_loading_name)

    tar = _el(item, "Tarification")
    _null(tar, "Tarification_data")
    hs = _el(tar, "HScode")
    _el(hs, "Commodity_code", it.commodity_code)
    _el(hs, "Precision_1", it.precision_1)
    for p in ("Precision_2", "Precision_3", "Precision_4"):
        _null(hs, p)
    _null(tar, "Preference_code")
    # Box 37 — per-item elements carrying the job-level reviewed pair (a
    # per-item override channel is a later phase; the XML shape is ready)
    _el(tar, "Extended_customs_procedure", decl.extended_customs_procedure)
    _el(tar, "National_customs_procedure", decl.national_customs_procedure)
    # three supplementary blocks; first populated
    s1 = _el(tar, "Supplementary_unit")
    _el(s1, "Suppplementary_unit_code", it.supplementary_unit_code)
    _el(s1, "Suppplementary_unit_name", it.supplementary_unit_name)
    _el(s1, "Suppplementary_unit_quantity", it.supplementary_quantity)
    for _ in range(2):
        s = _el(tar, "Supplementary_unit")
        _null(s, "Suppplementary_unit_code")
        _el(s, "Suppplementary_unit_name")
        _el(s, "Suppplementary_unit_quantity")
    _el(tar, "Item_price", it.item_price_foreign)
    _el(tar, "Valuation_method_code")
    _el(tar, "Value_item", it.value_item)

    gd = _el(item, "Goods_description")
    _el(gd, "Country_of_origin_code", it.coo_alpha2)
    _el(gd, "Description_of_goods", it.goods_description)
    _el(gd, "Commercial_Description", it.commercial_description)

    pd = _el(item, "Previous_doc")
    # Field 40 — the reviewed value, stamped identically on every item.
    _el(pd, "Summary_declaration", decl.field_40_summary_declaration)
    _null(pd, "Summary_declaration_sl")
    if first and decl.first_item_previous_doc_ref:
        _el(pd, "Previous_document_reference", decl.first_item_previous_doc_ref)
    else:
        _el(pd, "Previous_document_reference")
    _null(pd, "Previous_warehouse_code")

    _null(item, "Licence_number")
    if first and decl.first_item_free_text_1:
        _el(item, "Free_text_1", decl.first_item_free_text_1)
    else:
        _el(item, "Free_text_1")
    _el(item, "Free_text_2")

    tax = _el(item, "Taxation")
    for _ in range(8):
        tl = _el(tax, "Taxation_line")
        _null(tl, "Duty_tax_code")
        _el(tl, "Duty_tax_Base")
        _el(tl, "Duty_tax_rate")
        _el(tl, "Duty_tax_amount")

    vi = _el(item, "Valuation_item")
    w = _el(vi, "Weight_itm")
    _el(w, "Gross_weight_itm", it.gross_weight_kg)
    _el(w, "Net_weight_itm", it.net_weight_kg)
    _el(vi, "Total_cost_itm", it.total_cost_itm)
    _el(vi, "Total_CIF_itm", it.total_cif_itm)
    _el(vi, "Rate_of_adjustement", "1")
    _el(vi, "Statistical_value", it.statistical_value)
    _el(vi, "Alpha_coeficient_of_apportionment", it.alpha_coefficient)
    _money(vi, "Item_Invoice", it.item_invoice_national, it.item_invoice_foreign)
    _money(vi, "item_external_freight", it.item_external_freight_national, it.item_external_freight_foreign)
    _money(vi, "item_internal_freight", "0.00", "0.00")
    _money(vi, "item_insurance", it.item_insurance_national, it.item_insurance_foreign)
    _money(vi, "item_other_cost", "0.00", "0.00")
    _money(vi, "item_deduction", "0.00", "0.00")


def checksum(xml_bytes: bytes) -> str:
    return hashlib.sha256(xml_bytes).hexdigest()
