"""Reviews recompute FRESH after any evidence change (user rule 2026-08-02).

Reviewer overlay state is derived from the evidence it was entered against.
When that evidence changes — a document (re)extracted, its role decided, or it
removed — the overlay's item channels (bulk COO, per-item field edits incl.
COO, tombstones, ordering, manual rows) and the shipment-totals override must
NOT be blindly re-applied to the new item list: their ``src:`` ids are
positional and silently re-bind to different physical rows.

Two sanctioned survivors, and only these:

* Customs office / regime selections (``Job.review_selections``);
* explicit HS selections — folded into content-keyed ``Job.hs_history``
  (normalized item name -> code) and RE-PROPOSED through the resolver's
  HISTORY cascade at low confidence, outranking the semantic guess but never
  blindly final.
"""
import json
from decimal import Decimal

from app import services
from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.enums import DeclaredRole
from app.models import AuditEvent
from app.reference.store import get_reference
from app.review import item_mutations as itemmut
from app.rules import hs_resolver
from app.rules.models import WorkItem

ref = get_reference()


# --------------------------------------------------------------------------- #
# Overlay reset — pure unit level
# --------------------------------------------------------------------------- #
def _full_overlay() -> dict:
    return {
        "schema": 1, "revision": 7,
        "ordered_item_ids": ["src:aaa", "man:m1"],
        "manual_items": [{"item_id": "man:m1", "active": True, "seed": {}},
                         {"item_id": "man:m0", "active": False, "seed": {}}],
        "tombstones": [{"item_id": "src:bbb"}],
        "hs_selections": {"src:aaa": {"final_hs_code": "90189090900",
                                      "hs_review_source": "detailed_review",
                                      "explicit": True,
                                      "description_key": "pulseoximeter"}},
        "shipment_override": {"gross_weight": "500", "weight_unit": "KGM",
                              "total_packages": "10"},
        "field_edits": {"src:aaa": {"country_of_origin": "LK"}},
        "coo_all": "CN",
        "bms_edits": {"src:aaa": {"brand": "ACME"}},
    }


def test_invoice_change_discards_every_item_channel_and_folds_hs():
    overlay, hs_fold, discarded = itemmut.reset_for_evidence_change(
        _full_overlay(), "INVOICE", cause="corrected invoice extracted")
    assert overlay is not None
    assert overlay["coo_all"] is None
    assert overlay["field_edits"] == {}
    assert overlay["tombstones"] == []
    assert overlay["bms_edits"] == {}
    assert overlay["ordered_item_ids"] == []
    assert overlay["hs_selections"] == {}
    # manual rows deactivate (kept for audit), never silently re-applied
    m1 = next(r for r in overlay["manual_items"] if r["item_id"] == "man:m1")
    assert m1["active"] is False and m1["superseded_by_extraction"]
    # the explicit HS selection became content-keyed history, not a positional carry
    assert hs_fold == {"pulseoximeter": {"code": "90189090900",
                                         "source": "detailed_review",
                                         "at": hs_fold["pulseoximeter"]["at"]}}
    # shipment override is AWB/packing-derived state — an invoice change keeps it
    assert overlay["shipment_override"] is not None
    assert overlay["revision"] == 8
    notice = overlay["reset_notice"]
    assert notice["revision"] == 8 and notice["role"] == "INVOICE"
    assert "coo_all" in discarded and discarded["hs_selections"] == 1


def test_awb_or_packing_change_discards_only_the_shipment_override():
    for role in ("AIR_WAYBILL", "PACKING_LIST"):
        overlay, hs_fold, discarded = itemmut.reset_for_evidence_change(
            _full_overlay(), role, cause="new transport evidence")
        assert overlay["shipment_override"] is None
        # item channels were entered against invoice evidence — untouched
        assert overlay["coo_all"] == "CN"
        assert overlay["field_edits"] and overlay["hs_selections"]
        assert hs_fold == {} and list(discarded) == ["shipment_override"]


def test_banking_and_no_op_changes_reset_nothing():
    assert itemmut.reset_for_evidence_change(_full_overlay(), "BANKING") == (None, {}, {})
    # empty overlay: nothing to discard -> no revision bump, no notice
    assert itemmut.reset_for_evidence_change(None, "INVOICE") == (None, {}, {})
    assert itemmut.reset_for_evidence_change(
        itemmut.empty_overlay(), "AIR_WAYBILL") == (None, {}, {})


