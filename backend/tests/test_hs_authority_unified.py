"""One definition of a reviewed HS code, and no unconfirmed guess in the XML (A5).

Two things wrote `final_hs_code_11` under different rules.

`apply_hs_reviews` (item_id-keyed, Detailed Review) demands an exact official
11-digit code: no zfill, no prefix completion, source allowlisted.  It runs
during resolution.

`apply_manual_hs` (SN-keyed, posted with /finalize) accepted a 4/6/8-digit
prefix, completed it by taking the FIRST sibling in the band, stamped 0.8
confidence — and ran at finalize, i.e. AFTER the strict channel and over the
top of it.  So `{"1": "9018"}` in a request body replaced a code the reviewer
had picked with an arbitrary member of its band, and nothing on screen said so.
Prefix completion belongs to the automatic cascade, which labels its guesses; a
human typing a code is asserting one.

Separately, `SEMANTIC_DESCRIPTION` means "no HS was printed on the invoice, so
the description was matched against the official database" — confidence 0.3.
Because the cascade always finalizes something and `validate_declaration` only
checks that the code is 11 digits, that guess was indistinguishable from an
invoice-printed exact match by the time it reached the XML, and HS sets the
duty rate.  It is now blocking until a human confirms it — blocking in the same
warn-mode-bypassable way as an unresolved HS, so the file can still be taken to
ASYCUDA for testing.
"""
from decimal import Decimal

import pytest

from app.declaration.validator import WARN_MODE_HARD_CODES
from app.reference.store import get_reference
from app.rules import hs_resolver
from app.rules.models import WorkItem

ref = get_reference()


def _item(sn=1, desc="Random Gizmo XYZ", hs=None):
    return WorkItem(sn, "INV", "24/02/2026", sn, str(sn), desc, Decimal("1"),
                    "PCS", Decimal("1"), Decimal("1"), "USD",
                    hs_code_raw=hs, country_of_origin_raw="CN")


# --------------------------------------------------------------------------- #
# Both channels now demand the same thing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("partial", ["9018", "901811", "90181100"])
def test_finalize_channel_refuses_every_partial_code(partial):
    it = _item()
    hs_resolver.apply_manual_hs([it], {1: partial}, ref)
    assert it.final_hs_code_11 is None
    msg = next(w for w in it.warnings if w.code == "HS_MANUAL_REVIEW")
    assert "partial" in msg.message.lower()


@pytest.mark.parametrize("partial", ["9018", "901811", "90181100"])
def test_review_channel_refuses_every_partial_code(partial):
    """The strict channel already did this — pinned so the two cannot drift."""
    it = _item()
    it.item_id = "src:test"
    hs_resolver.apply_hs_reviews(
        [it], {"src:test": {"final_hs_code": partial,
                            "hs_review_source": "detailed_review_hs_search"}}, ref)
    assert it.final_hs_code_11 is None
    assert any(w.code == "HS_REVIEW_REJECTED" for w in it.warnings)


def test_both_channels_accept_the_same_exact_code_with_the_same_authority():
    a, b = _item(), _item()
    b.item_id = "src:test"
    hs_resolver.apply_manual_hs([a], {1: "90181100000"}, ref)
    hs_resolver.apply_hs_reviews(
        [b], {"src:test": {"final_hs_code": "90181100000",
                           "hs_review_source": "detailed_review_hs_search"}}, ref)

    assert a.final_hs_code_11 == b.final_hs_code_11 == "90181100000"
    assert a.hs_confidence == b.hs_confidence == 1.0
    assert a.hs_selection_explicit and b.hs_selection_explicit


def test_a_stray_finalize_override_can_no_longer_downgrade_a_reviewed_code():
    """The exact ordering that made this dangerous: reviewed first, then the
    finalize body.  A partial code must now leave the reviewed one standing."""
    it = _item()
    it.item_id = "src:test"
    hs_resolver.apply_hs_reviews(
        [it], {"src:test": {"final_hs_code": "90181100000",
                            "hs_review_source": "detailed_review_hs_search"}}, ref)
    assert it.final_hs_code_11 == "90181100000"

    hs_resolver.apply_manual_hs([it], {1: "9018"}, ref)      # runs later, at finalize
    assert it.final_hs_code_11 == "90181100000"              # untouched
    assert it.hs_confidence == 1.0


def test_an_exact_finalize_override_still_wins():
    """Unifying the strictness must not remove the channel's purpose."""
    it = _item()
    it.item_id = "src:test"
    hs_resolver.apply_hs_reviews(
        [it], {"src:test": {"final_hs_code": "90181100000",
                            "hs_review_source": "detailed_review_hs_search"}}, ref)
    hs_resolver.apply_manual_hs([it], {1: "90181200000"}, ref)
    assert it.final_hs_code_11 == "90181200000"


# --------------------------------------------------------------------------- #
# A description-matched guess is not a resolution
# --------------------------------------------------------------------------- #
def test_semantic_guess_blocks_the_declaration_until_confirmed():
    from app.database import SessionLocal, init_db
    from app.demo import seed_demo_job
    from app.pipeline import finalize, resolve_context, to_critical_review
    from app.review.critical_review import CriticalReviewConfirmation, merge_confirmation
    from app import services

    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    ctx = resolve_context(services.declarable_documents(job))
    # force one item onto the no-invoice-HS path
    target = ctx.items[0]
    hs_resolver._accept(target, ref.hs_by_11[target.final_hs_code_11], "SEMANTIC_DESCRIPTION")
    assert not target.hs_selection_explicit

    review = to_critical_review(ctx, services.declarable_documents(job))
    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation(
        field_40_confirmed=True))
    decl = finalize(ctx, reviewed)

    blocked = [m for m in decl.blocking_errors if m.code == "HS_GUESS_UNCONFIRMED"]
    assert len(blocked) == 1
    assert blocked[0].item_sequence == target.xml_item_sequence
    assert "no HS code" in blocked[0].message
    assert decl.ready_for_xml is False
    db.close()


def test_a_confirmed_selection_clears_the_block():
    from app.database import SessionLocal, init_db
    from app.demo import seed_demo_job
    from app.pipeline import finalize, resolve_context, to_critical_review
    from app.review.critical_review import CriticalReviewConfirmation, merge_confirmation
    from app import services

    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    ctx = resolve_context(services.declarable_documents(job))
    target = ctx.items[0]
    rec = ref.hs_by_11[target.final_hs_code_11]
    hs_resolver._accept(target, rec, "SEMANTIC_DESCRIPTION")
    # the reviewer looks at it and confirms — same record, now explicit
    hs_resolver._accept(target, rec, "DETAILED_REVIEW", confidence=1.0, explicit=True)

    review = to_critical_review(ctx, services.declarable_documents(job))
    reviewed, _ = merge_confirmation(review, CriticalReviewConfirmation(
        field_40_confirmed=True))
    decl = finalize(ctx, reviewed)

    assert not [m for m in decl.blocking_errors if m.code == "HS_GUESS_UNCONFIRMED"]
    db.close()


def test_the_guess_block_is_still_testable_in_asycuda():
    """Warn mode exists so a reviewer can take an otherwise-complete file to
    real ASYCUDA.  An unconfirmed HS does not make the file untestable, so it
    must not be one of the codes warn mode may never bypass."""
    assert "HS_GUESS_UNCONFIRMED" not in WARN_MODE_HARD_CODES
