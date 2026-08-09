"""Vendor field profiles — learned per-vendor COO defaults (storage JSON).

The live failure this store exists for: a trading exporter (Medtronic
Singapore) shipping goods made elsewhere (Ireland) — the exporter-country
fallback declared SG on every COO-less item and the reviewer bulk-stamped 25
items to IE.  After ONE such finalized correction, the next job from the same
vendor must propose IE (with a warning) instead of repeating the wrong guess.

Only LIVE-OCR jobs may teach the store: fixture/demo uploads carry the offline
OCR envelope, and without that gate every test run wrote the demo vendor into
the real ``storage/vendor_field_profiles.json``.
"""
import json

import pytest

from app.extraction import field_profiles
from app.reference.store import get_reference
from app.rules.coo import resolve_coo_all
from app.rules.models import WorkItem


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    from app import config as config_mod
    s = config_mod.get_settings_uncached()
    s.storage_dir = tmp_path
    monkeypatch.setattr(field_profiles, "get_settings", lambda: s)
    yield s


def _item(seq=1, coo_raw=None):
    return WorkItem(xml_item_sequence=seq, source_invoice_number="I", source_invoice_date="",
                    source_invoice_item_index=seq, source_invoice_item_no=None,
                    description_raw="X", quantity=1, invoice_uom_raw="PCS",
                    unit_price=1, line_total=1, currency="USD",
                    country_of_origin_raw=coo_raw)


VENDOR = "MEDTRONIC INTERNATIONAL LTD"


def test_reviewer_confirmed_default_is_learned_and_proposed():
    assert field_profiles.coo_default_for(VENDOR) is None
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 25, reviewer_confirmed=True)
    assert field_profiles.coo_default_for(VENDOR) == "IE"
    # name normalization: punctuation/case variants are the same vendor
    assert field_profiles.coo_default_for("Medtronic International Ltd.") == "IE"


def test_observed_default_needs_two_agreeing_jobs():
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 10, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) is None      # one job is not knowledge
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 8, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) == "IE"


def test_fallback_and_profile_sourced_values_never_teach_the_store():
    # the store must not learn from its own output or from the exporter guess
    field_profiles.record_coo_observation(
        VENDOR, [("SG", "EXPORTER_FALLBACK")] * 20, reviewer_confirmed=False)
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "VENDOR_PROFILE")] * 20, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) is None


def test_contradicting_observation_clears_an_observed_default():
    for _ in range(2):
        field_profiles.record_coo_observation(
            VENDOR, [("IE", "ITEM_LEVEL")] * 5, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) == "IE"
    field_profiles.record_coo_observation(
        VENDOR, [("US", "ITEM_LEVEL")] * 5, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) is None      # vendor is not uniform


def test_observed_contradiction_never_erases_a_reviewer_decision():
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 5, reviewer_confirmed=True)
    field_profiles.record_coo_observation(
        VENDOR, [("US", "ITEM_LEVEL")] * 5, reviewer_confirmed=False)
    assert field_profiles.coo_default_for(VENDOR) == "IE"


def test_mixed_jobs_below_share_threshold_record_nothing():
    coos = [("IE", "ITEM_LEVEL")] * 5 + [("US", "ITEM_LEVEL")] * 5
    field_profiles.record_coo_observation(VENDOR, coos, reviewer_confirmed=True)
    assert field_profiles.coo_default_for(VENDOR) is None


def test_disabled_flag_disables_both_directions(_isolated_store):
    _isolated_store.vendor_field_profiles_enabled = False
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 5, reviewer_confirmed=True)
    assert field_profiles.coo_default_for(VENDOR) is None


# --------------------------------------------------------------------------- #
# resolution ladder: item level > vendor profile > exporter fallback
# --------------------------------------------------------------------------- #
def test_profile_outranks_exporter_fallback_and_warns():
    ref = get_reference()
    field_profiles.record_coo_observation(
        VENDOR, [("IE", "ITEM_LEVEL")] * 25, reviewer_confirmed=True)
    items = resolve_coo_all([_item(1), _item(2, coo_raw="JP")], "Singapore", ref,
                            exporter_name=VENDOR)
    assert (items[0].coo_alpha2, items[0].coo_source) == ("IE", "VENDOR_PROFILE")
    assert any(w.code == "COO_VENDOR_PROFILE" for w in items[0].warnings)
    # a printed item-level COO always beats the profile
    assert (items[1].coo_alpha2, items[1].coo_source) == ("JP", "ITEM_LEVEL")


def test_without_a_profile_the_exporter_fallback_is_unchanged():
    ref = get_reference()
    items = resolve_coo_all([_item(1)], "China", ref, exporter_name="UNSEEN VENDOR CO")
    assert (items[0].coo_alpha2, items[0].coo_source) == ("CN", "EXPORTER_FALLBACK")


# --------------------------------------------------------------------------- #
# fixture/demo jobs must never teach the store (live-OCR gate in finalize)
# --------------------------------------------------------------------------- #
def test_demo_finalize_records_no_profile(_isolated_store):
    from fastapi.testclient import TestClient

    from app.database import init_db
    from app.main import app

    init_db()
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        review = client.get(f"/api/jobs/{job_id}/critical-review").json()
        r = client.post(f"/api/jobs/{job_id}/finalize", json={
            "review_fingerprint": review["review_fingerprint"],
            "field_40_confirmed": True,
            "border_mode": "01", "inland_mode_of_transport": "09",
        })
        assert r.status_code == 200
    store = _isolated_store.storage_dir / "vendor_field_profiles.json"
    assert not store.exists() or not json.loads(store.read_text())["profiles"], (
        "a fixture-driven demo finalize wrote a vendor profile — the live-OCR "
        "gate in services._finalize_job_locked is gone")
