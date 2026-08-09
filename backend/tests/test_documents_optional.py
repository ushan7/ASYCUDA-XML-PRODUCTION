"""Documents-optional redesign (user rule 2026-07-17): only the INVOICE is
compulsory.

* PACKING_LIST absent  -> authorised gross split by item QUANTITY share,
  cartons by weight share, both exact-sum reconciled.
* AIR_WAYBILL absent   -> reviewer enters total weight, total cartons and a
  MAWB / HAWB / Bill-of-Lading number manually; finalize blocks until then.
* BANKING absent       -> resolves to ABSENT states + manual review fields.
"""
from decimal import Decimal
from types import SimpleNamespace

from app.pipeline import finalize, resolve_context, to_critical_review
from app.review.critical_review import (
    CriticalReviewConfirmation,
    compose_field9,
    merge_confirmation,
)
from app.rules.models import WorkItem
from app.rules.weight_carton import allocate_weights_and_cartons


def _item(seq, qty, total):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=f"ITEM {seq}", quantity=Decimal(qty), invoice_uom_raw="PCS",
        unit_price=Decimal("1"), line_total=Decimal(total), currency="USD")


# --------------------------------------------------------------------------- #
# packing absent: weight by quantity share, cartons by weight share
# --------------------------------------------------------------------------- #
def test_no_packing_list_weight_by_quantity_cartons_by_weight():
    items = [_item(1, "3", "500"), _item(2, "1", "400"), _item(3, "6", "100")]
    warnings = allocate_weights_and_cartons(
        items, {}, Decimal("100"), Decimal("5"), packing_present=False)
    # quantity share 3/10, 1/10, 6/10 of 100 kg — NOT value share
    assert [i.gross_weight_kg for i in items] == [Decimal("30.0000"), Decimal("10.0000"), Decimal("60.0000")]
    assert sum(i.gross_weight_kg for i in items) == Decimal("100.0000")   # exact-sum rule
    # cartons proportional to weight, reconciled to the authorised total
    assert [i.package_count for i in items] == [Decimal("1.50"), Decimal("0.50"), Decimal("3.00")]
    assert sum(i.package_count for i in items) == Decimal("5.00")
    assert any(w.code == "WEIGHT_BASIS_QUANTITY" for w in warnings)
    # net defaults to gross x ratio and stays below gross
    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)


def test_packing_present_but_unmatched_keeps_value_share():
    items = [_item(1, "3", "500"), _item(2, "1", "500")]
    warnings = allocate_weights_and_cartons(
        items, {}, Decimal("100"), Decimal("4"), packing_present=True)
    # historical behavior pinned: equal VALUES -> equal weights (quantity ignored)
    assert [i.gross_weight_kg for i in items] == [Decimal("50.0000"), Decimal("50.0000")]
    assert any(w.code == "WEIGHT_BASIS_VALUE" for w in warnings)


# --------------------------------------------------------------------------- #
# invoice-only job end-to-end: review guidance + finalize gates + manual entry
# --------------------------------------------------------------------------- #
_INVOICE_RAW = {
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1],
    "header": {"invoice_number_raw": "INV-77", "invoice_date_raw": "01.07.2026",
               "currency_raw": "USD",
               "exporter": {"name_raw": "ACME GMBH", "country_raw": "GERMANY"},
               "consignee": {"name_raw": "KTM IMPORTS", "exim_code_raw": "1234567890123"}},
    "rows": [
        {"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET A",
         "quantity_raw": "3", "uom_raw": "PCS", "unit_price_raw": "10.00", "line_total_raw": "30.00"},
        {"source_page_no": 1, "source_row_index": 2, "description_raw": "WIDGET B",
         "quantity_raw": "1", "uom_raw": "PCS", "unit_price_raw": "70.00", "line_total_raw": "70.00"},
    ],
    "totals": {"grand_total_raw": "100.00"},
}


def _invoice_only_docs():
    return [SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                            original_file_name="inv.pdf", raw_extraction=_INVOICE_RAW)]


