"""Cross-row description attribution — put each printed name on its own row.

OCR reads a goods table one cell at a time.  When a vendor's description column
wraps across row and page boundaries, the text it recovers stops lining up with
the rows that own it: a cell opens with the PREVIOUS row's batch breakdown, or
closes with the NEXT row's name, or holds nothing but a continuation while its
own name sits in a neighbour.  Live page 16 of Medtronic invoice 4050033058
does all three at once, and the row below it — whose name is stranded up there
— ships with its bare part code as its customs description.

This is a document-level problem: a single cell cannot be judged on its own,
because the evidence that a fragment belongs elsewhere is what the OTHER rows
print.  So the repair runs here, as a gate over the whole extracted row list,
once per document, and not in ``rules.field_allocation`` — which sees one row
at a time, is re-run on every reviewer click, and is contractually pure.

WHAT THIS PASS MAY DO: move a run of text from one row's description to
another row's, and drop a run that provably belongs to a row that already has
its name.  WHAT IT MAY NEVER DO: write a character that the OCR did not print
on that page.  Every emitted string is an exact slice of the segmenter's own
output, a page-level reconstruction invariant is asserted before returning, and
any page that fails it is rolled back whole.

Attribution rests on two independent proofs drawn from different columns of the
same printed table:

* the MODEL echo — a goods name repeats its own part code
  (``CATHETER LA6EBU35 …`` carries ``LA6EBU35``), so a name naming a DIFFERENT
  row's model belongs to that row; and
* QUANTITY conservation — the ``<n> EA`` echoes inside a row's own annotation
  runs sum to that row's printed quantity, so a misplaced run announces itself
  arithmetically.

A move is only made when the first proof is unique and the target row has no
name of its own.  Anything else is left byte-identical and reported, or handed
to a narrow LLM call that is only ever allowed to choose among segments this
module already cut (see ``resolve``).
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal

from ..domain.enums import DeclaredRole
from .description_segments import (
    Segment, is_code_only, model_matches, part_codes, qty_echoes, segment_description)
from .manifest import normalize_token

log = logging.getLogger("easycustoms.extraction")

LEAD_NOTE = "DESCRIPTION_LEAD_RUN_REATTRIBUTED"
MOVED_NOTE = "DESCRIPTION_SEGMENT_MOVED"
UNRESOLVED_NOTE = "DESCRIPTION_SEGMENT_UNRESOLVED"
MISSING_NOTE = "DESCRIPTION_ROW_NAME_MISSING"


def _page(row) -> int:
    return getattr(row, "source_page_no", None) or 0


def _decimal(raw) -> Decimal | None:
    try:
        return Decimal(str(raw).replace(",", "").strip())
    except Exception:
        return None


class _Cell:
    """One row's description cell, segmented once."""

    __slots__ = ("row", "index", "segments", "text", "prose", "ann", "original")

    def __init__(self, row, index: int, normalize_country):
        self.row = row
        self.index = index
        self.original = getattr(row, "description_raw", None) or ""
        self.segments, self.text = segment_description(self.original, normalize_country)
        self.prose = [s for s in self.segments if s.kind == "PROSE"]
        self.ann = [s for s in self.segments if s.kind == "ANN"]

    @property
    def lead_run(self) -> Segment | None:
        """The annotation run the cell OPENS with — never this row's own."""
        return self.segments[0] if self.segments and self.segments[0].kind == "ANN" else None

    @property
    def own_runs(self) -> list[Segment]:
        """Annotation runs that follow a prose run, i.e. this row's own."""
        seen_prose = False
        out = []
        for s in self.segments:
            if s.kind == "PROSE":
                seen_prose = True
            elif seen_prose:
                out.append(s)
        return out

    def has_name(self) -> bool:
        return any(not is_code_only(s.text) for s in self.prose)


def _own_prose(cell: _Cell) -> Segment | None:
    """The prose run that names THIS row, proven by its model code."""
    model = getattr(cell.row, "model_raw", None)
    if model:
        for s in cell.prose:
            if any(model_matches(c, model) for c in part_codes(s.text)):
                return s
    return cell.prose[0] if len(cell.prose) == 1 else None


