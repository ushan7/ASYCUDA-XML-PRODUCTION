"""Date normalization for reviewed/composed declaration text.

Raw document dates arrive in many shapes — ``24/02/2026``, ``2026-02-24``,
``260224`` (SWIFT YYMMDD), ``24 FEB 2026``, ``FEB 24, 2026`` — and the
Critical Review / Field 9 composition needs two canonical presentations:

* ``DD-MMM-YYYY`` (``24-FEB-2026``) for invoice / LC lines, and
* ``DD/MM/YYYY``  (``24/02/2026``) for the first-item previous-document text.

Parsing is deterministic and conservative: an unrecognized string is returned
unchanged (never guessed), so provenance survives review.
"""
from __future__ import annotations

import re
from datetime import date

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_MONTH_NO = {m: i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_NO.update({m.capitalize(): i + 1 for i, m in enumerate(_MONTHS)})
_LONG = {"JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "JUNE": 6, "JULY": 7,
         "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12}


def parse_date(raw: str | None) -> date | None:
    """Best-effort parse of a raw document date; ``None`` when not confident."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    d = _parse_single(s)
    if d:
        return d
    # Composite SWIFT printouts repeat the tag date with an interpretation,
    # e.g. "260405 2026 Apr 05". Parse the leading tag; if the trailing text
    # also parses it must agree, otherwise stay conservative.
    m = re.fullmatch(r"(\d{6}|\d{8})\b[\s,:-]*(.*)", s)
    if m:
        lead = _parse_single(m.group(1))
        rest = _parse_single(m.group(2).strip()) if m.group(2).strip() else None
        if lead and (rest is None or rest == lead):
            return lead
    return None


def _parse_single(s: str) -> date | None:
    # SWIFT tag-style YYMMDD (e.g. LC field 31C "260224")
    if re.fullmatch(r"\d{6}", s):
        yy, mm, dd = int(s[:2]), int(s[2:4]), int(s[4:6])
        return _safe_date(2000 + yy, mm, dd)
    if re.fullmatch(r"\d{8}", s):  # YYYYMMDD
        return _safe_date(int(s[:4]), int(s[4:6]), int(s[6:8]))

    # ISO 2026-02-24
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD/MM/YYYY or DD-MM-YYYY (day-first: Nepal/commercial convention)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    up = s.upper()
    # 24 FEB 2026 / 24-FEB-2026 / FEB 24, 2026 / 24 FEBRUARY 2026
    m = re.fullmatch(r"(\d{1,2})[\s\-/.]*([A-Z]{3,9})[\s\-/.,]*(\d{4})", up)
    if m:
        mon = _month_no(m.group(2))
        return _safe_date(int(m.group(3)), mon, int(m.group(1))) if mon else None
    m = re.fullmatch(r"([A-Z]{3,9})[\s\-/.]*(\d{1,2})[\s,\-/.]*(\d{4})", up)
    if m:
        mon = _month_no(m.group(1))
        return _safe_date(int(m.group(3)), mon, int(m.group(2))) if mon else None
    # 2026 APR 05 (SWIFT printout interpretation line)
    m = re.fullmatch(r"(\d{4})[\s\-/.]*([A-Z]{3,9})[\s\-/.,]*(\d{1,2})", up)
    if m:
        mon = _month_no(m.group(2))
        return _safe_date(int(m.group(1)), mon, int(m.group(3))) if mon else None
    return None


def _month_no(token: str) -> int | None:
    return _MONTH_NO.get(token[:3]) if token[:3] in _MONTH_NO else _LONG.get(token)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def fmt_dd_mmm_yyyy(raw: str | None) -> str:
    """``24/02/2026`` -> ``24-FEB-2026``; unparseable input passes through."""
    d = parse_date(raw)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}" if d else (raw or "").strip()


def fmt_dd_mm_yyyy(raw: str | None) -> str:
    """``260224`` -> ``24/02/2026``; unparseable input passes through."""
    d = parse_date(raw)
    return f"{d.day:02d}/{d.month:02d}/{d.year}" if d else (raw or "").strip()
