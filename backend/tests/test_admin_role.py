"""An admin manages accounts, quotas and usage. An admin cannot read a declaration.

`docs/plan-signup-and-roles.md` step 2, scoped by
`docs/ADR-001-identity-and-tenancy.md`.  Three properties, and the second is the
one that either holds here or quietly stops holding:

  * **A member cannot reach an admin route.**  Route by route, and 404 rather
    than 403 — a 403 confirms the admin surface exists to a signed-in stranger.
  * **An admin is not privileged over DATA.**  `services.job_visible_to` does not
    know this role exists; `tests/test_tenant_isolation.py` runs its whole
    parametrized sweep as an admin and gets the same 404 a stranger gets.  What
    is asserted HERE is the shape of the thing that could break it: the predicate
    takes no role, and no admin route is job-scoped.
  * **The role is read, per request, from the local table.**  Not from a token
    claim (a stateless 24h token cannot be revoked, and late revocation is the
    direction that matters for a privilege) and not from a network call.  Both
    are asserted directly, including the statement count.

And one closing of a loop step 1 left open: `account_quota.updated_by` shipped
with no writer, and `metering.quota_exceeded` has always promised a blocked
reviewer that extraction resumes "when an administrator raises the limit".  The
end-to-end test below is that sentence: an account hits its cap, is refused, is
findable in the admin listing, has its cap raised by a named administrator, and
is no longer refused.
"""
from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import accounts, auth, metering, services
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.main import app
from app.models import AccountDisabled, AccountQuota, AccountRole, BmsArtifact, Job, XmlArtifact

# Distinct from every other module's principals: the suite shares one database
# for the whole run, so a reused key would make these totals depend on which
# module ran first.
BOSS = "0b055000-0000-4000-8000-00000000000a"      # the administrator
CLERK = "c1e4c000-0000-4000-8000-00000000000b"     # an ordinary member
BOSS_EMAIL, BOSS_PW = "boss@easycustoms.np", "pw-boss"
CLERK_EMAIL, CLERK_PW = "clerk@broker.np", "pw-clerk"

PDF = b"%PDF-1.4\nadmin role fixture\n"


# --------------------------------------------------------------------------- #
# Two accounts.  Supabase is the only provider that HAS two — `local` verifies
# the token subject against its single configured username.  The HTTP call is
# stubbed; nothing leaves the box.
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
    """Module-scoped, so the signed-in clients below outlive one test."""
    import httpx

    logins = {(BOSS_EMAIL, BOSS_PW): BOSS, (CLERK_EMAIL, CLERK_PW): CLERK}

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
    client = TestClient(app, raise_server_exceptions=False)
    # conftest patches TestClient.__init__ to attach the single local operator's
    # token; drop it or the whole module would run as a third principal.
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": email, "password": password})
    assert r.status_code == 200, r.text
    client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return client


@pytest.fixture(scope="module")
def admin(supabase) -> TestClient:
    init_db()
    db = SessionLocal()
    try:
        accounts.grant_admin(db, BOSS, granted_by="the deployment runbook")
        db.commit()
    finally:
        db.close()
    return _signed_in(BOSS_EMAIL, BOSS_PW)


@pytest.fixture(scope="module")
def member(supabase) -> TestClient:
    init_db()
    return _signed_in(CLERK_EMAIL, CLERK_PW)


@pytest.fixture(autouse=True)
def _clean_slate(supabase):
    """No quota row, no deny-list row, and CLERK never an admin — before and after.

    Scoped to this module's two principals rather than truncating, so it cannot
    disturb another module's rows in the same run.  BOSS's own role row survives:
    the module-scoped client fixture granted it once.

    `init_db` first because the two routing-table tests below take no client
    fixture, so nothing else in their setup would have built a schema to delete
    from.  It is idempotent (create_all, then a stamp/upgrade that no-ops once at
    head), so the first test through pays for it and the rest do not.
    """
    init_db()

    def _clear() -> None:
        db = SessionLocal()
        try:
            db.query(AccountQuota).filter(
                AccountQuota.owner_key.in_([BOSS, CLERK])).delete(synchronize_session=False)
            db.query(AccountDisabled).filter(
                AccountDisabled.owner_key.in_([BOSS, CLERK])).delete(synchronize_session=False)
            db.query(AccountRole).filter(
                AccountRole.owner_key == CLERK).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    _clear()
    yield
    _clear()


