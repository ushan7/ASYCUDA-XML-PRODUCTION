"""The derived packing-list view: the reviewer's picture of the weight authority.

Two failure modes this file pins:

* the view used to re-implement matching from the same private helper as the
  allocator, so the two could disagree — and the pairing on screen was not the
  pairing in the XML;
* `*_kg` fields were printed straight from the raw value, ignoring the printed
  unit, so a row in grams was DISPLAYED as kilograms while the allocator used a
  value a thousand times smaller.
"""
from decimal import Decimal

import pytest

from app.config import get_settings_uncached
from app.extraction.common_models import PackingListChunkRaw
from app.review.packing_view import build_packing_view
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing

D = Decimal


@pytest.fixture
def settings():
    return get_settings_uncached()


def _item(seq, desc, qty="1", total="100"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw="PCS",
        unit_price=D("1"), line_total=D(total), currency="USD")


def _payload(rows, **header):
    return PackingListChunkRaw.model_validate({
        "role_validation": {"expected_role": "PACKING_LIST", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": i + 1, **r} for i, r in enumerate(rows)],
        **header,
    })


class _Ship:
    selected_authority_type = "HAWB"
    gross_weight = D("12")
    packages = D("2")
    hawb_number = "9AD3222"
    mawb_number = "160-07712261"


def _view(rows, items, settings, *, header=None, over_budget=False, partial=False):
    payload = _payload(rows, **(header or {}))
    evidence = match_packing(items, [payload])
    return build_packing_view([payload], _Ship(), items, [], over_budget=over_budget,
                              partial=partial, packing_evidence=evidence, settings=settings)


ROWS = [
    {"description_raw": "SHAMPOO 500 ML", "quantity_raw": "10", "uom_raw": "PCS",
     "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"},
     "net_weight": {"value_raw": "6", "unit_raw": "KG"},
     "carton_count": {"value_raw": "1"}, "carton_no_raw": "1",
     "batch_no_raw": "B-1", "expiry_date_raw": "2027-04"},
    {"description_raw": "HAND WASH 250 ML", "quantity_raw": "5", "uom_raw": "PCS",
     "gross_weight": {"value_raw": "4.5", "unit_raw": "KG"},
     "net_weight": {"value_raw": "3", "unit_raw": "KG"},
     "carton_count": {"value_raw": "1"}, "carton_no_raw": "2"},
]
HEADER = {"packing_list_number_raw": "PL-1", "packing_list_date_raw": "2026-06-26",
          "lc_reference_raw": "EB0010FOU01833", "lc_date_raw": "2026-06-16",
          "exporter": {"name_raw": "MEDTRONIC INTERNATIONAL LTD"},
          "importer": {"name_raw": "SINDHU SURGICAL CONCERN PVT. LTD."},
          "country_of_final_destination_raw": "NP",
          "total_gross_weight": {"value_raw": "12", "unit_raw": "KG"},
          "total_net_weight": {"value_raw": "9", "unit_raw": "KG"},
          "total_packages": {"value_raw": "2", "unit_raw": "CTN"},
          "total_quantity": {"value_raw": "15", "unit_raw": "EA"}}


def test_header_and_totals_are_reported(settings):
    items = [_item(1, "SHAMPOO 500 ML"), _item(2, "HAND WASH 250 ML")]
    v = _view(ROWS, items, settings, header=HEADER)
    h, t = v["packing_header"], v["shipment_totals"]
    assert h["packing_list_no"] == "PL-1" and h["packing_list_date"] == "2026-06-26"
    assert h["lc_no"] == "EB0010FOU01833" and h["country_of_final_destination"] == "NP"
    assert h["exporter_name"].startswith("MEDTRONIC")
    assert t["total_packages"] == "2" and t["package_type_normalized"] == "CT"
    assert t["total_quantity"] == "15" and t["quantity_uom_normalized"] == "UNT"
    assert t["gross_weight_kg"] == "12" and t["net_weight_kg"] == "9"


def test_item_detail_fields_are_carried(settings):
    items = [_item(1, "SHAMPOO 500 ML"), _item(2, "HAND WASH 250 ML")]
    row = _view(ROWS, items, settings, header=HEADER)["items"][0]
    assert row["batch_no"] == "B-1" and row["expiry_date"] == "2027-04"
    assert row["carton_no"] == "1" and row["package_count"] == "1"
    assert row["uom_normalized"] == "UNT" and row["package_type"] == "CT"
    assert row["matched_invoice_line_no"] == 1


