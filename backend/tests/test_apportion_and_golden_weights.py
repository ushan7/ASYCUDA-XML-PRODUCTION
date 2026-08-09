"""Regression harness for the allocation invariants (docs/allocation-spec.md).

Three layers, because the previous defects each slipped past a different one:

* PROPERTY  — apportion's contract holds for arbitrary inputs.  The carton
  allocator used to break its own exact-sum invariant only at specific item
  counts (200 items in 3 cartons summed to 3.99), which no hand-written example
  happened to cover.
* VECTOR    — the spec's own worked examples, verbatim.
* GOLDEN    — the bundled 119-item demo reproduces the reference declaration's
  item weights exactly.  This is the check that would have caught the 0.3/0.7
  drift, and it is the acceptance test for the whole allocation rewrite.
"""
from __future__ import annotations

import random
import re
from decimal import Decimal as D

import pytest

from app.numbers import q3_down
from app.rules.description_weight import net_from_description
from app.rules.models import WorkItem
from app.rules.packing_match import match_packing
from app.rules.weight_carton import allocate_weights_and_cartons, apportion

P2 = D("0.01")
P3 = D("0.001")


def _item(seq, desc, qty="1", total="100", uom="PCS"):
    return WorkItem(
        xml_item_sequence=seq, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=seq, source_invoice_item_no=None,
        description_raw=desc, quantity=D(qty), invoice_uom_raw=uom,
        unit_price=D("1"), line_total=D(total), currency="USD")


# --------------------------------------------------------------------------- #
# PROPERTY — apportion's contract
# --------------------------------------------------------------------------- #
def test_apportion_contract_holds_for_random_inputs():
    """sum == total exactly, every floor respected, or None. Never approximate."""
    rnd = random.Random(20260721)
    for _ in range(4000):
        k = rnd.randint(1, 40)
        total = D(rnd.randint(1, 50_000)) / D(1000)
        floors = [D(rnd.randint(0, 30)) / D(1000) for _ in range(k)]
        basis = [D(rnd.randint(0, 100)) for _ in range(k)]
        out = apportion(total, basis, floors, P3)
        if out is None:
            assert sum(floors) > total, "refused a feasible allocation"
            continue
        assert sum(out) == total, f"sum drifted: {sum(out)} != {total}"
        assert all(out[i] >= floors[i] for i in range(k)), "floor violated"


def test_apportion_refuses_rather_than_approximating():
    assert apportion(D("1.000"), [D("1"), D("1")], [D("0.800"), D("0.900")], P3) is None
    # ... and the carton form: 200 items cannot each hold 0.01 CTN out of 1.00
    assert apportion(D("1.00"), [D("1")] * 200, [P2] * 200, P2) is None


def test_apportion_floor_is_a_constraint_not_an_additive_base():
    """A large fixed net on one item must not distort the others' shares.

    Adding the floor to the proportional share gave [44.000, 56.000] for a
    packing basis of 30/70 — the split has to stay 30/70 while still clearing
    the floor.
    """
    assert apportion(D("100.000"), [D("30"), D("70")],
                     [D("20.001"), D("0.001")], P3) == [D("30.000"), D("70.000")]
    # when a floor genuinely binds, it is honoured and the sum stays exact
    out = apportion(D("100.000"), [D("30"), D("70")], [D("80.001"), D("0.001")], P3)
    assert out[0] >= D("80.001") and sum(out) == D("100.000")


def test_apportion_spreads_the_residual_instead_of_dumping_it():
    """200 items in 3 cartons. The old allocator dumped the whole delta on one
    index, drove it negative, clamped to 0.01 and silently summed to 3.99."""
    out = apportion(D("3.00"), [D("1")] * 200, [P2] * 200, P2)
    assert sum(out) == D("3.00")
    assert min(out) >= P2


def test_apportion_equal_split_when_basis_is_all_zero():
    out = apportion(D("10.000"), [D("0"), D("0")], [P3, P3], P3)
    assert sum(out) == D("10.000") and out[0] == out[1]


# --------------------------------------------------------------------------- #
# VECTOR — the spec's own worked examples
# --------------------------------------------------------------------------- #
def test_spec_vector_pack_multiplier_by_carton_and_by_piece():
    """10 CTN x 24 x 500 ml = 120 kg. The same goods billed per piece are the
    bottle count already, so the multiplier must not apply."""
    by_carton = net_from_description("SHAMPOO 24 x 500 ml", D("10"), "CTN")
    assert by_carton.net_kg == D("120.000") and by_carton.confidence == "HIGH"
    by_piece = net_from_description("SHAMPOO 24 x 500 ml", D("72"), "EA")
    assert by_piece.net_kg == D("36.000") and by_piece.confidence == "LOW"


def test_spec_vector_duplicate_packing_rows_are_summed():
    """Item A = 1 + 2 + 0.5 CTN groups to 3.5 CTN before any assignment."""
    items = [_item(1, "ITEM A")]
    payload = type("P", (), {"rows": [
        type("R", (), {"source_page_no": 1, "source_row_index": i + 1,
                       "description_raw": "ITEM A", "quantity_raw": "1",
                       "gross_weight": None, "net_weight": None,
                       "carton_count": type("N", (), {"value_raw": v, "unit_raw": None})(),
                       "shared_carton_group_raw": None})()
        for i, v in enumerate(["1", "2", "0.5"])], "page_numeric_locales": {}})()
    ev = match_packing(items, [payload])
    assert ev[1].carton_count == D("3.5")


