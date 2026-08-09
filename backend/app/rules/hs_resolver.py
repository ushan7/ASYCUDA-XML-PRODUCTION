"""HS finalization — a four-priority cascade with an official-database gate.

Priority 1  Invoice HS  -> exact official 11-digit, else completed via official
                          8/6/4-digit prefixes, choosing within the band by
                          description match (falling back to the band's "Other").
Priority 2  Same-item history (stable normalized item name) — a previously
                          reviewer-CONFIRMED code for this exact item name.  It
                          outranks the semantic guess (user rule 2026-08-02)
                          because its source is a human decision, but stays LOW
                          confidence: history is a proposal, never blindly final.
Priority 3  Semantic description match against the official DB, when the
                          invoice printed no HS at all.  Always LOW confidence.
Priority 4  LLM HS8 hint (8-digit only; expanded through the DB).  Offline mode
                          has no LLM, so this degrades to manual review.

Every accepted HS must be an official 11-digit record.  The system never
invents digits, never nearest-numeric corrects, and never accepts an
LLM-proposed HS11.

**A resolution is not a confirmation.** Priority 2 nearly always finds
something, so an item is rarely left visibly unresolved — and the declaration
validator only checks that the final code is 11 digits.  A priority-2 guess is
therefore blocked at finalize (`HS_GUESS_UNCONFIRMED`, in ``pipeline.finalize``)
until a human confirms it through the review channel below.

Reviewed codes arrive through exactly two channels, and both require an EXACT
official 11-digit code — never a prefix:

* :func:`apply_hs_reviews` — item_id-keyed, from Detailed Review, applied
  during resolution;
* :func:`apply_manual_hs`  — sequence-keyed, posted with /finalize, applied
  after it.

They must not diverge: the second runs last and overwrites the first.
"""
from __future__ import annotations

import re

from ..domain.errors import ValidationMessage
from ..reference.store import HsRecord, ReferenceStore, digits_only, hs_text_tokens
from .models import WorkItem

# Reviewed-HS sources the server accepts as authoritative (review-save gate).
HS_REVIEW_SOURCES = {"detailed_review", "detailed_review_hs_search"}
_HS11_RE = re.compile(r"^\d{11}$")

# Deterministic confidence per resolution source: 1.0 only for an exact code
# (invoice-printed or reviewer-selected); any prefix completion / history /
# hint is a guess among official siblings -> AUTO_LOW_CONFIDENCE in review.
_SOURCE_CONFIDENCE = {
    "INVOICE_HS_EXACT": 1.0,
    "INVOICE_HS_COMPLETED_8": 0.8,
    "INVOICE_HS_COMPLETED_6": 0.6,
    "INVOICE_HS_COMPLETED_4": 0.5,
    # description didn't match the band -> broad "Other" catch-all (lower)
    "INVOICE_HS_COMPLETED_8_OTHER": 0.5,
    "INVOICE_HS_COMPLETED_6_OTHER": 0.4,
    "INVOICE_HS_COMPLETED_4_OTHER": 0.35,
    "HISTORY": 0.7,
    "LLM_HS8": 0.6,
    # no invoice HS hint at all — HS finalized by matching the item description
    # to the official DB description (user rule 2026-07-19). Always LOW so the
    # reviewer verifies / overrides via HS search.
    "SEMANTIC_DESCRIPTION": 0.3,
}

# Invoice-HS-hint semantic selection (user rule 2026-07-18): stay within the
# hinted band, pick the 11-digit code whose official description (+ AI
# explanation) is closest to the item description; if nothing clears the
# threshold, fall back to the band's broad "Other" catch-all.
_HS_SEMANTIC_THRESHOLD = 0.34
_OTHER_DESC_RE = re.compile(r"^others?\b")


def _normalized_item_name(desc: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (desc or "").lower())


def _meaningful_tokens(text: str) -> list[str]:
    # drop 1-char noise and bare numbers so scoring reflects real words
    return [t for t in hs_text_tokens(text) if len(t) >= 2 and not t.isdigit()]