# --------------------------------------------------------------------------- #
# Every admin route, with a body good enough to reach the handler.
#
# The bodies matter: FastAPI validates a request model BEFORE the handler runs,
# so a route swept with a malformed body answers 422 and never reaches
# `require_admin` — the assertion would pass while proving nothing.
# --------------------------------------------------------------------------- #
_ADMIN_ROUTES: list[tuple[str, str, dict]] = [
    ("/api/admin/accounts", "GET", {}),
    ("/api/admin/accounts/{owner_key}/usage", "GET", {}),
    ("/api/admin/accounts/{owner_key}/quota", "PUT",
     {"json": {"monthly_document_cap": 5, "note": "probe"}}),
    ("/api/admin/accounts/{owner_key}/disable", "POST", {"json": {"reason": "probe"}}),
    ("/api/admin/accounts/{owner_key}/enable", "POST", {}),
]
_IDS = [f"{method} {path}" for path, method, _ in _ADMIN_ROUTES]


def _url(template: str, owner_key: str) -> str:
    return template.replace("{owner_key}", owner_key)


def test_every_admin_route_is_swept_here():
    """A new admin route fails this module until someone states who may reach it.

    Same discipline as `tests/test_tenant_isolation.py`: the gap that suite found
    was not a wrong predicate, it was handlers that never called it.
    """
    registered = {
        (route.path, method)
        for route in app.routes
        for method in getattr(route, "methods", ()) or ()
        if getattr(route, "path", "").startswith("/api/admin")
        and method not in ("OPTIONS", "HEAD")
    }
    swept = {(path, method) for path, method, _ in _ADMIN_ROUTES}
    assert registered - swept == set(), (
        f"admin routes exist that this suite does not exercise: {sorted(registered - swept)}")
    assert swept - registered == set(), (
        f"this suite exercises routes the app does not serve: {sorted(swept - registered)}")


def test_no_admin_route_is_job_scoped():
    """Which is why `test_every_job_scoped_route_is_listed_here` does not fire for
    them — it keys on `{job_id}`.  If an admin route ever grows one, that suite
    starts failing until someone states its ownership rule, and this assertion is
    the note explaining why the two suites are not the same sweep."""
    job_scoped = [route.path for route in app.routes
                  if getattr(route, "path", "").startswith("/api/admin")
                  and "{job_id}" in route.path]
    assert job_scoped == [], (
        f"an admin route is job-scoped: {job_scoped}. It must state its ownership rule "
        f"and be listed in tests/test_tenant_isolation.py::_ROUTES.")


# --------------------------------------------------------------------------- #
# A member cannot reach any of it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,method,kwargs", _ADMIN_ROUTES, ids=_IDS)
def test_a_member_is_refused_every_admin_route(member, path, method, kwargs):
    """404, never 403: a 403 confirms the admin surface is there to be found."""
    r = member.request(method, _url(path, CLERK), **kwargs)
    assert r.status_code == 404, (
        f"{method} {path} answered {r.status_code} to a member account: {r.text[:300]}")


@pytest.mark.parametrize("path,method,kwargs", _ADMIN_ROUTES, ids=_IDS)
def test_the_same_routes_answer_the_admin(admin, path, method, kwargs):
    """The control.  Without it a typo in a path passes the sweep above."""
    r = admin.request(method, _url(path, CLERK), **kwargs)
    assert r.status_code == 200, f"{method} {path} answered {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("path,method,kwargs", _ADMIN_ROUTES, ids=_IDS)
def test_an_anonymous_caller_never_reaches_the_role_check(path, method, kwargs):
    """401 from the middleware, not 404 from `require_admin`.

    Worth asserting separately: it means the admin surface leaks nothing to a
    stranger either way — every /api path answers 401 unauthenticated, real or
    not — and it proves the routes are inside the gate rather than relying on
    `require_admin` to be remembered.
    """
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.request(method, _url(path, CLERK), **kwargs)
    assert r.status_code == 401, f"{method} {path} answered {r.status_code} with no session"
    assert r.json()["code"] == "AUTH_REQUIRED"


