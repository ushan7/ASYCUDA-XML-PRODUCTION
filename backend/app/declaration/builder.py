"""Merged-declaration builder — the ASYCUDA valuation model.

    item_invoice_national     = line_total(foreign) x rate
    item_external_freight_nat = item_freight(foreign) x rate
    Total_cost_itm            = ext_freight_nat + insurance_nat
    Total_CIF_itm             = item_invoice_national + Total_cost_itm
    Statistical_value         = Total_CIF_itm
    Alpha_coefficient         = Total_CIF_itm / Total_CIF
    Value_item                = "extF+intF+ins+other-ded" (national, thousands)

Header identity/party/banking values come from the reviewer-confirmed
:class:`~app.review.critical_review.ReviewedValues` — the Critical Review is
the single authority for everything the XML header carries.  Field 9 and the
first-item banking texts are composed here in the locked reference-XML style.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from ..config import get_settings
from ..dates import fmt_dd_mm_yyyy
from ..numbers import fmt2, fmt_thousands2, q2, q4, trim_min1
from ..reference.store import get_reference
from ..review.critical_review import (
    ReviewedValues,
    compose_field9,
    compose_first_item_texts,
)
from ..rules.models import (
    BankingResolution,
    FreightResult,
    InvoiceAuthorityResult,
    ShipmentAuthority,
    WorkItem,
)
from .models import (
    DeclarationItem,
    DeclarationParties,
    DeclarationValuation,
    MergedDeclaration,
)


def _fmt_packages(v: Decimal) -> str:
    return str(int(v)) if v == v.to_integral_value() else f"{q2(v):.2f}"


def _name_with_address(name: str, address: str) -> str:
    return f"{name}\n{address}" if name and address else (name or address or "")


def build_declaration(
    *,
    job_id: str,
    inv: InvoiceAuthorityResult,
    ship: ShipmentAuthority,
    banking: BankingResolution,
    items: list[WorkItem],
    freight: FreightResult,
    reviewed: ReviewedValues,
    exchange_rate: Decimal,
) -> MergedDeclaration:
    settings = get_settings()
    ref = get_reference()
    rate = exchange_rate
    gross_weight = reviewed.gross_weight_kg
    total_packages = reviewed.total_packages
    insurance_national = q2(reviewed.insurance_amount)

    total_invoice_foreign = inv.goods_total
    total_invoice_national = q2(total_invoice_foreign * rate)
    ext_freight_foreign = freight.effective_freight_foreign
    ext_freight_national = q2(ext_freight_foreign * rate)
    total_cost = q2(ext_freight_national + insurance_national)
    total_cif = q2(total_invoice_national + total_cost)

    decl_items: list[DeclarationItem] = []
    for it in items:
        inv_foreign = q2(it.line_total)
        inv_national = q2(it.line_total * rate)
        extf_foreign = q2(it.item_external_freight)
        extf_national = q2(it.item_external_freight * rate)
        ins_national = q2(it.item_insurance)
        total_cost_itm = q2(extf_national + ins_national)
        total_cif_itm = q2(inv_national + total_cost_itm)
        alpha = (total_cif_itm / total_cif) if total_cif > 0 else Decimal("0")
        value_item = f"{fmt_thousands2(extf_national)}+0.00+{fmt_thousands2(ins_national)}+0.00-0.00"

        hs = it.final_hs_code_11 or ""
        decl_items.append(DeclarationItem(
            xml_item_sequence=it.xml_item_sequence,
            source_invoice_number=it.source_invoice_number,
            source_invoice_date=it.source_invoice_date,
            source_invoice_item_index=it.source_invoice_item_index,
            commercial_description=it.commercial_description,
            goods_description=it.hs_official_description or it.description_raw,
            quantity=f"{it.quantity.normalize():f}",
            invoice_uom=it.invoice_uom_raw,
            brand=it.brand or "NA",
            model=it.model or "NA",
            size=it.size or "NA",
            commodity_code=hs[:8],
            precision_1=hs[8:11] or "000",
            hs_code_11=hs,
            hs_source=it.hs_source or "",
            coo_alpha2=it.coo_alpha2 or "",
            coo_source=it.coo_source or "",
            gross_weight_kg=trim_min1(q4(it.gross_weight_kg or Decimal(0))),
            net_weight_kg=trim_min1(q4(it.net_weight_kg or Decimal(0))),
            package_count=f"{q2(it.package_count or Decimal(0)):.2f}",
            supplementary_unit_code=it.supplementary_unit_code or "UNT",
            supplementary_unit_name=it.supplementary_unit_name or "Unit",
            supplementary_quantity=trim_min1(it.supplementary_quantity or Decimal(0)),
            item_price_foreign=fmt2(inv_foreign),
            item_invoice_national=fmt2(inv_national),
            item_invoice_foreign=fmt2(inv_foreign),
            item_external_freight_national=fmt2(extf_national),
            item_external_freight_foreign=fmt2(extf_foreign),
            item_insurance_national=fmt2(ins_national),
            item_insurance_foreign=fmt2(ins_national),
            total_cost_itm=fmt2(total_cost_itm),
            total_cif_itm=fmt2(total_cif_itm),
            statistical_value=f"{total_cif_itm:.6f}",
            alpha_coefficient=f"{alpha:.15f}",
            value_item=value_item,
            warnings=it.warnings,
        ))

    # Export country: reviewed exporter country wins; first item COO is the
    # controlled fallback (visible via the review's resolution state).
    export_cc = reviewed.exporter_country or _first_coo_alpha2(items)
    parties = DeclarationParties(
        exporter_name=_name_with_address(reviewed.exporter_name, reviewed.exporter_address),
        exporter_code=reviewed.exporter_exim,
        exporter_country_code=export_cc,
        consignee_name=_name_with_address(reviewed.importer_name, reviewed.importer_address),
        consignee_code=reviewed.importer_exim,
        consignee_country_code=reviewed.importer_country,
        country_of_origin_name=ref.country_name_by_alpha2.get(export_cc, "") or _first_coo_name(items),
        trading_country=export_cc,
        # Field 9 (Financial_name): reviewer's verbatim text when overridden,
        # else deterministically recomposed from the reviewed values.
        financial_name=(reviewed.field_9_text if reviewed.field_9_override
                        else compose_field9(
                            reviewed.mawb_no, reviewed.hawb_no, reviewed.invoice_refs,
                            reviewed.bank_reference, reviewed.bank_date, reviewed.terms_code,
                            bill_of_lading=reviewed.bill_of_lading_no,
                            bill_of_lading_date=reviewed.bill_of_lading_date,
                            doc_type=reviewed.transport_doc_type)),
    )

    valuation = DeclarationValuation(
        exchange_rate=f"{rate}",
        currency_foreign=inv.currency,
        total_invoice_foreign=fmt2(total_invoice_foreign),
        total_invoice_national=fmt2(total_invoice_national),
        external_freight_foreign=fmt2(ext_freight_foreign),
        external_freight_national=fmt2(ext_freight_national),
        insurance_national=fmt2(insurance_national),
        insurance_foreign=fmt2(insurance_national),   # amount only; currency set in ASYCUDA
        total_cost=fmt2(total_cost),
        total_cif=fmt2(total_cif),
        total_weight=f"{q4(gross_weight):.4f}",
        value_details=fmt2(total_cost),
    )

    terms_desc = ref.terms_description(reviewed.terms_code) if reviewed.terms_code else ""
    prev_ref, free_text = compose_first_item_texts(
        reviewed.terms_code, reviewed.bank_reference, reviewed.bank_amount,
        reviewed.bank_currency, fmt_dd_mm_yyyy(reviewed.bank_date))

    return MergedDeclaration(
        declaration_id=str(uuid.uuid4()),
        job_id=job_id,
        rule_set_version=settings.rule_set_version,
        customs_office_code=reviewed.customs_office_code,
        customs_office_name=reviewed.customs_office_name,
        declaration_type=reviewed.declaration_type,
        gen_procedure_code=reviewed.gen_procedure_code,
        # EX/PEX are the export-flow Box-1 types; IM/PEI/PEM/TIV/MIS stay "I"
        # (MIS flow direction unverified with DoC — revisit with a specimen).
        sad_flow="E" if reviewed.declaration_type in ("EX", "PEX") else "I",
        extended_customs_procedure=reviewed.extended_customs_procedure,
        national_customs_procedure=reviewed.national_customs_procedure,
        manifest_number=reviewed.manifest_no,
        total_number_of_items=len(decl_items),
        total_number_of_packages=_fmt_packages(total_packages),
        package_type_code=reviewed.package_type_code,
        package_type_name=reviewed.package_type_name,
        parties=parties,
        bank_code=reviewed.bank_code,
        bank_name=reviewed.bank_name,
        terms_code=reviewed.terms_code,
        terms_description=terms_desc or (banking.terms_description or ""),
        mode_of_payment=reviewed.mode_of_payment,
        incoterm_code=reviewed.incoterm,
        incoterm_place=reviewed.delivery_place or settings.place_of_loading_name,
        place_of_loading_code=reviewed.place_of_loading_code,
        place_of_loading_name=settings.place_of_loading_name,
        field_18_identity=reviewed.field_18,
        field_21_identity=reviewed.field_21,
        border_nationality=reviewed.border_nationality,
        border_mode=reviewed.border_mode,
        inland_mode_of_transport=reviewed.inland_mode_of_transport,
        border_office_code=reviewed.border_office_code,
        border_office_name=reviewed.border_office_name,
        location_of_goods=reviewed.location_of_goods,
        container_flag=reviewed.container_flag,
        hawb_number=reviewed.hawb_no,
        mawb_number=reviewed.mawb_no,
        bill_of_lading_number=reviewed.bill_of_lading_no,
        bill_of_lading_date=reviewed.bill_of_lading_date,
        transport_doc_type=reviewed.transport_doc_type,
        field_40_summary_declaration=reviewed.field_40,
        shipment_authority_type=reviewed.shipment_authority_type,
        field9_financial_name=parties.financial_name,
        first_item_previous_doc_ref=prev_ref,
        first_item_free_text_1=free_text,
        weight_unit_reviewed=reviewed.weight_unit,
        exclude_freight_insurance_confirmed=reviewed.exclude_freight_insurance,
        mixed_source_reason=reviewed.mixed_source_reason,
        valuation=valuation,
        items=decl_items,
        rule_versions={
            "rule_set": settings.rule_set_version,
            "prompt": settings.prompt_version,
            "extraction_schema": settings.extraction_schema_version,
        },
    )


def _first_coo_alpha2(items: list[WorkItem]) -> str:
    return next((it.coo_alpha2 for it in items if it.coo_alpha2), "")


def _first_coo_name(items: list[WorkItem]) -> str:
    code = _first_coo_alpha2(items)
    return get_reference().country_name_by_alpha2.get(code, "") if code else ""