def test_spec_vector_shared_carton_total_is_not_duplicated():
    """Three items sharing 2 CTN split it without changing the group total."""
    items = [_item(1, "ONE"), _item(2, "TWO"), _item(3, "THREE")]
    payload = type("P", (), {"rows": [
        type("R", (), {"source_page_no": 1, "source_row_index": i + 1,
                       "description_raw": d, "quantity_raw": q,
                       "gross_weight": None, "net_weight": None,
                       "carton_count": type("N", (), {"value_raw": "2", "unit_raw": None})(),
                       "shared_carton_group_raw": "GRP1"})()
        for i, (d, q) in enumerate([("ONE", "3"), ("TWO", "3"), ("THREE", "4")])],
        "page_numeric_locales": {}})()
    ev = match_packing(items, [payload])
    assert sum(ev[s].carton_count for s in (1, 2, 3)) == D("2")


# --------------------------------------------------------------------------- #
# INVARIANTS — end to end through the allocator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n,gross,ctn", [(1, "12", "2"), (3, "199", "12"),
                                         (40, "78.5", "5"), (119, "199", "12")])
def test_allocation_invariants_hold(n, gross, ctn):
    items = [_item(i + 1, f"ITEM {i}", qty=str(i + 1), total=str(100 + i)) for i in range(n)]
    msgs = allocate_weights_and_cartons(items, {}, D(gross), D(ctn), packing_present=False)
    assert not [m for m in msgs if m.severity.value == "BLOCKING"]
    assert sum(i.gross_weight_kg for i in items) == D(gross)      # exact
    assert sum(i.package_count for i in items) == D(ctn)          # exact
    assert all(i.net_weight_kg < i.gross_weight_kg for i in items)
    assert all(i.package_count >= D("0.01") for i in items)
    assert [i.xml_item_sequence for i in items] == list(range(1, n + 1))  # order kept


def test_ratio_net_follows_the_final_gross_rounded_down():
    """net = ROUND_DOWN(0.7 x FINAL gross, 3).

    Rounding DOWN is required: at gross 0.001 a half-up 0.7 x 0.001 = 0.0007
    would come back as 0.001 and equal its own gross.  It also never
    over-declares a net weight.  When gross lands on 2dp — as it does
    throughout the reference declaration — 0.7 x gross is exactly 3dp and no
    rounding occurs at all.
    """
    items = [_item(i + 1, f"ITEM {i}", qty=str(i + 1)) for i in range(6)]
    allocate_weights_and_cartons(items, {}, D("199"), D("12"), packing_present=False)
    for it in items:
        assert it.net_weight_kg == q3_down(D("0.7") * it.gross_weight_kg)
        assert it.net_weight_kg < it.gross_weight_kg


def test_ratio_net_is_exact_when_gross_lands_on_two_decimals():
    items = [_item(1, "ALPHA"), _item(2, "BETA")]
    allocate_weights_and_cartons(items, {}, D("20.00"), D("4"), packing_present=False)
    for it in items:
        assert it.gross_weight_kg == D("10.000")
        assert it.net_weight_kg == D("7.000") == D("0.7") * it.gross_weight_kg


def test_infeasible_input_assigns_no_weights():
    """Final Condition 2 says stop, not reconcile. A 'best-effort' proportional
    gross is what produced a declaration with net > gross on every line."""
    items = [_item(1, "A"), _item(2, "B")]
    items[0].item_weight_kg, items[0].item_weight_scope = D("500"), "LINE_TOTAL"
    msgs = allocate_weights_and_cartons(items, {}, D("12"), D("2"), packing_present=False)
    assert any(m.code == "GROSS_ALLOCATION_IMPOSSIBLE" for m in msgs)
    assert all(i.gross_weight_kg is None for i in items)
    # and the diagnostic names the cause rather than only the symptom
    assert any(m.code == "NET_TO_GROSS_RATIO_IMPLAUSIBLE" for m in msgs)


# --------------------------------------------------------------------------- #
# GOLDEN — the demo reproduces the reference declaration's item weights
# --------------------------------------------------------------------------- #
def _weights(xml: str) -> list[tuple[str, str]]:
    return list(zip(re.findall(r"<Gross_weight_itm>([^<]*)", xml),
                    re.findall(r"<Net_weight_itm>([^<]*)", xml)))


def test_demo_reproduces_reference_item_weights(tmp_path, monkeypatch):
    """The acceptance test for the allocation rewrite.

    Every item in sample_xml_format.xml satisfies net = 0.7 x gross. This test
    fails on any drift in the ratio, the precision, the ladder or the
    reconciliation — the exact class of change that shipped unnoticed before.
    """
    from app.config import BACKEND_ROOT
    from app.database import get_session, init_db
    from app.demo import seed_demo_job
    from app import services

    init_db()
    db = next(get_session())
    job = seed_demo_job(db)
    db.commit()
    services.critical_review(db, job)
    services.finalize_job(db, job, {})
    generated = services.latest_xml(db, job.id).xml_bytes.decode()

    reference = (BACKEND_ROOT / "sample_data" / "sample_xml_format.xml").read_text(encoding="utf-8")
    ref, got = _weights(reference), _weights(generated)

    assert len(got) == len(ref) == 119
    mismatches = [(i + 1, r, g) for i, (r, g) in enumerate(zip(ref, got))
                  if D(r[0]) != D(g[0]) or D(r[1]) != D(g[1])]
    assert not mismatches, f"item weights drifted from the reference: {mismatches[:5]}"
    assert all(D(g[1]) == D("0.7") * D(g[0]) for g in got)
    assert sum(D(g[0]) for g in got) == D("199.00")
