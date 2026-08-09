"""Packing-list column roles: the document's own totals decide, not the model.

A packing row has no self-verifying arithmetic.  An invoice row proves itself
(quantity x unit price == line total); a packing row is just numbers in
columns, so reading a NET column as GROSS, or a PER-CARTON figure as the row
total, produces a complete and plausible declaration that is silently wrong —
and weight drives duty.  The only cross-check a packing list carries is the
totals it prints about itself.

That is why the escalation added here is shaped the way it is.  When the
header-derived column map fails the printed-totals gate, a model may be asked
which column is which — but it returns COLUMN INDICES ONLY, never a number, and
its proposal is re-parsed and kept only when the parsed sums then match the
printed totals.  The model proposes; the document disposes.

These tests pin both halves: the override mechanism itself, and the refusal to
accept an override that does not reconcile.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import (
    packing_column_candidates, parse_pages)
from app.numbers import detect_numeric_locale

# A packing list whose weight columns are printed in the REVERSE of the usual
# order and labelled only "Weight 1" / "Weight 2" — no vocabulary can read it,
# and the printed totals are the only thing that can.
AMBIGUOUS = """
| C/NO. | DESCRIPTION | CTNS | Weight 1 (KGS) | Weight 2 (KGS) |
| --- | --- | --- | --- | --- |
| 1-20 | COTTON TOWEL SET | 20 | 240.00 | 220.00 |
| 21-45 | BATH ROBE | 25 | 300.00 | 275.00 |
| 46-60 | KITCHEN NAPKIN | 15 | 180.00 | 165.00 |
| TOTAL |  | 60 | 720.00 | 660.00 |
"""

CLEAR = """
| C/NO. | DESCRIPTION | CTNS | N.W. (KGS) | G.W. (KGS) |
| --- | --- | --- | --- | --- |
| 1-20 | COTTON TOWEL SET | 20 | 220.00 | 240.00 |
| 21-45 | BATH ROBE | 25 | 275.00 | 300.00 |
| 46-60 | KITCHEN NAPKIN | 15 | 165.00 | 180.00 |
| TOTAL |  | 60 | 660.00 | 720.00 |
"""


def _parse(page, roles=None):
    loc = detect_numeric_locale(page)
    return parse_pages(DeclaredRole.PACKING_LIST, {1: page}, {1: loc}, column_roles=roles)


def test_candidates_expose_the_header_and_sample_rows():
    """What the model is shown: the header cells and a couple of data rows —
    never asked to read a value, only to label a column."""
    cand = packing_column_candidates({1: AMBIGUOUS})
    assert cand is not None
    assert cand["header"][3] == "Weight 1 (KGS)"
    assert cand["rows"][0][1] == "COTTON TOWEL SET"
    assert len(cand["rows"]) == 2


def test_candidates_are_none_without_a_header():
    assert packing_column_candidates({1: "just some prose, no table at all"}) is None


def test_a_correct_role_override_reconciles_and_parses():
    """Weight 1 is gross, Weight 2 is net — and the printed totals confirm it."""
    res = _parse(AMBIGUOUS, roles={"gross_wt": 3, "net_wt": 4})
    assert res.confirmed_row_count() == 3
    assert any("matches the printed total" in n for n in res.notes)
    r = res.pages[1].rows[0]
    assert r.gross_weight.value_raw == "240.00"
    assert r.net_weight.value_raw == "220.00"


def test_an_override_onto_a_text_column_yields_no_weight_and_no_confirmation():
    """The failure the acceptance test has to catch: a role pointing at a text
    column produces NO weights, which would reconcile vacuously.  Nothing
    positively matches a printed total, so the caller must not accept it."""
    res = _parse(AMBIGUOUS, roles={"gross_wt": 1})
    assert all(r.gross_weight is None for r in res.pages[1].rows)
    assert not any("gross_wt sum" in n and "matches" in n for n in res.notes)


def test_an_out_of_range_role_is_ignored_not_applied():
    res = _parse(CLEAR, roles={"gross_wt": 99})
    r = res.pages[1].rows[0]
    assert r.gross_weight.value_raw == "240.00"      # the header reading stands


def test_an_override_takes_the_column_away_from_whichever_key_held_it():
    """Roles are exclusive: reassigning a column must not leave it mapped twice,
    or one figure is declared as both the net and the gross weight."""
    res = _parse(CLEAR, roles={"net_wt": 4})
    r = res.pages[1].rows[0]
    assert r.net_weight.value_raw == "240.00"
    assert r.gross_weight is None                    # column 4 was taken from gross


def test_the_unaided_parser_still_owns_a_clearly_labelled_list():
    """The escalation must stay unreachable for ordinary documents."""
    res = _parse(CLEAR)
    assert res.confirmed_row_count() == 3
    r = res.pages[1].rows[0]
    assert (r.net_weight.value_raw, r.gross_weight.value_raw) == ("220.00", "240.00")


def test_reversed_labels_are_read_as_printed_not_as_assumed():
    """Net larger than gross is what a REVERSED pair looks like.  The parser
    reports what the header says; it must not silently swap them to satisfy an
    expectation, because a swap invents a fact the document does not state."""
    # the whole table reversed, totals included, so the document is internally
    # consistent and only the net/gross ORDER is unusual
    page = """
| C/NO. | DESCRIPTION | CTNS | N.W. (KGS) | G.W. (KGS) |
| --- | --- | --- | --- | --- |
| 1-20 | COTTON TOWEL SET | 20 | 240.00 | 220.00 |
| 21-45 | BATH ROBE | 25 | 300.00 | 275.00 |
| 46-60 | KITCHEN NAPKIN | 15 | 180.00 | 165.00 |
| TOTAL |  | 60 | 720.00 | 660.00 |
"""
    res = _parse(page)
    r = res.pages[1].rows[0]
    assert r.net_weight.value_raw == "240.00"
    assert r.gross_weight.value_raw == "220.00"


@pytest.mark.parametrize("roles", [{"gross_wt": 3, "net_wt": 3}, {}, None])
def test_degenerate_role_sets_never_crash(roles):
    res = _parse(CLEAR, roles=roles)
    assert res.confirmed_row_count() in (0, 3)