def _tok_matches(q: str, toks: set[str]) -> bool:
    return any(q == b or (len(q) >= 4 and b.startswith(q)) or (len(b) >= 4 and q.startswith(b))
               for b in toks)


def _semantic_score(query: list[str], rec: HsRecord) -> float:
    """Coverage of the item-description tokens by the candidate.  A match in the
    OFFICIAL description counts full (1.0); a match only in the AI explanation
    counts half — so a distinctive description word (e.g. 'ICU') outweighs an
    explanation that merely mentions the term in passing."""
    if not query:
        return 0.0
    desc = set(_meaningful_tokens(rec.description))
    expl = set(_meaningful_tokens(rec.explanation))
    total = 0.0
    for q in query:
        if _tok_matches(q, desc):
            total += 1.0
        elif _tok_matches(q, expl):
            total += 0.5
    return total / len(query)


def _is_other(rec: HsRecord) -> bool:
    return bool(_OTHER_DESC_RE.match((rec.description or "").strip().lower()))


def _other_fallback(candidates: list[HsRecord]) -> HsRecord:
    # broadest "Other"-titled code in the band (conventionally the highest
    # subheading, e.g. 9018 -> 90189090900); else the residual last code
    others = [c for c in candidates if _is_other(c)]
    return max(others or candidates, key=lambda c: c.hs11)


def _pick_within_band(item_desc: str, candidates: list[HsRecord]) -> tuple[HsRecord, bool]:
    """Return (chosen, is_other_fallback) — the closest semantic match, or the
    band's broad Other code when nothing matches the item description."""
    query = _meaningful_tokens(item_desc)
    scored = [(_semantic_score(query, c), c) for c in candidates]
    best_score = max((s for s, _ in scored), default=0.0)
    if best_score >= _HS_SEMANTIC_THRESHOLD:
        # highest score; prefer a specific (non-Other) code; stable by hs11
        best = sorted(scored, key=lambda sc: (-sc[0], _is_other(sc[1]), sc[1].hs11))[0][1]
        return best, False
    return _other_fallback(candidates), True


def _complete_via_prefix(ref: ReferenceStore, digits: str, *, item_desc: str = "",
                         semantic: bool = False) -> tuple[HsRecord | None, str | None]:
    for n in (8, 6, 4):
        if len(digits) < n:
            continue
        prefix = digits[:n]
        candidates = ref.hs_candidates_for_prefix(prefix)
        if not candidates:
            continue
        if not semantic:
            # legacy path (manual override): first candidate, preferring an
            # exact digit extension — behaviour deliberately unchanged
            exact_ext = [c for c in candidates if c.hs11.startswith(digits)] if len(digits) > n else []
            return (exact_ext or candidates)[0], f"INVOICE_HS_COMPLETED_{n}"
        # invoice-hint path: a longer hint narrows to the exact sub-band first,
        # then description-based selection within it, with the Other fallback
        if len(digits) > n:
            ext = [c for c in candidates if c.hs11.startswith(digits)]
            if ext:
                candidates = ext
        chosen, is_other = _pick_within_band(item_desc, candidates)
        return chosen, f"INVOICE_HS_COMPLETED_{n}" + ("_OTHER" if is_other else "")
    return None, None