def test_weights_are_converted_before_being_labelled_kg(settings):
    """A row printed in GRAMS must not be displayed as kilograms — the
    allocator converts, so an unconverted display contradicts the XML."""
    rows = [{"description_raw": "SHAMPOO 500 ML", "quantity_raw": "10",
             "gross_weight": {"value_raw": "7500", "unit_raw": "G"}}]
    v = _view(rows, [_item(1, "SHAMPOO 500 ML")], settings)
    assert v["items"][0]["gross_weight_kg"] == "7.500"


def test_an_unrecognized_weight_unit_is_not_displayed_at_all(settings):
    rows = [{"description_raw": "SHAMPOO 500 ML",
             "gross_weight": {"value_raw": "7.5", "unit_raw": "BANANAS"}}]
    v = _view(rows, [_item(1, "SHAMPOO 500 ML")], settings)
    assert v["items"][0]["gross_weight_kg"] is None


def test_the_view_reports_the_allocators_match_not_its_own(settings):
    """The invoice wording differs, so only the scored matcher pairs these.  A
    view computing its own exact-equality match would show 'unmatched' beside
    an item whose weight the allocation actually used."""
    items = [_item(1, "Shampoo Bottle 500 ml")]
    rows = [{"description_raw": "500ML SHAMPOO BOTTLE",
             "gross_weight": {"value_raw": "7.5", "unit_raw": "KG"}}]
    row = _view(rows, items, settings)["items"][0]
    assert row["matched_invoice_line_no"] == 1
    assert row["match_method"] == "description similarity"
    assert Decimal(row["match_confidence"]) < 1


def test_sum_checks_pass_when_the_rows_add_up(settings):
    items = [_item(1, "SHAMPOO 500 ML"), _item(2, "HAND WASH 250 ML")]
    val = _view(ROWS, items, settings, header=HEADER)["validation"]
    assert val["sum_item_gross_equals_total_gross"] is True
    assert val["sum_item_net_equals_total_net"] is True
    assert val["sum_item_packages_equals_total_packages"] is True
    assert val["ready_for_invoice_merge"] is True
    assert val["blocking_errors"] == []


def test_sum_mismatch_is_reported_without_blocking(settings):
    items = [_item(1, "SHAMPOO 500 ML"), _item(2, "HAND WASH 250 ML")]
    header = {**HEADER, "total_gross_weight": {"value_raw": "99", "unit_raw": "KG"}}
    val = _view(ROWS, items, settings, header=header)["validation"]
    assert val["sum_item_gross_equals_total_gross"] is False
    assert any("Row gross weights total" in w for w in val["warnings"])
    assert val["ready_for_invoice_merge"] is True          # warn mode: never blocks XML


def test_net_above_gross_totals_block_the_merge(settings):
    items = [_item(1, "SHAMPOO 500 ML")]
    header = {"total_gross_weight": {"value_raw": "5", "unit_raw": "KG"},
              "total_net_weight": {"value_raw": "9", "unit_raw": "KG"}}
    val = _view([ROWS[0]], items, settings, header=header)["validation"]
    assert val["net_weight_less_than_gross"] is False
    assert val["ready_for_invoice_merge"] is False and val["blocking_errors"]


def test_partial_extraction_is_visible_and_named(settings):
    items = [_item(1, "SHAMPOO 500 ML")]
    v = _view([ROWS[0]], items, settings, partial=True)
    assert v["validation"]["extraction_partial"] is True
    assert v["document_confidence"] == "0.80"
    assert "quantity_proportional" in v["allocation_rules_applied"]["item_weight_method"]
    assert any("time budget" in w for w in v["validation"]["warnings"])


def test_over_budget_still_reports_the_old_flag(settings):
    items = [_item(1, "SHAMPOO 500 ML")]
    v = _view([ROWS[0]], items, settings, over_budget=True)
    assert v["validation"]["extraction_over_budget"] is True
    assert v["validation"]["ready_for_invoice_merge"] is False


def test_no_packing_list_yields_no_view(settings):
    assert build_packing_view([], _Ship(), [], [], over_budget=False, settings=settings) is None
