"""One user's declarations are invisible to every other user — route by route.

`docs/ADR-001-identity-and-tenancy.md` records the decision this asserts: the
ACCOUNT is the tenant, and there is nothing above it.  `services.job_visible_to`
is the single predicate that implements it, and it is correct.

This module exists because a correct predicate is not the property that matters.
The property that matters is that EVERY route asks it, and that is per-route
discipline — twenty-two handlers each remembering one line.  `tests/
test_multi_user_auth.py` proves the predicate and covers two routes
(`GET /api/jobs/{id}` and the dashboard listing); it does not prove the other
twenty ask at all.

So the sweep here is deliberately table-driven and CLOSED:

  * `_ROUTES` lists every job-scoped route with a body good enough to reach the
    handler (a 422 from a malformed body proves nothing — it never got as far as
    the ownership check).
  * `test_every_job_scoped_route_is_listed_here` reads the app's own routing
    table and fails if `_ROUTES` has drifted from it.  A route added later
    therefore fails this module until someone states what it does about
    ownership.  That is the point: the gap this suite found was not a wrong
    predicate, it was two routes that never called it.

Two things are asserted for every entry, and the second is what stops the suite
being vacuous:

  * B is refused (404, never 403 — an id that exists but belongs to someone else
    must be indistinguishable from one that does not), and
  * the SAME request against the owner's own job is not a 404, so a mistyped URL
    cannot masquerade as isolation.

The isolation asserted here is PYTHON isolation.  Supabase's row-level security
does not protect this backend — it connects to Postgres as a privileged role and
bypasses RLS — so a policy in the database would not catch a regression in any
of these.

A THIRD account runs the same sweep as a PLATFORM ADMIN.  That is the assertion
that makes `docs/ADR-001-identity-and-tenancy.md`'s position enforceable rather
than aspirational: an admin manages accounts, quotas and usage metadata and
cannot read declarations, so on somebody else's job an admin is exactly as
refused as a stranger — same route, same 404, no exception anywhere.  It is
asserted rather than assumed because the failure mode is one sympathetic commit
("support needs to see this") in one predicate.
"""
from __future__ import annotations

import json
import types

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, metering, services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import BmsArtifact, Job, XmlArtifact

# Distinct from tests/test_multi_user_auth.py's ids on purpose: the suite shares
# one database for the whole run, and reusing a principal would make this
# module's usage totals depend on whether that module ran first.
ALICE = "a11ce000-0000-4000-8000-000000000001"
BOB = "b0b00000-0000-4000-8000-000000000002"
# The platform administrator.  Holds `account_role.role = 'admin'`, and therefore
# holds nothing at all over ALICE's declarations.
CARLA = "ca41a000-0000-4000-8000-000000000003"

ALICE_EMAIL, ALICE_PW = "alice@broker.np", "pw-alice"
BOB_EMAIL, BOB_PW = "bob@broker.np", "pw-bob"
CARLA_EMAIL, CARLA_PW = "carla@easycustoms.np", "pw-carla"

PDF = b"%PDF-1.4\nisolation fixture\n"


# --------------------------------------------------------------------------- #
# Two accounts.  Supabase is the only provider that HAS two — `local` verifies
# the token subject against its single configured username, so a second account
# cannot exist there at all.  The HTTP call is stubbed; nothing leaves the box.
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(scope="module")
def supabase():
    """Module-scoped, so the two logged-in clients below outlive one test.

    `pytest.MonkeyPatch.context()` rather than the `monkeypatch` fixture, which
    is function-scoped and would undo the provider between tests.
    """
    import httpx

    logins = {(ALICE_EMAIL, ALICE_PW): ALICE, (BOB_EMAIL, BOB_PW): BOB,
              (CARLA_EMAIL, CARLA_PW): CARLA}

    def fake_post(url, *, params=None, headers=None, json=None, timeout=None):
        user_id = logins.get((json["email"], json["password"]))
        if user_id is None:
            return _FakeResponse(400, {"error": "invalid_grant"})
        return _FakeResponse(200, {"access_token": "supabase-jwt",
                                   "user": {"id": user_id, "email": json["email"]}})

    with pytest.MonkeyPatch.context() as mp:
        settings = get_settings()
        mp.setattr(settings, "auth_provider", "supabase")
        mp.setattr(settings, "supabase_url", "https://project.supabase.co")
        mp.setattr(settings, "supabase_anon_key", "anon-key")
        mp.setattr(httpx, "post", fake_post)
        yield


