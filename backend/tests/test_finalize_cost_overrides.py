"""Freight and insurance overrides are validated where they enter (W2 sibling).

/finalize takes an untyped dict body, so these two amounts reached the engine
unchecked and then moved the duty base directly: `select_freight` returns a
manual override verbatim, `allocate_cost` spreads it across every item, and the
builder folds it into total_cost_itm, total_cif_itm and the statistical value.
Nothing downstream treats a negative cost as blocking, so the declaration still
came out ready_for_xml with a clean XML_BUILT audit line.  Measured against the
demo shipment before this gate existed:

    "(250000)"  the accounting form parses as -250000 and was applied verbatim:
                every item's CIF fell BELOW its own invoice value and the XML
                declared an understated duty base, with nothing on screen
                saying so.
    "-250000"   same, via the plain leading-minus form.
    "abc"       unparseable -> None, so the typed figure was silently DISCARDED
                and the deterministic freight used instead — the same
                silent-drop failure the exchange-rate gate documents.

Both fields are plain text inputs in Critical Review, so the realistic failure
is a pasted accounting figure, not an attack — but the same body is reachable
by anything holding the session, which is why it is refused rather than warned.
"""
import pytest

from decimal import Decimal

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.errors import BlockingValidationError
from app.main import app
from app import services

BODY = {
    "manual_insurance_amount": "1665.49",
    "manifest_no": "2026/1436", "field_18_transport_identity": "BA16CHA8099",
    "field_21_transport_identity": "BA16CHA8099", "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


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
    body = dict(BODY, review_fingerprint=review["review_fingerprint"], **extra)
    return services.finalize_job(db, job, body)


# --------------------------------------------------------------------------- #
# Refused: amounts that would falsify the customs value
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["-1", "-250000", "(250000)", "(1,200.50)", "-0.01"])
def test_negative_insurance_is_refused(bad):
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    with pytest.raises(BlockingValidationError) as e:
        _finalize(db, job, review, manual_insurance_amount=bad)
    db.rollback()
    assert e.value.message.code == "INSURANCE_AMOUNT_INVALID"
    assert e.value.message.field == "manual_insurance_amount"
    assert bad in e.value.message.message
    db.close()


@pytest.mark.parametrize("bad", ["-1", "-250000", "(250000)", "-0.01"])
def test_negative_freight_is_refused(bad):
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    with pytest.raises(BlockingValidationError) as e:
        _finalize(db, job, review, manual_freight_amount=bad)
    db.rollback()
    assert e.value.message.code == "FREIGHT_AMOUNT_INVALID"
    assert e.value.message.field == "manual_freight_amount"
    db.close()


@pytest.mark.parametrize("bad", ["abc", "1.2.3", "NaN", "Infinity", "-Infinity"])
def test_unparseable_amount_is_refused_not_silently_discarded(bad):
    """These used to fall through to None/0 and quietly use the deterministic
    freight, so the reviewer's typed figure vanished with no message."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    with pytest.raises(BlockingValidationError) as e:
        _finalize(db, job, review, manual_freight_amount=bad)
    db.rollback()
    assert e.value.message.code == "FREIGHT_AMOUNT_INVALID"
    db.close()


@pytest.mark.parametrize("legacy,code", [
    ("insurance_national", "INSURANCE_AMOUNT_INVALID"),
    ("freight_override", "FREIGHT_AMOUNT_INVALID"),
])
def test_legacy_body_aliases_go_through_the_same_gate(legacy, code):
    """The gate sits AFTER the legacy-alias fold, so the old key names cannot
    be used to route a negative amount around it."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    body = dict(BODY, manual_insurance_amount="", **{legacy: "-250000"})
    with pytest.raises(BlockingValidationError) as e:
        services.finalize_job(db, job,
                              dict(body, review_fingerprint=review["review_fingerprint"]))
    db.rollback()
    assert e.value.message.code == code
    db.close()


def test_refused_amount_returns_409_and_builds_no_xml(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    r = client.post(f"/api/jobs/{job_id}/finalize",
                    json=dict(BODY, review_fingerprint=review["review_fingerprint"],
                              manual_insurance_amount="(250000)"))
    assert r.status_code == 409
    assert r.json()["blocking_errors"][0]["code"] == "INSURANCE_AMOUNT_INVALID"
    # nothing was built from the understated value
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404


# --------------------------------------------------------------------------- #
# Accepted: unchanged behaviour for every amount that was already fine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("good", ["1665.49", "0", "0.00", "1,200.50", "$500", ""])
def test_valid_and_blank_amounts_are_unchanged(good):
    """Zero is legitimate (no insurance on this shipment), blank means "use the
    deterministic value", and the thousands/symbol forms parse as they always
    did.  Refusing any of these would break the normal finalize path."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    result = _finalize(db, job, review, manual_insurance_amount=good)
    assert result.get("status") != "REVIEW_STALE"
    assert db.get(type(job), job.id).declaration is not None
    db.close()


def test_positive_insurance_still_raises_the_customs_value():
    """The guard must not neuter the field it protects: a larger insurance
    figure still increases the declared CIF."""
    init_db()
    db = SessionLocal()
    job, review = _reviewed_job(db)
    _finalize(db, job, review, manual_insurance_amount="1000.00")
    low = Decimal(str(db.get(type(job), job.id).declaration["valuation"]["total_cif"]))
    db.close()

    db = SessionLocal()
    job, review = _reviewed_job(db)
    _finalize(db, job, review, manual_insurance_amount="9000.00")
    high = Decimal(str(db.get(type(job), job.id).declaration["valuation"]["total_cif"]))
    db.close()
    assert high > low