def resolve_hs_for_item(
    item: WorkItem,
    ref: ReferenceStore,
    history: dict[str, str] | None = None,
    llm_hint_fn=None,
) -> WorkItem:
    history = history or {}

    # -- Priority 1: invoice HS -------------------------------------------
    # 11-digit exact first, then 8/6/4-digit bands; within a band the code is
    # chosen by matching the item description to the DB description/explanation,
    # falling back to the broad "Other" code (user rule 2026-07-18).
    digits = digits_only(item.hs_code_raw)
    if digits:
        if len(digits) == 11:
            rec = ref.hs_exact(digits)
            if rec:
                return _accept(item, rec, "INVOICE_HS_EXACT")
        rec, src = _complete_via_prefix(ref, digits,
                                        item_desc=item.description_raw, semantic=True)
        if rec:
            return _accept(item, rec, src)

    # -- Priority 2: same-item history (user rule 2026-08-02) ---------------
    # A code a reviewer explicitly confirmed for this exact normalized item
    # name on earlier evidence of this job.  A prior human decision outranks
    # the semantic guess below — but it is still a PROPOSAL: 0.7 confidence
    # keeps the AUTO badge so the reviewer re-confirms it against the fresh
    # extraction rather than it being blindly re-applied as final.
    key = _normalized_item_name(item.description_raw)
    if key and key in history:
        rec = ref.hs_exact(history[key])
        if rec:
            item.warnings.append(ValidationMessage.warning(
                "HS_HISTORY_APPLIED",
                f"Item {item.xml_item_sequence}: HS {rec.hs11} proposed from the previously "
                f"confirmed selection for this item name — re-confirm it in Detailed Review.",
                scope="ITEM", item_sequence=item.xml_item_sequence, field="hs_code"))
            return _accept(item, rec, "HISTORY")

    # -- Priority 3: semantic description match (user rule 2026-07-19) ------
    # No HS on the invoice and no confirmed history -> finalize an official
    # 11-digit code by matching the item description to the DB description /
    # explanation. Always LOW confidence (AUTO badge) so the reviewer
    # verifies/overrides via HS search.
    match = ref.best_hs_by_description(item.description_raw)
    if match is not None:
        rec, _score = match
        item.warnings.append(ValidationMessage.warning(
            "HS_SEMANTIC_GUESS",
            f"Item {item.xml_item_sequence}: no HS on the invoice; auto-selected {rec.hs11} "
            f"({rec.description[:40]!r}) by description match — verify or correct it.",
            scope="ITEM", item_sequence=item.xml_item_sequence, field="hs_code"))
        return _accept(item, rec, "SEMANTIC_DESCRIPTION")

    # -- Priority 4: LLM HS8 hint (expanded through DB) --------------------
    if llm_hint_fn is not None:
        for hint8 in llm_hint_fn(item.description_raw) or []:
            d = digits_only(hint8)
            if len(d) != 8:
                continue
            candidates = ref.hs_candidates_for_prefix(d)
            if candidates:
                return _accept(item, candidates[0], "LLM_HS8")

    # -- Fallback: manual review (no description overlap at all) -----------
    item.warnings.append(ValidationMessage.blocking(
        "HS_MANUAL_REVIEW",
        f"No official HS11 found for item {item.xml_item_sequence} ({item.description_raw[:40]!r})",
        scope="ITEM", item_sequence=item.xml_item_sequence, field="hs_code",
    ))
    return item


def _accept(item: WorkItem, rec: HsRecord, source: str,
            confidence: float | None = None, explicit: bool = False) -> WorkItem:
    item.final_hs_code_11 = rec.hs11
    item.hs_official_description = rec.description
    item.hs_tariff_unit = rec.unit
    item.hs_source = source
    item.hs_confidence = (confidence if confidence is not None
                          else _SOURCE_CONFIDENCE.get(source, 0.6))
    item.hs_selection_explicit = explicit
    return item


