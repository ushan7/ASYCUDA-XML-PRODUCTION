"""Invoice-HS-hint semantic band selection (user rule 2026-07-18).

Only the invoice-HS-hint completion path changes: within the 11->8->6->4
priority band, the 11-digit code is chosen by comparing the item description to
the DB description (+ AI explanation); if nothing matches, the band's broad
'Other' catch-all is used.  Exact-11, history, LLM, manual-override and
reviewed-HS paths are untouched."""
from decimal import Decimal

import pytest

from app.reference.store import get_reference
from app.rules import hs_resolver
from app.rules.models import WorkItem

ref = get_reference()


def _resolve(desc, hint):
    it = WorkItem(1, "INV", "24/02/2026", 1, "1", desc, Decimal("1"), "PCS",
                  Decimal("1"), Decimal("1"), "USD",
                  hs_code_raw=hint, country_of_origin_raw="CN")
    return hs_resolver.resolve_hs_for_item(it, ref)


def test_exact_11_digit_hint_unchanged():
    it = _resolve("anything", "90189090900")
    assert it.final_hs_code_11 == "90189090900"
    assert it.hs_source == "INVOICE_HS_EXACT" and it.hs_confidence == 1.0


def test_semantic_match_within_band():
    # description present in the band -> that specific code wins
    assert _resolve("Solar Inverter", "85044090").final_hs_code_11 == "85044090200"
    assert _resolve("Pulse Oximeter", "9018").final_hs_code_11 == "90181910000"
    assert _resolve("ICU Monitor", "9018").final_hs_code_11 == "90181920000"
    assert _resolve("Dental drill", "9018").final_hs_code_11 == "90184100000"


def test_explanation_does_not_override_description_match():
    # a Pulse Oximeter's AI explanation mentions "ICU monitor", but the item
    # "ICU Monitor" must still resolve to the ICU Monitor code by description
    it = _resolve("ICU Monitor", "9018")
    assert it.final_hs_code_11 == "90181920000"
    assert it.hs_source.startswith("INVOICE_HS_COMPLETED")
    assert not it.hs_source.endswith("_OTHER")


def test_no_semantic_match_uses_other_catch_all():
    # user's example: 9018 hint, unrelated description -> 90189090900
    it = _resolve("Random Gizmo XYZ", "9018")
    assert it.final_hs_code_11 == "90189090900"
    assert it.hs_source == "INVOICE_HS_COMPLETED_4_OTHER"
    assert 0 < it.hs_confidence < 1.0                    # AUTO_LOW_CONFIDENCE
    # an adapter is not a solar controller/inverter -> Other static converters
    assert _resolve("Adaptar", "85044090").final_hs_code_11 == "85044090900"


def test_priority_11_then_8_then_6_then_4():
    # a 6-digit hint drops to the 6-digit band; still semantic within it
    six = _resolve("Syringe", "901831")
    assert six.final_hs_code_11.startswith("901831")
    assert six.hs_source.startswith("INVOICE_HS_COMPLETED_6")


def test_other_fallback_is_broadest_other_code():
    # among several 'Other'-titled codes in the 9018 band, the broadest wins
    it = _resolve("qwerty nonsense zzz", "9018")
    assert it.final_hs_code_11 == "90189090900"          # not 90181990000 etc.


def test_no_hint_finalizes_via_semantic_description():
    # user rule 2026-07-19: no invoice HS -> finalize an 11-digit code from the
    # DB by matching the description; always LOW confidence + a review warning
    it = _resolve("Copper Winding Wire", None)
    assert it.final_hs_code_11 == "85441100000"          # "Winding wire of copper"
    assert it.hs_source == "SEMANTIC_DESCRIPTION"
    assert it.hs_confidence < 1.0
    assert any(w.code == "HS_SEMANTIC_GUESS" for w in it.warnings)
    # exact-name items land on their exact code
    assert _resolve("Pulse Oximeter", None).final_hs_code_11 == "90181910000"
    assert _resolve("Solar Inverter", None).final_hs_code_11 == "85044090200"


def test_hint_present_always_finalizes():
    # a hint is ALWAYS finalized — exact, band-semantic, or Other fallback
    assert _resolve("x", "85044090900").final_hs_code_11 == "85044090900"   # exact
    assert _resolve("Solar Inverter", "8504").final_hs_code_11 == "85044090200"  # band
    assert _resolve("nonsense", "9018").final_hs_code_11 == "90189090900"   # Other


def test_no_description_overlap_still_blocks():
    # only when NOTHING in the DB shares a description word does it fall to
    # manual review (nothing is invented)
    it = _resolve("zzqmarkerzzz qqxnoword", None)
    assert it.final_hs_code_11 is None
    assert any(w.code == "HS_MANUAL_REVIEW" for w in it.warnings)


def test_manual_override_refuses_a_bare_prefix():
    """A reviewer-typed prefix is refused rather than completed.

    It used to resolve to the band's first candidate (`9018` -> `90181100000`)
    at 0.8 confidence.  That is the automatic cascade's job, where the guess is
    labelled as one; a code typed by a human at finalize is an assertion, and
    this channel overwrites the strict item_id review channel, so it may not
    accept a weaker input than that channel does.
    """
    it = WorkItem(1, "INV", "24/02/2026", 1, "1", "Random Gizmo XYZ", Decimal("1"),
                  "PCS", Decimal("1"), Decimal("1"), "USD",
                  hs_code_raw=None, country_of_origin_raw="CN")
    hs_resolver.resolve_hs_for_item(it, ref)
    before = it.final_hs_code_11                        # whatever the cascade proposed
    hs_resolver.apply_manual_hs([it], {1: "9018"}, ref)
    # the prefix is refused outright — it neither lands nor displaces the
    # cascade's own (labelled, low-confidence) proposal
    assert it.final_hs_code_11 == before
    assert it.hs_source != "MANUAL_OVERRIDE"
    assert any(w.code == "HS_MANUAL_REVIEW" and "partial" in w.message.lower()
               for w in it.warnings)

    # the full official code IS accepted, and counts as an explicit decision
    hs_resolver.apply_manual_hs([it], {1: "90181100000"}, ref)
    assert it.final_hs_code_11 == "90181100000"
    assert it.hs_source == "MANUAL_OVERRIDE" and it.hs_selection_explicit
    assert it.hs_confidence == 1.0
    assert not any(w.code == "HS_MANUAL_REVIEW" for w in it.warnings)
