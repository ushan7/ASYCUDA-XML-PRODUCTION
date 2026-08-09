"""Over-extraction honesty gate (2026-07-20 audit).

Root cause pinned: an LLM window re-emitted context/overlap-page rows, so a
29-item invoice extracted as 49. The gate compares the extracted goods-row count
to the goods-row anchors the OCR PROVABLY prints (the manifest) and flags the
over-count — pointing at the offending pages — WITHOUT ever removing a row.

Automatic dedup was withdrawn after an adversarial audit found it deletes
genuine distinct customs lines (shared/garbled/borrowed identity tokens). These
tests pin the safe contract: the gate flags but never mutates the row set — so
the two-real-identical-lines case, which broke every auto-drop, is a no-op here.
"""
from app.domain.enums import DeclaredRole
from app.extraction.common_models import (
    Evidence, InvoiceChunkRaw, InvoiceLineRaw, RoleValidation)
from app.extraction.manifest import anchor_count
from app.extraction.openai_extractor import reconcile_row_duplicates

HDR = "| MODEL NO | GTIN | DESCRIPTION | QTY | U/M | UNIT PRICE | TOTAL |"


def _line(code, qty, total):
    return f"| {code} 00763000248437 | STENT {code} | {qty} | EA | 320.00 | {total} |"


def _row(page, ridx, code, qty, total):
    return InvoiceLineRaw(
        source_page_no=page, source_row_index=ridx, description_raw=f"STENT {code}",
        model_raw=code, quantity_raw=qty, uom_raw="EA", line_total_raw=total,
        evidence=[Evidence(page_no=page, quote=_line(code, qty, total))])


class _P:                          # minimal stand-in for an OcrPage
    def __init__(self, page_no, plain_text):
        self.page_no, self.plain_text = page_no, plain_text


def _payload(rows):
    return InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.INVOICE),
        rows=rows, page_numbers=[1])


# --------------------------------------------------------------------------- #
# anchor_count — the reference count
# --------------------------------------------------------------------------- #
def test_anchor_count_counts_printed_goods_lines_per_page():
    page = "\n".join([HDR, _line("RX1", "3", "960.00"), _line("RX2", "2", "640.00")])
    assert anchor_count({1: page}) == {1: 2}


# --------------------------------------------------------------------------- #
# reconcile_row_duplicates — flags over-count, NEVER drops
# --------------------------------------------------------------------------- #
def test_overcount_is_flagged_but_no_row_is_removed():
    page = "\n".join([HDR, _line("RX1", "3", "960.00")])           # OCR prints ONE line
    payload = _payload([_row(1, 1, "RX1", "3", "960.00"),
                        _row(1, 2, "RX1", "3", "960.00")])         # extraction has TWO
    out, warnings = reconcile_row_duplicates(DeclaredRole.INVOICE, [_P(1, page)], payload, [])
    assert len(out.rows) == 2                                      # nothing removed — read-only
    assert any(w.startswith("EXTRACTION_OVERCOUNT") for w in warnings)
    assert any("p1: 2 rows vs 1 printed" in w for w in warnings)


def test_two_real_identical_lines_are_never_touched():
    # the case that broke every auto-drop: two printed anchors, two rows -> the
    # counts agree, so the gate is a silent no-op (and it never removes anyway)
    page = "\n".join([HDR, _line("RX1", "3", "960.00"), _line("RX1", "3", "960.00")])
    payload = _payload([_row(1, 1, "RX1", "3", "960.00"),
                        _row(1, 2, "RX1", "3", "960.00")])
    out, warnings = reconcile_row_duplicates(DeclaredRole.INVOICE, [_P(1, page)], payload, [])
    assert len(out.rows) == 2 and warnings == []


def test_clean_extraction_is_not_flagged():
    page = "\n".join([HDR, _line("RX1", "3", "960.00"), _line("RX2", "2", "640.00")])
    payload = _payload([_row(1, 1, "RX1", "3", "960.00"), _row(1, 2, "RX2", "2", "640.00")])
    out, warnings = reconcile_row_duplicates(DeclaredRole.INVOICE, [_P(1, page)], payload, [])
    assert len(out.rows) == 2 and warnings == []


def test_no_manifest_means_no_flag_and_no_change():
    # a page with no parseable goods-row anchors cannot bound the count
    payload = _payload([_row(1, 1, "RX1", "3", "960.00"), _row(1, 2, "RX1", "3", "960.00")])
    out, warnings = reconcile_row_duplicates(DeclaredRole.INVOICE, [_P(1, "free text, no table")], payload, [])
    assert len(out.rows) == 2 and warnings == []


def test_gate_is_a_noop_for_non_row_roles():
    payload = _payload([_row(1, 1, "RX1", "3", "960.00")])
    out, warnings = reconcile_row_duplicates(DeclaredRole.AIR_WAYBILL, [_P(1, "x")], payload, ["pre"])
    assert out is payload and warnings == ["pre"]
