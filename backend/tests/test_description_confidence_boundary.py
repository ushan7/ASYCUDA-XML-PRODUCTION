"""The description-conversion confidence boundary — approved 2026-07-30.

Three buckets, pinned case by case so the line cannot drift silently:

* HIGH   — unambiguous mass or volume token, known density, pack multiplier
           consistent with the invoice UOM; GSM / denier / tex / kg-per-m /
           g-per-m with every variable present.
* LOW    — used but flagged: assumed density, ambiguous token, unverifiable
           pack multiplier, package-only weight, a US/Imperial-ambiguous unit,
           a space-thousands reading, or a context-suppressed multiplier.
* REFUSE — ambiguous token AND assumed density; dosage forms; concentrations
           and rates; container/appliance capacities; numeric ranges; CBM/m³;
           length or area alone; ST; bare oz on a liquid/cosmetic with no net
           marker.

A case moving between buckets is a rule change and belongs in
docs/allocation-spec.md in the same commit.
"""
from decimal import Decimal

import pytest

from app.rules.description_weight import net_from_description

D = Decimal


def _conf(desc, qty="1", uom="PCS"):
    r = net_from_description(desc, D(qty), uom)
    return None if r is None else r.confidence


# --------------------------------------------------------------------------- #
# HIGH — a stated content in an unambiguous unit, nothing assumed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,qty,uom", [
    ("RICE 5 KG BAG", "1", "PCS"),                       # unambiguous mass
    ("SHAMPOO 1000 ML", "1", "PCS"),                     # unambiguous volume, known density
    ("DETERGENT 2 LITRE BOTTLE", "1", "PCS"),
    ("10 CTN 24 X 500 ML SHAMPOO", "10", "CTN"),         # pack multiplier, pack-counting UOM
    ("CARGO 2 SHORT TON", "1", "PCS"),                   # explicit system, no ambiguity
    ("SHAMPOO 10 IMPERIAL GAL DRUM", "1", "PCS"),
    ("POLYESTER FABRIC 200 GSM 2 M X 1.5 M", "1", "PCS"),  # special formula, all variables
    ("YARN 150 DENIER 1000 M", "1", "PCS"),
    ("STEEL WIRE 0.5 KG/M, 100 M", "1", "PCS"),
    ("COTTON CORD 250 G/M, 40 M", "1", "PCS"),
])
def test_high(desc, qty, uom):
    assert _conf(desc, qty, uom) == "HIGH"


# --------------------------------------------------------------------------- #
# LOW — converts, but something was assumed, ambiguous or suppressed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,qty,uom,why", [
    ("LOTION 500 ML", "1", "PCS", "assumed-density band (lotion is estimated)"),
    ("UNKNOWN FLUID 500 ML", "1", "PCS", "assumed density, unknown liquid"),
    ("NEEDLE 22 G", "1", "PCS", "ambiguous token g"),
    ("DETERGENT 2 L", "1", "PCS", "ambiguous token l on a known density"),
    ("WIDGET 24 X 500 ML", "1", "WEIRDUOM", "pack multiplier vs unrecognized UOM"),
    ("WIDGET, MASTER CARTON 12 KG", "1", "CTN", "package-only weight"),
    ("SHAMPOO 10 GAL DRUM", "1", "PCS", "US/Imperial-ambiguous gallon"),
    ("SHAMPOO 12 FL OZ BOTTLE", "1", "PCS", "US/Imperial-ambiguous fl oz"),
    ("JUICE 2 QT CARTON", "1", "PCS", "US/Imperial-ambiguous quart"),
    ("SUGAR 2,500 G BAG", "1", "PCS", "ambiguous token g"),
    ("SODA ASH DENSE, NET WEIGHT 1 250 KG", "1", "CTN", "space thousands separator"),
    ("MEN'S COTTON T-SHIRT, 1 DOZEN PER POLYBAG, CARTON NET 6 KG", "100", "CTN",
     "dozen multiplier suppressed on a per-package weight"),
])
def test_low(desc, qty, uom, why):
    r = net_from_description(desc, D(qty), uom)
    assert r is not None and r.confidence == "LOW", why
    assert r.warnings, "every LOW carries its demotion reason"


# --------------------------------------------------------------------------- #
# REFUSE — nothing in the text safely states a weight
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,why", [
    ("STAINLESS STEEL 316 L SUTURE", "ambiguous token AND assumed density"),
    ("PARACETAMOL 500 MG TABLETS", "dosage form"),
    ("INSULIN 100 IU VIAL", "dosage form"),
    ("AMOXICILLIN 250 MG/5 ML SYRUP", "concentration"),
    ("SUBMERSIBLE WATER PUMP 1 HP, 100 LTR/MIN", "a flow rate"),
    ("PLASTIC WATER TANK 1000 LTR TRIPLE LAYER", "container capacity"),
    ("WATER HEATER 50 LITRE", "appliance capacity"),
    ("SOFA SET 1.2 CBM", "CBM is package volume"),
    ("CABLE 100 m", "length alone"),
    ("PANEL 2 M2 SHEET", "area alone"),
    ("GUIDEWIRE PTFE 2 ST", "ST is set/sterile, not stone"),
    ("FACE CREAM 2 oz", "bare oz on a cosmetic, no net marker"),
    ("MAGNET ASSEMBLY 2 OZ", "no net marker — and MAGNET is not 'net'"),
    ("SYRUP CONCENTRATE 2 L", "ambiguous token l AND an estimated density band"),
    ("RICE BAG 25-50 KG", "a numeric range"),
    ("PET BOTTLES 500-1000 ML JUICE", "a numeric range"),
])
def test_refuse(desc, why):
    assert net_from_description(desc, D("1"), "PCS") is None, why


# --------------------------------------------------------------------------- #
# The gate that keeps the boundary honest
# --------------------------------------------------------------------------- #
def test_every_low_result_names_its_reason():
    r = net_from_description("SHAMPOO 10 GAL DRUM", D("1"), "PCS")
    assert any("US" in w and "Imperial" in w for w in r.warnings)


def test_high_results_carry_no_warnings():
    r = net_from_description("RICE 5 KG BAG", D("1"), "PCS")
    assert r.confidence == "HIGH" and r.warnings == []