def _signed_in(email: str, password: str) -> TestClient:
    """A client holding this account's Bearer token AND its session cookie.

    Both, because the two are not interchangeable in this app: the SPA's fetches
    send the header, but the evidence <iframe> and the XML / .xls download links
    are plain navigations that carry only the cookie.  A predicate that leaked
    would most likely leak there.

    `raise_server_exceptions=False` so an unhandled 500 arrives as a response.
    The owner-side control asserts only "not a 404"; a route that reached the
    handler and then threw still proves the URL is real and the gate was passed.
    """
    client = TestClient(app, raise_server_exceptions=False)
    # conftest patches TestClient.__init__ to attach the single local operator's
    # token to every client.  Drop it: under this module's provider that token
    # verifies fine, and would silently run the whole sweep as a third principal.
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": email, "password": password})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    assert client.cookies.get(auth.COOKIE_NAME), "login must set the session cookie"
    return client


@pytest.fixture(scope="module")
def alice(supabase) -> TestClient:
    init_db()
    return _signed_in(ALICE_EMAIL, ALICE_PW)


@pytest.fixture(scope="module")
def bob(supabase) -> TestClient:
    init_db()
    return _signed_in(BOB_EMAIL, BOB_PW)


@pytest.fixture(scope="module")
def carla_the_admin(supabase) -> TestClient:
    """A signed-in PLATFORM ADMIN — the real role, from the real table.

    Granted through `accounts.grant_admin`, the same call the bootstrap script
    makes, so this is not a test-only shape of privilege: if the role were ever
    honoured by an ownership check, this client is the one that would get in.
    """
    init_db()
    db = SessionLocal()
    try:
        accounts.grant_admin(db, CARLA, granted_by="tests/test_tenant_isolation.py")
        db.commit()
    finally:
        db.close()
    client = _signed_in(CARLA_EMAIL, CARLA_PW)
    assert client.get("/api/auth/session").json()["role"] == "admin", (
        "this fixture is worthless unless the role actually took")
    return client


def _job_with_everything(client: TestClient) -> dict:
    """A job carrying one row of each thing this suite has to protect.

    The declaration and the two artifacts are written straight to the database
    rather than produced by a finalize.  That is on purpose and it is what makes
    those three assertions mean anything: `/declaration`, `latest_xml` and
    `latest_bms` all answer 404 when nothing has been built, so a job without
    them would let three routes pass this suite without ever being asked who was
    calling.  Running a real finalize instead would drag the whole extraction
    pipeline into a test about an access predicate.
    """
    job_id = client.post("/api/jobs").json()["job_id"]
    doc_id = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                         files={"file": ("invoice.pdf", PDF, "application/pdf")}
                         ).json()["document_id"]
    db = SessionLocal()
    try:
        db.get(Job, job_id).declaration = {"isolation_fixture": True}
        db.add(XmlArtifact(job_id=job_id, declaration_version=1,
                           checksum="isolation", xml_bytes=b"<ASYCUDA/>"))
        db.add(BmsArtifact(job_id=job_id, declaration_version=1,
                           checksum="isolation", xls_bytes=b"\xd0\xcf\x11\xe0bms"))
        db.commit()
    finally:
        db.close()
    return {"job": job_id, "doc": doc_id}


@pytest.fixture(scope="module")
def alice_job(alice) -> dict:
    return _job_with_everything(alice)


# --------------------------------------------------------------------------- #
# Every job-scoped route, with a body that reaches the handler.
#
# The bodies matter.  FastAPI validates a request model BEFORE the handler runs,
# so a route swept with a malformed body answers 422 and never touches
# `services.get_job` — the assertion would pass while proving nothing.
# --------------------------------------------------------------------------- #
_FILE_UPLOAD = {"files": {"file": ("invoice.pdf", PDF, "application/pdf")}}