def apply_manual_hs(items: list[WorkItem], overrides: dict, ref: ReferenceStore) -> list[ValidationMessage]:
    """User-supplied HS codes posted with /finalize, gated by the official DB.

    ``overrides``: {item_sequence: raw hs string}.  Requires an EXACT official
    11-digit code — the same bar as the item_id-keyed review channel
    (:func:`apply_hs_reviews`), and for the same reason.

    Two channels used to write the same field with different rules, and the
    weaker one ran last.  ``apply_hs_reviews`` refuses anything but an exact
    database HS11 and runs during resolution; this one accepted a 4/6/8-digit
    prefix, completed it by taking the FIRST sibling in the band, and runs at
    finalize — so a stray `{"1": "9018"}` in a request body silently replaced a
    code the reviewer had deliberately chosen with an arbitrary member of its
    band, at 0.8 confidence, with nothing on screen to show it happened.

    A partial code is not a decision. Prefix completion belongs to the
    automatic cascade, which flags what it guessed; a human typing a code is
    asserting one, so it must exist.
    """
    warnings: list[ValidationMessage] = []
    by_seq = {it.xml_item_sequence: it for it in items}
    for seq_raw, hs_raw in (overrides or {}).items():
        try:
            seq = int(seq_raw)
        except (TypeError, ValueError):
            continue
        item = by_seq.get(seq)
        if item is None or not str(hs_raw or "").strip():
            continue
        digits = digits_only(str(hs_raw))
        rec = ref.hs_exact(digits) if _HS11_RE.fullmatch(digits) else None
        if rec is None:
            partial = bool(digits) and len(digits) < 11
            detail = ("only " + str(len(digits)) + " digits — partial HS4/HS6/HS8 codes can "
                      "never become final; search the official database and pick the full code"
                      if partial else "not an exact code in the official HS database")
            # item-scoped so the declaration validator routes it as blocking
            item.warnings.append(ValidationMessage.blocking(
                "HS_MANUAL_REVIEW",
                f"Item {seq}: manual HS {hs_raw!r} was not applied — {detail}.",
                scope="ITEM", item_sequence=seq, field="hs_code"))
            continue
        _accept(item, rec, "MANUAL_OVERRIDE", confidence=1.0, explicit=True)
        # the earlier unresolved-HS block is superseded by the accepted override
        item.warnings = [w for w in item.warnings if w.code != "HS_MANUAL_REVIEW"]
    return warnings


def apply_hs_reviews(items: list[WorkItem], selections: dict, ref: ReferenceStore
                     ) -> list[ValidationMessage]:
    """Reviewer HS selections from the Detailed Review (stored in the item
    overlay), keyed by immutable item_id.  Authoritative ONLY when the source
    is allowlisted AND the normalized code is exactly 11 digits AND that exact
    code exists in the supplied database — leading zeros preserved, no zfill,
    no prefix completion.  An invalid stored selection is never applied (and
    never serialized): the item keeps its rule-cascade proposal, or stays
    blocked when none exists."""
    warnings: list[ValidationMessage] = []
    by_id = {it.item_id: it for it in items if it.item_id}
    for item_id, sel in (selections or {}).items():
        item = by_id.get(item_id)
        if item is None:
            continue                                  # deleted row — selection inert
        source = str((sel or {}).get("hs_review_source") or "").strip()
        code = digits_only(str((sel or {}).get("final_hs_code") or ""))
        rec = ref.hs_by_11.get(code) if _HS11_RE.fullmatch(code) else None
        if source not in HS_REVIEW_SOURCES or rec is None:
            msg = (f"Item {item.xml_item_sequence}: stored reviewed HS "
                   f"{(sel or {}).get('final_hs_code')!r} (source {source!r}) is not an "
                   "exact official 11-digit database code — selection ignored.")
            # item-scoped BLOCKING gates the XML at finalize; the review-level
            # WARNING copy makes the finding visible on the review screen
            item.warnings.append(ValidationMessage.blocking(
                "HS_REVIEW_REJECTED", msg,
                scope="ITEM", item_sequence=item.xml_item_sequence, field="hs_code"))
            warnings.append(ValidationMessage.warning(
                "HS_REVIEW_REJECTED", msg,
                scope="ITEM", item_sequence=item.xml_item_sequence, field="hs_code"))
            continue
        _accept(item, rec, source.upper(), confidence=1.0, explicit=True)
        item.warnings = [w for w in item.warnings if w.code != "HS_MANUAL_REVIEW"]
    return warnings


def resolve_hs_all(items: list[WorkItem], ref: ReferenceStore, history=None, llm_hint_fn=None) -> list[WorkItem]:
    return [resolve_hs_for_item(it, ref, history, llm_hint_fn) for it in items]