def test_legacy_hs_selection_without_description_key_is_discarded_not_folded():
    raw = _full_overlay()
    raw["hs_selections"]["src:old"] = {"final_hs_code": "85044090900",
                                       "hs_review_source": "detailed_review",
                                       "explicit": True}       # pre-2026-08-02 record
    overlay, hs_fold, discarded = itemmut.reset_for_evidence_change(raw, "INVOICE")
    assert "src:old" not in json.dumps(hs_fold)
    assert set(hs_fold) == {"pulseoximeter"}
    assert discarded["hs_selections"] == 2
    assert discarded["hs_selections_unfoldable"] == 1


# --------------------------------------------------------------------------- #
# HS cascade — confirmed history outranks the semantic guess
# --------------------------------------------------------------------------- #
def _item(desc, hint=None, **kw):
    return WorkItem(1, "INV", "24/02/2026", 1, "1", desc, Decimal("1"), "PCS",
                    Decimal("1"), Decimal("1"), "USD",
                    hs_code_raw=hint, country_of_origin_raw="CN", **kw)


def test_history_outranks_semantic_description_guess():
    # without history the semantic guess wins ("Winding wire of copper")
    plain = hs_resolver.resolve_hs_for_item(_item("Copper Winding Wire"), ref)
    assert plain.hs_source == "SEMANTIC_DESCRIPTION"
    assert plain.final_hs_code_11 == "85441100000"
    # a previously confirmed code for the same normalized name outranks it
    it = hs_resolver.resolve_hs_for_item(
        _item("Copper Winding Wire"), ref,
        history={"copperwindingwire": "90189090900"})
    assert it.final_hs_code_11 == "90189090900"
    assert it.hs_source == "HISTORY"
    # ...but stays a PROPOSAL: low confidence + a visible provenance warning
    assert it.hs_confidence < 1.0 and not it.hs_selection_explicit
    assert any(w.code == "HS_HISTORY_APPLIED" for w in it.warnings)


def test_invoice_printed_hs_still_outranks_history():
    it = hs_resolver.resolve_hs_for_item(
        _item("Copper Winding Wire", hint="85441100000"), ref,
        history={"copperwindingwire": "90189090900"})
    assert it.final_hs_code_11 == "85441100000"
    assert it.hs_source == "INVOICE_HS_EXACT"


def test_select_hs_stores_the_content_key():
    items = [_item("Pulse Oximeter", item_id="src:abc")]
    overlay, _event = itemmut.select_hs(
        None, items, ref, item_id="src:abc",
        final_hs_code="90189090900", hs_review_source="detailed_review")
    sel = overlay["hs_selections"]["src:abc"]
    assert sel["description_key"] == "pulseoximeter"
    overlay2, _e = itemmut.select_hs_range(
        None, items, ref, final_hs_code="90189090900",
        hs_review_source="detailed_review", sn_range="all")
    assert overlay2["hs_selections"]["src:abc"]["description_key"] == "pulseoximeter"


# --------------------------------------------------------------------------- #
# End to end through the services (demo fixtures, offline)
# --------------------------------------------------------------------------- #
def _fixture(name):
    return json.loads((SAMPLE_DIR / "fixtures" / name).read_text())


def _corrected_bytes(pdf_name):
    pdf = SAMPLE_DIR / pdf_name
    base = pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\n"
    return base + b"\n% corrected\n"


def _seeded_job(db):
    job = seed_demo_job(db)
    db.commit()
    services.critical_review(db, job)
    db.commit()
    return job


def _replace_invoice(db, job):
    inv_doc = next(d for d in job.documents
                   if d.declared_role == DeclaredRole.INVOICE.value)
    services.remove_document(db, job, inv_doc)
    db.commit()
    services.add_document(db, job, DeclaredRole.INVOICE, "sample_invoice_corrected.pdf",
                          _corrected_bytes("sample_invoice.pdf"),
                          _fixture("invoice.json"))
    db.commit()