def _qty_confirms(cell: _Cell, extra: list[Segment] = ()) -> bool:
    """Do the quantity echoes attributed to this row sum to its printed qty?"""
    printed = _decimal(getattr(cell.row, "quantity_raw", None))
    if printed is None or printed <= 0:
        return False
    total = Decimal("0")
    seen = False
    for s in list(cell.own_runs) + list(extra):
        for q in qty_echoes(s.text):
            total += q
            seen = True
    return seen and total == printed


def attribute_row_descriptions(role, pages, payload, warnings, resolve=None):
    """Extraction gate: repair cross-row description attribution.

    Follows the gate convention of ``neutralize_invented_fragment_values`` —
    four positional arguments, role-guarded, returns ``(payload, warnings)``
    unchanged when it has nothing to say, and appends its notes to BOTH the
    payload and the returned list.  The whole body is defensive: an unexpected
    failure here must never cost a document whose OCR has already been paid
    for, so anything other than a deadline abort returns the input untouched.
    """
    if role != DeclaredRole.INVOICE:
        return payload, warnings
    # One row is enough: a cross-row MOVE needs a neighbour, but a name
    # stranded on a value-less line, and the honest report that a row has no
    # name at all, both apply to a single-row invoice.
    rows = list(getattr(payload, "rows", None) or [])
    if not rows:
        return payload, warnings
    try:
        return _attribute(rows, pages, payload, warnings, resolve)
    except Exception as e:                       # never fail a paid-for extraction
        if type(e).__name__ == "ExtractionDeadlineExceeded":
            raise
        log.warning("description attribution skipped (%s: %s)", type(e).__name__, e)
        return payload, warnings


def _normalize_country():
    try:
        from ..reference.store import get_reference

        return get_reference().normalize_country
    except Exception as e:                       # degrade: fewer runs close, no wrong move
        log.warning("description attribution: reference store unavailable (%s)", e)
        return None


def _fingerprint(cells: list[_Cell]) -> str:
    return normalize_token("".join(c.original for c in cells))


def _fragment_prose(pages, normalize_country) -> dict[int, list[Segment]]:
    """Goods names printed on table lines that produced NO row.

    Some vendors wrap the description column onto its own line, with every
    value column empty::

        |  RONYX40034X 00763000248932 | <previous row's batch runs> | 6 | EA | 320.00 | 1.920.00 |
        |   |  STENT RONYX40034X ONYX 4.00X34RX Batch: 0013039750 6 EA COO: Ireland |  |  |  |  |

    The parser rightly refuses to make a row out of the second line — it has no
    value to declare, and a fragment that gains invented values is exactly how
    a phantom item once reached a declaration.  But the NAME on it is real and
    printed, and the row above it is left holding only somebody else's batch
    numbers.  This harvests those names so they can be matched back by model
    code; it never creates a row.
    """
    from .table_parser import _cells, _is_separator

    out: dict[int, list[Segment]] = {}
    for page in pages or []:
        text = getattr(page, "plain_text", None) or ""
        page_no = getattr(page, "page_no", None)
        if not text or page_no is None:
            continue
        for line in text.splitlines():
            if line.count("|") < 4:
                continue
            cells = _cells(line)
            if _is_separator(cells) or not any(cells):
                continue
            # value-less: no cell is a bare number, so no value column was read
            if any(re.fullmatch(r"[\d][\d.,]*", (c or "").strip()) for c in cells):
                continue
            blob = " ".join(c for c in cells if c)
            if not re.search(r"[A-Za-z]{4,}", blob):
                continue
            segs, _ = segment_description(blob, normalize_country)
            prose = [s for s in segs if s.kind == "PROSE" and not is_code_only(s.text)]
            if not prose:
                continue
            # The quantity echoes live in the line's ANNOTATION runs, not in
            # its prose, so they are carried alongside: they are the second,
            # arithmetically independent proof that this name belongs to the
            # row that claims it.
            echoes = [q for s in segs if s.kind == "ANN" for q in qty_echoes(s.text)]
            total = sum(echoes, Decimal("0")) if echoes else None
            out.setdefault(page_no, []).extend((s, total) for s in prose)
    return out


