"""Deterministic goods-row manifest built from OCR markdown tables.

Before any LLM output is trusted, plain code inventories the goods rows the
OCR text *provably* contains: a markdown table line with a ``| qty | UOM |``
cell pair and at least one identity token (GTIN or part number) in the cells
before the quantity is a goods-row anchor.  The extraction must cover every
anchor — an anchor with no matching extracted row is a hard validation error
(``ROW_ANCHOR_MISSING``), which is exactly the failure that silently dropped
70 rows on the 2026-07-17 sindhu job.

The manifest is deliberately conservative: continuation fragments, header
rows, SSCC/carton banner lines and free text never carry the qty|UOM cell
pair plus an identity token, so they produce no anchors and can never demand
rows that do not exist.  Pages without markdown tables produce no anchors at
all (e.g. the offline pypdf demo fixtures).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Units of measure seen across vendors, industries and a few languages. Broad
# on purpose so the deterministic fast path recognizes many layouts; a false
# UOM match alone never creates a row — the parser still needs qty x price ==
# total, and a goods-row anchor still needs an identity token. THE single
# source of truth: the validator and table parser build their patterns from
# UOM_ALT, so the three layers can never drift apart.
UOM_WORDS = (
    # count / piece
    "EA", "EACH", "PC", "PCS", "PIECE", "PIECES", "NO", "NOS", "NR", "UN", "UNT", "UNIT", "UNITS",
    # pack / packaging
    "PK", "PKT", "PKG", "PKGS", "PACK", "PACKS", "PKTS",
    "BX", "BOX", "BOXES", "CS", "CASE", "CASES", "CTN", "CTNS", "CARTON", "CARTONS",
    "BDL", "BUNDLE", "BUNDLES", "BALE", "BALES", "DRUM", "DRUMS", "PLT", "PALLET", "PALLETS",
    "BAG", "BAGS", "SAC", "SACHET", "SACHETS", "POUCH", "JAR", "JARS", "CAN", "CANS",
    "BTL", "BOTTLE", "BOTTLES", "TUBE", "TUBES", "VIAL", "VIALS", "AMP", "AMPS", "AMPOULE",
    "ROLL", "ROLLS", "RL", "REAM", "REAMS", "SHT", "SHEET", "SHEETS", "COIL", "COILS",
    # grouping.  "PRS" (pairs) is the footwear/glove trade's spelling and its
    # absence was silently expensive: the UOM cell did not read as a unit, so
    # the row produced no goods-row anchor (a 15-row invoice counted 12 and
    # tripped a phantom EXTRACTION_OVERCOUNT) and the parser fell back to its
    # default unit instead of the printed one.
    "SET", "SETS", "KIT", "KITS", "PR", "PRS", "PAIR", "PAIRS", "DZ", "DZN", "DOZ", "DOZEN",
    "GRS", "GROSS",
    # European unit spellings that arrive on German / French / Spanish /
    # Italian / Portuguese / Turkish invoices.  Accented forms ("Stück") reach
    # here already folded to ASCII by the table parser.
    "STK", "STUCK", "STUECK", "PCE", "PZ", "PZS", "UD", "UDS", "UN", "UNID",
    "ADET", "PAAR", "PAIRE", "STUKS", "BUAH", "CAI",
    # Nordic / central-European / Balkan piece words, as printed in the unit
    # column: st (SE), stk (NO/DK), kpl (FI/PL), ks (CZ/SK), db (HU), buc (RO),
    # kom (HR/RS), kos (SI), szt (PL), lev (FI sheet), tk (EE), gab (LV)
    "ST", "KPL", "KS", "DB", "BUC", "KOM", "KOS", "SZT", "LEV", "TK", "GAB",
    # mass / volume / length used as the trade UOM
    "KG", "KGS", "KGM", "G", "GM", "GMS", "MG", "TON", "TONS", "TONNE", "MT",
    "L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "LITER", "ML", "CL",
    "M", "MTR", "MTRS", "METER", "METERS", "METRE", "CM", "MM", "SQM", "SQFT", "CBM", "YD", "YDS", "FT",
    "M2", "M3", "ML2", "BBL", "BBLS", "MWH", "KWH",
)
# longest-first so the merged-cell regex prefers "PIECES" over "PC", etc.
UOM_ALT = "|".join(sorted({w for w in UOM_WORDS}, key=len, reverse=True))

_UOM_CELL = re.compile(rf"(?:{UOM_ALT})$", re.IGNORECASE)
_QTY_CELL = re.compile(r"\d+(?:[.,]\d+)?$")
# some vendors print quantity and UOM merged in ONE cell, with an optional
# pack-size annotation: "25 EA (1/EA)", "2 KIT", "5 EA (1/EA)"
_QTY_UOM_MERGED = re.compile(
    rf"(\d+(?:[.,]\d+)?)\s+({UOM_ALT})"
    r"(?:\s*\([^()]{0,24}\))?$", re.IGNORECASE)
# Count / packaging / grouping units only — used for the NO-SPACE merged form
# ("6PCS", "6PC", "10NOS", "2SET") common on Indian invoices.  Length / mass /
# volume units (CM, MM, KG, L, …) are deliberately EXCLUDED so a size
# annotation like "20CM" or "2MM" is never misread as a quantity.
_COUNT_UOM_WORDS = (
    "EA", "EACH", "PC", "PCS", "PIECE", "PIECES", "NO", "NOS", "NR", "UN", "UNT", "UNIT", "UNITS",
    "PK", "PKT", "PKG", "PKGS", "PACK", "PACKS", "PKTS",
    "BX", "BOX", "BOXES", "CS", "CASE", "CASES", "CTN", "CTNS", "CARTON", "CARTONS",
    "BDL", "BUNDLE", "BUNDLES", "BALE", "BALES", "DRUM", "DRUMS", "PLT", "PALLET", "PALLETS",
    "BAG", "BAGS", "SAC", "SACHET", "SACHETS", "POUCH", "JAR", "JARS", "CAN", "CANS",
    "BTL", "BOTTLE", "BOTTLES", "TUBE", "TUBES", "VIAL", "VIALS", "AMP", "AMPS", "AMPOULE",
    "ROLL", "ROLLS", "RL", "REAM", "REAMS", "SHT", "SHEET", "SHEETS", "COIL", "COILS",
    "SET", "SETS", "KIT", "KITS", "PR", "PRS", "PAIR", "PAIRS", "DZ", "DZN", "DOZ", "DOZEN",
    "GRS", "GROSS",
    "STK", "STUCK", "STUECK", "PCE", "PZ", "PZS", "UD", "UDS", "UNID",
    "ADET", "PAAR", "PAIRE", "STUKS", "BUAH", "CAI",
)
_COUNT_ALT = "|".join(sorted(set(_COUNT_UOM_WORDS), key=len, reverse=True))
_QTY_UOM_NOSPACE = re.compile(
    rf"(\d+(?:[.,]\d+)?)({_COUNT_ALT})"
    r"(?:\s*\([^()]{0,24}\))?$", re.IGNORECASE)
_GTIN = re.compile(r"\b0\d{12,13}\b")
# part numbers: start with a letter, contain a digit, length >= 5 (RONYX22515X,
# LA6JL30, A7630005676201); pure-digit order/batch numbers never qualify
_PART = re.compile(r"\b(?=[A-Z0-9-]*\d)([A-Z][A-Z0-9-]{4,})\b")
# digit-led part numbers ("01E3120", "1H7301", "69698UQ01"): must contain at
# least one letter AND one digit, length >= 5 — pure numbers (totals, tariff
# codes, weights) can never qualify
_PART_DIGIT_LED = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[0-9-]*[A-Z])([A-Z0-9][A-Z0-9-]{4,})\b")


def qty_uom_cell_at(cells: list[str]) -> tuple[int, int, str, str] | None:
    """Locate a row's quantity: either a ``| qty | UOM |`` cell pair or a
    single merged ``| qty UOM (size) |`` cell.  Returns (qty_cell_index,
    first_index_after_qty_and_uom, qty_raw, uom_raw)."""
    for i in range(len(cells) - 1):
        if cells[i] and _QTY_CELL.fullmatch(cells[i]) and cells[i + 1] and _UOM_CELL.fullmatch(cells[i + 1]):
            return i, i + 2, cells[i], cells[i + 1]
    for i, c in enumerate(cells):
        m = _QTY_UOM_MERGED.fullmatch(c or "") or _QTY_UOM_NOSPACE.fullmatch(c or "")
        if m:
            return i, i + 1, m.group(1), m.group(2)
    return None


def normalize_token(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


@dataclass
class RowAnchor:
    page_no: int
    tokens: list[str] = field(default_factory=list)   # ANY match = row covered
    snippet: str = ""


def goods_row_anchors(page_no: int, page_text: str) -> list[RowAnchor]:
    anchors: list[RowAnchor] = []
    for line in (page_text or "").splitlines():
        if line.count("|") < 4:
            continue
        cells = [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]
        found = qty_uom_cell_at(cells)
        if found is None or found[0] == 0:  # no qty cell, or it sits in the first column
            continue
        qty_at = found[0]
        identity = " ".join(cells[:qty_at]).upper()
        tokens = [normalize_token(t) for t in _GTIN.findall(identity)]
        tokens += [normalize_token(t) for t in _PART.findall(identity) if normalize_token(t) not in tokens]
        tokens += [normalize_token(t) for t in _PART_DIGIT_LED.findall(identity)
                   if normalize_token(t) not in tokens]
        if not tokens:
            continue
        anchors.append(RowAnchor(page_no=page_no, tokens=tokens, snippet=line.strip()[:90]))
    return anchors


def build_manifest(ocr_pages: dict[int, str]) -> dict[int, list[RowAnchor]]:
    """{page_no: [anchors]} for every page that provably prints goods rows."""
    out: dict[int, list[RowAnchor]] = {}
    for page_no, text in ocr_pages.items():
        anchors = goods_row_anchors(page_no, text)
        if anchors:
            out[page_no] = anchors
    return out


def uncovered_anchors(rows, ocr_pages: dict[int, str], text_fields: tuple[str, ...]) -> list[RowAnchor]:
    """Anchors with no extracted row on the same page — or an adjacent one —
    containing any of their identity tokens.

    Rows and anchors read the same OCR line, so a faithful extraction always
    echoes at least one token.  Coverage spans page +/-1 because a printed row
    can straddle a page break (description line on one page, the qty/identity
    line on the next) and legitimately be extracted on either side.  Evidence
    quotes count as row text: they quote the exact OCR line."""
    per_page: dict[int, list[str]] = {}
    for row in rows:
        page = getattr(row, "source_page_no", None)
        if page is None:
            continue
        parts = per_page.setdefault(page, [])
        for f in text_fields:
            v = getattr(row, f, None)
            if v:
                parts.append(str(v))
        for ev in getattr(row, "evidence", None) or []:
            q = getattr(ev, "quote", None)
            if q:
                parts.append(str(q))

    blobs = {p: normalize_token(" ".join(parts)) for p, parts in per_page.items()}
    missing: list[RowAnchor] = []
    for page_no, anchors in build_manifest(ocr_pages).items():
        blob = "".join(blobs.get(p, "") for p in (page_no - 1, page_no, page_no + 1))
        for a in anchors:
            if not any(t and t in blob for t in a.tokens):
                missing.append(a)
    return missing


# A printed line/serial number: a short bare integer in the row's FIRST cell.
# Six digits is generous for a line counter while still excluding amounts
# (which carry separators and fail the fullmatch).
_SERIAL_CELL = re.compile(r"\d{1,6}")


def serial_row_count(page_text: str) -> int:
    """Goods rows this page provably prints, counted by their LINE NUMBERS.

    Fallback reference for the over-extraction honesty gate on vendors whose
    rows carry no GTIN/part token at all (description-only invoices — live
    failure 2026-08-01: a surgical-instruments invoice produced ZERO token
    anchors, the gate stood down silently, and 20 duplicated rows shipped to
    review unflagged).  A table line whose first non-empty cell is a bare
    small integer and which carries a quantity cell is a printed goods row;
    headers ("Sn"), banners, totals ("Total") and continuation fragments (no
    quantity, or no serial) never match."""
    count = 0
    for line in (page_text or "").splitlines():
        if line.count("|") < 4:
            continue
        cells = [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]
        found = qty_uom_cell_at(cells)
        if found is None or found[0] == 0:
            continue
        first = next((c for c in cells if c), "")
        if not _SERIAL_CELL.fullmatch(first):
            continue
        count += 1
    return count


def anchor_count(ocr_pages: dict[int, str]) -> dict[int, int]:
    """Distinct goods-row anchors the OCR PROVABLY prints, per page.  Used as the
    reference count for the over-extraction honesty gate (a lower bound on the
    real goods-row count — a UOM-less row prints no anchor).

    Per page, the better of two lower bounds: identity-token anchors
    (GTIN/part) and printed-line-number rows — description-only vendors print
    no tokens, token-only vendors may print no serial column, and either alone
    left the gate blind on the other's layout."""
    counts = {p: len(a) for p, a in build_manifest(ocr_pages).items()}
    for page_no, text in ocr_pages.items():
        serial = serial_row_count(text)
        if serial > counts.get(page_no, 0):
            counts[page_no] = serial
    return counts