def test_a_member_cannot_promote_themselves(member):
    """NO HTTP SURFACE WRITES `account_role`.  The first admin cannot be granted
    through a route that requires one, and once the bootstrap is out of band a
    privileged "promote this account" endpoint has no reason to exist — so the
    routing layer must not reference the grant at all.

    Asserted against the source of `app/main.py` rather than against path
    spellings, because `{role}` in the document routes is a declared document
    role and has nothing to do with this one.
    """
    import pathlib

    routing_layer = (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py"
                     ).read_text(encoding="utf-8")
    # The CALL form: the comment block in main.py names the bootstrap script on
    # purpose, and pointing a reader at it is the opposite of the problem.
    for granted in ("grant_admin(", "revoke_admin(", "AccountRole"):
        assert granted not in routing_layer, (
            f"app/main.py references {granted!r} — a route that can mint an administrator "
            f"is a route that does not need an administrator to exist.")

    db = SessionLocal()
    try:
        assert accounts.role_of(db, CLERK) == accounts.MEMBER
    finally:
        db.close()
    # ...and the role is not smuggled in through a body the admin routes accept.
    assert member.request("PUT", _url("/api/admin/accounts/{owner_key}/quota", CLERK),
                          json={"monthly_document_cap": 1, "role": "admin"}
                          ).status_code in (404, 422)


def test_a_role_row_that_is_not_admin_is_not_a_role(admin):
    """`role_of` reads one value and only one.  A typo in the column must not
    grant something — which is why absence and "administrator" and "ADMIN " all
    resolve deliberately rather than by accident."""
    db = SessionLocal()
    try:
        db.add(AccountRole(owner_key=CLERK, role="administrator"))
        db.commit()
        assert accounts.role_of(db, CLERK) == accounts.MEMBER

        row = db.get(AccountRole, CLERK)
        row.role = " Admin "                     # spelling a human would type
        db.commit()
        assert accounts.role_of(db, CLERK) == accounts.ADMIN
    finally:
        db.query(AccountRole).filter(AccountRole.owner_key == CLERK).delete()
        db.commit()
        db.close()


def test_the_empty_principal_is_not_an_account():
    """`job_visible_to` has the equivalent guard, for the reason its docstring
    records: "" is what a caller with no identity looks like, and a row keyed on
    it must never become everybody's."""
    db = SessionLocal()
    try:
        db.add(AccountRole(owner_key="", role="admin"))
        db.commit()
        assert accounts.role_of(db, "") == accounts.MEMBER
        assert accounts.role_of(db, CLERK) == accounts.MEMBER
    finally:
        db.query(AccountRole).filter(AccountRole.owner_key == "").delete()
        db.commit()
        db.close()


# --------------------------------------------------------------------------- #
# How the role reaches a request: a local table read, per request, and nothing else
# --------------------------------------------------------------------------- #
def test_the_role_is_not_a_claim_in_the_session_token():
    """Rejected on purpose.  A claim would be free at request time and stale for
    up to the 24h TTL: granting late is harmless, revoking late is not, and a
    stateless token has no revocation story."""
    token, session = auth.issue_token(BOSS_EMAIL, user_id=BOSS)
    payload, _, _ = token.partition(".")
    claims = json.loads(auth._b64d(payload))
    assert set(claims) == {"sub", "dsp", "iat", "exp"}, (
        f"the session token grew a claim: {sorted(claims)}. A role in here cannot be "
        f"revoked before the token expires.")
    assert not hasattr(session, "role")
    assert "role" not in {f.name for f in __import__("dataclasses").fields(auth.Session)}


def test_the_session_endpoint_reports_the_role_for_the_spa(admin, member):
    """One read per page load, always current — deliberately not a token claim
    labelled "display hint", because a value that looks authoritative eventually
    gets treated as authoritative by someone reading the code later."""
    assert admin.get("/api/auth/session").json()["role"] == "admin"
    assert member.get("/api/auth/session").json()["role"] == "member"


def test_signing_in_reports_the_role_too(supabase):
    """The SPA takes its session from the LOGIN response as well as from
    /api/auth/session, so a role only on the latter would leave a freshly
    signed-in admin without the nav item until they reloaded."""
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    body = client.post("/api/auth/login",
                       json={"username": BOSS_EMAIL, "password": BOSS_PW}).json()
    assert body["role"] == "admin"


@contextmanager
def _statements():
    """Every SQL statement this process issues while the block runs."""
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


def _role_reads(statements: list[str]) -> int:
    return sum(1 for s in statements if "account_role" in s.lower())