_ROUTES: list[tuple[str, str, dict]] = [
    # (registered path template, method, request kwargs)
    ("/api/jobs/{job_id}", "GET", {}),
    ("/api/jobs/{job_id}/audit", "GET", {}),
    ("/api/jobs/{job_id}/declaration", "GET", {}),
    ("/api/jobs/{job_id}/critical-review", "GET", {}),
    ("/api/jobs/{job_id}/critical-review", "POST", {}),
    ("/api/jobs/{job_id}/xml", "GET", {}),
    ("/api/jobs/{job_id}/brand-model-size.xls", "GET", {}),

    ("/api/jobs/{job_id}/documents/{role}", "POST", _FILE_UPLOAD),
    ("/api/jobs/{job_id}/documents/{role}/photos", "POST",
     {"files": [("files", ("page1.jpg", b"\xff\xd8\xff\xe0jpeg", "image/jpeg"))]}),
    ("/api/jobs/{job_id}/documents/{document_id}/file", "GET", {}),
    ("/api/jobs/{job_id}/documents/{document_id}/file", "HEAD", {}),
    ("/api/jobs/{job_id}/documents/{document_id}", "DELETE", {}),
    ("/api/jobs/{job_id}/documents/{document_id}/extract", "POST", {}),
    ("/api/jobs/{job_id}/documents/{document_id}/role-decision", "POST",
     {"json": {"accept": True, "reason": "isolation probe"}}),

    ("/api/jobs/{job_id}/items", "POST", {"json": {"insertion_sn": 1}}),
    ("/api/jobs/{job_id}/items/{item_id}", "PATCH", {"json": {"fields": {"description": "x"}}}),
    ("/api/jobs/{job_id}/items/{item_id}", "DELETE", {"json": {"confirmation_sn": "1"}}),
    ("/api/jobs/{job_id}/items/hs-review", "POST",
     {"json": {"item_id": "probe", "final_hs_code": "12345678",
               "hs_review_source": "detailed_review_hs_search"}}),
    ("/api/jobs/{job_id}/items/hs-review-range", "POST",
     {"json": {"final_hs_code": "12345678", "sn_range": "1"}}),
    ("/api/jobs/{job_id}/items/brand-model-size", "POST", {"json": {"edits": []}}),
    ("/api/jobs/{job_id}/items/coo-all", "POST", {"json": {"country_of_origin": "CN"}}),
    ("/api/jobs/{job_id}/shipment-totals", "POST",
     {"json": {"gross_weight": "100", "total_packages": "10"}}),
    ("/api/jobs/{job_id}/regime", "POST", {"json": {}}),
    ("/api/jobs/{job_id}/finalize", "POST", {"json": {}}),
]

_IDS = [f"{method} {path}" for path, method, _ in _ROUTES]


def _url(template: str, job_id: str, doc_id: str, item_id: str = "probe-item") -> str:
    return (template
            .replace("{job_id}", job_id)
            .replace("{document_id}", doc_id)
            .replace("{item_id}", item_id)
            .replace("{role}", "INVOICE"))


# --------------------------------------------------------------------------- #
# The table is closed against the app's own routing table
# --------------------------------------------------------------------------- #
def test_every_job_scoped_route_is_listed_here():
    """A new job-scoped route fails this module until someone states its answer.

    The two routes this suite found unscoped were not a wrong predicate — they
    were handlers that never called it.  Nothing except this check notices the
    next one.
    """
    registered = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", ()) or ()
        if "{job_id}" in getattr(route, "path", "") and method not in ("OPTIONS", "HEAD")
    }
    swept = {(path, method) for path, method, _ in _ROUTES if method != "HEAD"}
    assert registered - swept == set(), (
        "job-scoped routes exist that this isolation suite does not exercise: "
        f"{sorted(registered - swept)}")
    assert swept - registered == set(), (
        f"this suite exercises routes the app does not serve: {sorted(swept - registered)}")


