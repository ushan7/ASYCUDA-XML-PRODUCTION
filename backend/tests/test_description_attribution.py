"""Cross-page continuation text must land on the row that owns it.

Live defect (Medtronic invoice 4050033058, "4. 100 invoice.pdf", 21 pages).
OCR reads a goods table one cell at a time, and this vendor's description
column wraps across row AND page boundaries, so the text it recovers stops
lining up with the rows that own it.  Page 16 does three different versions of
it at once::

  |  LA6JL40 00763000567682 | <p15's batch runs> CATHETER LA6JL40 LA 6F 100CM
     JL40 Batch: <this row's runs> CATHETER LA6EBU35 LA 6F 100CM EB35 | 20 | EA | 24,46 | 489,20 |
  |  LA6EBU35 |  | 70 | EA | 24,46 | 1.712,20  |

Row 1's cell opens with the PREVIOUS page's continuation, carries its own name,
carries its own batches, and closes with the NEXT row's name — and row 2, whose
name is stranded up there, shipped its bare part code as its customs
description.  Page 8 shows the other half of the family: a row whose cell holds
NOTHING but the previous row's continuation, with its real name printed on a
value-less line the parser (rightly) refuses to make a row out of.

Attribution rests on two proofs from different columns of the same printed
table — the MODEL echo (a goods name repeats its own part code) and QUANTITY
conservation (the "<n> EA" echoes in a row's own runs sum to its printed
quantity).  Nothing is ever reconstructed: every emitted string is an exact
slice of text the OCR produced, and the pass asserts that before returning.

The safety cases at the bottom matter as much as the repairs.  This vendor's
shape must not cost the pinned incidents in ``rules.field_allocation`` —
"GENUINE MADE IN ITALY WALLET" keeps its name, "SINGLE ORIGIN COFFEE" is not
an origin statement, and a row that already has a name is never overwritten.
"""
from types import SimpleNamespace

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import Evidence, InvoiceChunkRaw, InvoiceLineRaw, RoleValidation
from app.extraction.description_attribution import attribute_row_descriptions
from app.extraction.description_segments import (
    is_code_only, model_matches, part_codes, qty_echoes, segment_description, unglue)
from app.reference.store import get_reference


@pytest.fixture(scope="module")
def ref():
    return get_reference()


@pytest.fixture(scope="module")
def nc(ref):
    return ref.normalize_country


# The live page-16 cell, verbatim.
P16_CELL = (
    "231951194 19 EA COO: Mexico 232046211 1 EA COO: Mexico 232154007 10 EA COO: Mexico "
    "232154038 18 EA COO: Mexico CATHETER LA6JL40 LA 6F 100CM JL40 Batch: 232720182 3 EA "
    "COO: Mexico 232720212 5 EA COO: Mexico 232720233 3 EA COO: Mexico 232720236 7 EA "
    "COO: Mexico 232720254 2 EA COO: Mexico CATHETER LA6EBU35 LA 6F 100CM EB35")
# The live page-8 cell: entirely the previous row's continuation.
P8_CELL = ("0012009275 1 EA COO: Ireland 0012325782 1 EA COO: Ireland "
           "0012637191 8 EA COO: Ireland")
P8_FRAGMENT_LINE = (
    "|   |  STENT RONYX40034X ONYX 4.00X34RX Batch: 0013039750 6 EA COO: Ireland |   |   |   |   |")


def _row(**kw):
    kw.setdefault("source_page_no", 1)
    kw.setdefault("source_row_index", 1)
    kw.setdefault("uom_raw", "EA")
    kw.setdefault("evidence", [Evidence(page_no=kw["source_page_no"], label="TABLE_PARSER",
                                        quote="x")])
    return InvoiceLineRaw(**kw)


def _payload(rows):
    return InvoiceChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.INVOICE,
                                       matches_expected_role=True),
        rows=rows)


def _run(rows, pages=()):
    payload, warnings = attribute_row_descriptions(
        DeclaredRole.INVOICE, list(pages), _payload(rows), [])
    return payload.rows, warnings


# --------------------------------------------------------------------------- #
# 1. the segmenter — the primitive the old allocator was missing
# --------------------------------------------------------------------------- #
def test_cell_segments_into_alternating_prose_and_annotation(nc):
    """The old allocator's only notion of a cut was 'label to end of string',
    so a run in the MIDDLE of a cell had no end and a name after it could not
    be separated.  A BOUNDED run is the whole fix."""
    segs, _ = segment_description(P16_CELL, nc)
    assert [s.kind for s in segs] == ["ANN", "PROSE", "ANN", "PROSE"]
    assert segs[1].text == "CATHETER LA6JL40 LA 6F 100CM JL40"
    assert segs[3].text == "CATHETER LA6EBU35 LA 6F 100CM EB35"


def test_every_segment_is_an_exact_slice_of_the_printed_cell(nc):
    segs, text = segment_description(P16_CELL, nc)
    for s in segs:
        assert text[s.start:s.end].strip() == s.text


def test_all_annotation_cell_yields_no_prose(nc):
    segs, _ = segment_description(P8_CELL, nc)
    assert [s.kind for s in segs] == ["ANN"]