def test_invoice_only_review_warns_and_finalize_blocks_until_manual_entry():
    docs = _invoice_only_docs()
    ctx = resolve_context(docs)
    assert ctx.packing_present is False
    review = to_critical_review(ctx, docs)
    codes = {w.code for w in review.warnings}
    assert {"TRANSPORT_DOC_MISSING", "PACKING_DOC_MISSING", "BANKING_DOC_MISSING"} <= codes
    assert review.gross_weight == "" and review.total_packages == ""

    # no manual entry -> the three documents-optional gates block XML
    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation())
    decl = finalize(ctx, reviewed)
    blocking = {m.code for m in decl.blocking_errors}
    assert {"SHIPMENT_GROSS_REQUIRED", "SHIPMENT_PACKAGES_REQUIRED",
            "TRANSPORT_REFERENCE_REQUIRED"} <= blocking
    assert decl.ready_for_xml is False


def test_invoice_only_manual_entry_satisfies_gates_and_allocates_by_quantity():
    docs = _invoice_only_docs()
    ctx = resolve_context(docs)
    review = to_critical_review(ctx, docs)
    conf = CriticalReviewConfirmation(
        confirmed_gross_weight="100", confirmed_total_packages="4",
        bill_of_lading_no="MBL-2026-99")
    reviewed, _ = merge_confirmation(review, conf)
    assert reviewed.bill_of_lading_no == "MBL-2026-99"
    decl = finalize(ctx, reviewed)
    blocking = {m.code for m in decl.blocking_errors}
    assert not ({"SHIPMENT_GROSS_REQUIRED", "SHIPMENT_PACKAGES_REQUIRED",
                 "TRANSPORT_REFERENCE_REQUIRED"} & blocking)
    # weights split by quantity share (3:1), cartons by weight share, sums exact
    assert [i.gross_weight_kg for i in ctx.items] == [Decimal("75.0000"), Decimal("25.0000")]
    assert [i.package_count for i in ctx.items] == [Decimal("3.00"), Decimal("1.00")]
    # the manual BL reaches Field 9 on the declaration, and with no air waybill
    # in sight it is the B/L that Field 9 states (user rule 2026-08-06)
    assert "B/L NO:MBL-2026-99" in decl.parties.financial_name
    assert "MAWB" not in decl.parties.financial_name
    assert reviewed.transport_doc_type == "BL"


def test_field9_carries_bill_of_lading_line():
    f9 = compose_field9("", "", [("INV-1", "24-FEB-2026")], "", "", None,
                        bill_of_lading="MBL-77", bill_of_lading_date="24/02/2026")
    lines = f9.splitlines()
    assert lines[0] == "B/L NO:MBL-77 B/L DATE:24-FEB-2026"
    assert lines[1].startswith("INVOICE NO:INV-1")


def test_field9_bill_of_lading_replaces_the_awb_line():
    """A B/L shipment prints B/L NO / B/L DATE INSTEAD OF MAWB NO / HAWB —
    never both, so the declaration says which document it travelled on."""
    f9 = compose_field9("", "", [], "", "", None, bill_of_lading="HLCU-1", doc_type="BL")
    assert f9 == "B/L NO:HLCU-1"
    # an air shipment whose invoice quotes a B/L keeps the AWB line and adds the
    # B/L underneath
    air = compose_field9("160-1", "KTM-2", [], "", "", None, bill_of_lading="HLCU-1")
    assert air.splitlines() == ["MAWB NO:160-1 HAWB:KTM-2", "B/L NO:HLCU-1"]


def test_field9_without_a_date_prints_the_number_alone():
    assert compose_field9("", "", [], "", "", None, bill_of_lading="BL-9") == "B/L NO:BL-9"


def test_field9_never_drops_the_transport_line():
    """A B/L job whose B/L number was cleared but which carries an AWB number
    falls back to the air line rather than stating no transport document."""
    assert compose_field9("160-1", "", [], "", "", None, doc_type="BL") == "MAWB NO:160-1 HAWB:"
