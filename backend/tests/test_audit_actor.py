"""Every audited action names the operator who took it.

This trail is the only record of who did what to a legally binding customs
declaration.  `AuditEvent.actor` has always existed, but nothing ever supplied
it: `_audit` did not take one, and the eleven service functions that did accept
`actor` were never passed one by any route.  So every event — every upload,
every HS override, every finalize — was stamped with the literal string
"system" or "reviewer", and `GET /jobs/{id}/audit` did not even return the
column.  `request.state.session` was read in exactly one place in the whole
application: the /auth/session endpoint.

A trail that cannot say WHICH operator changed a tariff code cannot support
non-repudiation, which is the one thing a post-clearance audit asks of it.
Today there is a single account, so nothing is ambiguous yet — these tests are
what keeps that true the day a second credential is issued.

The system actor is still correct for events no human triggered (restart
recovery, a background rebuild); what must never happen again is a
person-driven action attributed to a machine.
"""
import os

import pytest

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.services import SYSTEM_ACTOR

# conftest signs every TestClient in as this account.
OPERATOR = os.environ.get("EASYCUSTOMS_AUTH_USERNAME", "pytest-operator")


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _events(client, job_id):
    return client.get(f"/api/jobs/{job_id}/audit").json()


def _actors_for(events, code):
    return {e["actor"] for e in events if e["code"] == code}


def test_audit_endpoint_returns_the_actor(client):
    """An actor nobody can read is not an audit trail."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    events = _events(client, job_id)
    assert events, "the demo job should have audited something"
    assert all("actor" in e for e in events)


def test_job_creation_and_upload_name_the_operator(client):
    job_id = client.post("/api/jobs").json()["job_id"]
    assert _actors_for(_events(client, job_id), "JOB_CREATED") == {OPERATOR}


@pytest.mark.parametrize("code", [
    "JOB_CREATED", "DOCUMENT_UPLOADED", "DOCUMENT_EXTRACTION_STARTED",
    "DOCUMENT_EXTRACTED",
])
def test_document_lifecycle_events_name_the_operator(client, code):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    actors = _actors_for(_events(client, job_id), code)
    assert actors, f"no {code} event was recorded"
    assert actors == {OPERATOR}, f"{code} was attributed to {actors}, not the signed-in operator"


def test_reviewer_overrides_name_the_operator(client):
    """The HS code and the country of origin decide the duty — an override of
    either is exactly what a post-clearance audit asks 'who?' about."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    client.get(f"/api/jobs/{job_id}/critical-review")
    r = client.post(f"/api/jobs/{job_id}/items/coo-all", json={"country_of_origin": "CN"})
    assert r.status_code == 200
    events = _events(client, job_id)
    assert _actors_for(events, "ITEM_COO_APPLIED_ALL") == {OPERATOR}
    assert _actors_for(events, "CRITICAL_REVIEW_BUILT") == {OPERATOR}


def test_no_person_driven_event_is_attributed_to_the_machine(client):
    """The regression that matters: a real action stamped 'system'."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    client.get(f"/api/jobs/{job_id}/critical-review")
    person_driven = {
        "JOB_CREATED", "DOCUMENT_UPLOADED", "DOCUMENT_EXTRACTION_STARTED",
        "DOCUMENT_EXTRACTED", "CRITICAL_REVIEW_BUILT",
    }
    stamped_system = [e["code"] for e in _events(client, job_id)
                      if e["code"] in person_driven and e["actor"] == SYSTEM_ACTOR]
    assert not stamped_system, f"attributed to the machine: {sorted(set(stamped_system))}"


def test_an_unauthenticated_route_would_still_record_something(client):
    """_actor falls back to the system actor rather than inventing a name or
    raising, so a route reached without a session still audits honestly."""
    from app.main import _actor

    class _NoSession:
        state = type("S", (), {"session": None})()

    assert _actor(_NoSession()) == SYSTEM_ACTOR
