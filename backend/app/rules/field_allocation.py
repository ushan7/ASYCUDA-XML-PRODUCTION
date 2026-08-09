"""Deterministic per-item FIELD ALLOCATION — vendor-mixed description cells.

Live root cause (job 88491b56, Medtronic invoice 4050032873, 2026-07-30): some
vendors print a row's batch number, a quantity echo and the country of origin
INSIDE the description cell::

    STENT RSINT25012X MICROTRAC 2.50X12RX Batch: 0013032995 3 EA COO: Ireland

so the customs-declared description carried the batch/COO tail, the per-row
COO was never captured as a field (the silent exporter fallback then declared
Singapore for Irish goods), and OCR line-wrap even glued the PREVIOUS row's
overflow onto the next description.  ``description_clean`` could not help: its
vocabulary is batch/date annotations only — ``EA``, ``COO:`` and country names
are not annotation material there.

This module is the deterministic allocator that fixes it at the ingest
boundary, for BOTH extraction sources (table parser and LLM alike — the
pipeline never trusts either raw):

* :func:`allocate_description` — strip a leading previous-row overflow run,
  split the cell at the first strong annotation label (``Batch:``, ``Lot#``,
  ``Batch/Expiry Date``, ``(Qty)``, ``COO:``, ``Country of Origin``,
  ``Made in``), and mine the row's own COO / batch from the removed tail.
  A cut is only taken when everything removed is provably annotation
  material (labels, codes, numbers, dates, UOM words, a normalizable
  country) — ``GENUINE MADE IN ITALY WALLET`` keeps its description whole
  (while still yielding COO=IT), because ``WALLET`` is not annotation.
* :func:`normalize_coo_candidate` — progressive country normalization for
  raw COO values that carry a label or trailing words (``"COO: Ireland"``,
  ``"Ireland Tariff Code"`` -> IE), used by ``rules.coo``.
* :func:`audit_items` — post-resolution field gates (run at the end of
  ``resolve_context``): flags a barcode-only MODEL and a final description
  that still carries annotation labels, whatever their source.

Nothing here invents text: the allocator only removes, relocates or
classifies, and every removal is surfaced to the reviewer by the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..domain.errors import ValidationMessage
from ..extraction.manifest import _GTIN, UOM_ALT

# --------------------------------------------------------------------------- #
# vocabularies
# --------------------------------------------------------------------------- #
# COO label — shared by mining and splitting.  "Origin" alone is accepted for
# MINING (normalize-gated, so junk dies) but never anchors a CUT: "SINGLE
# ORIGIN COFFEE" must keep its description.  ``C.O.C`` is accepted as the
# routine OCR misread of ``COO`` (live job page 6 printed "COC: Ireland");
# every capture is still normalize-gated, so a real certificate-of-conformity
# reference never yields a country.
_COO_LABEL_STRONG = r"(?:C\.?O\.?[OC]\b|COUNTRY\s*OF\s*ORIGIN\b|MADE\s*IN\b)"
_COO_LABEL_ANY = rf"(?:{_COO_LABEL_STRONG}|\bORIGIN\b)"
# The words after a COO label: letters/spaces/dots up to 39 chars (stops at any
# digit, so "COO: Ireland Tariff Code 38220090" captures "Ireland Tariff Code"
# and progressive normalization trims it back to Ireland).
_COO_AFTER = re.compile(rf"(?i){_COO_LABEL_ANY}\s*[:.\-]?\s*([A-Za-z][A-Za-z .'\-]{{1,39}})")

# A strong in-description annotation label that may START the removed tail.
# Batch/lot/serial need an explicit separator or the compound "Batch/Expiry"
# form so "Job Lot" / "Parking Lot" never cut (mirrors description_clean).
_SPLIT_AT = re.compile(
    rf"(?i)"
    rf"\b(?:BATCH(?:ES)?|LOT|SERIAL|S/N)\b\s*(?:/\s*(?:EXPIRY|EXP)(?:\s*(?:DATE|DT))?)?\s*"
    rf"(?:NO|NUMBER|\#)?\s*[:#]"
    rf"|\bBATCH\s*/\s*EXPIRY(?:\s*DATE)?\b"
    rf"|\(\s*QTY\s*\)"
    # a colon may be glued to its value ("COO:Ireland"); a label with no
    # separator at all still needs the space so "COOLER" never anchors
    rf"|{_COO_LABEL_STRONG}\s*(?:[:.]\s*|\s+)")

# Words that may appear inside a removed annotation tail without invalidating
# the cut (labels + their qualifiers); numbers/dates/codes are checked by shape.
_TAIL_WORDS = frozenset({
    "batch", "batches", "lot", "lots", "serial", "sn", "s/n", "no", "nos",
    "number", "code", "codes", "qty", "quantity", "exp", "expiry", "expiration",
    "date", "dt", "mfg", "mfd", "coo", "coc", "country", "of", "origin", "made",
    "in", "tariff", "hs", "hsn", "hts", "customs",
})
_UOM_RE = re.compile(rf"(?i)^(?:{UOM_ALT})$")
_NUMBERISH = re.compile(r"[\d][\d.,/\-]*")
# an alphanumeric code (letters AND digits) — batch "69698UQ01", "7A/2024"
_CODEISH = re.compile(r"(?=[A-Za-z0-9/.\-]*[A-Za-z])(?=[A-Za-z0-9/.\-]*\d)[A-Za-z0-9/.\-]{2,}")
_SEP_ONLY = re.compile(r"[-/.,;:()|#]+")
_WORDS = re.compile(r"[A-Za-z]{2,}")

# A previous-row overflow run glued to the FRONT of a description by OCR line
# wrap: batch-length number + quantity + UOM (+ optional COO clause with a
# single-word country).  Only this full shape strips — a description that
# merely starts with a number is never touched.
_LEAD_OVERFLOW = re.compile(
    rf"(?i)^\s*(?:\d{{9,}}\s+\d+(?:[.,]\d+)?\s+(?:{UOM_ALT})\b[\s,;]*"
    rf"(?:{_COO_LABEL_STRONG}\s*[:.\-]?\s*[A-Za-z]{{2,20}}\b[\s,;]*)?)+")


@dataclass
class DescriptionAllocation:
    """Result of :func:`allocate_description` — nothing is ever discarded."""
    description: str                     # the goods name kept for declaration
    annotation: str | None = None        # text removed (tail and/or leading run)
    coo_raw: str | None = None           # the row's own COO mined from the text
    batch_raw: str | None = None         # batch/lot code mined from the tail


def _tail_is_annotation(tail: str, ref) -> bool:
    """Every removed token must be annotation material.  COO clauses are
    consumed as label + country words (ref-normalized when a store is given,
    else only a single terminal word), so ``COO: Ireland`` validates while
    ``MADE IN ITALY WALLET`` does not.  Consumes the STRING front-to-back —
    token-count arithmetic mis-stepped when a label glued to its value
    (``COO:Ireland``) spanned one physical token, letting a following real
    word escape the check."""
    rest = tail.strip()
    while rest:
        m = re.match(rf"(?i){_COO_LABEL_ANY}\s*[:.\-]?\s*", rest)
        if m:
            after = rest[m.end():].split()
            take = 0
            if ref is not None:
                for n in range(min(3, len(after)), 0, -1):
                    if ref.normalize_country(" ".join(after[:n]).strip(" .,-;:")):
                        take = n
                        break
            elif len(after) == 1:
                take = 1                       # no store: only a terminal 1-word country
            if take:
                rest = " ".join(after[take:])
                continue
        tok, _, remainder = rest.partition(" ")
        core = tok.strip("().,;:[]{}|")
        parts = [p for p in re.split(r"[^A-Za-z]+", tok.lower()) if p]
        if (_SEP_ONLY.fullmatch(tok) or _UOM_RE.match(core)
                or _NUMBERISH.fullmatch(core) or _CODEISH.fullmatch(core)
                or (parts and all(p in _TAIL_WORDS for p in parts))):
            rest = remainder.strip()
            continue
        return False
    return True


def _mine_coo(text: str, ref) -> str | None:
    """The LAST labelled COO in ``text`` (a leading overflow clause belongs to
    the previous row; the row's own COO prints last), normalize-gated."""
    raw = None
    for m in _COO_AFTER.finditer(text):
        raw = m.group(1)
    if raw is None:
        return None
    if ref is None:
        return raw.strip(" .,-;:") or None
    words = raw.strip().split()
    for n in range(min(4, len(words)), 0, -1):
        cand = " ".join(words[:n]).strip(" .,-;:")
        code = ref.normalize_country(cand)
        if code:
            return cand                      # raw (not the code): extraction stays raw
    return None


_BATCH_AFTER = re.compile(
    r"(?i)\b(?:BATCH(?:ES)?|LOT)\b\s*(?:NO|NUMBER|\#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9/.\-]{2,})")


def allocate_description(raw: str, ref=None) -> DescriptionAllocation:
    """Split a vendor-mixed description cell into its fields.

    Returns the goods description plus whatever was removed and mined.  When no
    strong label is found — or removing the tail would take real description
    words with it — the text is returned unchanged (mining may still yield a
    COO).  ``ref`` is the ReferenceStore; without it country validation is
    conservative and COO-label cuts are effectively disabled mid-string.
    """
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    if not text:
        return DescriptionAllocation(description=text)
    removed_parts: list[str] = []

    lead = _LEAD_OVERFLOW.match(text)
    if lead and _WORDS.search(text[lead.end():]):
        removed_parts.append(text[:lead.end()].strip())
        text = text[lead.end():].strip()

    tail = None
    for m in _SPLIT_AT.finditer(text):
        head = text[:m.start()].rstrip(" \t,;:-|/(")
        cand_tail = text[m.start():].strip()
        if not head or not _WORDS.search(head):
            continue                          # a cut may never blank the name
        if not _tail_is_annotation(cand_tail, ref):
            continue                          # real description words follow — keep whole
        text, tail = head, cand_tail
        removed_parts.append(cand_tail)
        break

    mine_from = tail if tail is not None else text
    coo_raw = _mine_coo(mine_from, ref)
    batch_raw = None
    if tail:
        b = _BATCH_AFTER.search(tail)
        if b and not _GTIN.fullmatch(b.group(1)):
            batch_raw = b.group(1)
    return DescriptionAllocation(
        description=text,
        annotation=(" ".join(removed_parts) or None),
        coo_raw=coo_raw, batch_raw=batch_raw)


def normalize_coo_candidate(raw: str | None, ref) -> str | None:
    """Alpha-2 from a raw COO that plain ``normalize_country`` rejects:
    a labelled value (``"COO: Ireland"``), or a country name with trailing
    words (``"Ireland Tariff Code"``).  Progressive and label-gated — a bare
    non-country word never resolves."""
    if not raw or ref is None:
        return None
    text = str(raw).strip()
    m = None
    for m2 in _COO_AFTER.finditer(text):
        m = m2
    cand = (m.group(1) if m else text).strip()
    words = cand.split()
    for n in range(min(4, len(words)), 0, -1):
        code = ref.normalize_country(" ".join(words[:n]).strip(" .,-;:"))
        if code:
            return code
    return None


# --------------------------------------------------------------------------- #
# post-resolution field gates (P5) — run at the end of resolve_context
# --------------------------------------------------------------------------- #
def audit_items(items) -> None:
    """Deterministic per-item field gates, source-agnostic.

    Warnings only — these fields are reviewer-editable and export-only (model)
    or already covered by blocking COO rules, so the gate surfaces rather than
    stops.  Runs after every resolver INCLUDING reviewer edits, so an edited
    description that re-introduces batch text is caught too.
    """
    for it in items:
        seq = getattr(it, "xml_item_sequence", None)
        model = getattr(it, "model", None)
        if model and _GTIN.fullmatch(str(model).strip()):
            it.warnings.append(ValidationMessage.warning(
                "MODEL_BARCODE_ONLY",
                f"Item {seq}: MODEL resolved to a GTIN/EAN barcode number ({model}) — the "
                f"catalogue/part code was not captured; verify the model in the BMS export.",
                scope="ITEM", item_sequence=seq, field="model"))
        desc = getattr(it, "description_raw", "") or ""
        if _SPLIT_AT.search(desc):
            it.warnings.append(ValidationMessage.warning(
                "DESCRIPTION_EXTRA_INFO",
                f"Item {seq}: the declared description still carries batch/origin annotation "
                f"text ({desc[:60]!r}) — verify it is the goods name only.",
                scope="ITEM", item_sequence=seq, field="description"))
