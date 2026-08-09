"""Bill-of-lading shipments (user rule 2026-08-06, land/sea customs).

A B/L uploaded INSTEAD OF an air waybill / delivery order is the shipment's
transport document: it supplies the gross weight and packages exactly like a
house air waybill does, its number is the Field 40 default, and Field 9 prints

    B/L NO:<number> B/L DATE:<date>

INSTEAD OF the MAWB NO / HAWB line.  The B/L number is also read off the
invoice, which states it on most sea/land jobs.
"""
from decimal import Decimal
from types import SimpleNamespace

from app.extraction.common_models import AirWaybillExtractionRaw
from app.pipeline import finalize, resolve_context, to_critical_review
from app.review.critical_review import (
    CriticalReviewConfirmation,
    merge_confirmation,
    transport_doc_type,
)
from app.rules.airwaybill_authority import derive_field40, resolve_shipment_authority

_INVOICE_RAW = {
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1],
    "header": {"invoice_number_raw": "INV-88", "invoice_date_raw": "01.07.2026",
               "currency_raw": "USD",
               "exporter": {"name_raw": "ACME LTD", "country_raw": "INDIA"},
               "consignee": {"name_raw": "KTM IMPORTS", "exim_code_raw": "1234567890123"}},
    "rows": [
        {"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET A",
         "quantity_raw": "3", "uom_raw": "PCS", "unit_price_raw": "10.00", "line_total_raw": "30.00"},
        {"source_page_no": 1, "source_row_index": 2, "description_raw": "WIDGET B",
         "quantity_raw": "1", "uom_raw": "PCS", "unit_price_raw": "70.00", "line_total_raw": "70.00"},
    ],
    "totals": {"grand_total_raw": "100.00"},
}


def _bl_form(form_id="BL1", *, title="BILL OF LADING", number="HLCUBOM240012345",
             date="24-FEB-2026", gross="480.5", pieces="12", page=1):
    return {"logical_form_id": form_id, "source_pages": [page],
            "document_title_raw": title, "document_kind_raw": "BILL_OF_LADING",
            "bill_of_lading_number_raw": number, "bill_of_lading_date_raw": date,
            "gross_weight": {"value_raw": gross, "unit_raw": "KG"},
            "pieces_or_packages": {"value_raw": pieces}}


def _awb_payload(*forms):
    return AirWaybillExtractionRaw.model_validate({
        "role_validation": {"expected_role": "AIR_WAYBILL", "matches_expected_role": True},
        "forms": list(forms)})


def _docs(*, invoice=None, awb_forms=()):
    docs = [SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                            original_file_name="inv.pdf",
                            raw_extraction=invoice or _INVOICE_RAW)]
    if awb_forms:
        docs.append(SimpleNamespace(
            declared_role="AIR_WAYBILL", upload_index_within_role=0,
            original_file_name="bl.pdf",
            raw_extraction={"role_validation": {"expected_role": "AIR_WAYBILL",
                                                "matches_expected_role": True},
                            "forms": list(awb_forms)}))
    return docs


# --------------------------------------------------------------------------- #
# classification & authority
# --------------------------------------------------------------------------- #
def test_bill_of_lading_is_the_shipment_authority():
    ship = resolve_shipment_authority([_awb_payload(_bl_form())])
    assert ship.selected_authority_type == "BILL_OF_LADING"
    assert ship.gross_weight == Decimal("480.5") and ship.packages == Decimal("12")
    assert ship.bill_of_lading_number == "HLCUBOM240012345"
    assert ship.bill_of_lading_date == "24-FEB-2026"
    assert not ship.hawb_number and not ship.mawb_number
    # Field 40 defaults to the B/L number instead of being left empty
    assert derive_field40(ship) == ("HLCUBOM240012345",
                                   "Bill of Lading shipment (no air waybill) — "
                                   "the B/L number is used.")


def test_bill_of_lading_number_in_the_generic_reference_field_still_counts():
    """An extraction that put the B/L number in `primary_awb_number_raw` (the
    document's own reference) must not lose it: the title settles what the
    document is, and the number is its identity wherever it landed."""
    form = {"logical_form_id": "BL1", "source_pages": [1],
            "document_title_raw": "ORIGINAL BILL OF LADING",
            "primary_awb_number_raw": "MAEU987654",
            "gross_weight": {"value_raw": "100", "unit_raw": "KG"},
            "pieces_or_packages": {"value_raw": "4"}}
    ship = resolve_shipment_authority([_awb_payload(form)])
    assert ship.selected_authority_type == "BILL_OF_LADING"
    assert ship.bill_of_lading_number == "MAEU987654"
    assert not ship.mawb_number          # never mistaken for an air waybill number


def test_house_bill_of_lading_wins_over_the_master():
    """Same rule as HAWB vs MAWB: the LOWER gross weight is this consignment's
    house-level document; the master covers the whole consolidation."""
    ship = resolve_shipment_authority([_awb_payload(
        _bl_form("MBL", title="MASTER BILL OF LADING", number="MAEU-MASTER",
                 gross="1600", pieces="40", page=1),
        _bl_form("HBL", title="HOUSE BILL OF LADING", number="FWD-HOUSE-9",
                 gross="480.5", pieces="12", page=2))])
    assert ship.gross_weight == Decimal("480.5") and ship.packages == Decimal("12")
    assert ship.bill_of_lading_number == "FWD-HOUSE-9"
    assert any(w.code == "BL_HOUSE_SELECTED" for w in ship.warnings)


