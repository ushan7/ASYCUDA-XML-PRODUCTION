"""Arithmetic gates on a packing extraction.

`validate_packing` used to check only the role, the evidence quotes and page
coverage — nothing looked at the numbers.  A net-weight column read as gross, a
carton NUMBER read as a count, or a totals line extracted as a goods row all
produced a full, individually plausible, silently wrong set of item weights.
The packing list states what its rows add up to; these gates use that.
"""
from decimal import Decimal

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import (
    Evidence,
    PackingListChunkRaw,
    PackingRowRaw,
    RawNumber,
    RoleValidation,
)
from app.extraction.validator import validate_packing


def _row(idx, desc, *, gross=None, net=None, ctn=None, qty=None, unit="KG", page=1):
    return PackingRowRaw(
        source_page_no=page, source_row_index=idx, description_raw=desc,
        quantity_raw=qty,
        gross_weight=RawNumber(value_raw=gross, unit_raw=unit) if gross else None,
        net_weight=RawNumber(value_raw=net, unit_raw=unit) if net else None,
        carton_count=RawNumber(value_raw=ctn) if ctn else None,
        evidence=[Evidence(page_no=page, quote=desc)],
    )


def _payload(rows, *, gross=None, net=None, packages=None, unit="KG"):
    return PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST),
        rows=rows,
        total_gross_weight=RawNumber(value_raw=gross, unit_raw=unit) if gross else None,
        total_net_weight=RawNumber(value_raw=net, unit_raw=unit) if net else None,
        total_packages=RawNumber(value_raw=packages) if packages else None,
    )


# OCR text carrying the evidence quotes so the existing quote check passes.
PAGES = {1: "SHAMPOO 500 ML\nHAND WASH 250 ML\nWIDGET ALPHA\nTOTAL"}


def _errors(payload, whole_document=True):
    return validate_packing(payload, PAGES, whole_document=whole_document)


def test_row_sum_matching_the_printed_total_raises_nothing():
    payload = _payload([_row(1, "SHAMPOO 500 ML", gross="7.5"),
                        _row(2, "HAND WASH 250 ML", gross="4.5")], gross="12.0")
    assert not any("PACKING_SUM_MISMATCH" in e for e in _errors(payload))


def test_gross_sum_contradicting_the_printed_total_is_reported():
    payload = _payload([_row(1, "SHAMPOO 500 ML", gross="7.5"),
                        _row(2, "HAND WASH 250 ML", gross="4.5")], gross="99.0")
    errs = [e for e in _errors(payload) if "PACKING_SUM_MISMATCH" in e]
    assert len(errs) == 1 and "gross weight" in errs[0]


def test_sum_check_does_not_run_on_a_single_page_window():
    """A window holds part of the rows and sometimes all of the totals — a
    mismatch there is arithmetic about nothing, and would cost a repair round."""
    payload = _payload([_row(1, "SHAMPOO 500 ML", gross="7.5")], gross="99.0")
    assert not any("PACKING_SUM_MISMATCH" in e for e in _errors(payload, whole_document=False))


def test_sum_check_is_unit_aware():
    """Rows in grams against a total in kilograms is a 1000x trap: comparing the
    printed numbers directly would 'match' only by accident, or mismatch a
    perfectly good extraction."""
    payload = _payload([_row(1, "SHAMPOO 500 ML", gross="7500", unit="G"),
                        _row(2, "HAND WASH 250 ML", gross="4500", unit="G")],
                       gross="12.0")
    payload.total_gross_weight.unit_raw = "KG"
    assert not any("PACKING_SUM_MISMATCH" in e for e in _errors(payload))


def test_package_count_sum_mismatch_is_reported():
    payload = _payload([_row(1, "SHAMPOO 500 ML", ctn="1"),
                        _row(2, "HAND WASH 250 ML", ctn="2")], packages="10")
    errs = [e for e in _errors(payload) if "PACKING_SUM_MISMATCH" in e]
    assert len(errs) == 1 and "package count" in errs[0]


def test_row_with_net_above_gross_is_reported():
    payload = _payload([_row(1, "WIDGET ALPHA", gross="4.0", net="9.0")])
    errs = [e for e in _errors(payload) if "PACKING_ROW_NET_ABOVE_GROSS" in e]
    assert len(errs) == 1 and "WIDGET ALPHA" in errs[0]


def test_row_with_net_below_gross_is_accepted():
    payload = _payload([_row(1, "WIDGET ALPHA", gross="9.0", net="4.0")])
    assert not any("PACKING_ROW_NET_ABOVE_GROSS" in e for e in _errors(payload))


@pytest.mark.parametrize("desc", ["TOTAL", "Total:", "GRAND TOTAL", "TOTAL GROSS WEIGHT"])
def test_totals_line_extracted_as_a_goods_row_is_reported(desc):
    payload = _payload([_row(1, desc, gross="12.0")])
    assert any("PACKING_TOTALS_ROW_EXTRACTED" in e for e in _errors(payload))


def test_a_goods_description_containing_total_is_not_flagged():
    payload = _payload([_row(1, "TOTAL KNEE SYSTEM FEMORAL COMPONENT", gross="12.0")])
    assert not any("PACKING_TOTALS_ROW_EXTRACTED" in e for e in _errors(payload))


def test_rows_without_weights_never_trip_the_sum_check():
    payload = _payload([_row(1, "SHAMPOO 500 ML"), _row(2, "HAND WASH 250 ML")], gross="12.0")
    assert not any("PACKING_SUM_MISMATCH" in e for e in _errors(payload))


def test_unrecognized_row_unit_is_excluded_rather_than_read_as_kilograms():
    """`to_kg` disqualifies a printed-but-unknown unit; the sum check must
    inherit that rather than quietly counting the number as kilograms."""
    payload = _payload([_row(1, "SHAMPOO 500 ML", gross="7.5", unit="BANANAS"),
                        _row(2, "HAND WASH 250 ML", gross="4.5")], gross="4.5")
    assert not any("PACKING_SUM_MISMATCH" in e for e in _errors(payload))
    assert Decimal("4.5") == Decimal("4.5")