def _harvest_name(cell: _Cell, fragments: dict[int, list[Segment]],
                  used: set[int]) -> tuple[str, str] | None:
    """A value-less line's name that provably belongs to this nameless row.

    Requires a UNIQUE model-code echo on this page or its neighbours, and the
    fragment must not already have been claimed by another row.  Quantity
    conservation corroborates it when the line prints echoes.
    """
    model = getattr(cell.row, "model_raw", None)
    if not model:
        return None
    page = _page(cell.row)
    hits = []
    for pno in (page, page - 1, page + 1):
        for seg, echoes in fragments.get(pno, ()):
            if id(seg) in used:
                continue
            if any(model_matches(c, model) for c in part_codes(seg.text)):
                hits.append((seg, echoes))
    if len(hits) != 1:
        return None
    seg, echoes = hits[0]
    used.add(id(seg))
    printed = _decimal(getattr(cell.row, "quantity_raw", None))
    proof = ("model echo" if echoes is None or printed is None or echoes != printed
             else f"model echo, QTY_CONFIRMED {echoes} == printed {printed}")
    return seg.text, proof


def _attribute(rows, pages, payload, warnings, resolve):
    normalize_country = _normalize_country()
    cells = [_Cell(r, i, normalize_country) for i, r in enumerate(rows)]
    fragments = _fragment_prose(pages, normalize_country)
    used_fragments: set[int] = set()
    notes: list[str] = []
    by_page: dict[int, list[_Cell]] = {}
    for c in cells:
        by_page.setdefault(_page(c.row), []).append(c)

    # ---- S2 guard ladder: the fast no-op path for ~99 % of all rows -------- #
    def movable(cell: _Cell) -> bool:
        if len(cell.prose) <= 1:
            return False                          # G1
        model = getattr(cell.row, "model_raw", None)
        for s in cell.prose:                      # G2
            codes = part_codes(s.text)
            if codes and not any(model_matches(c, model) for c in codes):
                return True
        return False

    # The invariant's universe is everything the DOCUMENT printed that this pass
    # is allowed to draw from: the row cells, plus the value-less lines the
    # harvest reads.  Both are OCR output; neither is generated here.  (Leaving
    # the fragment lines out made the invariant reject its own legitimate
    # harvest — correctly, since the text genuinely was not in any cell.)
    before = normalize_token(
        "".join(c.original for c in cells)
        + "".join(s.text for segs in fragments.values() for s, _ in segs))
    planned: dict[int, str] = {}                  # row index -> new description
    donated: dict[int, tuple[str, str]] = {}      # row index -> (text, from-model)
    unresolved: list[_Cell] = []

    for cell in cells:
        lead = cell.lead_run
        # ---- M1: the cell OPENS with the previous row's continuation ------ #
        if lead is not None and cell.prose:
            prev = cells[cell.index - 1] if cell.index else None
            confirmed = prev is not None and _qty_confirms(prev, [lead])
            planned[cell.index] = cell.text[lead.end:].strip()
            notes.append(
                f"{LEAD_NOTE}: p{_page(cell.row)} row {cell.index + 1} opened with "
                f"{lead.text[:70]!r}, which is the previous row's batch/origin "
                f"continuation; it was removed from the declared description"
                + (" (QTY_CONFIRMED against that row's printed quantity)."
                   if confirmed else " (quantity could not corroborate it).")
                + " Nothing was rewritten.")
        # ---- M5: the row has no name of its own ---------------------------- #
        # Either the whole cell is somebody else's continuation, or it holds
        # nothing but the part code.  The name may still be printed on this
        # page, on a value-less line the parser could not turn into a row.
        if not cell.has_name() and cell.index not in donated:
            found = _harvest_name(cell, fragments, used_fragments)
            if found is not None:
                text, proof = found
                if getattr(cell.row, "description_printed_raw", None) is None:
                    donated[cell.index] = (text, "")
                notes.append(
                    f"{MOVED_NOTE}: p{_page(cell.row)} row {cell.index + 1} received "
                    f"{text[:70]!r} — printed on this page on a line carrying no values of "
                    f"its own, and attributed to this row by its MODEL code ({proof}). "
                    f"Nothing was rewritten; verify the description.")
            else:
                notes.append(
                    f"{MISSING_NOTE}: p{_page(cell.row)} row {cell.index + 1}'s description "
                    f"cell carries no goods name — only a continuation or a bare code. It "
                    f"was NOT reconstructed; enter it.")
            continue

        if not movable(cell):
            continue

        # ---- M3: a prose run that names ANOTHER row --------------------- #
        own = _own_prose(cell)
        keep = [s for s in cell.prose if s is own] if own else []
        strays = [s for s in cell.prose if s is not own]
        resolved_all = True
        for stray in strays:
            codes = part_codes(stray.text)
            cands = [o for o in cells
                     if o.index != cell.index
                     and abs(_page(o.row) - _page(cell.row)) <= 1
                     and any(model_matches(c, getattr(o.row, "model_raw", None)) for c in codes)]
            if len(cands) != 1:
                resolved_all = False
                continue
            target = cands[0]
            if target.has_name():
                resolved_all = False              # never overwrite printed text
                continue
            after = stray is cell.prose[-1]
            if (after and target.index < cell.index) or (not after and target.index > cell.index):
                resolved_all = False              # order-preserving only
                continue
            donated[target.index] = (stray.text, str(getattr(cell.row, "model_raw", "") or ""))
            notes.append(
                f"{MOVED_NOTE}: p{_page(target.row)} row {target.index + 1} received "
                f"{stray.text[:70]!r} — text printed inside p{_page(cell.row)} row "
                f"{cell.index + 1}'s description cell and attributed to this row by its "
                f"MODEL code. Nothing was rewritten; verify both descriptions.")
        if own is not None and (keep or strays):
            planned[cell.index] = own.text
        if not resolved_all:
            unresolved.append(cell)

    # ---- S5: escalate what could not be proven ---------------------------- #
    if unresolved and resolve is not None:
        try:
            planned, donated, extra = _apply_resolved(
                cells, unresolved, planned, donated, resolve)
            notes.extend(extra)
            unresolved = []
        except Exception as e:
            if type(e).__name__ == "ExtractionDeadlineExceeded":
                raise
            log.warning("description attribution: escalation failed (%s)", e)
    for cell in unresolved:
        notes.append(
            f"{UNRESOLVED_NOTE}: p{_page(cell.row)} row {cell.index + 1} "
            f"({cell.original[:80]!r}) prints more than one product name but ownership "
            f"could not be proven — the cell was left exactly as printed. "
            f"Verify and split it manually.")

    if not planned and not donated:
        if not notes:
            return payload, warnings
        payload.warnings = list(getattr(payload, "warnings", None) or []) + notes
        return payload, list(warnings) + notes

    # ---- apply, then S6: reconstruction invariant, per page --------------- #
    originals = {c.index: c.original for c in cells}
    for idx, text in planned.items():
        if text:
            rows[idx].description_raw = text
    for idx, (text, from_model) in donated.items():
        if not text:
            continue
        if getattr(rows[idx], "description_printed_raw", None) is None:
            rows[idx].description_printed_raw = originals[idx]
        rows[idx].description_raw = text

    kept = normalize_token("".join(
        (getattr(r, "description_raw", None) or "") for r in rows))
    if not _is_submultiset(kept, before):
        for idx, original in originals.items():
            rows[idx].description_raw = original
            if hasattr(rows[idx], "description_printed_raw"):
                rows[idx].description_printed_raw = None
        log.warning("description attribution: reconstruction invariant failed — rolled back")
        notes = [f"{UNRESOLVED_NOTE}: description attribution was rolled back — the repaired "
                 f"text did not reconstruct from the printed cells. Nothing was changed."]

    payload.warnings = list(getattr(payload, "warnings", None) or []) + notes
    return payload, list(warnings) + notes


