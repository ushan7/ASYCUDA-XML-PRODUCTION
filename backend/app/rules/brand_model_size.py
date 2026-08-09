"""Deterministic per-item BRAND / MODEL / SIZE resolver (export-only).

These three values feed the sibling ``brand-model-size.xls`` produced after the
ASYCUDA XML — one row per item, in exact XML-item order.  They are display /
export data only and are **never** written into the customs XML, so this module
only ever *reads* a row's already-extracted fields; it invents nothing.

Rules (user-decided 2026-07-21):

* **BRAND** — a per-row brand column when the invoice printed one, else the
  document exporter/manufacturer name.  Either way: the first *significant*
  word, UPPERCASE.  Courtesy / legal-form prefixes and bare initials are skipped
  first, so ``"M/S. Abbott Laboratories"`` -> ``ABBOTT`` (not ``M``) and
  ``"A. Menarini Diagnostics"`` -> ``MENARINI``.  Neither available -> ``NA``.

* **MODEL** — the invoice's model/part/SKU/catalogue cell, else a *labelled*
  code embedded in the description.  The cell is parsed label-aware: vendors
  routinely print the label inside the cell (``"REF 01R6070"``, ``"P/N: AB-12"``,
  ``"CFN 07K5901"``), so the label is stripped and the code kept — never the
  other way round.  Failing a label, the first letter-AND-digit token wins,
  then the first digit-bearing one (real codes carry digits; a stray count in
  the cell never beats a real code).  A 13-14 digit GTIN/EAN barcode is never
  a model, wherever it appears.  Nothing -> ``NA``.

* **SIZE** — a real *size*, never a quantity.  Cascade: a size/dimension/
  capacity column (trusted verbatim) -> a measured specification mined from the
  description (``400ML``, ``1L``, ``20 X 30MM``, ``10 x 113 mL``, ``32GB``,
  ``42mm``, ``EU 42``) -> a word size (``LARGE``, ``EXTRA LARGE``) -> ``NA``.

  Count / packaging units (PCS, EA, NOS, SET, BOX, …) are deliberately **absent
  from the size vocabulary**, so a quantity echo such as ``"Card Reader 5 PCS"``
  can never be reported as a size — the original defect, fixed at the root
  rather than out-ranked.  All candidates are collected and the most specific
  wins (a pack configuration beats a lone value), so a specification later in
  the string still beats an earlier one.

  Short forms are never emitted: ``S/M/L/XL/XXL`` expand to ``SMALL`` /
  ``MEDIUM`` / ``LARGE`` / ``EXTRA LARGE`` / ``EXTRA EXTRA LARGE``.  A letter
  bound to a number stays a unit (``1L`` = one litre), while a standalone letter
  is a garment size (``"T-Shirt L"`` -> ``LARGE``); designator labels are
  guarded, so ``"Model L"`` is not a size.  Measured sizes keep the invoice's own
  casing; word sizes are emitted as full uppercase words.

Every raw cell first passes :func:`_cell`, which treats placeholder text
(``-``, ``N/A``, ``NIL``, ``TBD``, ``0`` …) as absent rather than as data.
"""
from __future__ import annotations

import re

from ..extraction.manifest import _GTIN

NA = "NA"

_WS = re.compile(r"\s+")
# Cell text that means "nothing here" — never a real brand / model / size.
_PLACEHOLDERS = frozenset({
    "", "n/a", "na", "n.a", "n.a.", "nil", "none", "null", "tbd", "tba",
    "?", "??", "0", "not applicable", "not available",
})


def _cell(raw) -> str | None:
    """Whitespace-normalised cell text, or ``None`` when empty / a placeholder."""
    text = _WS.sub(" ", str(raw or "").strip())
    if not text:
        return None
    probe = text.strip(" .-_*").lower()
    return None if probe in _PLACEHOLDERS else text


# --------------------------------------------------------------------------- #
# BRAND
# --------------------------------------------------------------------------- #
_WORD = re.compile(r"[^\W_]+", re.UNICODE)   # first alphanumeric run

