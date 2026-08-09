"""Description unit conversion (allocation spec, Override 2 / Extended).

Derives an item's NET weight from its invoice description when no invoice
weight is printed: direct mass units first, then volume x density, then the
special conditional formulas (GSM, denier, tex, kg/m) — never from ambiguous
units.  All arithmetic is Decimal.  Returns None when the description does not
clearly support conversion (the caller keeps its previously calculated value).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from ..numbers import parse_decimal

D = Decimal

# ---- mass units -> kilograms ----------------------------------------------- #
_MASS_TO_KG = {
    "kg": D("1"), "kgs": D("1"), "kilogram": D("1"), "kilograms": D("1"),
    "kilo": D("1"), "kilos": D("1"),
    "g": D("0.001"), "gm": D("0.001"), "gms": D("0.001"), "gram": D("0.001"), "grams": D("0.001"),
    "mg": D("0.000001"), "milligram": D("0.000001"), "milligrams": D("0.000001"),
    "mcg": D("0.000000001"), "ug": D("0.000000001"), "µg": D("0.000000001"),
    "microgram": D("0.000000001"), "micrograms": D("0.000000001"),
    "tonne": D("1000"), "tonnes": D("1000"),
    "lb": D("0.45359237"), "lbs": D("0.45359237"), "pound": D("0.45359237"), "pounds": D("0.45359237"),
    # "st" (the abbreviation) is deliberately NOT here: on a commercial invoice
    # it means set / sterile / Stück far more often than stone, and reading it
    # as 6.35 kg per unit is enough on its own to push a consignment's net
    # weight above its gross.  The spelled-out word stays.
    "stone": D("6.35029318"),
    "grain": D("0.00006479891"),
    # bare "ton"/"tons" is the metric tonne in trade documents; the short and
    # long tons are matched as MULTI-WORD units below, before this table is
    # consulted, so "2 SHORT TON" can never be read as 2000 kg
    "ton": D("1000"), "tons": D("1000"), "mt": D("1000"),
}

# Multi-word mass units.  Matched BEFORE the single-token table: "short ton"
# ends in "ton", and reading it as a metric tonne over-declares by 10%.
_MULTIWORD_MASS: list[tuple[re.Pattern, Decimal, str]] = []      # filled after _NUM

# Units whose bare token collides with size, gauge, model or packaging words.
# They still convert, but only ever at LOW confidence (see _confidence).
_AMBIGUOUS_UNITS = frozenset({"g", "l", "cc", "dl", "dal", "pt", "qt", "cup", "tsp", "tbsp", "mt"})
# Units whose SIZE depends on which measurement system the document uses.  The
# US and Imperial gallons differ by 20%, so the value is usable but the choice
# has to be stated rather than assumed silently (spec: "if unclear, prefer the
# metric value or raise an audit warning").
_SYSTEM_AMBIGUOUS = frozenset({"gal", "gallon", "qt", "quart", "pt", "pint", "fl oz", "cup",
                               "tsp", "teaspoon", "tbsp", "tablespoon"})
# "fl oz" is spelled with the space because that is the unit NAME the multiword
# matcher reports — "floz" never matched it, so a bare fluid ounce converted at
# HIGH confidence while the US and Imperial fl oz differ by 4%.  An explicitly
# prefixed "US fl oz" / "imperial fl oz" names its system and stays out of this
# set, exactly like "US gallon" / "imperial gallon".
# Words that mark a weight as the OUTER package's, not the goods' own content.
_OUTER_PKG = re.compile(
    r"\b(carton|cartons|ctn|ctns|case|cases|pallet|pallets|master|outer|shipper|crate|crates|"
    r"gross|tare)\b", re.I)
# Words that mark a weight as the stated CONTENT — these outrank _OUTER_PKG, so
# "carton net 6 kg" is six kilograms of goods, not a carton to be ignored.
_CONTENT = re.compile(r"\b(net|nett|netto|contents?|fill|filled|capacity)\b", re.I)

# Invoice UOMs that count PACKS — a "24 x 500 ml" multiplier applies per line.
_PACK_UOMS = frozenset({
    "CTN", "CT", "CTNS", "CARTON", "CARTONS", "CASE", "CASES", "BOX", "BOXES",
    "PACK", "PACKS", "PKT", "PKTS", "PACKET", "PACKETS", "BAG", "BAGS",
    "DRUM", "DRUMS", "DZN", "DOZ", "DOZEN",
})
# Invoice UOMs that count PIECES — the multiplier must NOT be applied, or a
# 72 EA line of 500 ml bottles is declared as 72 x 24 x 500 ml (24x too heavy).
_PIECE_UOMS = frozenset({
    "PCS", "PC", "PIECE", "PIECES", "PCE", "EA", "EACH", "NOS", "NO", "NO.",
    "UNIT", "UNITS", "UNT", "SET", "SETS",
})

# ---- volume units -> litres ------------------------------------------------ #
_VOL_TO_L = {
    "ml": D("0.001"), "millilitre": D("0.001"), "milliliter": D("0.001"),
    "cl": D("0.01"), "dl": D("0.1"),
    "l": D("1"), "ltr": D("1"), "litre": D("1"), "liter": D("1"), "litres": D("1"), "liters": D("1"),
    "dal": D("10"), "hl": D("100"),
    "cc": D("0.001"), "cm3": D("0.001"),
    "dm3": D("1"),
    "cup": D("0.237"), "tsp": D("0.005"), "teaspoon": D("0.005"),
    "tbsp": D("0.015"), "tablespoon": D("0.015"),
    "pt": D("0.4731765"), "pint": D("0.4731765"),
    "qt": D("0.9463529"), "quart": D("0.9463529"),
    "gal": D("3.785411784"), "gallon": D("3.785411784"),
}

# density: (keywords, kg per litre, estimated?)  — first match wins
_DENSITIES: list[tuple[tuple[str, ...], Decimal, bool]] = [
    (("rapeseed oil", "olive oil"), D("0.91"), False),
    (("cooking oil", "edible oil", "vegetable oil"), D("0.92"), False),
    (("hair oil", "essential oil"), D("0.92"), True),
    (("perfume", "eau de parfum", "eau de toilette", "cologne", "alcohol"), D("0.80"), True),
    (("honey",), D("1.40"), True),
    (("syrup",), D("1.30"), True),
    (("glycerin", "glycerine"), D("1.25"), True),
    (("lotion", "cream", "gel"), D("1.00"), True),
    (("shampoo", "conditioner", "body wash", "liquid soap", "hand wash", "handwash",
      "detergent", "cleaning", "sanitizer", "disinfectant",
      "water", "juice"), D("1.00"), False),
]

# ambiguity guards: descriptions where a bare unit must NOT be converted
_DOSAGE = re.compile(
    r"\b(tablet|tablets|capsule|capsules|caps|dose|dosage|softgel|lozenge|suppositor(?:y|ies)|"
    r"ampoule|ampule|ampul|vial|vials)\b", re.I)
# A strength or RATE ("250 mg / 5 ml", "100 LTR/MIN", "5% w/v") is a ratio,
# never a net weight.  Masked out of the text before any unit search, so the
# bottle's own volume can still be read from the rest of the description.
# `m` is deliberately NOT a denominator: "0.5 KG/M" is the wire formula's
# input, and masking it would make _special_formula unreachable again.
_CONCENTRATION = re.compile(
    r"\d[\d.,]*\s*(?:mg|mcg|ug|µg|g|gm|kg|iu|%|ml|l|ltr|litre|liter)\s*/\s*"
    r"\d*[\d.,]*\s*(?:ml|l|litre|liter|ltr|kg|g|dose|min|minute|hr|hour|sec|second|day)\b",
    re.I)
# Goods that ARE a container or appliance: a volume printed on them is the
# thing's CAPACITY, not liquid contents being shipped.  "PLASTIC WATER TANK
# 1000 LTR" declared 1000 kg of net weight per tank — the description names
# water AND a volume, and the density table did the rest.  Bottles/jars/cans
# are deliberately absent: those really do ship full.
_CONTAINER_GOODS = re.compile(
    r"\b(tank|tanks|pump|pumps|dispenser|dispensers|heater|heaters|geyser|geysers|boiler|"
    r"boilers|cooler|coolers|chiller|chillers|urn|urns|reservoir|radiator|radiators|aquarium|"
    r"kettle|kettles|fryer|fryers|cooker|cookers|washer|washing\s+machine|refrigerator|"
    r"freezer|fridge|compressor|autoclave|sterili[sz]er|bathtub|bucket|buckets|bin|bins)\b", re.I)
# One capturing group, and it swallows whole thousands groups: "2,500" must
# reach parse_decimal intact, or the comma is read as a decimal point and the
# item is declared at a thousandth of its weight.  The first alternative spans
# SPACE-separated thousands groups ("1 250 KG" is 1250 kg — European invoices
# print this routinely); without it the regex captured only the last group,
# reading "1 250 KG" as 250 and "1 000 ML" as a zero.
_NUM = r"(\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|\d[\d.,]*\d|\d)"
# pack multiplier: "24 x 500 ml", "12 pcs x 1 litre", "pack of 12 x 500 ml", "24*500ml".
# The trailing group is the unit the pack binds to — the multiplier applies to
# THAT value and to nothing else on the line.
_PACK = re.compile(
    r"(?:(?:pack|case|carton)\s+of\s+)?(\d+)\s*(?:pcs|pc|bottles|bottle|cans|can|jars|jar|"
    r"tubes|tube|sachets|sachet|pouches|pouch)?\s*[x×*]\s*" + _NUM + r"\s*([a-zµ]{1,6})?", re.I)
# Reversed and slash pack notations — the same goods, written the other way
# round: "1 LTR X 12", "500 ML X 24", "24/250ML".  _PACK requires the count
# BEFORE the x, so these dropped the multiplier entirely (a silent 12-24x
# under-declaration) while "12 X 1 LTR" on the same invoice converted fine.
_PACK_UNIT = r"(ml|l|ltr|litre|liter|kg|g|gm|cl)"
_PACK_REV = re.compile(_NUM + r"\s*" + _PACK_UNIT + r"\s*[x×*]\s*(\d{1,4})\b", re.I)
_PACK_SLASH = re.compile(r"\b(\d{1,4})\s*/\s*" + _NUM + r"\s*" + _PACK_UNIT + r"\b", re.I)
# Three-part "A x B x C [unit]" chains.  With a trailing mass/volume unit this
# is a NESTED pack ("4 x 6 x 1.5 LTR" = 24 bottles of 1.5 L); with a length
# unit, or none, it is a package DIMENSION ("MASTER CARTON 40 X 30 X 20 CM")
# and must never bind a pack multiplier — _PACK's leftmost match on such a
# line was the dimension pair, which stole the binding and silently dropped
# the real "12 X 1 KG" multiplier printed later on the same line.
_CHAIN3 = re.compile(_NUM + r"\s*[x×*]\s*" + _NUM + r"\s*[x×*]\s*" + _NUM
                     + r"\s*([a-zµ]{1,6})?", re.I)
_DOZEN = re.compile(r"\b(dozen|dzn|dz)\b", re.I)

_MULTIWORD_MASS.extend([
    (re.compile(_NUM + r"\s*(?:short|u\.?\s*s\.?)\s*tons?\b", re.I), D("907.18474"), "short ton"),
    (re.compile(_NUM + r"\s*(?:long|imp(?:erial)?\.?)\s*tons?\b", re.I), D("1016.0469088"), "long ton"),
    (re.compile(_NUM + r"\s*metric\s*tons?\b", re.I), D("1000"), "metric ton"),
])
# Imperial volumes, matched before the single-token table so an explicitly
# Imperial gallon is not silently converted at the US value (20% smaller).
_MULTIWORD_VOL: list[tuple[re.Pattern, Decimal, str]] = [
    (re.compile(_NUM + r"\s*(?:imp(?:erial)?\.?|uk)\s*gal(?:lons?)?\b", re.I), D("4.546"),
     "imperial gallon"),
    (re.compile(_NUM + r"\s*(?:u\.?\s*s\.?)\s*gal(?:lons?)?\b", re.I), D("3.785411784"),
     "us gallon"),
    (re.compile(_NUM + r"\s*(?:imp(?:erial)?\.?|uk)\s*fl\.?\s*oz\b", re.I), D("0.028412"),
     "imperial fl oz"),
    (re.compile(_NUM + r"\s*(?:u\.?\s*s\.?\s*)?fl\.?\s*oz\b", re.I), D("0.02957353"), "fl oz"),
]


@dataclass
class DescWeight:
    net_kg: Decimal
    source: str
    estimated: bool = False
    warnings: list[str] = field(default_factory=list)
    # "HIGH" — unambiguous content declaration, known density, pack multiplier
    #          consistent with the invoice UOM.  Only a HIGH conversion may
    #          outrank the invoice-printed weight (spec 2026-07-21).
    # "LOW"  — assumed density, ambiguous unit token, or an unverifiable pack
    #          multiplier.  Still used, but never as the top authority.
    confidence: str = "HIGH"


def _pack_uom_class(uom: str | None) -> str:
    """PACK / PIECE / UNKNOWN — decides whether a pack multiplier may apply."""
    u = re.sub(r"[^A-Z.]", "", (uom or "").strip().upper())
    if u in _PACK_UOMS:
        return "PACK"
    if u in _PIECE_UOMS:
        return "PIECE"
    return "UNKNOWN"


def _num(s: str) -> Decimal | None:
    """Parse a numeric token from a description.

    Delegates to the pipeline's one numeric boundary rather than replacing
    commas with dots: that shortcut read "2,500 G" as 2.5 grams and declared a
    2.5 kg bag of sugar at 0.0025 kg.
    """
    return parse_decimal(s)


def _finish(net_kg: Decimal, source: str, *, estimated: bool,
            unit: str | None, notes: list[str]) -> DescWeight:
    """Build the result and decide its confidence.

    HIGH requires all three: an unambiguous unit token, a known (not assumed)
    density, and a pack multiplier consistent with the invoice UOM.  Anything
    else is LOW — still used, but never allowed to outrank the invoice-printed
    weight.  Every demotion reason is recorded (spec: "if unit is ambiguous, do
    not silently convert; keep audit warning").
    """
    reasons = list(notes)
    if unit and unit.lower() in _AMBIGUOUS_UNITS:
        reasons.append(f"unit {unit!r} is ambiguous in product descriptions "
                       "(size / gauge / model codes use the same token)")
    if estimated:
        reasons.append("value depends on an assumed density")
    return DescWeight(net_kg=net_kg, source=source, estimated=estimated,
                      warnings=reasons, confidence="LOW" if reasons else "HIGH")


@dataclass
class _Hit:
    """One '<value> <unit>' occurrence, converted, with where it was found."""
    amount: Decimal                 # kilograms (mass) or litres (volume)
    unit: str
    raw_value: str
    start: int
    end: int = 0
    outer_package: bool = False     # names the shipping package, not the goods
    near_package: bool = False      # a package word governs it, content word or not
    spaced: bool = False            # value used a space thousands separator


# "25-50 KG" states a size range, not a content: the value next to the unit is
# the range's upper bound, and declaring it is up to a 2x over-declaration.
_RANGE_BEFORE = re.compile(r"\d\s*(?:-|–|—|to)\s*$", re.I)
_RANGE_AFTER = re.compile(r"\s*(?:-|–|—|to)\s*\d", re.I)


def _package_context(text: str, start: int) -> tuple[bool, bool]:
    """(outer_package, near_package) for the value at ``start``.

    Two signals, either may flag the value as the PACKAGE's:

    * a package word within 24 characters either side (the original rule);
    * a package word earlier in the same clause with NO digit between it and
      the value — "TOTAL GROSS WEIGHT OF THE CONSIGNMENT INCLUDING PACKING
      1250 KG" puts GROSS 48 characters back, and a fixed window declared the
      shipment's gross as an item's net at HIGH confidence.

    A clause that holds nothing but the number ("…, 15 KG") borrows the
    previous clause: the words describing that weight are on the other side of
    the comma.  A content word (NET, CONTENTS…) standing after the package word
    turns it back into goods content — "CARTON NET 6 KG" is six kilograms of
    goods, not a carton to be ignored.
    """
    window = text[max(0, start - 24):start + 24]
    near = bool(_OUTER_PKG.search(window))
    content = bool(_CONTENT.search(window))

    clause_start = max(text.rfind(",", 0, start), text.rfind(";", 0, start)) + 1
    before = text[clause_start:start]
    if not re.search(r"[a-z]{3,}", before) and clause_start > 0:
        prev_start = max(text.rfind(",", 0, clause_start - 1),
                         text.rfind(";", 0, clause_start - 1)) + 1
        before = text[prev_start:start]
    pkg = cnt = None
    for m in _OUTER_PKG.finditer(before):
        pkg = m
    for m in _CONTENT.finditer(before):
        cnt = m
    if pkg is not None and not re.search(r"\d", before[pkg.end():]):
        near = True
        if cnt is not None and cnt.start() > pkg.start():
            content = True
    return near and not content, near


def _unit_hits(text: str, units: dict[str, Decimal],
               multiword: list[tuple] | None = None) -> list[_Hit]:
    """Every convertible '<value> <unit>' in the text, in printed order.

    Multi-word units are matched first and their spans blanked, so a longer
    unit is never re-read as the shorter one it ends with ("short ton" ->
    "ton", "imperial gallon" -> "gal").  A zero value is never a hit ("1 000
    ML" once yielded ('000', 'ml') = 0 kg at HIGH), and a value that is one
    end of a printed range ("25-50 KG") is dropped rather than declared.
    """
    hits: list[_Hit] = []
    working = text
    for pattern, factor, name in (multiword or []):
        for m in pattern.finditer(text):
            value = _num(m.group(1))
            if value:
                hits.append(_Hit(value * factor, name, m.group(1), m.start(), m.end()))
        working = pattern.sub(lambda m: " " * len(m.group(0)), working)
    for m in re.finditer(_NUM + r"\s*([a-zµ]{1,12})\b", working, re.I):
        u = m.group(2).lower()
        if u in units:
            value = _num(m.group(1))
            if value:
                hits.append(_Hit(value * units[u], u, m.group(1), m.start(), m.end()))
    hits = [h for h in hits
            if not _RANGE_BEFORE.search(text[:h.start]) and not _RANGE_AFTER.match(text[h.end:])]
    for h in hits:
        h.outer_package, h.near_package = _package_context(text, h.start)
        h.spaced = " " in h.raw_value or " " in h.raw_value
    hits.sort(key=lambda h: h.start)
    return hits


def _pick(hits: list[_Hit]) -> tuple[_Hit | None, bool]:
    """The hit that states the GOODS' own quantity, and whether that had to be
    settled by falling back to a package weight.

    "500 ML SHAMPOO IN 5 KG CARTON" prints two perfectly good numbers, and the
    5 kg is the box.  A weight sitting next to CARTON / PALLET / MASTER / GROSS
    describes the packaging unless a content word (NET, CONTENTS, CAPACITY)
    also sits there — "CARTON NET 6 KG" is six kilograms of goods.

    Among equals the LAST occurrence still wins: in "24 x 500 ml" the specific
    value is the trailing one.
    """
    if not hits:
        return None, False
    own = [h for h in hits if not h.outer_package]
    if own:
        return own[-1], False
    return hits[-1], True


def _density_for(text: str) -> tuple[Decimal, bool]:
    low = text.lower()
    for keys, dens, est in _DENSITIES:
        if any(k in low for k in keys):
            return dens, est
    return D("1.00"), True                       # unknown liquid: 1.00, estimated


def _pack_multiplier(text: str, uom_class: str,
                     ) -> tuple[Decimal, tuple[str, str] | None, list[str], bool]:
    """Pack multiplier for the line, the value it BINDS to, its notes, and
    whether it came from a bare DOZEN with no stated per-piece value.

    "24 x 500 ml" multiplies the line only when the invoice quantity counts
    packs.  When it counts pieces, the quantity already IS the bottle count and
    applying the multiplier over-declares the line by the pack size — the
    single largest over-count path in description conversion.

    The binding matters just as much.  In "24 X 250 ML, CARTON NET 6 KG" the
    24 multiplies the 250 ml bottles, NOT the carton's 6 kg — applying it there
    declared a 600 kg line at 14 400 kg, at HIGH confidence.

    Search order: nested three-part chains first (their dimension twins are
    masked so a "MASTER CARTON 40 X 30 X 20 CM" printed BEFORE the real pack
    cannot steal the binding), then the ordinary count-first form, then the
    reversed ("1 LTR X 12") and slash ("24/250ML") notations, then dozen.
    """
    mult: Decimal | None = None
    inner: tuple[str, str] | None = None
    masked = text
    for m in _CHAIN3.finditer(text):
        unit3 = (m.group(4) or "").lower()
        if unit3 in _VOL_TO_L or unit3 in _MASS_TO_KG:
            if mult is None:                     # "4 x 6 x 1.5 LTR" = 24 x 1.5 L
                a, b = _num(m.group(1)), _num(m.group(2))
                if a and b:
                    mult, inner = a * b, (m.group(3), unit3)
        else:                                    # a dimension — never a pack
            masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
    if mult is None:
        for m in _PACK.finditer(masked):
            unit3 = (m.group(3) or "").lower()
            if unit3 in ("x", "×"):
                continue                         # fragment of a longer chain
            mult, inner = D(m.group(1)), (m.group(2), unit3)
            break
    if mult is None and (m := _PACK_REV.search(masked)):
        mult, inner = D(m.group(3)), (m.group(1), m.group(2).lower())
    if mult is None and (m := _PACK_SLASH.search(masked)):
        mult, inner = D(m.group(1)), (m.group(2), m.group(3).lower())
    dozen_only = False
    if _DOZEN.search(text):
        if mult is None:
            mult, dozen_only = D("12"), True
        else:
            mult *= D("12")
    if mult is None:
        return D("1"), None, [], False
    if uom_class == "PIECE":
        return D("1"), inner, [
            f"pack format ({mult} per pack) ignored: the invoice quantity counts pieces, "
            "so it already is the pack-content count"], False
    if uom_class == "UNKNOWN":
        return mult, inner, [
            f"pack multiplier {mult} applied against an unrecognized invoice UOM — "
            "verify whether the quantity counts packs or pieces"], dozen_only
    return mult, inner, [], dozen_only


def _binds(hit: _Hit, inner: tuple[str, str] | None) -> bool:
    """Does the pack multiplier apply to THIS value?  Only when it is the value
    the pack expression names."""
    if inner is None:
        return True
    value, unit = inner
    if _num(value) != _num(hit.raw_value):
        return False
    return not unit or unit == hit.unit


def net_from_description(description: str, quantity: Decimal | None,
                         uom: str | None = None) -> DescWeight | None:
    """Net kg for the WHOLE invoice line, or None when nothing converts safely.

    ``uom`` is the invoice line's unit of measure.  It decides whether a pack
    format in the description multiplies the line (packs) or is already
    accounted for by the quantity (pieces).
    """
    text = (description or "").strip()
    if not text:
        return None
    qty = quantity if quantity and quantity > 0 else D("1")
    low = text.lower()

    if _DOSAGE.search(low):
        # 500 mg tablets are dosage strength, never net weight
        return None
    # A strength ("250 mg / 5 ml") is a ratio, not a content: blank it out so
    # the search cannot read one half of it as the item's weight, while the
    # rest of the description stays available.
    low = _CONCENTRATION.sub(lambda m: " " * len(m.group(0)), low)

    uom_class = _pack_uom_class(uom)
    mult, pack_inner, notes, dozen_only = _pack_multiplier(low, uom_class)

    def _mult_for(hit: _Hit) -> Decimal:
        """The multiplier THIS hit takes.  A bare DOZEN has no stated per-piece
        value to bind to, so it must never land on a package-level weight:
        "1 DOZEN PER POLYBAG, CARTON NET 6 KG" is 6 kg per carton, and x12
        turned it into 7 200 kg at HIGH confidence."""
        if not _binds(hit, pack_inner):
            return D("1")
        if dozen_only and hit.near_package:
            notes.append("dozen multiplier not applied: the stated weight is a per-package "
                         "figure, not a per-piece one")
            return D("1")
        return mult

    def _extra_notes(hit: _Hit, base: list[str]) -> list[str]:
        out = list(base)
        if hit.spaced:
            out.append(f"{hit.raw_value!r} read with a space thousands separator "
                       f"(= {_num(hit.raw_value)}) — verify")
        return out

    # -- 0. per-length / per-area formulas ----------------------------------
    # These run FIRST because their unit strings contain a mass unit: "0.5
    # KG/M" was matched by the bare mass search below and declared as 0.5 kg
    # per piece, which made every one of these formulas unreachable.
    special = _special_formula(low, qty)
    if special is not None:
        return special

    # -- 1. direct mass unit (ct/CT/CTN is carton in this domain, never carat)
    # No US/Imperial note here: every unit in _MASS_TO_KG is the same mass in
    # both systems, and the two that are not (short/long ton) are matched as
    # explicit multi-word units. Only volumes carry that ambiguity.
    mass, mass_from_package = _pick(_unit_hits(low, _MASS_TO_KG, _MULTIWORD_MASS))
    if mass is not None and not mass_from_package:
        return _finish(qty * _mult_for(mass) * mass.amount,
                       f"description unit conversion ({mass.unit})",
                       estimated=False, unit=mass.unit, notes=_extra_notes(mass, notes))

    # oz alone: mass only when the text says net weight AND it's not fluid;
    # ambiguous 'oz' (liquid/cosmetic without ml) is never converted silently
    m_oz = re.search(_NUM + r"\s*(?:oz|ounce|ounces)\b", low)
    if m_oz and "fl" not in low:
        # `_CONTENT`, not `"net" in low`: the substring test fired on MAGNET,
        # BONNET and CABINET, converting an ounce figure the document never
        # called a net weight.
        if _CONTENT.search(low) and not re.search(r"\bml\b", low):
            value = _num(m_oz.group(1))
            if value is not None:
                return _finish(qty * mult * value * D("0.028349523125"),
                               "description unit conversion (oz mass)",
                               estimated=False, unit="oz", notes=notes)
        return None

    # -- 2. volume x density -------------------------------------------------
    # CBM / m3 is volume of the package, never net weight.  A volume printed on
    # goods that ARE a container or appliance is the thing's CAPACITY: a water
    # tank's 1000 LTR is not a thousand kilograms of shipped water.
    if not re.search(r"\b(cbm|m3|m³)\b", low) and not _CONTAINER_GOODS.search(low):
        vol, vol_from_package = _pick(_unit_hits(low, _VOL_TO_L, _MULTIWORD_VOL))
        if vol is not None and vol_from_package and mass is not None:
            vol = None                  # both readings are package-level; prefer the mass below
        if vol is not None:
            litres, unit = vol.amount, vol.unit
            dens, est = _density_for(low)
            # Two guesses do not make a weight.  A bare "l" / "cc" / "qt" token
            # collides with alloy grades, engine displacement and gauge codes,
            # and an assumed density means nothing in the text says what liquid
            # this even is — so the pair is a number and a letter, not a volume.
            # "Stainless Steel 316 L" read as 316 litres x 1.00 kg/L is 316 kg
            # of surgical suture per piece; four such lines put a 62 kg
            # consignment's net weight at 6320 kg, which made the whole
            # allocation infeasible and blanked every weight in the table.
            # Same rule as an unrecognized printed unit (spec section 5,
            # Units): the source is disqualified, never assumed.  An
            # unambiguous token (ml, litre, gal) still converts on an assumed
            # density, and a known density still converts an ambiguous token.
            if not (est and unit.lower() in _AMBIGUOUS_UNITS):
                extra = _extra_notes(vol, notes)
                if est:
                    extra.append(f"density {dens} kg/L assumed — no product keyword matched")
                if unit in _SYSTEM_AMBIGUOUS:
                    extra.append(f"the {unit} differs between the US and Imperial systems — the "
                                 f"US value was used")
                return _finish(qty * _mult_for(vol) * litres * dens,
                               f"description unit conversion ({unit} x density {dens})",
                               estimated=est, unit=unit, notes=extra)

    # -- 3. last resort: the only figure printed is a PACKAGE weight ---------
    # Better than nothing, and better than pretending it is the goods' own
    # content: the value is used and the reason is on the record.
    if mass is not None:
        return _finish(qty * _mult_for(mass) * mass.amount,
                       f"description unit conversion ({mass.unit})",
                       estimated=False, unit=mass.unit,
                       notes=_extra_notes(mass, notes)
                       + ["the only weight in the description names the "
                          "shipping package, not the goods' own content"])
    return None


def _special_formula(low: str, qty: Decimal) -> DescWeight | None:
    """GSM / denier / tex / kg-per-metre / g-per-metre.

    All variables must be present, and every result goes through `_finish` so
    it is subject to the same confidence gate as everything else — these used
    to return a bare DescWeight, i.e. HIGH confidence unconditionally.
    """
    def num(m, group=1):
        return _num(m.group(group)) if m else None

    gsm = re.search(_NUM + r"\s*gsm\b", low)
    dims = re.search(_NUM + r"\s*m\s*[x×]\s*" + _NUM + r"\s*m\b", low)
    if gsm and dims:
        w, h, g = num(dims), num(dims, 2), num(gsm)
        if None not in (w, h, g):
            return _finish(qty * w * h * g / D("1000"), "description unit conversion (GSM)",
                           estimated=False, unit=None, notes=[])

    length = re.search(_NUM + r"\s*(?:m|mtr|meter|metre)s?\b", low)
    # kg/m and g/m are matched BEFORE the plain-length pairings and before the
    # bare mass search in the caller: "0.5 KG/M" contains "kg", so the mass
    # search claimed it first and these two branches were unreachable.
    kgm = re.search(_NUM + r"\s*kg\s*/\s*m\b", low)
    if kgm and length:
        a, b = num(kgm), num(length)
        if None not in (a, b):
            return _finish(qty * a * b, "description unit conversion (kg/m)",
                           estimated=False, unit=None, notes=[])
    gm_m = re.search(_NUM + r"\s*g\s*/\s*m\b", low)
    if gm_m and length:
        a, b = num(gm_m), num(length)
        if None not in (a, b):
            return _finish(qty * a * b / D("1000"), "description unit conversion (g/m)",
                           estimated=False, unit=None, notes=[])
    den = re.search(_NUM + r"\s*(?:denier|den)\b", low)
    if den and length:
        a, b = num(den), num(length)
        if None not in (a, b):
            return _finish(qty * a * b / D("9000000"), "description unit conversion (denier)",
                           estimated=False, unit=None, notes=[])
    tex = re.search(_NUM + r"\s*tex\b", low)
    if tex and length:
        a, b = num(tex), num(length)
        if None not in (a, b):
            return _finish(qty * a * b / D("1000000"), "description unit conversion (tex)",
                           estimated=False, unit=None, notes=[])
    return None
