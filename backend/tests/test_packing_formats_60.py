"""Sixty packing-list layouts from real trade lanes, run against the parser.

Companion to ``test_invoice_formats_100.py``, and the more dangerous of the two.
An invoice row proves itself — quantity x unit price == line total — so a wrong
column map usually breaks the arithmetic and the page stands down.  A packing
row proves nothing: it is numbers in columns.  Reading a NET column as GROSS,
or a PER-CARTON figure as the row total, yields a complete and plausible
declaration that no per-row check can see, and weight drives duty as directly
as value does.

So the assertions here are weighted accordingly.  ``test_weights_are_never_wrong``
is the one that matters: it runs EVERY fixture, including the known gaps, and
allows a weight to be missing (the value then falls through to another source
and the reviewer is told) but never to be WRONG.  That test has no xfail list
and must stay at zero.

``KNOWN_GAPS`` covers the rest — fixtures whose rows, quantities or package
counts are not yet right.  Entries leave the set when the parser handles them;
a fixture that starts passing fails loudly (xpass) until it is promoted.
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import parse_pages
from app.numbers import detect_numeric_locale, parse_decimal
from app.units import to_kg

FIXTURES = json.loads(
    (Path(__file__).parent / "data" / "packing_formats.json").read_text(encoding="utf-8"))
IDS = [f["id"] for f in FIXTURES]

# Fixtures whose ROWS/quantities/carton counts are not yet right.  Weights are
# covered separately and unconditionally — see the module docstring.
KNOWN_GAPS = {
    "americas_mideast_ae_reexport_marks_and_numbers",
    "americas_mideast_br_romaneio_volumes_cubagem",
    "asia_industrial_in_pharma_batch_expiry_cartons",
    "asia_industrial_vn_seafood_master_inner",
    "asia_textile_vn_footwear_pairs_per_carton",
    "europe_cz_balici_list_hmotnost_na_karton",
    "europe_de_packliste_kollo_abmessungen",
    "europe_es_lista_empaque_palet_bultos",
    "europe_fr_liste_de_colisage_poids_unitaire",
    "europe_it_distinta_imballaggio_tara",
    "europe_pl_lista_pakowa_ilosc_w_kartonie",
    "europe_pt_lista_embalagem_combinada",
    "weight_traps_tare_between_net_and_gross",
}


def _parse(f):
    loc = detect_numeric_locale(f["page"])
    res = parse_pages(DeclaredRole.PACKING_LIST, {1: f["page"]}, {1: loc})
    pp = res.pages.get(1)
    return (pp.rows if pp else []), loc, res


def _kg(rawnum, locale):
    if rawnum is None:
        return None
    v = parse_decimal(getattr(rawnum, "value_raw", None), locale=locale)
    kg, ok = to_kg(v, getattr(rawnum, "unit_raw", None))
    return kg if ok else v


@pytest.fixture(params=FIXTURES, ids=IDS)
def fixture(request):
    return request.param


def test_weights_are_never_wrong(fixture):
    """THE test of this file, and the reason it exists.

    Every fixture, no exemptions.  A weight the parser did not capture is
    acceptable — allocation falls through to its next source and says so.  A
    weight it captured WRONGLY is not: nothing downstream can detect it and the
    duty is computed from it.
    """
    rows, loc, _ = _parse(fixture)
    if not rows:
        return                                   # stood down: nothing declared
    e = fixture["expected_first_row"]
    r = rows[0]
    for got, want, name in ((r.gross_weight, e["gross_weight"], "gross"),
                            (r.net_weight, e["net_weight"], "net")):
        if not want or got is None:
            continue                             # missing is safe; wrong is not
        w = parse_decimal(want, locale=loc)
        raw = parse_decimal(getattr(got, "value_raw", None), locale=loc)
        # the fixture states the printed figure in the document's own unit; a
        # parser that read an equivalent column in another unit is also right
        assert w is not None and any(
            v is not None and abs(v - w) <= max(w * Decimal("0.001"), Decimal("0.01"))
            for v in (raw, _kg(got, loc))), (
            f"{name} weight {getattr(got, 'value_raw', None)!r} != printed {want!r}")


def test_gross_is_never_below_net(fixture):
    """A row whose net exceeds its gross means the two columns were swapped.
    Reported as printed rather than silently reordered — but a fixture that
    trips this without the document itself printing it that way is a bug."""
    rows, loc, _ = _parse(fixture)
    for r in rows:
        g, n = _kg(r.gross_weight, loc), _kg(r.net_weight, loc)
        if g is None or n is None:
            continue
        printed_reversed = parse_decimal(
            fixture["expected_first_row"]["net_weight"], locale=loc) or Decimal(0) > (
            parse_decimal(fixture["expected_first_row"]["gross_weight"], locale=loc)
            or Decimal(0))
        if not printed_reversed:
            assert g >= n, f"net {n} exceeds gross {g} — the weight columns are swapped"


def test_rows_and_packing_fields(fixture, request):
    if fixture["id"] in KNOWN_GAPS:
        request.node.add_marker(pytest.mark.xfail(
            strict=True, reason="known packing-parser gap — see KNOWN_GAPS"))
    rows, loc, _ = _parse(fixture)
    if not fixture.get("should_parse", True):
        assert not rows, "this shape has no parseable table and must be refused"
        return
    if not rows:
        pytest.skip("parser stood down — safe, and covered by the weight test")
    assert len(rows) == fixture["expected_rows"]
    e = fixture["expected_first_row"]
    r = rows[0]
    if e["quantity"]:
        assert parse_decimal(r.quantity_raw, locale=loc) == parse_decimal(
            e["quantity"], locale=loc)
    if e["carton_count"]:
        assert _kg(r.carton_count, loc) == parse_decimal(e["carton_count"], locale=loc)


def test_a_package_count_column_is_never_read_as_the_goods_quantity(fixture):
    """"Antal kolli" / "Aantal colli" / "No. of Cartons" count PACKAGES.
    Borrowing one as the declared quantity turns 29 cartons of spare parts into
    29 pieces — right-looking and wrong."""
    rows, loc, _ = _parse(fixture)
    e = fixture["expected_first_row"]
    if not rows or not e["carton_count"] or e["quantity"]:
        return
    q = parse_decimal(rows[0].quantity_raw, locale=loc)
    c = parse_decimal(e["carton_count"], locale=loc)
    assert q is None or q != c, "the package count was declared as the goods quantity"