def test_reading_the_role_costs_one_primary_key_lookup_and_no_network_call(admin, member,
                                                                          monkeypatch):
    """The two properties the plan chose this design for.

    No network call: our own token is HMAC-verified in-process and the Supabase
    JWT was discarded at login, so a per-request round trip to an external service
    on the path of 1500 reviewers never happens.  httpx's outbound helpers are
    made to raise to prove it (TestClient drives the app through a transport, not
    through these).
    """
    import httpx

    # Two accounts in the universe, so the page-size comparison below is not
    # comparing one row with one row.  Signing in creates no row anywhere — an
    # account becomes visible to this listing by owning something.
    member.post("/api/jobs")

    for name in ("post", "get", "put", "delete", "request", "stream"):
        monkeypatch.setattr(httpx, name, lambda *a, **k: pytest.fail(
            "an admin request made an outbound HTTP call"), raising=False)

    with _statements() as sql:
        small = admin.get("/api/admin/accounts?limit=1")
        assert small.status_code == 200
    one = _role_reads(sql)
    with _statements() as sql:
        big = admin.get("/api/admin/accounts?limit=100")
        assert big.status_code == 200
    hundred = _role_reads(sql)
    assert len(big.json()["accounts"]) > len(small.json()["accounts"]), (
        "the two pages are the same size, so the comparison below proves nothing — "
        "this module creates at least two accounts, so something has gone wrong")

    # One read for `require_admin`, one batched read for the page it returns.
    assert 1 <= one <= 3, (
        f"{one} account_role reads for one admin request:\n"
        + "\n".join(s for s in sql if "account_role" in s.lower()))
    # The number that matters: it does not grow with the page.  A lookup per
    # listed account would put back, per row, the cost this design refused to put
    # on every request — and it is the kind of regression a fixed bound only
    # catches once the test database happens to hold enough accounts.
    assert hundred == one, (
        f"{one} role read(s) for 1 account and {hundred} for a page of 100 — the "
        f"listing is reading account_role once per row")


def test_a_job_route_never_reads_the_role_at_all(member):
    """The reason it is a dependency on the admin router and not middleware, and
    the reason it is not on `principal_dep`: ~1500 concurrent reviewers must not
    pay a lookup per request for a capability that matters on five routes — and a
    role in scope at every job route is a role available to the predicate ADR-001
    says must never see one."""
    with _statements() as sql:
        assert member.get("/api/jobs").status_code == 200
        job_id = member.post("/api/jobs").json()["job_id"]
        assert member.get(f"/api/jobs/{job_id}").status_code == 200
    assert _role_reads(sql) == 0, (
        "a job route read account_role:\n"
        + "\n".join(s for s in sql if "account_role" in s.lower()))


def test_revoking_the_role_takes_effect_on_the_next_request(admin):
    """The whole argument for a table read over a token claim.  Same token, same
    client, no re-login: the next request is already a member's."""
    assert admin.get("/api/admin/accounts").status_code == 200
    db = SessionLocal()
    try:
        assert accounts.revoke_admin(db, BOSS) is True
        db.commit()
        assert admin.get("/api/admin/accounts").status_code == 404
        assert admin.get("/api/auth/session").json()["role"] == "member"
    finally:
        accounts.grant_admin(db, BOSS, granted_by="restored by the test")
        db.commit()
        db.close()
    assert admin.get("/api/admin/accounts").status_code == 200


# --------------------------------------------------------------------------- #
# The predicate this role is not allowed anywhere near
# --------------------------------------------------------------------------- #
def test_job_visible_to_takes_a_job_and_a_principal_and_no_role():
    """Asserted by signature, because the failure mode is additive: someone adds
    `role=None` "just for the admin console" and every caller keeps compiling."""
    params = list(inspect.signature(services.job_visible_to).parameters)
    assert params == ["job", "principal"], (
        f"job_visible_to now takes {params}. ADR-001: the admin/member role must not "
        f"appear in this function, get_job or list_jobs.")


def test_no_ownership_predicate_mentions_a_role():
    """The source of the three functions ADR-001 names, read for the words that
    would mean a bypass had been added."""
    for func in (services.job_visible_to, services.get_job, services.list_jobs):
        source = inspect.getsource(func).lower()
        for word in ("accountrole", "role_of", "is_admin", "accounts."):
            assert word not in source, (
                f"{func.__name__} references {word!r} — ADR-001 forbids a role test in "
                f"the ownership predicate. A support-access feature is an explicit, "
                f"expiring, customer-created GRANT ROW, and amends the ADR.")


def test_an_admin_session_is_never_the_system_principal(admin):
    """`SYSTEM_PRINCIPAL` is the existing bypass, for callers with no HTTP request
    behind them.  An admin route that seemed to need it would be a route doing
    something ADR-001 forbids."""
    db = SessionLocal()
    try:
        assert services.SYSTEM_PRINCIPAL != BOSS
        assert accounts.role_of(db, services.SYSTEM_PRINCIPAL) == accounts.MEMBER
    finally:
        db.close()


