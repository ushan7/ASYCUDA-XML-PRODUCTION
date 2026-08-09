"""Deterministic invoice item-description cleaner + code-only detector.

Audit 2026-07-20: some vendors append batch / lot / manufacturing / expiry /
serial annotations to the product-name cell, so the extracted ``description_raw``
carries text like ``"... 8mm x 2.00mm Batch / Mfg Dt / Exp Dt: 36654442 -
02/JUN/2025 - 01/JUN/2028"`` straight into the customs-declared description.
Others do the opposite — OCR drops the product name and only the bare part code
survives (``"H7493912412200"``).

Two pure, deterministic helpers (no LLM, no network):

* :func:`clean_description` trims a trailing annotation region, leaving the
  product name + dimensions only.  Algorithm — an *anchor-based* trim, chosen
  after an adversarial audit (2026-07-20) that broke a simpler token-run rule:

    1. The trim can only START at an ANCHOR: a trigger label (Batch / Lot /
       Mfg / Exp / Expiry / Serial / MRP / Best / Use / Packed) or a real DATE.
       Everything BEFORE the anchor is kept verbatim — so a trailing MODEL
       NUMBER ("Filter Model 4021 Batch 5567" → "Filter Model 4021") or year is
       never swallowed.
    2. Every token from the anchor to the end must be annotation-material
       (label word / number / date / code / separator) AND the region must
       contain a real VALUE or DATE — so a bare trailing label with no value
       ("Job Lot", "Parking Lot", "Steel Rod 12 Batch") is left UNCHANGED.
    3. DATE recognition is strict (real month names, or a 4-digit year), so
       shop specs like ``8/32`` thread size, ``40/60`` grit, ``10/20`` yarn
       count, and grade codes like ``OPC-53`` / ``SAE-1018`` are never mistaken
       for dates.

  The trimmed text is returned, never silently discarded.  Nothing matches →
  the description is returned unchanged.

* :func:`is_code_only` flags the opposite failure: a description that carries
  no product NAME at all, only an identity code.  Reconstruction is NOT
  attempted (a guessed customs description would be an invented fact); the
  caller surfaces it for the reviewer instead.

Neither helper ever invents text: both only remove or classify.
"""
from __future__ import annotations

import re

from ..extraction.manifest import _GTIN, _PART, _PART_DIGIT_LED

# --------------------------------------------------------------------------- #
# clean_description
# --------------------------------------------------------------------------- #
# Trigger labels that may START a trailing annotation.  Kept narrow so a plain
# word never anchors a cut on its own (the value/date requirement guards the
# rest).  "best"/"use"/"packed"/"packing" lead the date phrases Best Before /
# Use By / Packed On.
_ANCHOR_WORDS = frozenset({
    "batch", "batches", "lot", "lots", "mfg", "mfd", "manufacture",
    "manufactured", "manufacturing", "exp", "expiry", "expiration", "expires",
    "expiring", "serial", "mrp", "best", "use", "packed", "packing",
})
# Supporting filler words that may appear inside an annotation region but are
# too generic to anchor a cut (Batch NO, Mfg DT, Date OF packing, Best BEFORE,
# Use BY, Packed ON, shelf LIFE …).  Anchors are filler too.
_MONTH_WORDS = frozenset({
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept",
    "oct", "nov", "dec", "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
})
_FILLER_WORDS = _ANCHOR_WORDS | _MONTH_WORDS | frozenset({
    "no", "nos", "number", "code", "dt", "date", "dates", "of", "by", "on",
    "before", "shelf", "life", "sr", "mm", "end",
})

# Real month names only — NOT arbitrary letters — so "OPC-53" / "SAE-1018" are
# not read as Mon-Year dates.
_MON = "(?:" + "|".join(sorted(_MONTH_WORDS, key=len, reverse=True)) + ")"
# One date token.  Numeric month/year is constrained to a valid month (01-12)
# and a 19xx/20xx year, so shop specs like "8/32", "40/60", "10/24", "40/2024"
# are never read as dates.
#   02/JUN/2025 · 23-04-2028 · 01/06/2025 · 2025/06/02 · Jun-2025 · 06/2024 · 2026-12
_ONE_DATE = rf"""
        \d{{1,2}}[-/.]{_MON}[-/.]\d{{2,4}}
      | \d{{1,2}}[-/.]\d{{1,2}}[-/.]\d{{2,4}}
      | \d{{4}}[-/.]\d{{1,2}}[-/.]\d{{1,2}}
      | {_MON}[-/.]?(?:19|20)\d{{2}}
      | (?:0?[1-9]|1[0-2])[-/.](?:19|20)\d{{2}}
      | (?:19|20)\d{{2}}[-/.](?:0?[1-9]|1[0-2])
"""
# A date, optionally a merged range ("02/JUN/2025-01/JUN/2028").
_DATEISH = re.compile(rf"(?ix)(?:{_ONE_DATE})(?:\s*[-–]\s*(?:{_ONE_DATE}))*")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")              # integer or decimal (batch/lot/MRP value)
# an alphanumeric code (letters AND digits): batch AB1234, lot A12B3, 7A/2024…
_CODE = re.compile(r"(?=[A-Za-z0-9/.\-]*[A-Za-z])(?=[A-Za-z0-9/.\-]*\d)[A-Za-z0-9/.\-]{2,}")
_SEP_ONLY = re.compile(r"[-/.,;:()|]+")
_LONGNUM = re.compile(r"\d{6,}")                      # batch-length number (never a model/year)
_PUNCT_EDGE = "().,;:[]{}|"
_TRAIL_STRIP = " \t\r\n/,-;:|.("