def test_unglue_only_ever_adds_whitespace():
    for raw in ("CATHETER LA6AL10Batch: 231052114 3 EA", "WIDGET COO:Ireland",
                "PIPE FITTING", "STENT RONYX45012X ONYX 4.50X12RX Batch:"):
        assert unglue(raw).replace(" ", "") == raw.replace(" ", "")


def test_unglue_leaves_a_plain_code_alone_on_vendors_without_the_defect():
    """The digit-run split is gated on the cell carrying an annotation label:
    ungated it would rewrite ordinary part numbers on every other vendor."""
    assert unglue("PIPE123456 STEEL") == "PIPE123456 STEEL"
    assert "PIPE 123456" in unglue("PIPE123456 Batch: 99 1 EA")


def test_model_matches_sees_a_part_code_welded_to_its_barcode():
    assert model_matches("LA6JL40", "LA6JL40 00763000567682")
    assert model_matches("LA6AL10", "LA6AL1000763000565299")
    assert model_matches("DXT5JR40", "DXT5JR4020763000394220")   # 14-digit GTIN, not 0-led
    assert model_matches("LA6EBU35", "LA6EBU35")
    assert not model_matches("LA6EBU35", "LA6JL40 00763000567682")


def test_code_only_segment_is_not_a_name():
    assert is_code_only("LA6EBU35")
    assert is_code_only("RONYX45012X 00763000248956")
    assert not is_code_only("CATHETER LA6JL40 LA 6F 100CM JL40")


def test_quantity_echoes_are_read_from_annotation_runs():
    assert sum(qty_echoes("Batch: 232720182 3 EA COO: Mexico 232720212 5 EA")) == 8


# --------------------------------------------------------------------------- #
# 2. the repairs
# --------------------------------------------------------------------------- #
def test_stranded_name_moves_to_the_row_whose_model_it_carries():
    """The reported defect, end to end: row 1 keeps only its own name and row
    2 — which shipped its bare part code — receives the name printed for it."""
    rows = [_row(source_page_no=16, source_row_index=1, description_raw=P16_CELL,
                 model_raw="LA6JL40 00763000567682", quantity_raw="20",
                 unit_price_raw="24,46", line_total_raw="489,20"),
            _row(source_page_no=16, source_row_index=2, description_raw="LA6EBU35",
                 model_raw="LA6EBU35", quantity_raw="70",
                 unit_price_raw="24,46", line_total_raw="1.712,20")]
    out, warns = _run(rows)
    assert out[0].description_raw == "CATHETER LA6JL40 LA 6F 100CM JL40"
    assert out[1].description_raw == "CATHETER LA6EBU35 LA 6F 100CM EB35"
    assert any("DESCRIPTION_SEGMENT_MOVED" in w for w in warns)
    assert any("DESCRIPTION_LEAD_RUN_REATTRIBUTED" in w for w in warns)


def test_the_repair_never_touches_a_value():
    rows = [_row(source_page_no=16, source_row_index=1, description_raw=P16_CELL,
                 model_raw="LA6JL40 00763000567682", quantity_raw="20",
                 unit_price_raw="24,46", line_total_raw="489,20"),
            _row(source_page_no=16, source_row_index=2, description_raw="LA6EBU35",
                 model_raw="LA6EBU35", quantity_raw="70",
                 unit_price_raw="24,46", line_total_raw="1.712,20")]
    out, _ = _run(rows)
    assert (out[0].quantity_raw, out[0].unit_price_raw,
            out[0].line_total_raw) == ("20", "24,46", "489,20")
    assert (out[1].quantity_raw, out[1].line_total_raw) == ("70", "1.712,20")


def test_leading_continuation_run_is_removed_from_the_declared_description():
    """A cell OPENING with a batch run is carrying the previous row's tail.
    `_LEAD_OVERFLOW` only ever matched a run starting with a bare number, so
    the COO-led shape below was left in the declared description."""
    rows = [_row(source_page_no=14, source_row_index=1, description_raw="X", model_raw="LA6JL30",
                 quantity_raw="1", line_total_raw="1.00"),
            _row(source_page_no=15, source_row_index=1, model_raw="LA6JL35", quantity_raw="70",
                 line_total_raw="1712.20",
                 description_raw=("COO: Mexico 231027541 1 EA COO: Mexico CATHETER LA6JL35 "
                                  "LA 6F 100CM JL35 Batch: 231755547 4 EA COO: Mexico"))]
    out, warns = _run(rows)
    assert out[1].description_raw.startswith("CATHETER LA6JL35")
    assert "COO: Mexico 231027541" not in out[1].description_raw
    assert any("DESCRIPTION_LEAD_RUN_REATTRIBUTED" in w for w in warns)