def _is_submultiset(kept: str, before: str) -> bool:
    """Every character kept was printed, with no character multiplied.

    The pass is a partition of already-printed text, so its output can only
    ever be a sub-multiset of its input.  Asserting that at runtime turns
    "this cannot invent text" from a design claim into a checked fact.
    """
    from collections import Counter

    a, b = Counter(kept), Counter(before)
    return all(b[ch] >= n for ch, n in a.items())


def _apply_resolved(cells, unresolved, planned, donated, resolve):
    """Ask the model to choose among segments THIS module cut, then validate.

    The response carries segment INDICES and MODEL codes only — never text —
    so the applied string is always the segmenter's own slice regardless of
    what comes back.  A cell whose answer fails any check is discarded whole.
    """
    payloads = []
    for cell in unresolved:
        payloads.append({
            "cell_id": f"p{_page(cell.row)}r{cell.index + 1}",
            "cell_text": cell.text,
            "segments": [{"index": i, "text": s.text}
                         for i, s in enumerate(cell.prose)],
            "this_row_model": str(getattr(cell.row, "model_raw", "") or ""),
            "candidates": [
                {"model": str(getattr(o.row, "model_raw", "") or ""),
                 "row": o.index + 1,
                 "quantity": str(getattr(o.row, "quantity_raw", "") or "")}
                for o in cells
                if o.index != cell.index and abs(_page(o.row) - _page(cell.row)) <= 1
                and not o.has_name()],
        })
    answer = resolve(payloads) or {}
    notes: list[str] = []
    asked = {p["cell_id"]: c for p, c in zip(payloads, unresolved)}
    for entry in (answer.get("cells") or []):
        cell = asked.get(entry.get("cell_id"))                       # V1
        if cell is None:
            continue
        assigns = entry.get("assignments") or []
        idxs = [a.get("segment_index") for a in assigns]
        if sorted(i for i in idxs if isinstance(i, int)) != list(range(len(cell.prose))):
            continue                                                 # V2
        this = [a for a in assigns if a.get("owner") == "THIS"]
        if len(this) != 1:
            continue                                                 # V4
        own = cell.prose[this[0]["segment_index"]]
        model = getattr(cell.row, "model_raw", None)
        codes = part_codes(own.text)
        if model and codes and not any(model_matches(c, model) for c in codes):
            continue                                                 # V4 (no contradiction)
        moves = []
        ok = True
        for a in assigns:
            if a.get("owner") == "THIS":
                continue
            target = next((o for o in cells
                           if model_matches(normalize_token(str(a.get("owner") or "")),
                                            getattr(o.row, "model_raw", None))
                           or normalize_token(str(a.get("owner") or ""))
                           == normalize_token(getattr(o.row, "model_raw", "") or "")), None)
            if target is None or target.has_name():                  # V3 / V5
                ok = False
                break
            seg = cell.prose[a["segment_index"]]
            after = seg is cell.prose[-1]
            if (after and target.index < cell.index) or (not after and target.index > cell.index):
                ok = False                                           # V6
                break
            moves.append((target, seg))
        if not ok:
            continue
        planned[cell.index] = own.text
        for target, seg in moves:
            donated[target.index] = (seg.text, str(getattr(cell.row, "model_raw", "") or ""))
            notes.append(
                f"{MOVED_NOTE}: p{_page(target.row)} row {target.index + 1} received "
                f"{seg.text[:70]!r} from p{_page(cell.row)} row {cell.index + 1} — segment "
                f"ownership was resolved by a model call that chose among the printed "
                f"segments only; no text was generated. Verify both descriptions.")
    return planned, donated, notes