def _core(tok: str) -> str:
    return tok.strip(_PUNCT_EDGE)


def _word_parts(tok: str) -> list[str]:
    return [p for p in re.split(r"[^A-Za-z]+", tok.lower()) if p]


def _is_sep(tok: str) -> bool:
    return bool(_SEP_ONLY.fullmatch(tok))


def _is_date(core: str) -> bool:
    return bool(_DATEISH.fullmatch(core))


def _is_value(core: str) -> bool:
    return bool(_NUMBER.fullmatch(core) or _CODE.fullmatch(core))


def _is_filler_word(tok: str) -> bool:
    parts = _word_parts(tok)
    return bool(parts) and all(p in _FILLER_WORDS for p in parts)


def _is_anchor_word(tok: str) -> bool:
    # an all-filler token that carries at least one trigger part: "Best",
    # "Batch/Exp", "Exp.Dt" all anchor; a bare "Dt"/"No"/"May" does not.
    parts = _word_parts(tok)
    return (bool(parts) and all(p in _FILLER_WORDS for p in parts)
            and any(p in _ANCHOR_WORDS for p in parts))


def _is_anchor(tok: str) -> bool:
    return _is_date(_core(tok)) or _is_anchor_word(tok)


def _is_annotation(tok: str) -> bool:
    core = _core(tok)
    return _is_sep(tok) or _is_date(core) or _is_value(core) or _is_filler_word(tok)


def _region_has_value(seg: list[str]) -> bool:
    return any(_is_date(_core(t)) or _is_value(_core(t)) for t in seg)


def clean_description(raw: str) -> tuple[str, str | None]:
    """Return ``(clean_name, trimmed_annotation_or_None)``.

    ``clean_name`` is whitespace-normalised.  ``trimmed_annotation`` is the text
    that was removed (returned, never silently discarded, so the reviewer can
    see it), or ``None`` when the description was already clean.
    """
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    if not text:
        return text, None
    # split a label glued to its value by a dot/colon ("Exp.Dt:01/JUN/2028",
    # "Batch:552") so the label and the date/number tokenize apart
    text = re.sub(r"(?i)([A-Za-z])[.:](\d)", r"\1 \2", text)
    tokens = text.split()
    for i, tok in enumerate(tokens):
        if not _is_anchor(tok):
            continue
        seg = tokens[i:]
        if not all(_is_annotation(t) for t in seg) or not _region_has_value(seg):
            continue
        start = i
        # Label-less date anchor (OCR dropped the "Batch"/"Mfg" word): absorb an
        # immediately preceding BATCH-length number (>= 6 digits) and separators,
        # but never a short trailing model number / year (<= 5 digits stays).
        if _is_date(_core(tok)):
            while start > 0 and (_is_sep(tokens[start - 1])
                                 or _LONGNUM.fullmatch(_core(tokens[start - 1]))):
                start -= 1
        if start == 0:                                # whole cell is annotation — keep as-is
            return text, None
        kept = " ".join(tokens[:start]).rstrip(_TRAIL_STRIP)
        if not kept:
            return text, None
        return kept, " ".join(tokens[start:])
    return text, None


# --------------------------------------------------------------------------- #
# is_code_only  (Mode-B detector)
# --------------------------------------------------------------------------- #
# barcode / identifier prefixes that are never a product name on their own
_CODE_LABELS = re.compile(r"\b(?:GTIN|SKU|EAN|UPC|MPN|CFN|ASIN)\b", re.I)


def is_code_only(description: str) -> bool:
    """True when the description carries no product NAME — only an identity code
    (part number / GTIN) plus separators.  The signal that OCR captured the code
    cell but dropped the name (e.g. ``"H7493912412200"`` or ``"GTIN 040063…"``).

    Strictly conservative: fires only when NO alphabetic word (a run of >= 2
    letters) survives after the identity tokens and bare code-label prefixes are
    removed, so a named item — even a terse one like ``"Bolt M8 x 40mm"`` —
    never trips it.
    """
    text = (description or "").strip()
    if not text:
        return False                                  # empty is a different problem
    stripped = _CODE_LABELS.sub(" ", text.upper())
    for pat in (_GTIN, _PART, _PART_DIGIT_LED):
        stripped = pat.sub(" ", stripped)
    return re.search(r"[A-Z]{2,}", stripped) is None