def test_the_admin_listing_carries_no_declaration_content(admin, member):
    """The listing returns COUNTS from their own aggregate and never loads a Job
    row to display.  Asserted against a member's job that has a declaration, an
    XML artifact and a .xls — so a listing that started joining job content would
    show up here as those bytes appearing in an admin's response."""
    job_id = member.post("/api/jobs").json()["job_id"]
    member.post(f"/api/jobs/{job_id}/documents/INVOICE",
                files={"file": ("invoice.pdf", PDF, "application/pdf")})
    db = SessionLocal()
    try:
        db.get(Job, job_id).declaration = {"consignee_name": "SECRET-IMPORTER-PVT-LTD"}
        db.add(XmlArtifact(job_id=job_id, declaration_version=1, checksum="admin-probe",
                           xml_bytes=b"<ASYCUDA>SECRET-IMPORTER-PVT-LTD</ASYCUDA>"))
        db.add(BmsArtifact(job_id=job_id, declaration_version=1, checksum="admin-probe",
                           xls_bytes=b"\xd0\xcf\x11\xe0SECRET"))
        db.commit()
    finally:
        db.close()

    body = admin.get("/api/admin/accounts").text
    assert "SECRET-IMPORTER-PVT-LTD" not in body
    assert "invoice.pdf" not in body
    usage = admin.get(f"/api/admin/accounts/{CLERK}/usage").text
    assert "SECRET-IMPORTER-PVT-LTD" not in usage
    assert job_id not in usage, "a job id in an account report is a job the admin can now name"

    row = next(a for a in json.loads(body)["accounts"] if a["owner_key"] == CLERK)
    assert row["jobs"] >= 1 and row["jobs_by_status"], "the COUNT is what an admin does get"


# --------------------------------------------------------------------------- #
# What the listing is, and honestly is not
# --------------------------------------------------------------------------- #
def test_the_listing_says_it_is_not_every_account(admin):
    """Identity lives with the auth provider and enumerating it needs the
    service-role key this process does not hold, so "every account" is not
    something this route can answer.  It says so in the payload rather than only
    in a docstring, because a client that labels this "all users" is labelling
    something else."""
    body = admin.get("/api/admin/accounts").json()
    assert "accounts this deployment has seen" in body["scope"]
    assert set(body) >= {"total", "limit", "offset", "accounts", "rate_version", "scope"}


def test_an_account_with_no_jobs_and_no_usage_is_not_listed(admin):
    stranger = "no5uch00-0000-4000-8000-0000000000ff"
    keys = {row["owner_key"] for row in admin.get("/api/admin/accounts").json()["accounts"]}
    assert stranger not in keys


def test_setting_a_cap_makes_an_account_appear_and_says_it_was_never_seen(admin):
    """Pre-provisioning is legitimate; a typo'd Supabase id is not, and there is
    no user table to tell them apart.  So `seen` is reported and not enforced."""
    stranger = "no5uch00-0000-4000-8000-0000000000ff"
    r = admin.put(f"/api/admin/accounts/{stranger}/quota",
                  json={"monthly_document_cap": 10, "note": "pre-provisioned"})
    assert r.status_code == 200
    assert r.json()["account"]["seen"] is False
    db = SessionLocal()
    try:
        db.query(AccountQuota).filter(
            AccountQuota.owner_key == stranger).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_the_busiest_account_this_month_is_on_the_first_page(admin, member):
    """Ordered by documents extracted this month, not by owner_key: the question
    this route exists to answer is "who is at their limit", and a page of UUIDs in
    lexical order answers neither that nor "who is burning the budget"."""
    db = SessionLocal()
    try:
        for _ in range(3):
            metering.record(db, owner_key=CLERK, provider="mistral", operation="ocr",
                            model="mistral-ocr-latest", calls=1, pages=2)
        db.commit()
    finally:
        db.close()
    rows = admin.get("/api/admin/accounts?limit=5").json()["accounts"]
    assert rows[0]["owner_key"] == CLERK, [r["owner_key"] for r in rows]
    assert rows[0]["documents_this_month"] >= 3


def test_the_listing_agrees_with_the_number_the_quota_gate_enforces(admin, member):
    """A listing that disagreed with `documents_this_month` would be worse than
    no listing: an admin would raise a cap for an account that was not at it."""
    db = SessionLocal()
    try:
        metering.record(db, owner_key=CLERK, provider="mistral", operation="ocr",
                        model="mistral-ocr-latest", calls=1, pages=1)
        db.commit()
        expected = metering.documents_this_month(db, CLERK)
    finally:
        db.close()
    row = next(a for a in admin.get("/api/admin/accounts").json()["accounts"]
               if a["owner_key"] == CLERK)
    assert row["documents_this_month"] == expected


