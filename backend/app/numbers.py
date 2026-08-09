"""Decimal helpers.

Raw OCR/extraction values are *strings* (they may carry thousands separators,
currency symbols, parentheses, stray unicode).  All money / weight / quantity
maths happens on :class:`decimal.Decimal` — never binary float — after parsing
here.  This is the single conversion boundary between untrusted text and
authoritative numbers.
"""
from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

_CLEAN = re.compile(r"[^0-9.\-]")
TWO = Decimal("0.01")
THREE = Decimal("0.001")
FOUR = Decimal("0.0001")


def parse_decimal(raw: str | int | float | Decimal | None, locale: str | None = None) -> Decimal | None:
    """Best-effort parse of a raw numeric token into Decimal, else ``None``.

    Handles ``"1,234.56"``, ``"USD 1 234,56"`` (european), ``"#7023,17#"``
    (SWIFT), ``"(1,200)"`` (accounting negative), ``"7,023.17"`` and bare
    numbers.

    ``locale`` is an optional page-level hint from :func:`detect_numeric_locale`
    ("EU"/"US") that disambiguates forms both conventions claim: under "EU",
    ``1.600`` is one thousand six hundred and ``13,000`` is 13.000 (three
    decimal places, common on weight labels); without a hint the historical
    behavior is unchanged.
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    s = str(raw).strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1]
    # Currency / notation wrappers, common on Indian & mixed invoices:
    # "Rs. 1,234.00", "₹1,234", "INR 1,234.00", "1,234/-", "75,000/-",
    # "1,234.00 Dr".  Strip the leading currency word (its trailing dot in
    # "Rs." must NOT leak into the number and create a double dot), symbols,
    # the "/-" rupees-only suffix and Dr/Cr markers — otherwise the value fails
    # to parse and the item price silently collapses to 0.
    s = re.sub(r"(?i)^\s*(?:rs|inr|npr|usd|aed|eur|gbp|sgd|us\$|u\.s\.)\b\.?\s*", "", s)
    s = re.sub(r"[₹$€£]", "", s)
    s = re.sub(r"\s*/\s*[-=]\s*$", "", s)                 # trailing "/-" or "/="
    s = re.sub(r"(?i)\s*\b(?:dr|cr)\b\.?\s*$", "", s)     # accounting Dr/Cr
    s = s.strip()
    if not s:
        return None
    # Whitespace between digit groups: either space-thousands ("1 234,56") or
    # two distinct numbers jammed into one token ("35,00 700,00" from a merged
    # OCR table cell).  The latter must fail parsing, not concatenate.
    if re.search(r"\d\s+\d", s):
        core = re.sub(r"\s+", " ", re.sub(r"[^\d\s.,-]", "", s)).strip()
        if re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d{1,2})?", core):
            s = re.sub(r"\s+", "", s)
        else:
            return None
    # Page-locale hint resolves separator-ambiguous forms deterministically.
    if locale == "EU":
        sign = "-" if re.match(r"\s*-", s) else ""
        digits = re.sub(r"[^\d.,]", "", s)
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", digits):
            s = sign + digits.replace(".", "")               # "1.600" -> 1600
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3})*,\d{1,6}", digits):
            # Up to SIX decimals, not three: a Brazilian NF-e and an Argentine
            # factura E print quantities and unit prices to four ("300,0000",
            # "1.480,0000"), and capping at three left the comma looking like a
            # thousands separator — "300,0000" parsed as three million.  A
            # thousands group is always exactly three digits, so accepting more
            # decimal places here cannot swallow one.
            head, dec = digits.rsplit(",", 1)                # "13,000" -> 13.000
            s = sign + head.replace(".", "") + "." + dec
    # European decimal comma: "1.234,56" -> "1234.56", SWIFT "#7023,17#" -> "7023.17"
    if re.search(r"\d,\d{1,2}\b", s) and s.count(",") == 1 and s.rfind(".") < s.rfind(","):
        s = s.replace(".", "").replace(",", ".")
    else:
        sign = "-" if re.match(r"\s*-", s) else ""
        digits = re.sub(r"[^\d.,]", "", s)
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+\.\d{2}", digits):
            # OCR-corrupted European "1.600.00" (decimal comma read as a dot):
            # 3-digit dot groups with a final 2-digit group — last dot is decimal.
            head, dec = digits.rsplit(".", 1)
            s = sign + head.replace(".", "") + "." + dec
        elif re.fullmatch(r"\d{1,3}(?:\.\d{3}){2,}", digits):
            s = sign + digits.replace(".", "")   # unambiguous dot-thousands "1.600.000"
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+,\d{2}", digits):
            head, dec = digits.rsplit(",", 1)    # corrupted "1,600,00"
            s = sign + head.replace(",", "") + "." + dec
    s = _CLEAN.sub("", s)
    # A lone trailing "-" is the Indian rupees-only "/-" remnant (e.g. "1,234-"),
    # not an accounting negative — drop it so the value still parses.
    if s.endswith("-") and not s.startswith("-"):
        s = s.rstrip("-")
    if s in ("", "-", ".", "-."):
        return None
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    return -val if negative else val


def count_from_number_range(raw: str | None) -> int | None:
    """How many items a printed NUMBER or number range covers.

    ``"7"`` -> 1, ``"1-5"`` -> 5, ``"1,3,5"`` -> 3, ``"C/NO 12-18"`` -> 7.
    ``None`` when nothing countable is printed.

    Used for carton identifiers, where the distinction is load-bearing: a
    carton *number* is not a carton *count*, and "cartons 1-5" is five cartons
    however many rows are printed against it.
    """
    text = (raw or "").strip()
    if not text:
        return None
    total = 0
    for part in re.split(r"[,;]+|\s+(?:and|&)\s+", text, flags=re.I):
        # The part must be ESSENTIALLY NOTHING BUT the range — an optional
        # C/NO-style prefix, then digits-dash-digits (spec §3).  A bare
        # `re.search` read the hyphen wherever it fell: `F02-6` is marka F02
        # with six cartons and became "cartons 2 through 6" (five), `F05-2`
        # became nothing at all, and `PO-1001-2024` is a purchase order that
        # spans 1024.  A shipper's carton id is not arithmetic.
        m = re.fullmatch(
            r"\s*(?:C\s*/?\s*N[O0]\.?|CTNS?|CARTONS?|CASES?|BOX(?:ES)?|PALLETS?|"
            r"BALES?|NOS?\.?|#)?\s*"
            r"(\d+)\s*(?:-|–|—|to|thru|through)\s*(\d+)\s*", part, re.I)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi >= lo:
                total += hi - lo + 1
            continue
        if re.search(r"\d", part):
            total += 1
    return total or None


def detect_numeric_locale(text: str) -> str | None:
    """Majority-vote a page's numeric convention from unambiguous patterns.

    Returns "EU" (comma decimals / dot thousands), "US" (dot decimals /
    comma thousands) or ``None`` when the page gives no clear signal.
    Corrupted forms like ``1.600.00`` (decimal comma OCR-read as a dot) count
    as EU evidence; ``12,000KG``-style weight labels are the EU three-decimal
    kilogram convention.
    """
    if not text:
        return None
    eu = 3 * len(re.findall(r"\d\.\d{3},\d{1,2}\b", text))          # 1.234,56
    eu += 3 * len(re.findall(r"\d\.\d{3}\.\d{2}\b", text))          # 1.600.00 (corrupted)
    eu += 2 * len(re.findall(r"\d,\d{3}\s*KGS?\b", text, re.I))     # 13,000KG
    eu += len(re.findall(r"\d,\d{2}\b(?![.\d])", text))             # 35,00
    us = 3 * len(re.findall(r"\d,\d{3}\.\d{1,2}\b", text))          # 1,234.56
    us += len(re.findall(r"\d\.\d{2}\b(?![,\d])", text))            # 320.00
    if eu > us:
        return "EU"
    if us > eu:
        return "US"
    return None


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO, rounding=ROUND_HALF_UP)


def q3(value: Decimal) -> Decimal:
    return value.quantize(THREE, rounding=ROUND_HALF_UP)


def q3_down(value: Decimal) -> Decimal:
    """3dp, rounding DOWN.

    Used for ratio-derived net weights: ``net = r x gross`` with ``r < 1`` must
    stay strictly below gross after quantization, and rounding half-up would
    make ``0.7 x 0.001`` come back as ``0.001`` — equal to its own gross.
    Rounding down also never over-declares a net weight.
    """
    return value.quantize(THREE, rounding=ROUND_DOWN)


def q4(value: Decimal) -> Decimal:
    return value.quantize(FOUR, rounding=ROUND_HALF_UP)


def fmt2(value: Decimal) -> str:
    return f"{q2(value):.2f}"


def fmt_thousands2(value: Decimal) -> str:
    """Format like the sample XML Value_item string: ``2,045.01``."""
    return f"{q2(value):,.2f}"


def trim(value: Decimal) -> str:
    """Decimal -> string with trailing zeros removed (e.g. 1.0640 -> '1.064')."""
    s = f"{value:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def trim_min1(value: Decimal) -> str:
    """Like trim but keeps at least one decimal (11 -> '11.0')."""
    s = trim(value)
    return s if "." in s else s + ".0"