# Courtesy / legal-form prefixes that precede the real trading name.  Skipped
# when picking the significant word ("M/S. Abbott Laboratories" -> ABBOTT).
_BRAND_SKIP = frozenset({
    "m/s", "ms", "mrs", "messrs", "messers", "mssrs", "the", "pt", "cv", "ud",
    "pt.", "m/s.", "sdn", "bhd", "shri", "sri", "sh",
})


def _is_initial(token: str) -> bool:
    core = token.strip(".,;:'\"()[]")
    return len(core) == 1 and core.isalpha()


def _significant_word(text: str | None) -> str | None:
    """First meaningful word, UPPERCASE — skipping courtesy/legal prefixes and
    bare initials.  Falls back to the very first word if everything is skipped."""
    if not text:
        return None
    for token in str(text).split():
        norm = token.strip(".,;:'\"()[]").lower()
        if not norm or norm in _BRAND_SKIP or _is_initial(token):
            continue
        m = _WORD.search(token)
        if m:
            return m.group(0).upper()
    m = _WORD.search(str(text))              # everything looked like a prefix
    return m.group(0).upper() if m else None


def resolve_brand(brand_raw: str | None, exporter_name: str | None) -> str:
    """Significant word of the row brand cell, else of the exporter."""
    return (_significant_word(_cell(brand_raw))
            or _significant_word(_cell(exporter_name))
            or NA)


# --------------------------------------------------------------------------- #
# MODEL
# --------------------------------------------------------------------------- #
# A DIGIT-BEARING code.  Real model / part / catalogue codes always carry a
# digit ("4021", "01R6070", "AB-12/34"), which is what separates a code from the
# label or plain word sitting next to it.
_CODE_WITH_DIGIT = r"(?=[A-Za-z0-9/.\-]*\d)[A-Za-z0-9][A-Za-z0-9/.\-]{1,}"

_LABEL_WORDS = frozenset({
    "ref", "reference", "pn", "p/n", "cfn", "model", "art", "article", "cat",
    "catalog", "catalogue", "sku", "item", "part", "no", "no.", "number",
    "code", "mfg", "oem", "style", "id", "type", "series",
})

# A label introducing a code, either inside the model CELL or in the description.
_MODEL_LABEL = re.compile(
    r"(?i)\b(?:model|part|p/?n|cat(?:alogue|alog)?|art(?:icle)?|ref(?:erence)?|"
    r"sku|item\s*code|style|oem|cfn|mfg\s*(?:part|model))\b"
    r"(?:\s*(?:no|number|code|name|id|\#))?"          # optional 'No' / 'Code' qualifier
    r"(?:\s*[:\#.\-]\s*|\s+)"                         # separator: punctuation OR whitespace
    r"(" + _CODE_WITH_DIGIT + r")")

_TRIM = " \t\r\n.,;:|()[]{}"


def _clean_code(token: str | None) -> str | None:
    if not token:
        return None
    core = str(token).strip().strip(_TRIM)
    return core.upper() if core else None


def _is_label_token(code: str) -> bool:
    return code.lower().strip(_TRIM) in _LABEL_WORDS


def _is_gtin(code: str) -> bool:
    """A 13-14 digit GTIN/EAN barcode number is an identity BARCODE, never a
    model/part code — vendors print both in one cell ("RSINT25012X
    00763000478896") and the barcode used to win on the LLM path."""
    return bool(_GTIN.fullmatch(code))


def _code_from_cell(text: str) -> str | None:
    """Parse a model cell that may carry its own label ("REF 01R6070")."""
    m = _MODEL_LABEL.search(text)
    if m:
        code = _clean_code(m.group(1))
        if code and not _is_gtin(code):
            return code
    tokens = text.split()
    # a real code carries a digit — prefer that over any adjacent label word.
    # Letter-AND-digit codes ("01R6070") outrank digit-only tokens ("4021"),
    # so a stray count in the cell ("Qty 5 01R6070") never wins over the code;
    # GTIN barcodes are skipped everywhere (a cell holding ONLY a barcode
    # yields None so the description miner gets its chance).
    for token in tokens:
        code = _clean_code(token)
        if (code and any(ch.isdigit() for ch in code) and any(ch.isalpha() for ch in code)
                and not _is_label_token(code) and not _is_gtin(code)):
            return code
    for token in tokens:
        code = _clean_code(token)
        if (code and any(ch.isdigit() for ch in code)
                and not _is_label_token(code) and not _is_gtin(code)):
            return code
    # digit-less cell (e.g. a pure style code): first non-label token
    for token in tokens:
        code = _clean_code(token)
        if code and not _is_label_token(code) and not _is_gtin(code):
            return code
    return None


