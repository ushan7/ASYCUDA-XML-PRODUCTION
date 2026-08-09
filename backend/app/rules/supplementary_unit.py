"""Supplementary unit derivation.

Authority is the official tariff UNIT(S) of the final HS11 — never the invoice
UOM.  Each item gets exactly one valid supplementary code/name/quantity > 0 or a
blocking failure.  Formulas follow SUPPLIMENTARY UNIT QTY RULE PROMPT.txt
(PR/NPR = qty/2, DZN = qty/12, SQM = metres x 1.524, LTR/MTR = net-weight proxy)
with ADR-004 (pair divide-by-two) behind a config flag.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from ..config import get_settings
from ..domain.errors import ValidationMessage
from .models import WorkItem

_UNIT_NAME = {
    "UNT": "Unit", "KGM": "Kilogram", "PR": "Pair", "NPR": "Number of pairs",
    "DZN": "Dozen", "MTR": "Metre", "SQM": "Square metre", "LTR": "Litre",
}
_COUNT_UOMS = {"PCS", "PC", "NOS", "NO", "UNIT", "UNITS", "EA", "EACH", "SET", "SETS", "UNT"}


def _r(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def resolve_supplementary_for_item(item: WorkItem) -> WorkItem:
    settings = get_settings()
    unit = (item.hs_tariff_unit or "").strip().upper()
    qty = item.quantity or Decimal("0")
    net = item.net_weight_kg or Decimal("0")
    uom = (item.invoice_uom_raw or "").strip().upper()
    is_count = uom in _COUNT_UOMS
    # Reviewer-pinned quantity.  It REPLACES the derived number and nothing
    # else: every formula below is untouched, the code/name still come from the
    # official tariff unit, and an item with no pin behaves exactly as before.
    pin = item.manual_supplementary_quantity
    if pin is not None and pin <= 0:
        pin = None

    code = unit if unit in _UNIT_NAME else None
    supp_qty: Decimal | None = None
    warn: str | None = None

    if unit == "KGM":
        code = "KGM"
        supp_qty = _r(net, "0.0001")
        if supp_qty <= 0 and pin is None:
            return _fail(item, "net weight required for KGM supplementary unit")
    elif unit == "UNT":
        code, supp_qty = "UNT", _r(qty, "0.01")
    elif unit in ("PR", "NPR"):
        code = unit
        supp_qty = _r(qty / 2, "1") if (settings.pair_divide_by_two and is_count) else _r(qty, "1")
        if is_count and qty % 2 != 0:
            warn = f"{_UNIT_NAME[unit]} conversion from odd count quantity; verify manually."
    elif unit == "DZN":
        code = "DZN"
        supp_qty = _r(qty / 12, "0.001") if is_count else _r(qty, "0.001")
        if is_count and qty % 12 != 0:
            warn = "Dozen conversion creates fractional dozen; verify manually."
    elif unit == "MTR":
        code = "MTR"
        if uom in ("MTR", "M", "METER", "METRE"):
            supp_qty = _r(qty, "0.001")
        else:
            supp_qty, warn = _r(net, "0.001"), "net weight used as metre proxy (no metre quantity on invoice)."
    elif unit == "SQM":
        code = "SQM"
        if uom in ("MTR", "M", "METER", "METRE"):
            supp_qty = _r(qty * Decimal("1.524"), "0.001")
            warn = "metre->square metre via 60-inch width assumption; verify width."
        else:
            supp_qty, warn = _r(net, "0.001"), "net weight used as square-metre proxy."
    elif unit == "LTR":
        code = "LTR"
        supp_qty, warn = _r(net, "0.001"), "net weight used as litre proxy (density ~1)."
    else:
        code = "UNT"
        supp_qty = _r(qty, "0.01")
        warn = f"unknown tariff unit {unit!r}; fell back to UNT invoice quantity."

    if pin is not None:
        supp_qty = pin
        if unit in _UNIT_NAME:
            # the warning described the conversion the pin just replaced; the
            # unknown-unit fallback message is about the CODE, so it survives
            warn = None

    if supp_qty is None or supp_qty <= 0:
        return _fail(item, f"supplementary quantity <= 0 for unit {unit!r}")

    item.supplementary_unit_code = code
    item.supplementary_unit_name = _UNIT_NAME.get(code, code)
    item.supplementary_quantity = supp_qty
    if warn:
        item.warnings.append(ValidationMessage.warning(
            "SUPPLEMENTARY_ASSUMPTION", f"Item {item.xml_item_sequence}: {warn}",
            scope="ITEM", item_sequence=item.xml_item_sequence, field="supplementary_unit"))
    return item


def _fail(item: WorkItem, msg: str) -> WorkItem:
    item.warnings.append(ValidationMessage.blocking(
        "SUPPLEMENTARY_QTY_INVALID", f"Item {item.xml_item_sequence}: {msg}",
        scope="ITEM", item_sequence=item.xml_item_sequence, field="supplementary_unit"))
    return item


def resolve_supplementary_all(items: list[WorkItem]) -> list[WorkItem]:
    return [resolve_supplementary_for_item(it) for it in items]