# --------------------------------------------------------------------------- #
# customs_job, uploaded_document, xml_artifact, bms_artifact, audit_event
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,method,kwargs", _ROUTES, ids=_IDS)
def test_another_user_is_refused_every_job_route(bob, alice_job, path, method, kwargs):
    """B, authenticated, on A's job: 404 every time.

    404 rather than 403 deliberately — a 403 confirms the job is real, which is
    itself a disclosure to anyone enumerating ids.
    """
    r = bob.request(method, _url(path, alice_job["job"], alice_job["doc"]), **kwargs)
    assert r.status_code == 404, (
        f"{method} {path} answered {r.status_code} to a user who does not own the job"
        f"{': ' + r.text[:400] if method != 'HEAD' and r.content else ''}")


@pytest.mark.parametrize("path,method,kwargs", _ROUTES, ids=_IDS)
def test_a_platform_admin_is_refused_every_job_route_too(carla_the_admin, alice_job,
                                                         path, method, kwargs):
    """The SAME sweep, run by an account holding `role = 'admin'`.

    `docs/ADR-001-identity-and-tenancy.md`: an admin manages accounts and quotas
    and cannot read customer declarations, documents, extractions, XML artifacts
    or audit trails.  The data is not ours — a declaration is assembled from an
    importer's commercial invoice, and that importer is not a user of this
    platform, was never shown a privacy notice by us, and cannot object.

    So an admin gets the same 404 a stranger gets, on every one of these, and it
    is asserted rather than assumed: `if role == "admin": return True` in
    `job_visible_to` would be the "unowned job is visible to everybody" branch
    with a different condition, and this parametrization is the only thing that
    would notice.
    """
    r = carla_the_admin.request(method, _url(path, alice_job["job"], alice_job["doc"]), **kwargs)
    assert r.status_code == 404, (
        f"{method} {path} answered {r.status_code} to a PLATFORM ADMIN who does not own "
        f"the job — ADR-001 says an admin cannot read a declaration"
        f"{': ' + r.text[:400] if method != 'HEAD' and r.content else ''}")


def test_the_admin_can_reach_the_admin_routes_at_all(carla_the_admin):
    """The control for the sweep above.  Without it, a role that silently failed
    to apply would make those 404s prove nothing at all."""
    assert carla_the_admin.get("/api/admin/accounts").status_code == 200


def test_the_admin_download_links_are_scoped_even_with_only_the_cookie(carla_the_admin,
                                                                      alice_job):
    """The cookie paths, as an admin.  These are the shapes a leak would take —
    the XML and .xls links are plain navigations carrying no header, and the whole
    finished declaration is in them."""
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, carla_the_admin.cookies.get(auth.COOKIE_NAME))
    job, doc = alice_job["job"], alice_job["doc"]
    assert client.get(f"/api/jobs/{job}/xml").status_code == 404
    assert client.get(f"/api/jobs/{job}/brand-model-size.xls").status_code == 404
    assert client.get(f"/api/jobs/{job}/documents/{doc}/file").status_code == 404
    assert client.head(f"/api/jobs/{job}/documents/{doc}/file").status_code == 404


def test_the_admin_dashboard_listing_shows_only_the_admins_own_jobs(carla_the_admin,
                                                                   alice_job):
    """`list_jobs` mirrors the predicate in SQL and is a separate query from the
    per-job check.  An admin whose dashboard listed everybody's jobs would be
    reading party names and shipment totals out of the summary while every route
    above refused to open them."""
    theirs = {row["job_id"] for row in carla_the_admin.get("/api/jobs").json()["jobs"]}
    assert alice_job["job"] not in theirs


@pytest.mark.parametrize("path,method,kwargs", _ROUTES, ids=_IDS)
def test_the_same_routes_are_not_404_for_their_owner(alice, path, method, kwargs):
    """The control.  Without it a typo in a URL passes the sweep above.

    A fresh job per case, so a mutating probe cannot disturb another's fixture.
    Only "not 404" is asserted: several of these legitimately refuse a job with
    no declaration yet (409, 400), and what is being proved is that the request
    reached the handler rather than that the handler liked it.
    """
    own = _job_with_everything(alice)
    r = alice.request(method, _url(path, own["job"], own["doc"]), **kwargs)
    assert r.status_code != 404, (
        f"{method} {path} answered 404 to the job's OWNER — the sweep above is "
        f"not testing what it claims")


