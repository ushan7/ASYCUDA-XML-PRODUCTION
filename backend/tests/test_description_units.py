"""Description unit conversion — the gaps that produced HIGH-confidence lies.

Each case here was wrong before, and wrong in the worst possible way: a
plausible number at HIGH confidence, which under the old ladder outranked the
invoice's own printed weight.  The preserve-cases at the bottom are the guards
that must survive every future change to this module.
"""
from decimal import Decimal

import pytest

from app.rules.description_weight import net_from_description

D = Decimal


def _net(desc, qty="1", uom="PCS"):
    r = net_from_description(desc, D(qty), uom)
    return None if r is None else r.net_kg


def _conf(desc, qty="1", uom="PCS"):
    r = net_from_description(desc, D(qty), uom)
    return None if r is None else r.confidence


def test_thousands_separator_is_not_a_decimal_point():
    """"2,500 G" is 2.5 kg.  Read as 2.5 g it declared the bag at 0.0025 kg —
    a thousandfold under-declaration, at HIGH confidence."""
    assert _net("SUGAR 2,500 G BAG") == D("2.5")


def test_pack_multiplier_binds_only_to_the_value_the_pack_names():
    """"24 X 250 ML ... CARTON NET 6 KG": the 24 multiplies the bottles, not
    the carton's stated net weight.  Applied to the 6 kg it turned a 600 kg
    line into 14 400 kg."""
    assert _net("HAND WASH 250 ML BOTTLE, 24 X 250 ML, CARTON NET 6 KG", "100", "CTN") == D("600")


def test_pack_multiplier_still_applies_to_the_value_it_does_name():
    assert _net("10 CTN 24 X 500 ML SHAMPOO", "10", "CTN") == D("120.000")


def test_outer_package_weight_loses_to_the_goods_own_content():
    """Two good numbers, and the 5 kg is the box."""
    assert _net("500 ML SHAMPOO IN 5 KG CARTON", "10") == D("5.000")


def test_net_marker_promotes_a_weight_printed_next_to_a_package_word():
    assert _net("SHAMPOO, CARTON NET 6 KG", "1", "CTN") == D("6")


def test_a_package_only_weight_is_still_used_but_flagged():
    r = net_from_description("WIDGET, MASTER CARTON 12 KG", D("1"), "CTN")
    assert r.net_kg == D("12") and r.confidence == "LOW"
    assert any("shipping package" in w for w in r.warnings)


def test_concentration_is_never_read_as_a_net_weight():
    assert _net("AMOXICILLIN 250 MG/5 ML SYRUP") is None


def test_kg_per_metre_formula_is_reachable():
    """The bare mass search matched the "kg" inside "0.5 KG/M" and returned
    0.5 kg per piece, so this formula could never run."""
    assert _net("STEEL WIRE 0.5 KG/M, 100 M") == D("50.0")


def test_g_per_metre_formula_is_reachable():
    assert _net("COTTON CORD 250 G/M, 40 M") == D("10.000")


def test_gsm_formula_still_works():
    assert _net("POLYESTER FABRIC 200 GSM 2 M X 1.5 M") == D("0.600")


def test_denier_formula_still_works():
    assert _net("YARN 150 DENIER 1000 M") == D("150000") / D("9000000")


@pytest.mark.parametrize("desc,expected", [
    ("CARGO 2 SHORT TON", D("1814.36948")),
    ("CARGO 2 US TON", D("1814.36948")),
    ("CARGO 2 LONG TON", D("2032.0938176")),
    ("CARGO 2 IMPERIAL TON", D("2032.0938176")),
    ("CARGO 2 METRIC TONS", D("2000")),
    ("CARGO 2 TONS", D("2000")),
])
def test_ton_variants(desc, expected):
    assert _net(desc) == expected


def test_imperial_gallon_is_not_converted_at_the_us_value():
    assert _net("SHAMPOO 10 IMPERIAL GAL DRUM") == D("45.460")
    assert _net("SHAMPOO 10 US GAL DRUM") == D("37.85411784000")


def test_bare_gallon_uses_the_us_value_and_says_so():
    r = net_from_description("SHAMPOO 10 GAL DRUM", D("1"), "PCS")
    assert r.confidence == "LOW"
    assert any("Imperial" in w for w in r.warnings)


def test_oz_only_converts_on_a_real_net_marker():
    """`"net" in low` matched MAGNET, BONNET and CABINET."""
    assert _net("MAGNET ASSEMBLY 2 OZ") is None
    assert _net("STEEL BONNET 4 OZ") is None
    assert _net("BRASS FITTING NET 4 OZ") == D("4") * D("0.028349523125")


# --------------------------------------------------------------------------- #
# PRESERVE — these refusals are the guards, not omissions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc", [
    "GUIDEWIRE PTFE 2 ST",               # ST is set/sterile far more often than stone
    "FACE CREAM 2 oz",                   # ambiguous oz on a cosmetic
    "SOFA SET 1.2 CBM",                  # package volume, not net weight
    "CABLE 100 m",                       # length alone
    "PARACETAMOL 500 MG TABLETS",        # dosage strength
    "STAINLESS STEEL 316 L SUTURE",      # ambiguous unit AND assumed density
])
def test_refusals_are_preserved(desc):
    assert _net(desc) is None


def test_needle_gauge_still_converts_only_at_low_confidence():
    assert _conf("NEEDLE 22 G") == "LOW"