def test_name_printed_on_a_value_less_line_is_recovered():
    """Page 8: the cell holds only the previous row's continuation and the real
    name sits on a line with every value column empty.  The parser must not
    make a row out of that line (a fragment that gains values is how a phantom
    item once reached a declaration) — but the NAME on it is real and printed."""
    page = SimpleNamespace(page_no=8, plain_text="\n".join([
        "|  MODEL NO. | DESCRIPTION | QTY SHIPPED | U/M | UNIT PRICE (USD) | TOTAL (USD)  |",
        "| --- | --- | --- | --- | --- | --- |",
        f"|  RONYX40034X 00763000248932 | {P8_CELL} | 6 | EA | 320.00 | 1.920.00  |",
        P8_FRAGMENT_LINE,
    ]))
    rows = [_row(source_page_no=8, source_row_index=1, description_raw=P8_CELL,
                 model_raw="RONYX40034X 00763000248932", quantity_raw="6",
                 unit_price_raw="320.00", line_total_raw="1.920.00")]
    out, warns = _run(rows, [page])
    assert out[0].description_raw == "STENT RONYX40034X ONYX 4.00X34RX"
    assert any("DESCRIPTION_SEGMENT_MOVED" in w for w in warns)
    assert any("QTY_CONFIRMED" in w for w in warns), "6 EA on the line == the row's printed 6"


def test_a_name_that_cannot_be_attributed_is_reported_never_invented():
    rows = [_row(source_page_no=8, source_row_index=1, description_raw=P8_CELL,
                 model_raw="RONYX40034X", quantity_raw="6", line_total_raw="1920.00")]
    out, warns = _run(rows)
    assert out[0].description_raw == P8_CELL, "the cell must be left exactly as printed"
    assert any("DESCRIPTION_ROW_NAME_MISSING" in w for w in warns)


def test_the_original_printed_cell_is_kept_for_packing_list_matching():
    """The packing list was printed against what THIS row printed, so moving a
    name in must not re-key evidence matching — that would move weight
    allocation silently."""
    rows = [_row(source_page_no=16, source_row_index=1, description_raw=P16_CELL,
                 model_raw="LA6JL40 00763000567682", quantity_raw="20", line_total_raw="489,20"),
            _row(source_page_no=16, source_row_index=2, description_raw="LA6EBU35",
                 model_raw="LA6EBU35", quantity_raw="70", line_total_raw="1.712,20")]
    out, _ = _run(rows)
    assert out[1].description_printed_raw == "LA6EBU35"
    assert out[0].description_printed_raw is None      # row 1 only lost text, gained none


# --------------------------------------------------------------------------- #
# 3. safety — the pinned incidents must not move
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc", [
    "GENUINE MADE IN ITALY WALLET",
    "SINGLE ORIGIN COFFEE",
    "COC CERTIFIED PIPE FITTINGS",
    "Job Lot Mixed Hardware",
    "BATCH MIXER 400",
    "LEATHER BAG COO:Ireland WALLET",
])
def test_descriptions_without_part_codes_are_never_touched(desc):
    """The guard ladder: a cell whose prose fragments carry NO part code can
    never be attributed, so it is returned byte-identical.  This is where every
    incident recorded in rules.field_allocation lands."""
    rows = [_row(description_raw=desc, model_raw="X1234", quantity_raw="1",
                 line_total_raw="10.00"),
            _row(source_row_index=2, description_raw="OTHER GOODS", model_raw="Y5678",
                 quantity_raw="1", line_total_raw="10.00")]
    out, _ = _run(rows)
    assert out[0].description_raw == desc


def test_a_row_that_already_has_a_name_is_never_overwritten():
    """A move may only ever fill a hole."""
    cell = ("CATHETER LA6JL40 LA 6F 100CM JL40 Batch: 1 1 EA COO: Mexico "
            "CATHETER LA6EBU35 LA 6F 100CM EB35")
    rows = [_row(source_page_no=16, description_raw=cell, model_raw="LA6JL40",
                 quantity_raw="20", line_total_raw="489.20"),
            _row(source_page_no=16, source_row_index=2,
                 description_raw="CATHETER LA6EBU35 ALREADY NAMED", model_raw="LA6EBU35",
                 quantity_raw="70", line_total_raw="1712.20")]
    out, warns = _run(rows)
    assert out[1].description_raw == "CATHETER LA6EBU35 ALREADY NAMED"
    assert any("DESCRIPTION_SEGMENT_UNRESOLVED" in w for w in warns)


def test_an_ordinary_single_name_cell_is_a_no_op():
    rows = [_row(description_raw="STENT RONYX22522X ONYX 2.25X22RX Batch: 0012143849 1 EA "
                                 "COO: Ireland", model_raw="RONYX22522X",
                 quantity_raw="1", line_total_raw="320.00"),
            _row(source_row_index=2, description_raw="STENT RONYX25012X ONYX 2.50X12RX",
                 model_raw="RONYX25012X", quantity_raw="1", line_total_raw="320.00")]
    before = [r.description_raw for r in rows]
    out, warns = _run(rows)
    assert [r.description_raw for r in out] == before
    assert warns == []


def test_a_packing_list_is_left_alone():
    payload, warnings = attribute_row_descriptions(
        DeclaredRole.PACKING_LIST, [], _payload([]), [])
    assert warnings == []


def test_part_codes_are_read_in_printed_order():
    assert part_codes("CATHETER LA6EBU35 LA 6F 100CM EB35")[0] == "LA6EBU35"