# --------------------------------------------------------------------------- #
# The cookie paths specifically.
#
# These are the ones a broken predicate leaks through: the evidence panel frames
# the file URL and the XML/.xls links are plain <a href> navigations, so they
# arrive with the session cookie and NO Authorization header.  A route reading
# the principal from the header only would be wide open to exactly this shape.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def bob_cookie_only(bob) -> TestClient:
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, bob.cookies.get(auth.COOKIE_NAME))
    return client


def test_a_download_link_is_scoped_even_with_only_the_cookie(bob_cookie_only, alice_job):
    """`GET /api/jobs/{id}/xml` with a cookie and nothing else — a browser
    following a link.  The finished XML is the whole declaration: consignor,
    consignee, HS codes, invoice values, duty base."""
    r = bob_cookie_only.get(f"/api/jobs/{alice_job['job']}/xml")
    assert r.status_code == 404, (
        "another user's finalized ASYCUDA declaration was served to a cookie "
        f"that does not own it ({r.status_code}, {len(r.content)} bytes)")


def test_the_bms_workbook_is_scoped_even_with_only_the_cookie(bob_cookie_only, alice_job):
    r = bob_cookie_only.get(f"/api/jobs/{alice_job['job']}/brand-model-size.xls")
    assert r.status_code == 404, (
        f"another user's brand/model/size workbook was served ({r.status_code})")


def test_the_evidence_iframe_is_scoped_even_with_only_the_cookie(bob_cookie_only, alice_job):
    """GET and HEAD both: the panel probes with HEAD before it frames the URL,
    so a HEAD that answered while GET refused would still confirm the document
    exists to someone who may not see it."""
    url = f"/api/jobs/{alice_job['job']}/documents/{alice_job['doc']}/file"
    assert bob_cookie_only.get(url).status_code == 404
    assert bob_cookie_only.head(url).status_code == 404


def test_the_owner_can_use_those_links_with_only_the_cookie(alice, alice_job):
    """The control for the three above — the cookie path genuinely works, so
    their 404s are about ownership and not about a cookie that never applied."""
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    client.cookies.set(auth.COOKIE_NAME, alice.cookies.get(auth.COOKIE_NAME))
    job = alice_job["job"]
    assert client.get(f"/api/jobs/{job}/xml").status_code == 200
    assert client.get(f"/api/jobs/{job}/brand-model-size.xls").status_code == 200
    assert client.get(f"/api/jobs/{job}/documents/{alice_job['doc']}/file").status_code == 200


# --------------------------------------------------------------------------- #
# The predicate itself, and the listing that has to agree with it
# --------------------------------------------------------------------------- #
def test_the_owner_key_is_the_user_id(alice, alice_job):
    db = SessionLocal()
    try:
        assert db.get(Job, alice_job["job"]).owner_key == ALICE
    finally:
        db.close()


def test_an_unowned_job_is_visible_to_nobody():
    """Restated here because it is the branch that was once a live disclosure,
    and because every route above inherits it."""
    unowned = Job(status="UPLOADING", owner_key="")
    assert services.job_visible_to(unowned, ALICE) is False
    assert services.job_visible_to(unowned, BOB) is False
    assert services.job_visible_to(unowned, "") is False
    assert services.job_visible_to(unowned, CARLA) is False
    assert services.job_visible_to(unowned, services.SYSTEM_PRINCIPAL) is True


def test_the_predicate_cannot_be_told_about_a_role(carla_the_admin, alice_job):
    """The predicate itself, called directly with the admin's principal.

    Separate from the route sweep because a route could be refusing for the wrong
    reason.  This is the one function ADR-001 names, answering the one question it
    is allowed to answer: does this principal own this job.
    """
    db = SessionLocal()
    try:
        job = db.get(Job, alice_job["job"])
        assert services.job_visible_to(job, ALICE) is True
        assert services.job_visible_to(job, CARLA) is False
        assert accounts.role_of(db, CARLA) == accounts.ADMIN, (
            "...and CARLA really is an admin, so the False above is the point")
        assert services.get_job(db, alice_job["job"], principal=CARLA) is None
    finally:
        db.close()


