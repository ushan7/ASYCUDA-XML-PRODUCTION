"""Description-cell segmentation — typed PROSE / ANNOTATION runs.

A vendor-mixed DESCRIPTION cell is not "a name with a tail".  It is an
alternating sequence of ANNOTATION runs (batch codes, ``<n> EA`` quantity
echoes, ``COO: <country>`` clauses) and PROSE runs (goods names), and when OCR
reads a table whose description column wraps across row and page boundaries,
those runs stop lining up with the rows that own them.  A live page 16
(Medtronic invoice 4050033058) prints ONE cell holding four things::

    <previous page's item's batch runs> CATHETER LA6JL40 LA 6F 100CM JL40
    Batch: <this row's batch runs> CATHETER LA6EBU35 LA 6F 100CM EB35

— the row before it, this row's name, this row's batches, and the NEXT row's
name.  The next row's own cell is empty, because its name is up here.

``rules.field_allocation`` cannot express that shape: its only notion of a cut
is "from the label match to the end of the string", so a run in the MIDDLE of a
cell has no end, and a name after that run cannot be separated.  This module
adds the missing primitive — a run with a BOUNDED end — and nothing else.  It
decides no ownership and mutates no row; ``description_attribution`` does that,
where the neighbouring rows are visible.

Deliberately pure: ``re`` and ``.manifest`` only, with country validation
injected as a callable, so it never reaches for the ReferenceStore and can be
tested on strings alone.

The vocabulary constants below are COPIED from ``rules.field_allocation``,
which remains their source of truth.  They are duplicated rather than moved
because moving them would force edits inside a module whose behaviour is pinned
by 93 tests and four recorded live incidents, for no behavioural gain here.
``test_description_segments.py`` fails if the two copies ever diverge.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from .manifest import _PART, _PART_DIGIT_LED, UOM_ALT, normalize_token

# --------------------------------------------------------------------------- #
# vocabulary — mirrors rules.field_allocation (source of truth)
# --------------------------------------------------------------------------- #
_COO_LABEL_STRONG = r"(?:C\.?O\.?[OC]\b|COUNTRY\s*OF\s*ORIGIN\b|MADE\s*IN\b)"
_COO_LABEL_ANY = rf"(?:{_COO_LABEL_STRONG}|\bORIGIN\b)"
_TAIL_WORDS = frozenset({
    "batch", "batches", "lot", "lots", "serial", "sn", "s/n", "no", "nos",
    "number", "code", "codes", "qty", "quantity", "exp", "expiry", "expiration",
    "date", "dt", "mfg", "mfd", "coo", "coc", "country", "of", "origin", "made",
    "in", "tariff", "hs", "hsn", "hts", "customs",
})
_UOM_RE = re.compile(rf"(?i)^(?:{UOM_ALT})$")
_NUMBERISH = re.compile(r"[\d][\d.,/\-]*")
_CODEISH = re.compile(r"(?=[A-Za-z0-9/.\-]*[A-Za-z])(?=[A-Za-z0-9/.\-]*\d)[A-Za-z0-9/.\-]{2,}")
_SEP_ONLY = re.compile(r"[-/.,;:()|#]+")

# --------------------------------------------------------------------------- #
# where an annotation run may START
# --------------------------------------------------------------------------- #
# A1 — the non-COO alternatives of field_allocation._SPLIT_AT, verbatim.  The
# mandatory [:#] is what keeps "Job Lot", "Parking Lot" and "BATCH MIXER 400"
# from ever anchoring a run.
_LABEL_ANCHOR = re.compile(
    r"(?i)"
    r"\b(?:BATCH(?:ES)?|LOT|SERIAL|S/N)\b\s*(?:/\s*(?:EXPIRY|EXP)(?:\s*(?:DATE|DT))?)?\s*"
    r"(?:NO|NUMBER|\#)?\s*[:#]"
    r"|\bBATCH\s*/\s*EXPIRY(?:\s*DATE)?\b"
    r"|\(\s*QTY\s*\)")
# A2 — a COO label.  STRONG only (bare "ORIGIN" is excluded so "SINGLE ORIGIN
# COFFEE" survives) and it must be followed by a separator or a space, so
# "COOLER" cannot anchor.  A normalize gate on the country follows.
_COO_ANCHOR = re.compile(rf"(?i){_COO_LABEL_STRONG}\s*(?:[:.]\s*|\s+)")
_COO_CLAUSE = re.compile(rf"(?i){_COO_LABEL_ANY}\s*[:.\-]?\s*")
# A3 — field_allocation._LEAD_OVERFLOW's body, un-anchored from ^: a
# batch-shaped code followed by a quantity and a unit ("232720182 3 EA").
_CODE_QTY = re.compile(
    rf"(?i)(?=[A-Za-z0-9\-]{{6,}})(?:[A-Za-z0-9\-]*\d){{4,}}[A-Za-z0-9\-]*"
    rf"\s+\d+(?:[.,]\d+)?\s+(?:{UOM_ALT})\b")
_QTY_ECHO = re.compile(rf"(?i)\b(\d+(?:[.,]\d+)?)\s+(?:{UOM_ALT})\b")

# --------------------------------------------------------------------------- #
# unglue — OCR welds a label onto the text before it
# --------------------------------------------------------------------------- #
# NOTE: no leading \b in the alternation — in "AL10Batch:" there is no word
# boundary between "0" and "B", which is exactly the case that needs splitting.
_U1 = re.compile(
    r"(?i)(?<=[A-Za-z0-9])(?="
    r"(?:BATCH(?:ES)?|LOT|SERIAL|S/N)\s*(?:/\s*(?:EXPIRY|EXP)(?:\s*(?:DATE|DT))?)?\s*"
    r"(?:NO|NUMBER|\#)?\s*[:#]"
    r"|C\.?O\.?[OC]\s*[:.]|COUNTRY\s*OF\s*ORIGIN\b|MADE\s*IN\b)")
_U2 = re.compile(r"(?i)([A-Za-z])([:#])(?=[A-Za-z0-9])")
_U3 = re.compile(r"(?<=[A-Za-z]{3})(?=\d{6,})")


def unglue(raw: str) -> str:
    """Insert spaces where OCR welded an annotation label onto its neighbour.

    Only ever ADDS whitespace — never removes or rewrites a character, which is
    asserted below, so this can never change what the document says.

    U3 (a long digit run welded to a word) is GATED on the cell also carrying a
    strong annotation label somewhere.  Ungated it would rewrite ``PIPE123456``
    on vendors that have no such defect at all; gated, it only fires on cells
    already proven annotation-mixed.
    """
    text = raw or ""
    out = _U2.sub(r"\1\2 ", _U1.sub(" ", text))
    if _LABEL_ANCHOR.search(out) or _COO_ANCHOR.search(out):
        out = _U3.sub(" ", out)
    assert re.sub(r"\s+", "", out) == re.sub(r"\s+", "", text), "unglue must only add spaces"
    return out


@dataclass(frozen=True)
class Segment:
    kind: str          # "PROSE" (a goods name) | "ANN" (annotation)
    start: int
    end: int
    text: str


def _coo_clause_len(rest: str, normalize_country: Callable[[str], str | None] | None) -> int:
    """Chars consumed by a COO clause at the front of ``rest``, else 0.

    Word PREFIXES are walked, never suffixes: ``normalize_country("LA")``
    returns Laos, so a suffix walk over "Mexico CATHETER LA 6F" would happily
    "validate" the catheter's own words as a country.
    """
    m = _COO_CLAUSE.match(rest)
    if not m:
        return 0
    after = rest[m.end():]
    words = after.split()
    if not words:
        return 0
    if normalize_country is None:
        # conservative without a store: only a single TERMINAL word counts
        return m.end() + len(after) if len(words) == 1 else 0
    for n in range(min(3, len(words)), 0, -1):
        cand = " ".join(words[:n]).strip(" .,-;:")
        if cand and normalize_country(cand):
            # consume through the nth word as it appears in the raw text
            idx, seen = 0, 0
            for _ in range(n):
                idx = after.index(words[seen], idx) + len(words[seen])
                seen += 1
            return m.end() + idx
    return 0


def _run_end(text: str, i: int, normalize_country: Callable[[str], str | None] | None,
             label_end: int = 0) -> tuple[int, int]:
    """Consume an annotation run starting at ``i``; return ``(end, last_value_end)``.

    The acceptance rules are ``field_allocation._tail_is_annotation``'s, kept
    identical on purpose: a COO clause, or one token that is a separator, a
    unit, a number, an alphanumeric code, or an all-alphabetic token whose
    words are all annotation vocabulary.  Consumption is over the STRING
    front-to-back, never token arithmetic — a label glued to its value
    (``COO:Ireland``) spans one physical token and mis-stepped a token count.

    The run is reported as ending at ``last_value_end``, the end of the last
    VALUE-BEARING token, so trailing filler words ("Tariff", "Code", "of") are
    left outside the run instead of extending it over the prose that follows.
    """
    pos = i
    # A label that OPENED the run closes it on its own: "Batch:" with an empty
    # list after it (a page break cut the list off) is still annotation, and
    # leaving it unclosed left the bare label sitting in the goods description.
    last_value_end = max(i, label_end)
    while pos < len(text):
        rest = text[pos:]
        lead = len(rest) - len(rest.lstrip())
        pos += lead
        rest = text[pos:]
        if not rest:
            break
        take = _coo_clause_len(rest, normalize_country)
        if take:
            pos += take
            last_value_end = pos
            continue
        tok = rest.split(" ", 1)[0]
        core = tok.strip("().,;:[]{}|")
        parts = [p for p in re.split(r"[^A-Za-z]+", tok.lower()) if p]
        if (_SEP_ONLY.fullmatch(tok) or _UOM_RE.match(core)
                or _NUMBERISH.fullmatch(core) or _CODEISH.fullmatch(core)):
            pos += len(tok)
            last_value_end = pos
            continue
        if parts and all(p in _TAIL_WORDS for p in parts):
            pos += len(tok)          # a label word — consumed, but not a value
            continue
        break
    return pos, last_value_end


def segment_description(
        raw: str,
        normalize_country: Callable[[str], str | None] | None = None,
) -> tuple[list[Segment], str]:
    """Split a description cell into ordered PROSE / ANN runs.

    Returns ``(segments, unglued_text)``; every segment is an exact slice of
    ``unglued_text``, so a caller can only ever re-emit text the document
    printed.
    """
    text = re.sub(r"\s+", " ", unglue(raw or "")).strip()
    if not text:
        return [], text
    segments: list[Segment] = []
    i = 0
    cursor = 0
    while i < len(text):
        label_end = 0
        m = _LABEL_ANCHOR.match(text, i)
        if m:
            label_end = m.end()
        elif _COO_ANCHOR.match(text, i) and _coo_clause_len(text[i:], normalize_country):
            label_end = 0                     # the clause itself is the value
        elif not _CODE_QTY.match(text, i):
            i += 1
            continue
        end, last_value_end = _run_end(text, i, normalize_country, label_end)
        run_end = max(last_value_end, i)
        if run_end <= i:
            i += 1
            continue
        if i > cursor:
            prose = text[cursor:i].strip()
            if prose:
                segments.append(Segment("PROSE", cursor, i, prose))
        segments.append(Segment("ANN", i, run_end, text[i:run_end].strip()))
        cursor = i = run_end
    if cursor < len(text):
        prose = text[cursor:].strip()
        if prose:
            segments.append(Segment("PROSE", cursor, len(text), prose))
    return segments, text


def qty_echoes(seg_text: str) -> list[Decimal]:
    """Every ``<n> <UOM>`` quantity echo inside an annotation run."""
    out = []
    for m in _QTY_ECHO.finditer(seg_text or ""):
        try:
            out.append(Decimal(m.group(1).replace(",", ".")))
        except Exception:
            continue
    return out


def part_codes(seg_text: str) -> list[str]:
    """Part-code-shaped tokens in printed order, normalized, de-duplicated.

    Both manifest patterns are case-sensitive, so the text is upper-cased
    first — a lower-case vendor spelling is still a part code.
    """
    upper = (seg_text or "").upper()
    seen: dict[str, None] = {}
    for pat in (_PART, _PART_DIGIT_LED):
        for m in pat.finditer(upper):
            code = normalize_token(m.group(0))
            if code:
                seen.setdefault(code, None)
    return list(seen)


def model_matches(code: str, model_cell: str | None) -> bool:
    """Does ``code`` identify the row whose MODEL cell is ``model_cell``?

    Exact after normalization, or the model cell is the code followed by a
    12-14 digit barcode — vendors routinely print ``LA6JL40 00763000567682``
    and OCR routinely welds it to ``LA6AL1000763000565299``.  Directional and
    exact: it never manufactures a candidate code to test against.
    """
    mk = normalize_token(model_cell or "")
    if not mk or not code:
        return False
    if mk == code:
        return True
    return mk.startswith(code) and re.fullmatch(r"\d{12,14}", mk[len(code):]) is not None


def is_code_only(seg_text: str) -> bool:
    """The segment carries no goods NAME — only codes, numbers and units.

    A two-line local predicate rather than an import from ``rules``: the
    extraction layer does not import the rules layer, and the one-way layering
    is worth more than the shared function.  ``test_description_segments.py``
    pins that it agrees with ``rules.description_clean.is_code_only``.
    """
    # Tokenize on WHITESPACE, not on letter runs: splitting inside "LA6EBU35"
    # manufactures the words "LA" and "EBU" and the segment then looks named.
    for tok in (seg_text or "").split():
        core = tok.strip("().,;:[]{}|#-/")
        if not core or re.search(r"\d", core) or len(core) < 2:
            continue                              # a code, a number, a stray letter
        if core.lower() in _TAIL_WORDS or _UOM_RE.match(core):
            continue                              # annotation vocabulary / a unit
        return False
    return True