def test_an_unpriced_call_is_reported_as_unknown_not_as_free(admin, member):
    """Units are measured, money is estimated, and the listing does not blur them:
    `unpriced_events` is what says the cost shown is a floor."""
    db = SessionLocal()
    try:
        metering.record(db, owner_key=CLERK, provider="mistral", operation="ocr",
                        model="a-model-nobody-priced", calls=1, pages=1)
        db.commit()
    finally:
        db.close()
    row = next(a for a in admin.get("/api/admin/accounts").json()["accounts"]
               if a["owner_key"] == CLERK)
    assert row["unpriced_events"] >= 1
    assert row["estimated_cost_usd"] is None


def test_the_usage_route_is_the_same_shape_the_account_sees_itself(admin, member):
    mine = member.get("/api/usage").json()
    theirs = admin.get(f"/api/admin/accounts/{CLERK}/usage").json()
    assert set(mine) <= set(theirs), sorted(set(mine) - set(theirs))
    assert theirs["account"]["owner_key"] == CLERK


# --------------------------------------------------------------------------- #
# The sentence the reviewer is shown, end to end
# --------------------------------------------------------------------------- #
def test_an_administrator_can_actually_raise_the_limit(admin, member, monkeypatch):
    """`metering.quota_exceeded` promises a blocked reviewer that extraction
    resumes "at the start of next month, or when an administrator raises the
    limit".  Step 1 gave the limit somewhere to live; this is the whole remedy:

      blocked at the cap -> findable in the admin listing -> raised by a named
      administrator -> not blocked, with the raise attributed on the row.
    """
    from app.domain.enums import DocumentStatus
    from app.models import Document

    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 10_000)

    job_id = member.post("/api/jobs/demo").json()["job_id"]
    doc_id = member.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]
    db = SessionLocal()
    try:
        # The demo seeds documents already extracted; put one back so the route
        # reaches the quota gate rather than "already processed".
        db.get(Document, doc_id).status = DocumentStatus.UPLOADED.value
        db.commit()
    finally:
        db.close()

    # 1. At the cap, and refused before anything is bought.
    admin.put(f"/api/admin/accounts/{CLERK}/quota",
              json={"monthly_document_cap": 1, "note": "trial plan"})
    db = SessionLocal()
    try:
        metering.record(db, owner_key=CLERK, provider="mistral", operation="ocr",
                        model="mistral-ocr-latest", calls=1, pages=2)
        db.commit()
    finally:
        db.close()
    refused = member.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert refused.status_code == 429, refused.text
    assert refused.json()["code"] == "USAGE_LIMIT_REACHED"
    detail = refused.json()["detail"]
    assert "an administrator raises the limit" in detail and "limit of 1" in detail

    # 2. That account is findable — the message names no owner_key, so the admin
    #    has to be able to find it from the usage side.
    rows = admin.get("/api/admin/accounts?limit=5").json()["accounts"]
    at_the_cap = next(r for r in rows if r["owner_key"] == CLERK)
    assert at_the_cap["monthly_document_cap"] == 1
    assert at_the_cap["documents_this_month"] >= 1

    # 3. Raised, by a named administrator.
    raised = admin.put(f"/api/admin/accounts/{CLERK}/quota",
                       json={"monthly_document_cap": 50, "note": "paid plan, ticket 412"})
    assert raised.status_code == 200
    assert raised.json()["account"]["monthly_document_cap"] == 50

    # 4. ...and the reviewer is no longer refused.  Only the quota gate is being
    #    asserted: whatever the extraction itself then does, it is not a 429.
    again = member.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert again.status_code != 429, again.text


def test_the_row_records_who_set_the_cap_and_why(admin):
    """`account_quota.updated_by` shipped in step 1 with no writer at all — a row
    could only be inserted by hand, which is why the column is nullable.  This
    route is the writer, and the row is the audit record: `AuditEvent.job_id` is a
    non-nullable foreign key to `customs_job`, so an account-level action has no
    job to hang an event off."""
    admin.put(f"/api/admin/accounts/{CLERK}/quota",
              json={"monthly_document_cap": 42, "note": "ticket 412"})
    db = SessionLocal()
    try:
        row = db.get(AccountQuota, CLERK)
        assert row.monthly_document_cap == 42
        assert row.note == "ticket 412"
        assert row.updated_by == BOSS_EMAIL, (
            "the row names the human, matching AuditEvent.actor — the stable id goes "
            "on the structured log line")
        assert row.updated_at is not None
    finally:
        db.close()