def test_the_listing_does_not_leak_the_other_account(alice, bob, alice_job):
    """Separate SQL from the per-job check.  One that disagreed would put
    another user's shipment totals and party names in the dashboard summary
    while refusing to open the job."""
    theirs = {row["job_id"] for row in bob.get("/api/jobs").json()["jobs"]}
    assert alice_job["job"] not in theirs

    mine = {row["job_id"] for row in alice.get("/api/jobs").json()["jobs"]}
    assert alice_job["job"] in mine


def test_the_audit_trail_of_another_users_job_is_not_readable(alice, bob, alice_job):
    """audit_event.  Not vacuous: the owner's copy has real rows in it, so the
    refusal is a refusal and not an empty list."""
    owner_view = alice.get(f"/api/jobs/{alice_job['job']}/audit")
    assert owner_view.status_code == 200
    assert any(e["code"] == "JOB_CREATED" for e in owner_view.json())
    assert bob.get(f"/api/jobs/{alice_job['job']}/audit").status_code == 404


# --------------------------------------------------------------------------- #
# usage_event — spend and quota are per account
# --------------------------------------------------------------------------- #
@pytest.fixture()
def alice_spent_something(alice_job):
    """One OCR event on A's account.  Written through `metering.record`, the
    same call the extractor makes, so the row is shaped exactly as a real one."""
    db = SessionLocal()
    try:
        metering.record(db, owner_key=ALICE, provider="mistral", operation="ocr",
                        model="mistral-ocr-latest", job_id=alice_job["job"],
                        calls=1, pages=4)
        db.commit()
    finally:
        db.close()


def test_one_accounts_spend_is_not_reported_to_another(alice, bob, alice_spent_something):
    """/api/usage is what the reviewer is shown and what a bill would be drawn
    from.  B's must not move because A extracted a document."""
    mine = alice.get("/api/usage").json()
    theirs = bob.get("/api/usage").json()

    assert mine["documents_this_month"] >= 1
    assert any(row["provider"] == "mistral" for row in mine["by_model"])

    assert theirs["documents_this_month"] == 0
    assert theirs["by_model"] == []
    assert theirs["estimated_cost_usd"] is None


def test_one_accounts_usage_does_not_consume_anothers_quota(alice_spent_something,
                                                            monkeypatch):
    """`quota_exceeded` is the gate the extract route calls one line before it
    spends money.  A shared counter would block every account the moment any one
    of them reached the cap — and would let a heavy user exhaust a stranger's
    plan."""
    settings = get_settings()
    monkeypatch.setattr(settings, "usage_monthly_document_cap", 1)

    db = SessionLocal()
    try:
        assert metering.documents_this_month(db, ALICE) >= 1
        assert metering.documents_this_month(db, BOB) == 0

        assert metering.quota_exceeded(db, ALICE) is not None, (
            "A is at its cap and should be refused")
        assert metering.quota_exceeded(db, BOB) is None, (
            "B has extracted nothing and must not be blocked by A's usage")
    finally:
        db.close()


def test_the_reverse_direction_too(alice, bob, alice_job, monkeypatch):
    """B spends; A's quota and totals are untouched.  Asserted separately
    because a counter keyed on the wrong column is symmetric — it would pass a
    one-directional test in whichever direction happened to be written."""
    db = SessionLocal()
    try:
        before = metering.documents_this_month(db, ALICE)
        bob_job = bob.post("/api/jobs").json()["job_id"]
        metering.record(db, owner_key=BOB, provider="mistral", operation="ocr",
                        model="mistral-ocr-latest", job_id=bob_job, calls=1, pages=2)
        db.commit()
        assert metering.documents_this_month(db, BOB) >= 1
        assert metering.documents_this_month(db, ALICE) == before
    finally:
        db.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "usage_monthly_document_cap", 1)
    db = SessionLocal()
    try:
        assert metering.quota_exceeded(db, BOB) is not None
    finally:
        db.close()