def test_bulk_coo_never_survives_an_invoice_replacement():
    init_db()
    db = SessionLocal()
    job = _seeded_job(db)
    original_coos = [r["coo"] for r in job.critical_review["item_details"]]

    services.set_all_item_coo(db, job, {"country_of_origin": "LK"})
    db.commit()
    assert all(r["coo"] == "LK" for r in job.critical_review["item_details"])
    # office/regime selections made before the change must survive it
    services.review_regime_selections(db, job, {"border_mode": "04"})
    db.commit()

    _replace_invoice(db, job)
    overlay = itemmut.overlay_of(job.item_mutations)
    assert overlay["coo_all"] is None                       # the stamp is gone

    review = services.critical_review(db, job)
    db.commit()
    # the review is recomputed fresh from the current documents...
    assert [r["coo"] for r in review["item_details"]] == original_coos
    # ...says so to the reviewer instead of silently dropping their edits...
    assert any(w["code"] == "REVIEW_STATE_RESET" for w in review["warnings"])
    # ...and the sanctioned survivors are intact
    assert (job.review_selections or {}).get("values", {}).get("border_mode") == "04"
    events = [e.event_code for e in db.query(AuditEvent).filter(AuditEvent.job_id == job.id)]
    assert "REVIEW_STATE_RESET" in events
    db.close()


def test_shipment_override_never_survives_new_packing_evidence():
    init_db()
    db = SessionLocal()
    job = _seeded_job(db)
    services.review_shipment_totals(db, job, {"gross_weight": "500",
                                              "weight_unit": "KGM",
                                              "total_packages": "10"})
    db.commit()
    # a per-item COO edit rides along — invoice-derived, must NOT be reset here
    item_id = job.critical_review["item_details"][0]["item_id"]
    services.edit_job_item(db, job, item_id,
                           {"fields": {"country_of_origin": "LK"}})
    db.commit()
    assert itemmut.overlay_of(job.item_mutations)["shipment_override"]

    services.add_document(db, job, DeclaredRole.PACKING_LIST, "pl_corrected.pdf",
                          _corrected_bytes("sample_packing_list.pdf"), _fixture("packing_list.json"))
    db.commit()
    overlay = itemmut.overlay_of(job.item_mutations)
    assert overlay["shipment_override"] is None             # document authority returns
    assert overlay["field_edits"].get(item_id, {}).get("country_of_origin") == "LK"

    review = services.critical_review(db, job)
    db.commit()
    assert review["shipment_authority_type"] != "REVIEWER_OVERRIDE"
    assert any(w["code"] == "REVIEW_STATE_RESET" for w in review["warnings"])
    db.close()


def test_explicit_hs_selection_folds_into_job_history_on_invoice_replacement():
    init_db()
    db = SessionLocal()
    job = _seeded_job(db)
    row = job.critical_review["item_details"][0]
    services.review_item_hs(db, job, {"item_id": row["item_id"],
                                      "final_hs_code": "90189090900",
                                      "hs_review_source": "detailed_review"})
    db.commit()

    _replace_invoice(db, job)
    key = hs_resolver._normalized_item_name(row["description"])
    assert (job.hs_history or {}).get(key, {}).get("code") == "90189090900"
    assert itemmut.overlay_of(job.item_mutations)["hs_selections"] == {}
    # the fresh review still computes (history is a proposal inside the cascade,
    # applied only where invoice HS / stronger sources don't already resolve)
    review = services.critical_review(db, job)
    db.commit()
    assert review["invoice_item_count"] == 119
    db.close()


def test_reset_notice_retires_on_the_next_reviewer_mutation():
    init_db()
    db = SessionLocal()
    job = _seeded_job(db)
    services.set_all_item_coo(db, job, {"country_of_origin": "LK"})
    db.commit()
    _replace_invoice(db, job)
    review = services.critical_review(db, job)
    db.commit()
    assert any(w["code"] == "REVIEW_STATE_RESET" for w in review["warnings"])

    # the reviewer acts again — the notice has served its purpose
    services.set_all_item_coo(db, job, {"country_of_origin": "CN"})
    db.commit()
    assert not any(w["code"] == "REVIEW_STATE_RESET"
                   for w in job.critical_review["warnings"])
    db.close()


def test_first_extractions_of_a_fresh_job_reset_nothing():
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)                    # four extractions, empty overlay
    db.commit()
    events = [e.event_code for e in db.query(AuditEvent).filter(AuditEvent.job_id == job.id)]
    assert "REVIEW_STATE_RESET" not in events
    db.close()