def test_null_and_zero_and_a_number_stay_three_different_answers(admin):
    """The resolution order step 1 settled, driven through the route this time.
    NULL follows the deployment default, 0 is unlimited, n is n — and an OMITTED
    field is a 422 rather than quietly meaning whichever one this model's author
    happened to pick."""
    db = SessionLocal()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(get_settings(), "usage_monthly_document_cap", 7)

            admin.put(f"/api/admin/accounts/{CLERK}/quota",
                      json={"monthly_document_cap": None, "note": "under discussion"})
            assert db.get(AccountQuota, CLERK) is not None, "the row stays, to carry the note"
            assert metering.resolved_cap(db, CLERK) == 7

            admin.put(f"/api/admin/accounts/{CLERK}/quota", json={"monthly_document_cap": 0})
            db.expire_all()
            assert metering.resolved_cap(db, CLERK) == 0
            assert metering.quota_exceeded(db, CLERK) is None

            admin.put(f"/api/admin/accounts/{CLERK}/quota", json={"monthly_document_cap": 3})
            db.expire_all()
            assert metering.resolved_cap(db, CLERK) == 3
    finally:
        db.close()

    assert admin.put(f"/api/admin/accounts/{CLERK}/quota", json={}).status_code == 422
    assert admin.put(f"/api/admin/accounts/{CLERK}/quota",
                     json={"monthly_document_cap": -1}).status_code == 422