def test_air_waybill_still_outranks_a_bill_of_lading_reference():
    """An air job whose paperwork mentions a B/L stays an air job."""
    hawb = {"logical_form_id": "H1", "source_pages": [1],
            "document_title_raw": "HOUSE AIR WAYBILL", "hawb_number_raw": "DEMOHAWB0057",
            "gross_weight": {"value_raw": "199", "unit_raw": "KG"},
            "pieces_or_packages": {"value_raw": "9"}}
    ship = resolve_shipment_authority([_awb_payload(hawb)])
    assert ship.selected_authority_type == "HAWB"
    assert transport_doc_type(ship.mawb_number or "", ship.hawb_number or "",
                              "HLCU-1", ship.selected_authority_type) == "AWB"


# --------------------------------------------------------------------------- #
# the decision rule itself
# --------------------------------------------------------------------------- #
def test_transport_doc_type_rule():
    assert transport_doc_type("", "", "BL-1") == "BL"
    assert transport_doc_type("160-1", "", "BL-1") == "AWB"
    assert transport_doc_type("", "", "", "BILL_OF_LADING") == "BL"
    assert transport_doc_type("160-1", "KTM-2", "", "HAWB") == "AWB"
    assert transport_doc_type("", "", "") == "AWB"


# --------------------------------------------------------------------------- #
# end to end: review -> reviewed values -> declaration Field 9
# --------------------------------------------------------------------------- #
def test_review_and_declaration_state_the_bill_of_lading():
    docs = _docs(awb_forms=[_bl_form()])
    ctx = resolve_context(docs)
    review = to_critical_review(ctx, docs)
    assert review.transport_doc_type == "BL"
    assert review.bill_of_lading_no == "HLCUBOM240012345"
    assert review.bill_of_lading_date == "24-FEB-2026"
    assert review.bill_of_lading_source == "TRANSPORT_DOCUMENT"
    f9 = review.field_9_invoice_transport_document.splitlines()
    assert f9[0] == "B/L NO:HLCUBOM240012345 B/L DATE:24-FEB-2026"
    assert "MAWB" not in review.field_9_invoice_transport_document
    assert review.field_40_previous_document == "HLCUBOM240012345"
    assert any(c["source"] == "B/L" and c["suggested"] for c in review.field_40_candidates)

    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation(field_40_confirmed=True))
    decl = finalize(ctx, reviewed)
    assert decl.transport_doc_type == "BL"
    assert decl.bill_of_lading_number == "HLCUBOM240012345"
    assert decl.parties.financial_name.splitlines()[0] == \
        "B/L NO:HLCUBOM240012345 B/L DATE:24-FEB-2026"
    assert "TRANSPORT_REFERENCE_REQUIRED" not in {m.code for m in decl.blocking_errors}


def test_bill_of_lading_taken_from_the_invoice_when_no_transport_document():
    """The invoice states the B/L number on most sea/land jobs — with no
    transport document uploaded it is the only place it prints."""
    invoice = {**_INVOICE_RAW,
               "header": {**_INVOICE_RAW["header"],
                          "bill_of_lading_number_raw": "MAEU-INV-4455",
                          "bill_of_lading_date_raw": "18/06/2026"}}
    docs = _docs(invoice=invoice)
    ctx = resolve_context(docs)
    review = to_critical_review(ctx, docs)
    assert review.bill_of_lading_no == "MAEU-INV-4455"
    assert review.bill_of_lading_date == "18-JUN-2026"
    assert review.bill_of_lading_source == "INVOICE"
    assert review.transport_doc_type == "BL"
    assert any(w.code == "BL_FROM_INVOICE" for w in review.warnings)
    assert review.field_9_invoice_transport_document.splitlines()[0] == \
        "B/L NO:MAEU-INV-4455 B/L DATE:18-JUN-2026"


def test_reviewer_choice_of_transport_document_is_final():
    """The reviewer can force the air-waybill presentation (or the B/L one) —
    the system suggests, the user decides."""
    docs = _docs(awb_forms=[_bl_form()])
    ctx = resolve_context(docs)
    review = to_critical_review(ctx, docs)
    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation(
        transport_doc_type="AWB", mawb_no="160-99999999", field_40_confirmed=True))
    assert reviewed.transport_doc_type == "AWB"
    decl = finalize(ctx, reviewed)
    lines = decl.parties.financial_name.splitlines()
    assert lines[0] == "MAWB NO:160-99999999 HAWB:"
    assert lines[1] == "B/L NO:HLCUBOM240012345 B/L DATE:24-FEB-2026"


def test_bill_of_lading_job_must_confirm_field_40():
    """Field 40 is stamped on every item, so a B/L job is asked about it too."""
    docs = _docs(awb_forms=[_bl_form()])
    ctx = resolve_context(docs)
    review = to_critical_review(ctx, docs)
    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation())
    decl = finalize(ctx, reviewed)
    assert "FIELD_40_UNCONFIRMED" in {m.code for m in decl.blocking_errors}
