"""The finalize exchange rate is validated, recorded and sanity-checked (W2).

/finalize takes an untyped dict body, so `exchange_rate` reached the engine
unchecked and then set every national-currency value — and the duty. Measured
against the demo shipment before this gate existed:

    "0" / "0.00"  silently DISCARDED. `_rate`'s truthiness test let the truthy
                  string through, then resolve_context's `or` swallowed the
                  falsy Decimal, so the declaration quietly used the default
                  rate instead of the one the reviewer typed.
    "-145.76"     applied verbatim: total CIF -471244.09, ready_for_xml true.
    "1457.6"      applied verbatim: 4730761.23 instead of 474575.07.
    "145,76"      InvalidOperation escaping the endpoint as a 500.

and in every one of those cases `job.exchange_rate` stayed at the default, so
a filed declaration could not answer "at what rate?".

The rate is reachable from a plain text input in Critical Review, so the
realistic failure is a typo, not an attack.
"""
import pytest

from decimal import Decimal

from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.errors import BlockingValidationError
from app.main import app
from app.models import AuditEvent
from app import services

BODY = {
    "manual_insurance_amount": "1665.49",
    "manifest_no": "2026/1436", "field_18_transport_identity": "BA16CHA8099",
    "field_21_transport_identity": "BA16CHA8099", "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}
BASELINE_CIF = "474575.07"          # the demo shipment at the configured 145.76


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _reviewed_job(db):
    job = seed_demo_job(db)
    db.commit()
    review = services.critical_review(db, job)
    db.commit()
    return job, review


def _finalize(db, job, review, **extra):
    return services.finalize_job(db, job, dict(BODY, review_fingerprint=review["review_fingerprint"],
                                               **extra))


# --------------------------------------------------------------------------- #
# Refused: values that cannot produce a lawful declaration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["0", "0.00", "-145.76", "-0.01"])
def test_non_positive_rate_is_refused(bad):
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    with pytest.raises(BlockingValidationError) as e:
        _finalize(db, job, review, exchange_rate=bad)
    db.rollback()
    assert e.value.message.code == "EXCHANGE_RATE_INVALID"
    assert e.value.message.field == "exchange_rate"
    assert bad in e.value.message.message
    db.close()


@pytest.mark.parametrize("bad", ["145,76", "abc", "1.2.3", "145.76 NPR", "NaN", "Infinity"])
def test_unparseable_rate_is_refused_not_a_500(bad):
    """'145,76' and 'abc' used to raise InvalidOperation straight out of the
    endpoint. NaN/Infinity parse as Decimal but are not usable rates."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    with pytest.raises(BlockingValidationError) as e:
        _finalize(db, job, review, exchange_rate=bad)
    db.rollback()
    assert e.value.message.code == "EXCHANGE_RATE_INVALID"
    db.close()


def test_refused_rate_returns_409_with_a_machine_readable_code(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    r = client.post(f"/api/jobs/{job_id}/finalize",
                    json=dict(BODY, review_fingerprint=review["review_fingerprint"],
                              exchange_rate="-145.76"))
    assert r.status_code == 409
    body = r.json()
    assert body["blocking_errors"][0]["code"] == "EXCHANGE_RATE_INVALID"
    # nothing was built from the bad rate
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404


# --------------------------------------------------------------------------- #
# Accepted: unchanged behaviour for every rate that was already fine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("supplied", [None, "", "145.76"])
def test_absent_blank_and_correct_rates_are_unchanged(supplied):
    """A blank or missing rate keeps the job's own rate — this is the path
    every existing job takes, and it must be byte-identical."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    extra = {} if supplied is None else {"exchange_rate": supplied}
    decl = _finalize(db, job, review, **extra)
    assert decl["valuation"]["exchange_rate"] == "145.76"
    assert decl["valuation"]["total_cif"] == BASELINE_CIF
    assert decl["ready_for_xml"] is True
    db.close()


def test_a_genuine_rate_move_is_accepted_without_a_warning():
    """Doubling is a real market move, not a typo — no false alarm."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    decl = _finalize(db, job, review, exchange_rate="291.50")
    assert decl["ready_for_xml"] is True
    assert not [w for w in decl["warnings"] if w["code"] == "EXCHANGE_RATE_IMPLAUSIBLE"]
    db.close()


# --------------------------------------------------------------------------- #
# The typo that validation alone cannot catch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("typo", ["1457.6", "14.576"])
def test_a_shifted_decimal_point_warns(typo):
    """1457.6 is a perfectly legal number, simply ten times the right answer,
    and a 10x total CIF looks no different on screen. Note it lands EXACTLY on
    the factor, so the plausibility band has to be strict at its bounds."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    decl = _finalize(db, job, review, exchange_rate=typo)
    warned = [w for w in decl["warnings"] if w["code"] == "EXCHANGE_RATE_IMPLAUSIBLE"]
    assert len(warned) == 1
    assert typo in warned[0]["message"] and "145.76" in warned[0]["message"]
    # a warning, never a block: the reviewer may genuinely mean it
    assert decl["ready_for_xml"] is True
    db.close()


def test_no_plausibility_check_for_a_currency_the_default_is_not_quoted_for():
    """A JPY or KRW invoice legitimately sits orders of magnitude from a USD
    rate. Warning on those would be noise that teaches reviewers to ignore the
    banner, so the check is skipped rather than widened."""
    settings = get_settings()
    assert settings.default_exchange_rate_currency == "USD"
    assert services._rate_plausibility(Decimal("0.95"), "JPY") == []
    # ...but the same number IS implausible when the invoice really is in USD
    assert services._rate_plausibility(Decimal("0.95"), "USD")


# --------------------------------------------------------------------------- #
# The paper trail
# --------------------------------------------------------------------------- #
def test_the_rate_used_is_persisted_and_audited():
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    assert job.exchange_rate == "145.76"
    _finalize(db, job, review, exchange_rate="150.25")
    db.commit()

    # on the job, next to the declaration it produced
    assert job.exchange_rate == "150.25"
    assert job.declaration["valuation"]["exchange_rate"] == "150.25"

    locked = db.query(AuditEvent).filter(
        AuditEvent.job_id == job.id, AuditEvent.event_code == "CRITICAL_REVIEW_LOCKED").one()
    # unconditionally recorded: "what rate produced these numbers"
    assert locked.payload["exchange_rate"] == "150.25"
    # and as a human decision, because it differs from the job's previous rate
    assert locked.payload["overrides"]["exchange_rate"] == {"from": "145.76", "to": "150.25"}
    db.close()


def test_an_unchanged_rate_is_recorded_but_not_logged_as_an_override():
    """_overrides means 'entries that DIFFER from the default' — keeping the
    rate is not a reviewer decision, so it must not appear there."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    _finalize(db, job, review, exchange_rate="145.76")
    db.commit()
    locked = db.query(AuditEvent).filter(
        AuditEvent.job_id == job.id, AuditEvent.event_code == "CRITICAL_REVIEW_LOCKED").one()
    assert locked.payload["exchange_rate"] == "145.76"
    assert "exchange_rate" not in locked.payload["overrides"]
    db.close()


# --------------------------------------------------------------------------- #
# The two truthiness guards that cancelled each other out
# --------------------------------------------------------------------------- #
def test_resolve_context_no_longer_swallows_a_zero_rate():
    """pipeline's `exchange_rate or default` silently replaced a stated rate
    with the default. That `or` is why "0" appeared harmless: _rate let the
    truthy string through and resolve_context then discarded the falsy Decimal.
    Both halves are explicit now — services refuses <= 0 at the boundary, and
    the pipeline uses exactly what it is given."""
    init_db()
    db = SessionLocal()
    job, _ = _reviewed_job(db)
    from app.pipeline import resolve_context
    ctx = resolve_context(job.documents, Decimal("0"))
    assert ctx.exchange_rate == Decimal("0")          # used, not silently replaced
    assert resolve_context(job.documents, None).exchange_rate == Decimal("145.76")
    db.close()
