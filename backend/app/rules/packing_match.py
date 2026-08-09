"""Packing-list matching (allocation spec 2026-07-17).

Packing rows are matched to invoice items by *normalized product identity*,
never by row number, and packing order never reorders XML items.

Spec rules implemented here:
* normalization trims/lowers/collapses spaces, drops punctuation and
  separators, and keeps meaningful identifiers (size, model, ml, kg …) inline;
* repeated packing rows of the same normalized item are grouped and SUMMED
  before any assignment;
* rows sharing a carton group contribute the shared carton total ONCE — it is
  divided among the group's items (by row quantity, else equally) without
  changing the group total;
* when several invoice lines share an identity, the grouped packing values are
  split proportionally by invoice quantity, else by invoice line value, else
  equally.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from ..domain.errors import ValidationMessage
from ..numbers import count_from_number_range, parse_decimal
from ..units import to_kg
from .models import WorkItem

_ZERO = Decimal("0")


def _norm(desc: str) -> str:
    """lower, collapse whitespace/punctuation/separators; identifiers (500ml,
    model codes, pack sizes) survive inline so variants never merge."""
    return re.sub(r"[^a-z0-9]+", "", (desc or "").lower())


# ---- scored fallback matching --------------------------------------------- #
# Exact normalized equality is the right FIRST rule and the wrong ONLY rule: the
# two documents are typed by different people from the same goods, so "Shampoo
# 500ml" against "500ML SHAMPOO" cost the whole match — and a missed match is
# not visible as an error, it silently becomes an estimated weight.
_WORD = re.compile(r"[a-z]{3,}")
_NUM_UNIT = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-z]{0,4})")
# words that carry no product identity and would inflate every similarity score
_STOP = frozenset({
    "the", "and", "for", "with", "pcs", "pcx", "set", "sets", "new", "type", "size", "model",
    "item", "items", "product", "products", "unit", "units", "each", "box", "boxes", "carton",
    "cartons", "ctn", "ctns", "pack", "packs", "packing", "packed", "assy", "assembly",
})
_MIN_SCORE = 0.60          # below this, two descriptions are simply different
_MIN_MARGIN = 0.10         # a winner this close to the runner-up is ambiguous


def _tokens(desc: str) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """Identity of a description: its meaningful WORDS and its MEASUREMENTS.

    Measurements are kept keyed by unit so that 500ml vs 250ml is a conflict
    while 500ml vs 500ml plus an unrelated bare number is not.
    """
    low = re.sub(r"[^a-z0-9.,]+", " ", (desc or "").lower())
    words = frozenset(w for w in _WORD.findall(low) if w not in _STOP)
    measures: dict[str, set[str]] = {}
    for value, unit in _NUM_UNIT.findall(low):
        measures.setdefault(unit, set()).add(value.replace(",", ".").rstrip(".").lstrip("0") or "0")
    return words, {u: frozenset(v) for u, v in measures.items()}


def _measures_conflict(a: dict[str, frozenset[str]], b: dict[str, frozenset[str]]) -> bool:
    """True when both sides state the SAME kind of measurement with different
    values — 500 ml against 250 ml, 2.25x15 against 2.25x22.  This is the gate
    that keeps a fuzzy match from ever merging two sizes of one product, which
    the spec forbids outright."""
    for unit in set(a) & set(b):
        if not (a[unit] & b[unit]):
            return True
    return False


def _similarity(a: tuple, b: tuple) -> float:
    """Jaccard overlap of the meaningful words, 0.0 when the measurements
    disagree.  Deliberately crude: it decides only whether to PROPOSE a match,
    and every proposal below 1.0 is reported to the reviewer."""
    aw, am = a
    bw, bm = b
    if _measures_conflict(am, bm) or not aw or not bw:
        return 0.0
    return len(aw & bw) / len(aw | bw)


# A measurement is not an identifier.  "500ML", "3000W", "80GSM", "100AH" all
# pass the letters-and-digits test, and a pack size printed in BOTH documents'
# text silently paired a dishwash line with a floor-cleaner line at 0.95.
_MEASUREMENT_CODE = re.compile(r"^\d+[a-z]{1,4}$")           # digits, then a unit tail
_DIMENSION_CODE = re.compile(r"^\d+(?:x\d+)+[a-z]{0,4}$")    # 1200x600, 24x500ml
# A shared-carton group id that IS a carton range and nothing else — an
# optional C/NO-style prefix, then digits-dash-digits.  Anything with letters
# or extra number groups (a PO, a lot number, a date) is a MARK, not a range.
_PURE_RANGE = re.compile(
    r"^(?:c(?:tn|ase|arton)?\s*[./-]?\s*(?:no|nos|number|#)?\s*[.:]?\s*)?"
    r"\d+\s*(?:-|–|—|to|thru|through)\s*\d+$", re.I)


def _codes(*values: str | None) -> frozenset[str]:
    """Normalized product/part codes worth matching on — long enough to be an
    identifier, not a bare number (an order or line number), and not a
    measurement (a pack size, wattage or dimension is a property, not a name)."""
    out = set()
    for v in values:
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9./-]{3,}", (v or "")):
            norm = re.sub(r"[^a-z0-9]", "", tok.lower())
            if (len(norm) >= 5 and re.search(r"[a-z]", norm) and re.search(r"\d", norm)
                    and not _MEASUREMENT_CODE.match(norm) and not _DIMENSION_CODE.match(norm)):
                out.add(norm)
    return frozenset(out)


@dataclass
class PackingEvidence:
    gross_weight: Decimal | None = None
    net_weight: Decimal | None = None
    carton_count: Decimal | None = None
    matched: bool = False
    matched_name: str | None = None          # packing-side name (audit trail)
    carton_shared: bool = False              # any part came from a shared carton group
    # How the row was matched, and how sure the matcher is.  1.0 is exact
    # normalized equality; anything less is a proposal the reviewer must see.
    match_confidence: Decimal | None = None
    match_method: str | None = None


def _row_kg(raw, locale: str | None) -> tuple[Decimal | None, bool]:
    """A packing row's RawNumber weight in kilograms, honouring its printed unit.

    ``(None, False)`` means the unit was printed but is not a mass unit — the
    weight is discarded rather than assumed to be kilograms.
    """
    if raw is None:
        return None, True
    return to_kg(parse_decimal(raw.value_raw, locale=locale), raw.unit_raw)


def _classify_declared(payload, locales: dict) -> tuple[str, str | None]:
    """What an UNLABELLED per-row weight column is: 'GROSS', 'NET' or 'SHAPE'.

    Real packing lists routinely print one column headed just "Weight (KG)".
    The extractor stores it as ``declared_weight`` with ``weight_type_raw``
    UNKNOWN — and per-item weights that reach nobody are the exact failure this
    module exists to prevent: a 115-row packing list stating every weight was
    allocated by invoice VALUE share because the allocator only read the
    labelled fields.

    The document itself usually settles the question.  A row-stated type wins
    outright; otherwise the column's SUM is compared against the document's own
    printed totals — a column summing to the printed total gross IS the gross
    breakdown.  When nothing settles it, the column is still the best available
    weight SHAPE (Condition 1 rescales to the authority anyway); only the label
    is uncertain, and the caller says so.

    Returns ``(kind, note)`` — note is None when the document stated the type.
    """
    stated = {(getattr(r, "weight_type_raw", None) or "").upper()
              for r in payload.rows if getattr(r, "declared_weight", None)}
    stated.discard("")
    stated.discard("UNKNOWN")
    if stated == {"GROSS"}:
        return "GROSS", None
    if stated == {"NET"}:
        return "NET", None

    total = _ZERO
    seen = 0
    for r in payload.rows:
        if r.gross_weight is not None or r.net_weight is not None:
            continue
        kg, ok = _row_kg(getattr(r, "declared_weight", None),
                         (getattr(payload, "page_numeric_locales", None) or {}).get(r.source_page_no))
        if ok and kg is not None:
            total += kg
            seen += 1
    if not seen:
        return "SHAPE", None

    def _total_kg(raw):
        kg, ok = _row_kg(raw, None)
        return kg if ok else None

    printed_gross = _total_kg(payload.total_gross_weight)
    printed_net = _total_kg(payload.total_net_weight)

    def _close(a, b):
        return (a is not None and b is not None and b > 0
                and abs(a - b) <= max(b * Decimal("0.02"), Decimal("0.05")))

    # Net checked FIRST: net < gross always, so a column matching both totals
    # is impossible, and a column matching neither must not steal the stronger
    # claim.  Matching the NET total also implies the gross total disagrees,
    # which is exactly the evidence needed to call it net.
    if _close(total, printed_net):
        return "NET", (f"the packing list prints one unlabelled weight column; its sum "
                       f"({total} kg) equals the printed total NET weight, so it is used as "
                       f"the item-wise net weight")
    if _close(total, printed_gross):
        return "GROSS", (f"the packing list prints one unlabelled weight column; its sum "
                         f"({total} kg) equals the printed total GROSS weight ({printed_gross} "
                         f"kg), so it is used as the item-wise gross weight")
    return "SHAPE", (f"the packing list prints one unlabelled weight column whose sum "
                     f"({total} kg) matches neither printed total — it is used as the weight "
                     f"DISTRIBUTION SHAPE only, rescaled to the authorised gross; verify the "
                     f"item weights")


def match_packing(items: list[WorkItem], packing_payloads: list,
                  warnings_out: list | None = None) -> dict[int, PackingEvidence]:
    # ---- pass 1: flatten packing rows -------------------------------------
    # (key, display_name, qty, gross, net, carton, shared_group, codes, payload_idx)
    rows = []
    unknown_units: list[str] = []            # rows whose weight unit was unusable
    declared_notes: list[str] = []
    for pidx, payload in enumerate(packing_payloads):
        locales = getattr(payload, "page_numeric_locales", None) or {}
        declared_kind, declared_note = _classify_declared(payload, locales)
        if declared_note:
            declared_notes.append(declared_note)
        for row in payload.rows:
            key = _norm(row.description_raw)
            if not key:
                continue
            loc = locales.get(row.source_page_no)
            qty = parse_decimal(row.quantity_raw, locale=loc) if row.quantity_raw else None
            # Packing weights -> kilograms HERE (spec 2026-07-21).  A printed but
            # unrecognized unit disqualifies that weight (dropped + counted)
            # rather than being read as kilograms.
            gross, gross_ok = _row_kg(row.gross_weight, loc)
            net, net_ok = _row_kg(row.net_weight, loc)
            declared, decl_ok = _row_kg(getattr(row, "declared_weight", None), loc)
            if gross is None and net is None and declared is not None:
                # THIS row's own stated type wins; the payload-level
                # classification only fills in where the row says nothing.  A
                # document may label a handful of rows and leave the rest bare.
                row_type = (getattr(row, "weight_type_raw", None) or "").upper()
                kind = row_type if row_type in ("GROSS", "NET") else declared_kind
                if kind == "NET":
                    net = declared
                else:                        # GROSS, or SHAPE (rescaled anyway)
                    gross = declared
            if not gross_ok or not net_ok or not decl_ok:
                unknown_units.append(row.description_raw.strip()
                                     or f"page {row.source_page_no} row {row.source_row_index}")
            carton = parse_decimal(row.carton_count.value_raw, locale=loc) if row.carton_count else None
            shared = (row.shared_carton_group_raw or "").strip() or None
            # getattr, not attribute access: these fields are newer than some
            # stored extractions and than the row stand-ins used in tests.
            codes = _codes(getattr(row, "item_code_raw", None), getattr(row, "model_raw", None))
            rows.append((key, row.description_raw.strip(), qty, gross, net, carton, shared,
                         codes, pidx))

    # ---- pass 2: shared carton groups — count the group total ONCE --------
    # rows usually repeat the group's total on every member line (same value
    # per row -> use it once); rows may instead print already-split fractions
    # (differing values -> their sum IS the group total)
    # Keyed by (payload, group id): carton numbering restarts in each uploaded
    # packing-list document, so two suppliers' groups both labelled "1-5" are
    # two physically distinct sets of cartons — merging them counted five
    # cartons where the consignment held ten.
    shared_members: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        if r[6] and r[5] is not None:
            shared_members[(r[8], r[6])].append(i)
    shared_alloc: dict[int, Decimal] = {}    # row index -> divided carton value
    for (_pidx, grp), idxs in shared_members.items():
        vals = [rows[i][5] for i in idxs]
        # Rows printing DIFFERENT values already carry their own split — their
        # sum IS the group total, and nothing may override a printed figure.
        # Equal values are AMBIGUOUS: every row repeating the group total, or
        # every row stating its own single carton, and nothing in the numbers
        # can tell "2, 2, 2 = 2 cartons" from "1, 1, 1 = 3 cartons".  A group
        # id that IS a carton range settles it: "cartons 1-5" is five cartons
        # however many rows are printed against it.  The id must be essentially
        # nothing but the range — "PO-1001-2024" is a purchase order whose
        # hyphens span 1024, and reading marks, lot numbers or dates as ranges
        # replaced printed carton counts with numerology.
        ranged = count_from_number_range(grp) if _PURE_RANGE.match(grp) else None
        if len(set(vals)) > 1:
            group_total = sum(vals, _ZERO)
        elif ranged and ranged > 1:
            group_total = Decimal(ranged)
        else:
            group_total = vals[0]
        qtys = [rows[i][2] or _ZERO for i in idxs]
        total_q = sum(qtys, _ZERO)
        # The LAST member takes the remainder, so the divided shares add back up
        # to the group total EXACTLY.  A three-way split of 5 cartons is
        # 1.666… per row, and three of those is 4.999…998 — a shared carton
        # count that quietly loses part of itself is the one thing this whole
        # branch exists to prevent.
        running = _ZERO
        for n, (i, q) in enumerate(zip(idxs, qtys)):
            if n == len(idxs) - 1:
                shared_alloc[i] = group_total - running
                break
            frac = (q / total_q) if total_q > 0 else (Decimal("1") / len(idxs))
            shared_alloc[i] = group_total * frac
            running += shared_alloc[i]

    # ---- pass 3: group by normalized item name, summing values ------------
    grouped: dict[str, dict] = {}
    for i, (key, name, qty, gross, net, carton, shared, codes, _pidx) in enumerate(rows):
        acc = grouped.setdefault(key, {"name": name, "gross": _ZERO, "net": _ZERO,
                                       "carton": _ZERO, "shared": False,
                                       "codes": set(), "tokens": _tokens(name)})
        acc["codes"] |= codes
        if gross:
            acc["gross"] += gross
        if net:
            acc["net"] += net
        if shared and carton is not None:
            acc["carton"] += shared_alloc.get(i, _ZERO)
            acc["shared"] = True
        elif carton:
            acc["carton"] += carton

    # ---- pass 4: assign each packing group to invoice lines ---------------
    # Ladder, strongest first.  Every rung below exact equality records its
    # confidence and is reported to the reviewer — a proposed match is never
    # silently treated as a document fact.
    by_key: dict[str, list[WorkItem]] = defaultdict(list)
    for it in items:
        # evidence-to-evidence: a reviewer rename must not break the match
        # against the description the packing list actually printed
        by_key[_norm(it.evidence_description_raw or it.description_raw)].append(it)

    if unknown_units and warnings_out is not None:
        shown = ", ".join(unknown_units[:5]) + (" …" if len(unknown_units) > 5 else "")
        warnings_out.append(ValidationMessage.warning(
            "PACKING_WEIGHT_UNIT_UNKNOWN",
            f"{len(unknown_units)} packing-list row(s) print a weight in an unrecognized unit "
            f"and were ignored for weight allocation ({shown}). Confirm the item weights."))
    # An inferred column type is never resolved silently (audit rule).
    if declared_notes and warnings_out is not None:
        for note in dict.fromkeys(declared_notes):
            warnings_out.append(ValidationMessage.warning("PACKING_WEIGHT_TYPE_INFERRED", note))

    result: dict[int, PackingEvidence] = {}
    for it in items:
        result[it.xml_item_sequence] = PackingEvidence()

    assigned: dict[str, tuple[list[WorkItem], Decimal, str]] = {}   # group key -> siblings
    claimed: set[str] = set()                    # invoice keys already spoken for

    # 1. exact normalized equality — the same product name on both documents
    for key, group in grouped.items():
        siblings = by_key.get(key)
        if siblings:
            assigned[key] = (siblings, Decimal("1"), "exact description")
            claimed.add(key)

    # 2. product / part code equality.  A code is printed to be matched on and
    #    survives rewording, so it outranks any similarity score.
    free = {k: v for k, v in by_key.items() if k not in claimed}
    item_codes = {k: _codes(*(x for s in v for x in
                              (getattr(s, "model_raw", None), getattr(s, "model", None),
                               s.description_raw)))
                  for k, v in free.items()}
    code_pairs: list[str] = []
    for key, group in grouped.items():
        if key in assigned or not group["codes"]:
            continue
        hits = [k for k, codes in item_codes.items() if k not in claimed and (codes & group["codes"])]
        if len(hits) == 1:
            assigned[key] = (free[hits[0]], Decimal("0.95"), "product code")
            claimed.add(hits[0])
            first = free[hits[0]][0]
            code_pairs.append(f"{group['name']!r} -> SN {first.xml_item_sequence} "
                              f"{first.description_raw[:40]!r} "
                              f"(code {sorted(group['codes'] & item_codes[hits[0]])[0]})")
    # Rung 2 reassigns an item's whole weight on one token — it must report
    # itself exactly like rung 3 does.  It was the only rung with no message.
    if code_pairs and warnings_out is not None:
        warnings_out.append(ValidationMessage.warning(
            "PACKING_MATCH_BY_CODE",
            f"{len(code_pairs)} packing-list product(s) were matched to an invoice item by a "
            f"shared PRODUCT CODE rather than by name — check the pairings: "
            + "; ".join(code_pairs[:10]) + (" …" if len(code_pairs) > 10 else "") + "."))

    # 3. scored similarity, gated on measurements agreeing and on the winner
    #    being clearly ahead of the runner-up.  An ambiguous best match is no
    #    match: guessing between two candidates is worse than saying so.
    # Every remaining pair is scored ONCE and assigned BEST-FIRST across the
    # whole document.  Assigning in packing-row order instead would let a weak
    # early match claim the invoice item that a much stronger later match
    # describes — the pairing would then depend on the order the supplier
    # happened to type their packing list, which is exactly what "match by
    # identity, never by row" is supposed to rule out.
    proposals: list[str] = []
    ambiguous: list[str] = []
    scores: dict[tuple[str, str], float] = {}
    for key, group in grouped.items():
        if key in assigned:
            continue
        for k, siblings in by_key.items():
            score = _similarity(group["tokens"], _tokens(siblings[0].evidence_description_raw
                                                         or siblings[0].description_raw))
            if score >= _MIN_SCORE:
                scores[(key, k)] = score

    undecided: set[str] = set()          # groups already ruled ambiguous
    for (key, best_key), score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])):
        if key in assigned or key in undecided or best_key in claimed:
            continue
        rivals = [s for (g, k), s in scores.items()
                  if g == key and k != best_key and k not in claimed]
        if score - max(rivals, default=0.0) < _MIN_MARGIN:
            ambiguous.append(f"{grouped[key]['name']!r}")
            undecided.add(key)
            continue
        siblings = by_key[best_key]
        # Capped below the exact and code rungs: a similarity match can score a
        # perfect 1.0 (same words, reordered) and still be a PROPOSAL, and a
        # reviewer reading "1.00" in the audit would reasonably read it as
        # "this is certain".  Confidence here ranks proposals, it does not
        # promote them.
        assigned[key] = (siblings, Decimal(str(round(min(score, 0.9), 2))), "description similarity")
        claimed.add(best_key)
        proposals.append(f"{grouped[key]['name']!r} -> SN {siblings[0].xml_item_sequence} "
                         f"{siblings[0].description_raw[:40]!r} ({score:.0%})")

    if proposals and warnings_out is not None:
        warnings_out.append(ValidationMessage.warning(
            "PACKING_MATCH_LOW_CONFIDENCE",
            f"{len(proposals)} packing-list product(s) were matched to an invoice item by "
            f"DESCRIPTION SIMILARITY, not by an exact name or code match — the weights and "
            f"cartons below come from a proposed pairing, so check them: "
            + "; ".join(proposals[:10]) + (" …" if len(proposals) > 10 else "") + "."))
    if ambiguous and warnings_out is not None:
        shown = ", ".join(ambiguous[:5]) + (" …" if len(ambiguous) > 5 else "")
        warnings_out.append(ValidationMessage.warning(
            "PACKING_MATCH_AMBIGUOUS",
            f"{len(ambiguous)} packing-list product(s) resemble more than one invoice item "
            f"equally and were left unmatched rather than guessed ({shown}). Align the wording "
            f"on the two documents, or enter those item weights in Detailed Review."))

    # A packing row that matched nothing takes its weight and cartons out of
    # the allocation entirely — the reviewer has to be told how much of the
    # packing list went unused, and which products.
    dropped = [group["name"] for key, group in grouped.items() if key not in assigned]
    if dropped and warnings_out is not None:
        shown = ", ".join(repr(d) for d in dropped[:5]) + (" …" if len(dropped) > 5 else "")
        warnings_out.append(ValidationMessage.warning(
            "PACKING_ROWS_UNMATCHED",
            f"{len(dropped)} of {len(grouped)} packing-list product(s) do not match any invoice "
            f"item description and were not used for weight or carton allocation ({shown}). "
            f"Matching is by product name, so this is usually a wording difference between the "
            f"two documents — check the item descriptions."))

    # ---- pass 5: split each group's values across its invoice lines -------
    # proportional by invoice quantity -> by invoice line value -> equal
    for key, group in grouped.items():
        entry = assigned.get(key)
        if not entry:
            continue
        siblings, confidence, method = entry
        total_q = sum((s.quantity or _ZERO) for s in siblings)
        total_v = sum((s.line_total or _ZERO) for s in siblings)
        for s in siblings:
            if total_q > 0:
                frac = (s.quantity or _ZERO) / total_q
            elif total_v > 0:
                frac = (s.line_total or _ZERO) / total_v
            else:
                frac = Decimal("1") / len(siblings)
            ev = result[s.xml_item_sequence]
            ev.matched = True
            ev.matched_name = group["name"]
            ev.carton_shared = group["shared"]
            ev.match_confidence = confidence
            ev.match_method = method
            if group["gross"] > 0:
                ev.gross_weight = group["gross"] * frac
            if group["net"] > 0:
                ev.net_weight = group["net"] * frac
            if group["carton"] > 0:
                ev.carton_count = group["carton"] * frac
    return result
