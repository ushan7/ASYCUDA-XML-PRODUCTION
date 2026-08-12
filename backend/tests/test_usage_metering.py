"""What each extraction spends, and whose spend it is.

Every job buys Mistral OCR and OpenAI tokens. The counts already existed — the
extractor has always accumulated them — but only to be written to a log line and
discarded, so at 1500 users nothing could answer what a job costs, which account
is burning the budget, or whether anyone is abusing the service.

The rule these tests exist to hold: **units are measured, money is estimated,
and the two are never confused.** Token and page counts come from the vendor's
own response and are always recorded. A cost is computed only for a model this
deployment has configured a rate for; otherwise it is NULL, which means "not
known" and must never be reported as free. A price this code invented would look
exactly as authoritative as one verified against an invoice.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import metering, services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import UsageEvent

OWNER = "user-a"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    init_db()
    db = SessionLocal()
    try:
        db.query(UsageEvent).delete()
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(get_settings(), "usage_price_path", tmp_path / "absent.json")
    metering.reset_rate_cache()
    yield
    metering.reset_rate_cache()


def _priced(tmp_path, monkeypatch, **models):
    path = tmp_path / "vendor_prices.json"
    path.write_text(json.dumps({"version": "test-v1", "models": models}), encoding="utf-8")
    monkeypatch.setattr(get_settings(), "usage_price_path", path)
    metering.reset_rate_cache()
    return path


def _events():
    db = SessionLocal()
    try:
        return db.query(UsageEvent).all()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Units are always recorded; cost is only ever estimated
# --------------------------------------------------------------------------- #
def test_usage_is_recorded_even_with_no_price_table():
    """The counts are facts from the vendor's response and do not depend on
    anyone having configured what they cost."""
    db = SessionLocal()
    try:
        metering.record(db, owner_key=OWNER, provider="openai", operation="extraction",
                        model="gpt-4o-mini", calls=3, prompt_tokens=1000,
                        completion_tokens=500)
        db.commit()
    finally:
        db.close()

    event = _events()[0]
    assert (event.calls, event.prompt_tokens, event.completion_tokens) == (3, 1000, 500)
    # NULL, not 0: "unknown" and "free" are different facts and a report that
    # confuses them invites someone to budget against a number that is a floor.
    assert event.estimated_cost_usd is None


def test_a_configured_rate_produces_a_cost(tmp_path, monkeypatch):
    _priced(tmp_path, monkeypatch, **{"gpt-4o-mini": {
        "input_per_1m_tokens": "0.15", "output_per_1m_tokens": "0.60"}})
    db = SessionLocal()
    try:
        metering.record(db, owner_key=OWNER, provider="openai", operation="extraction",
                        model="gpt-4o-mini", calls=1,
                        prompt_tokens=1_000_000, completion_tokens=1_000_000)
        db.commit()
    finally:
        db.close()

    event = _events()[0]
    assert Decimal(event.estimated_cost_usd) == Decimal("0.75")
    # The rate version is stamped on the row, so changing prices later cannot
    # rewrite what last month appeared to cost.
    assert event.rate_version == "test-v1"


def test_cached_tokens_are_not_charged_twice(tmp_path, monkeypatch):
    """The vendor reports cached tokens as a SUBSET of prompt_tokens, not in
    addition. Adding them would overstate every extraction — the more damaging
    direction for a number that will eventually set a price."""
    _priced(tmp_path, monkeypatch, **{"m": {
        "input_per_1m_tokens": "1.00", "cached_input_per_1m_tokens": "0.00"}})
    cost, _ = metering.estimate_cost("m", prompt_tokens=1_000_000,
                                     cached_tokens=1_000_000)
    assert cost == Decimal("0")

    cost_half, _ = metering.estimate_cost("m", prompt_tokens=1_000_000,
                                          cached_tokens=500_000)
    assert cost_half == Decimal("0.5")


def test_a_model_with_no_rate_is_unknown_not_free(tmp_path, monkeypatch):
    _priced(tmp_path, monkeypatch, **{"known": {"input_per_1m_tokens": "1.00"}})
    assert metering.estimate_cost("known", prompt_tokens=1_000_000)[0] == Decimal("1")
    assert metering.estimate_cost("something-else", prompt_tokens=1_000_000)[0] is None


def test_an_unreadable_price_table_does_not_become_free(tmp_path, monkeypatch):
    path = tmp_path / "vendor_prices.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(get_settings(), "usage_price_path", path)
    metering.reset_rate_cache()
    cost, version = metering.estimate_cost("gpt-4o-mini", prompt_tokens=1_000_000)
    assert cost is None
    assert version == "unreadable"


def test_the_shipped_example_prices_nothing():
    """It ships with every rate null on purpose — see its own _README."""
    import pathlib
    example = json.loads((pathlib.Path(__file__).resolve().parent.parent
                          / "vendor_prices.example.json").read_text(encoding="utf-8"))
    for model, spec in example["models"].items():
        assert all(v is None for v in spec.values()), (
            f"{model} ships with a price. This app cannot know what a deployment "
            f"is charged, and a guessed rate looks exactly as authoritative as a "
            f"verified one.")


def test_ocr_pages_are_priced_per_page(tmp_path, monkeypatch):
    _priced(tmp_path, monkeypatch, **{"mistral-ocr-latest": {"per_page": "0.001"}})
    cost, _ = metering.estimate_cost("mistral-ocr-latest", pages=250)
    assert cost == Decimal("0.250")


def test_nothing_spent_records_nothing():
    db = SessionLocal()
    try:
        assert metering.record(db, owner_key=OWNER, provider="openai",
                               operation="extraction", model="m") is None
        db.commit()
    finally:
        db.close()
    assert _events() == []


def test_recording_never_breaks_the_job(monkeypatch):
    """A declaration is not worth losing to a broken cost report.

    Scoped to what this function does by itself — pricing, coercion, building
    the row. The INSERT deliberately rides the caller's transaction so a charge
    and the work behind it land together, which means a database-level write
    failure surfaces at their commit like any other; nothing here can cause one.
    """
    def _explode(*a, **k):
        raise RuntimeError("price table is on fire")

    monkeypatch.setattr(metering, "estimate_cost", _explode)
    db = SessionLocal()
    try:
        assert metering.record(db, owner_key=OWNER, provider="openai",
                               operation="extraction", model="m", calls=1,
                               prompt_tokens=10) is None
        db.commit()
    finally:
        db.close()
    assert _events() == []


# --------------------------------------------------------------------------- #
# Attribution and reporting
# --------------------------------------------------------------------------- #
def test_spend_is_attributed_per_owner():
    db = SessionLocal()
    try:
        metering.record(db, owner_key="user-a", provider="openai", operation="ocr",
                        model="m", calls=1, pages=10)
        metering.record(db, owner_key="user-b", provider="openai", operation="ocr",
                        model="m", calls=1, pages=99)
        db.commit()

        a = metering.summary(db, "user-a")
        assert a["by_model"][0]["pages"] == 10
        assert metering.summary(db, "user-b")["by_model"][0]["pages"] == 99
    finally:
        db.close()


def test_a_partly_priced_month_says_so(tmp_path, monkeypatch):
    """A total computed from rows where some models had no rate is an
    UNDERSTATEMENT, and reporting it silently invites budgeting against it."""
    _priced(tmp_path, monkeypatch, **{"priced": {"input_per_1m_tokens": "1.00"}})
    db = SessionLocal()
    try:
        metering.record(db, owner_key=OWNER, provider="openai", operation="extraction",
                        model="priced", calls=1, prompt_tokens=1_000_000)
        metering.record(db, owner_key=OWNER, provider="openai", operation="extraction",
                        model="unpriced", calls=1, prompt_tokens=5_000_000)
        db.commit()
        got = metering.summary(db, OWNER)
    finally:
        db.close()
    assert Decimal(got["estimated_cost_usd"]) == Decimal("1")
    assert got["unpriced_events"] == 1


def test_last_months_spend_is_not_counted_this_month():
    db = SessionLocal()
    try:
        old = metering.month_start() - timedelta(days=2)
        db.add(UsageEvent(owner_key=OWNER, provider="openai", operation="ocr",
                          model="m", calls=1, pages=5, created_at=old))
        db.commit()
        assert metering.documents_this_month(db, OWNER) == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The quota
# --------------------------------------------------------------------------- #
def test_no_cap_by_default():
    """On the `local` provider, which is what the suite runs as and what a
    single-operator install is.  The raw setting is None ("take the default for
    the auth provider") and resolves to 0 = unlimited; `supabase` resolves to a
    real number instead — see tests/test_account_quota.py."""
    db = SessionLocal()
    try:
        assert get_settings().usage_monthly_document_cap is None
        assert get_settings().resolved_monthly_document_cap() == 0
        assert metering.quota_exceeded(db, OWNER) is None
    finally:
        db.close()


def test_the_cap_counts_documents_and_explains_itself(monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 2)
    db = SessionLocal()
    try:
        for _ in range(2):
            metering.record(db, owner_key=OWNER, provider="mistral", operation="ocr",
                            model="mistral-ocr-latest", calls=1, pages=3)
        db.commit()
        reason = metering.quota_exceeded(db, OWNER)
        assert reason and "limit of 2" in reason
        # ...and it is per account
        assert metering.quota_exceeded(db, "someone-else") is None
    finally:
        db.close()


def test_the_cap_does_not_depend_on_prices_being_configured(monkeypatch):
    """Counted in documents, not dollars, so a model nobody priced cannot
    defeat it."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 1)
    db = SessionLocal()
    try:
        metering.record(db, owner_key=OWNER, provider="mistral", operation="ocr",
                        model="never-priced", calls=1, pages=1)
        db.commit()
        assert metering.quota_exceeded(db, OWNER) is not None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_the_usage_endpoint_is_scoped_to_the_caller():
    """Usage is derived from a user's own declarations, so their totals are as
    private as the shipments that produced them."""
    db = SessionLocal()
    try:
        metering.record(db, owner_key="somebody-else", provider="openai",
                        operation="ocr", model="m", calls=1, pages=42)
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        body = client.get("/api/usage").json()
    assert body["by_model"] == []
    assert body["estimated_cost_usd"] is None


def test_a_demo_finalize_costs_nothing(monkeypatch):
    """Fixture and offline runs buy nothing, and a cost report that counted them
    would make the demo look like spend."""
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        review = client.get(f"/api/jobs/{job_id}/critical-review").json()
        client.post(f"/api/jobs/{job_id}/finalize", json={
            "review_fingerprint": review["review_fingerprint"],
            "field_40_confirmed": True,
            "border_mode": "01", "inland_mode_of_transport": "09"})
    assert _events() == []


def test_extraction_is_refused_over_the_cap(monkeypatch):
    """Refused BEFORE the claim: past that point the OCR is paid for whether or
    not the account was over its limit."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 1)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        doc_id = client.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]

        db = SessionLocal()
        try:
            from app.domain.enums import DocumentStatus
            from app.models import Document

            # The demo seeds documents already extracted; put one back so the
            # route reaches the quota gate rather than "already processed".
            db.get(Document, doc_id).status = DocumentStatus.UPLOADED.value
            owner = services._owner_of_job(db, job_id)
            metering.record(db, owner_key=owner, provider="mistral", operation="ocr",
                            model="m", calls=1, pages=1)
            db.commit()
        finally:
            db.close()

        r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 429
    assert r.json()["code"] == "USAGE_LIMIT_REACHED"


def test_being_over_quota_does_not_mask_a_finished_document(monkeypatch):
    """"Already processed" is the truer answer, and telling someone they are
    over their limit when the work is done sends them to buy capacity they do
    not need."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 1)
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        doc_id = client.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]

        db = SessionLocal()
        try:
            owner = services._owner_of_job(db, job_id)
            metering.record(db, owner_key=owner, provider="mistral", operation="ocr",
                            model="m", calls=1, pages=1)
            db.commit()
        finally:
            db.close()

        r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 409
