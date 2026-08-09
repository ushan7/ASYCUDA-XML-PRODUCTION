"""Weight-unit normalization — the single conversion boundary for mass.

Every weight that enters the pipeline (invoice item weight, packing-list row
gross/net, air-waybill gross, reviewer entry) carries a printed unit.  This
module is the ONE place that turns ``(value, unit_raw)`` into kilograms.

The rule that matters (spec 2026-07-21): a unit that is *present but not
recognized* DISQUALIFIES its source — the caller falls through to the next
priority and warns.  It is never silently assumed to be kilograms.  A silent
``factor = 1`` default is what let ``500 G`` per unit become 500 kg and put an
entire consignment's net weight above its gross weight.

``None``/blank unit means "the document did not say", which for a weight column
on a customs document means kilograms; that assumption is recorded by the
caller, not hidden here.
"""
from __future__ import annotations

import re
from decimal import Decimal

# canonical code -> kilograms per unit
WEIGHT_UNIT_TO_KG: dict[str, Decimal] = {
    "KGM": Decimal("1"),
    "G": Decimal("0.001"),
    "MG": Decimal("0.000001"),
    "LB": Decimal("0.45359237"),
    "OZ": Decimal("0.028349523125"),
    "T": Decimal("1000"),
}

# printed spelling -> canonical code
_UNIT_ALIASES: dict[str, str] = {
    "KG": "KGM", "KGS": "KGM", "KGM": "KGM", "KILO": "KGM", "KILOS": "KGM",
    "KILOGRAM": "KGM", "KILOGRAMS": "KGM", "KILOGRAMME": "KGM", "KILOGRAMMES": "KGM",
    "G": "G", "GM": "G", "GMS": "G", "GR": "G", "GRAM": "G", "GRAMS": "G",
    "GRAMME": "G", "GRAMMES": "G",
    "MG": "MG", "MILLIGRAM": "MG", "MILLIGRAMS": "MG",
    "LB": "LB", "LBS": "LB", "POUND": "LB", "POUNDS": "LB",
    "OZ": "OZ", "OUNCE": "OZ", "OUNCES": "OZ",
    "T": "T", "TON": "T", "TONS": "T", "TONNE": "T", "TONNES": "T", "MT": "T",
    "METRICTON": "T", "METRICTONS": "T",
}


def normalize_weight_unit(raw: str | None) -> str | None:
    """Printed unit -> canonical code, or ``None`` when unrecognized/blank."""
    if not raw:
        return None
    return _UNIT_ALIASES.get(re.sub(r"[^A-Za-z]", "", str(raw)).upper() or "")


def unit_factor(raw: str | None) -> Decimal | None:
    """Kilograms per printed unit, or ``None`` when the unit is unrecognized."""
    code = normalize_weight_unit(raw)
    return WEIGHT_UNIT_TO_KG.get(code) if code else None


def to_kg(value: Decimal | None, unit_raw: str | None) -> tuple[Decimal | None, bool]:
    """Convert ``value`` (in ``unit_raw``) to kilograms.

    Returns ``(kg, recognized)``:

    * ``(x, True)``    — converted; a blank unit is taken as already-kg
    * ``(None, False)`` — the unit was PRINTED but is not a mass unit we know.
      The caller MUST discard this weight and fall through to its next source,
      emitting a warning.  Never treat this case as kilograms.
    """
    if value is None:
        return None, True
    if not (unit_raw or "").strip():
        return value, True                    # unit absent -> already kilograms
    factor = unit_factor(unit_raw)
    if factor is None:
        return None, False
    return value * factor, True