def test_one_accounts_cap_does_not_move_anothers(admin, member):
    """Restated through the route, because the whole point of step 1 was that
    raising one account's limit must not raise everyone's."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(get_settings(), "usage_monthly_document_cap", 9)
        admin.put(f"/api/admin/accounts/{CLERK}/quota", json={"monthly_document_cap": 500})
        db = SessionLocal()
        try:
            assert metering.resolved_cap(db, CLERK) == 500
            assert metering.resolved_cap(db, BOSS) == 9
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Disable / enable — what it stops, and what it honestly does not
# --------------------------------------------------------------------------- #
def test_a_disabled_account_cannot_sign_in_again(admin, supabase):
    admin.post(f"/api/admin/accounts/{CLERK}/disable", json={"reason": "abuse, ticket 91"})

    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": CLERK_EMAIL, "password": CLERK_PW})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "ACCOUNT_DISABLED"
    assert "abuse, ticket 91" in r.json()["detail"]
    assert not r.cookies.get(auth.COOKIE_NAME), "a refused sign-in must not set a session"

    assert admin.post(f"/api/admin/accounts/{CLERK}/enable").json()["was_disabled"] is True
    assert client.post("/api/auth/login",
                       json={"username": CLERK_EMAIL, "password": CLERK_PW}).status_code == 200


def test_the_deny_list_is_consulted_only_after_the_password_is_right(admin, supabase):
    """Otherwise the public login route becomes an account-existence oracle: any
    address someone wants to test would answer "disabled" or "unknown"."""
    admin.post(f"/api/admin/accounts/{CLERK}/disable", json={"reason": "barred"})
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": CLERK_EMAIL, "password": "wrong"})
    assert r.status_code == 401 and r.json()["code"] == "BAD_CREDENTIALS"
    assert "barred" not in r.text


def test_a_disabled_account_cannot_buy_any_more_ocr(admin, member, monkeypatch):
    """The reason the deny-list is read on the extraction route too.  A session
    already issued keeps working until its 24h token expires, so a disable that
    only bit at the next sign-in would do nothing about the account spending this
    deployment's Mistral and OpenAI budget right now — which is the case the
    button exists for."""
    from app.domain.enums import DocumentStatus
    from app.models import Document

    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 10_000)
    job_id = member.post("/api/jobs/demo").json()["job_id"]
    doc_id = member.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]
    db = SessionLocal()
    try:
        db.get(Document, doc_id).status = DocumentStatus.UPLOADED.value
        db.commit()
    finally:
        db.close()

    admin.post(f"/api/admin/accounts/{CLERK}/disable", json={"reason": "runaway spend"})
    r = member.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "ACCOUNT_DISABLED"


def test_a_disabled_admin_loses_the_admin_routes_at_once(admin, member):
    """`require_admin` consults the deny-list as well as the role, so a
    compromised admin session is closed off the account routes without waiting
    for its token to expire."""
    db = SessionLocal()
    try:
        accounts.grant_admin(db, CLERK, granted_by="the test")
        db.commit()
        assert member.get("/api/admin/accounts").status_code == 200
        accounts.disable(db, CLERK, reason="compromised", actor="the test")
        db.commit()
        assert member.get("/api/admin/accounts").status_code == 404
    finally:
        db.query(AccountDisabled).filter(AccountDisabled.owner_key == CLERK).delete()
        db.query(AccountRole).filter(AccountRole.owner_key == CLERK).delete()
        db.commit()
        db.close()


def test_a_disabled_account_keeps_its_existing_session_on_other_routes(admin, member):
    """ASSERTED, not assumed, because it is the limitation of a login-time
    deny-list and the thing an operator must not be surprised by: the session
    already in that browser keeps reading its OWN jobs until the 24h token
    expires.  Closing the window completely means a database read per request for
    every reviewer, which is the cost this design deliberately refuses; rotating
    EASYCUSTOMS_AUTH_SECRET is what ends every open session at once."""
    admin.post(f"/api/admin/accounts/{CLERK}/disable", json={"reason": "documented window"})
    assert member.get("/api/jobs").status_code == 200
    assert member.get("/api/auth/session").status_code == 200


def test_an_admin_cannot_disable_their_own_account(admin):
    """Locking yourself out is undoable only with SQL against a live database, and
    on the `local` provider there is no second admin to undo it."""
    r = admin.post(f"/api/admin/accounts/{BOSS}/disable", json={"reason": "oops"})
    assert r.status_code == 409
    db = SessionLocal()
    try:
        assert db.get(AccountDisabled, BOSS) is None
    finally:
        db.close()
    assert admin.get("/api/admin/accounts").status_code == 200


def test_enabling_an_account_that_was_not_disabled_is_not_an_error(admin):
    """Idempotent on purpose: an admin pressing it twice, or unsure whether the
    first call landed, must not be told something is wrong."""
    r = admin.post(f"/api/admin/accounts/{CLERK}/enable")
    assert r.status_code == 200 and r.json()["was_disabled"] is False


def test_disabling_records_who_did_it_and_why(admin):
    admin.post(f"/api/admin/accounts/{CLERK}/disable", json={"reason": "ticket 91"})
    db = SessionLocal()
    try:
        row = db.get(AccountDisabled, CLERK)
        assert row.reason == "ticket 91" and row.disabled_by == BOSS_EMAIL
        assert row.disabled_at is not None
    finally:
        db.close()


def test_a_disabled_account_with_no_reason_still_gets_a_sentence(admin, supabase):
    """"Refused" with no explanation sends the account holder to reset a password
    that was never the problem."""
    admin.post(f"/api/admin/accounts/{CLERK}/disable")
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": CLERK_EMAIL, "password": CLERK_PW})
    assert r.status_code == 403
    assert r.json()["detail"].strip(), "a refusal with an empty reason is not a refusal anyone can act on"


def test_an_unreadable_deny_list_refuses_the_sign_in(supabase, monkeypatch):
    """FAIL CLOSED, exactly as the login throttle does: a deny-list that cannot be
    read must not admit the account it was written to bar."""
    def boom(db, owner_key):
        raise RuntimeError("no such table: account_disabled")

    monkeypatch.setattr(accounts, "disabled_reason", boom)
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": CLERK_EMAIL, "password": CLERK_PW})
    assert r.status_code == 503, r.text
    assert not r.cookies.get(auth.COOKIE_NAME)


# --------------------------------------------------------------------------- #
# The bootstrap
# --------------------------------------------------------------------------- #
def test_the_first_admin_comes_from_a_script_and_not_from_a_route():
    """`scripts/grant_admin.py` is the documented bootstrap, and it has to keep
    working: it is the only way an administrator exists at all, and the only way
    one is taken back."""
    import importlib.util
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent / "scripts" / "grant_admin.py")
    assert path.exists(), "the documented bootstrap script is gone"
    spec = importlib.util.spec_from_file_location("grant_admin_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)
    # A dry run changes nothing — the same discipline scripts/reassign_job_owner.py
    # uses for the other operation that rewrites who may see what.
    probe = "d1y0000-0000-4000-8000-0000000000fe"
    assert module._grant(probe, apply=False, actor="test") == 0
    db = SessionLocal()
    try:
        assert db.get(AccountRole, probe) is None
    finally:
        db.close()