def resolve_model(model_raw: str | None, description: str | None) -> str:
    """Label-aware model cell; else a labelled code mined from the description."""
    cell = _cell(model_raw)
    if cell:
        code = _code_from_cell(cell)
        if code:
            return code
    text = _cell(description)
    if text:
        m = _MODEL_LABEL.search(text)
        if m:
            code = _clean_code(m.group(1))
            if code and not _is_gtin(code):
                return code
    return NA


# --------------------------------------------------------------------------- #
# SIZE
# --------------------------------------------------------------------------- #
# Tier 1 — SPECIFICATION units only (they measure the product itself).  Count /
# packaging units (PCS, PC, EA, NOS, SET, PAIR, BOX, CTN, DOZ …) are absent BY
# DESIGN: a quantity is not a size.  Multi-letter units precede single letters
# so "ML"/"MM"/"INCH" win over "M"/"L"/"IN".
_SPEC_UNIT = (r"(?:GB|TB|MB|KB|MAH|KWH|KW|MHZ|GHZ|HZ|INCHES|INCH|"
              r"MM|CM|KM|ML|MG|KG|LBS|LB|OZ|CC|CL|FT|"
              r"IN|W|V|A|L|G|M|\"|')")
_UNIT_END = r"(?![A-Za-z0-9])"

# One measured-specification token:
#   1. dimension / pack run:  10x20cm · 10 x 113 mL · 20 X 30MM — a short
#      NON-UNIT letter suffix glued to the run ("2.50X12RX", a delivery-system
#      designator) is allowed but excluded from the captured size
#   2. value + unit:          500ml · 42mm · 5kg · 55 Inch · 32GB · 2L · 40 EU
#   3. sizing system + value: EU 40 · US 9
_SPEC_RE = re.compile(
    rf"""(?ix)
    (
        \d+(?:\.\d+)?\s*[x×]\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?
        (?: \s*{_SPEC_UNIT}{_UNIT_END} | (?=[A-Za-z]{{1,3}}(?![A-Za-z0-9])) | {_UNIT_END} )
      | \d+(?:\.\d+)?\s*(?:{_SPEC_UNIT}|EU|UK|US){_UNIT_END}
      | (?:EU|UK|US)\s*\d+(?:\.\d+)?{_UNIT_END}
    )
    """)

# Word sizes — never emitted in short form.
_LETTER_SIZE = {
    "xs": "EXTRA SMALL", "s": "SMALL", "m": "MEDIUM", "l": "LARGE",
    "xl": "EXTRA LARGE", "xxl": "EXTRA EXTRA LARGE", "2xl": "EXTRA EXTRA LARGE",
    "xxxl": "EXTRA EXTRA EXTRA LARGE", "3xl": "EXTRA EXTRA EXTRA LARGE",
    "4xl": "EXTRA EXTRA EXTRA EXTRA LARGE",
}
# longest-first so XXL never matches as XL, and 2XL never as "2 litres"
_LETTER_ALT = r"XXXL|XXL|4XL|3XL|2XL|XL|XS|S|M|L"
# a standalone token only: a letter bound to a digit is a UNIT ("1L" = 1 litre)
_LETTER_SIZE_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9/\-])({_LETTER_ALT})(?![A-Za-z0-9/\-])")
# a slash list of sizes: "S/M/L" -> "SMALL/MEDIUM/LARGE"
_LETTER_LIST_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9\-])((?:{_LETTER_ALT})(?:\s*/\s*(?:{_LETTER_ALT}))+)(?![A-Za-z0-9\-])")
# spelled-out sizes, longest phrase first
_PHRASE_SIZE = [
    (r"extra\s+extra\s+extra\s+large", "EXTRA EXTRA EXTRA LARGE"),
    (r"extra\s+extra\s+large", "EXTRA EXTRA LARGE"),
    (r"double\s+extra\s+large", "EXTRA EXTRA LARGE"),
    (r"extra\s+small", "EXTRA SMALL"),
    (r"extra\s+large", "EXTRA LARGE"),
    (r"x-\s*large", "EXTRA LARGE"),
    (r"x-\s*small", "EXTRA SMALL"),
    (r"free\s+size", "FREE SIZE"),
    (r"one\s+size", "ONE SIZE"),
    (r"small", "SMALL"),
    (r"medium", "MEDIUM"),
    (r"large", "LARGE"),
]
# a letter following one of these is a designator, not a size ("Model L")
_DESIGNATOR_TAIL = re.compile(
    r"(?i)\b(?:model|type|grade|class|series|version|mark|mk|cat|category|"
    r"variant|rev|revision)\b\s*[:.\-]?\s*$")


def _clean_size(text: str) -> str:
    return _WS.sub(" ", str(text).strip())


def _best_spec(text: str) -> str | None:
    """Most specific measured specification in ``text``.

    All candidates are collected (not just the first), so a specification later
    in the string still wins; a pack configuration outranks a lone value.
    """
    cands = [_clean_size(m.group(1)) for m in _SPEC_RE.finditer(text)]
    cands = [c for c in cands if c]
    if not cands:
        return None
    packs = [c for c in cands if re.search(r"[x×]", c, re.I)]
    return packs[0] if packs else cands[0]


def _word_size(text: str) -> str | None:
    """A garment/word size, always expanded to full words."""
    for pattern, out in _PHRASE_SIZE:
        if re.search(rf"(?i)\b{pattern}\b", text):
            return out
    m = _LETTER_LIST_RE.search(text)          # "S/M/L" -> SMALL/MEDIUM/LARGE
    if m:
        parts = [p.strip().lower() for p in m.group(1).split("/")]
        return "/".join(_LETTER_SIZE[p] for p in parts if p in _LETTER_SIZE)
    for m in _LETTER_SIZE_RE.finditer(text):
        if _DESIGNATOR_TAIL.search(text[:m.start()]):
            continue                          # "Model L" is a designator
        return _LETTER_SIZE[m.group(1).lower()]
    return None


def _normalise_size(text: str) -> str:
    """Size cascade over one piece of text: measured spec -> word size -> as-is."""
    return _best_spec(text) or _word_size(text) or _clean_size(text)


def resolve_size(size_raw: str | None, description: str | None) -> str:
    """A size column (trusted, normalised) else a real size mined from the
    description.  Never a quantity; ``NA`` when no size can be found."""
    cell = _cell(size_raw)
    if cell:
        return _normalise_size(cell)
    text = _cell(description)
    if text:
        return _best_spec(text) or _word_size(text) or NA
    return NA


# --------------------------------------------------------------------------- #
# Combined
# --------------------------------------------------------------------------- #
def apply_edits(items, edits: dict | None) -> None:
    """Pre-set reviewer BRAND/MODEL/SIZE overrides (keyed by ``item_id``).

    Runs immediately BEFORE :func:`resolve_all`, which then only fills what is
    still unset — so a reviewer value always wins over the deterministic one,
    and clearing an override (the key is simply absent) restores it.
    """
    if not edits:
        return
    for it in items:
        rec = edits.get(getattr(it, "item_id", None) or "")
        if not rec:
            continue
        for field in ("brand", "model", "size"):
            value = rec.get(field)
            if value is not None and str(value).strip():
                setattr(it, field, str(value).strip())


def resolve_all(items, exporter_name: str | None) -> None:
    """Set ``brand`` / ``model`` / ``size`` on every WorkItem in place.

    Runs after item mutations + COO so an edited description is reflected in the
    parsed SIZE.  Never touches ``description_raw`` or any XML value.  Reviewer
    overrides pre-set by :func:`apply_edits` are honoured — this only fills
    values that are still ``None``.
    """
    for it in items:
        if getattr(it, "brand", None) is None:
            it.brand = resolve_brand(getattr(it, "brand_raw", None), exporter_name)
        if getattr(it, "model", None) is None:
            it.model = resolve_model(getattr(it, "model_raw", None),
                                     getattr(it, "description_raw", None))
        if getattr(it, "size", None) is None:
            it.size = resolve_size(getattr(it, "size_raw", None),
                                   getattr(it, "description_raw", None))
