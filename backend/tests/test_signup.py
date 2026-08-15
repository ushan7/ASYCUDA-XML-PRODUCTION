"""Self-service registration, email confirmation, and the abuse limiter.

`docs/plan-signup-and-roles.md` step 3.  Four properties, and the second is the
one the plan spends most of its argument on:

  * **A deployment has to SAY it is open.**  `allow_self_signup` is off by
    default, cannot be on under the single-account provider (refused at boot),
    and is the flag the route obeys — which is also the reader
    `tests/test_config_is_consumed.py` requires it to have.
  * **The limiter counts SUCCESSES and shares nothing with the login throttle.**
    That throttle counts failures and a correct password wipes the window; if
    signup shared the key, an attacker would interleave a registration between
    password guesses to reset their guessing budget indefinitely.  Both
    directions of non-interference are asserted directly.
  * **The route is nobody's account-existence oracle.**  Created, already
    registered, created-on-a-misconfigured-project, the provider's own
    per-address rate limit and its delivery failures are one 202 with one body,
    each costing one row of the same limiter budget.  The only refusal whose text
    is forwarded is the caller's own password.  The last two collapsed late —
    signup forwarded the provider's 429 until it was closed here, and the
    delivery-failure half was found while closing it.
  * **A brand-new account gets the deployment default cap and no row.**  Absence
    is the normal state (`models.AccountQuota`); a row written at signup would
    freeze today's default onto every account that ever registers.

Everything here runs against a stubbed provider.  NOTHING in this suite reaches
the real Supabase project, and the confirmation email loop — the one control that
lives in a dashboard rather than in this repository — is verified in staging.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth, metering
from app.config import DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP, Settings, get_settings
from app.database import SessionLocal, init_db
from app.main import _SIGNUP_ACCEPTED, app
from app.models import (AccountDisabled, AccountQuota, AccountRole, AccountSeen,
                        LoginAttempt, SignupAttempt)

# Distinct from every other module's principals: the suite shares one database
# for the whole run.
NEWCOMER = "5127a900-0000-4000-8000-0000000000a1"
NEWCOMER_EMAIL, NEWCOMER_PW = "newcomer@broker.np", "pw-newcomer-long-enough"

GOOD_LOGIN = {"username": "pytest-operator", "password": "pytest-password"}


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = text if text is not None else (
            json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _confirmation_pending(email: str, user_id: str) -> _FakeResponse:
    """What Supabase answers a NEW address when "Confirm email" is on: the user,
    no session, and an identity."""
    return _FakeResponse(200, {"id": user_id, "email": email,
                               "identities": [{"identity_id": "id-1"}],
                               "confirmation_sent_at": "2026-08-13T09:00:00Z"})


def _already_registered(email: str) -> _FakeResponse:
    """Supabase's own anti-enumeration answer with confirmation on: a user
    object with NO identities and an id belonging to nobody."""
    return _FakeResponse(200, {"id": "00000000-0000-0000-0000-000000000000",
                               "email": email, "identities": []})


@pytest.fixture()
def supabase(monkeypatch):
    """auth_provider=supabase with registration open, and the HTTP call stubbed.

    `state.signup` may be replaced with a callable to drive any provider answer;
    the default registers the address and reports a confirmation email.
    """
    import httpx

    init_db()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "supabase")
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(settings, "allow_self_signup", True)

    state = types.SimpleNamespace(calls=[], signups=[], signup=None, fail=None,
                                  registered={NEWCOMER_EMAIL: (NEWCOMER_PW, NEWCOMER)})

    def fake_post(url, *, params=None, headers=None, json=None, timeout=None):
        state.calls.append({"url": url, "json": json})
        if state.fail is not None:
            raise state.fail
        if url.endswith("/auth/v1/signup"):
            state.signups.append(json)
            if state.signup is not None:
                return state.signup(json)
            email = json["email"]
            if email in state.registered:
                return _already_registered(email)
            # Derived from the address, not from a counter: the suite shares one
            # database for the whole run, so an id that repeated between tests
            # would make one test's sign-in look like the next test's.
            user_id = f"new-{email.split('@')[0]}"
            state.registered[email] = (json["password"], user_id)
            return _confirmation_pending(email, user_id)
        # the password grant
        known = state.registered.get(json["email"])
        if known is None or known[0] != json["password"]:
            return _FakeResponse(400, {"error": "invalid_grant"})
        return _FakeResponse(200, {"access_token": "supabase-jwt",
                                   "user": {"id": known[1], "email": json["email"]}})

    monkeypatch.setattr(httpx, "post", fake_post)
    auth.reset_signup_limiter()
    yield state
    auth.reset_signup_limiter()


@pytest.fixture()
def anon():
    """A stranger on the network: no token, no cookie."""
    init_db()
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    return client


def _signup(client, email="stranger@broker.np", password="a-long-enough-password", **kw):
    return client.post("/api/auth/signup",
                       json={"email": email, "password": password}, **kw)


# --------------------------------------------------------------------------- #
# A deployment has to say it is open
# --------------------------------------------------------------------------- #
def test_the_single_account_provider_does_not_offer_registration(anon):
    """501, and nothing reaches the identity provider.  `local` verifies the
    token subject against ONE configured username, so a second account could not
    sign in even if something created it."""
    r = _signup(anon)
    assert r.status_code == 501, r.text
    assert r.json()["code"] == "SIGNUP_UNSUPPORTED"


def test_registration_is_closed_until_the_deployment_opens_it(anon, supabase, monkeypatch):
    """The default under the multi-account provider too.  Opening registration is
    a commercial decision with a vendor bill attached; it is never inherited by
    switching identity provider."""
    monkeypatch.setattr(get_settings(), "allow_self_signup", False)
    r = _signup(anon)
    assert r.status_code == 403 and r.json()["code"] == "SIGNUP_DISABLED"
    assert supabase.signups == [], "a closed deployment must not call the provider at all"


def test_the_flag_is_off_by_default_and_cannot_be_on_under_the_local_provider():
    """Enforced at boot rather than answered 501 for ever: a .env line saying
    registration is open, on a deployment where it silently is not, is the same
    class of defect as a quality gate that never ran."""
    assert Settings(_env_file=None).allow_self_signup is False
    with pytest.raises(Exception) as caught:
        Settings(_env_file=None, allow_self_signup=True)
    assert "AUTH_PROVIDER=supabase" in str(caught.value)


def test_boot_still_refuses_an_uncapped_multi_account_deployment(monkeypatch):
    """Step 1's refusal is NOT re-gated on this flag.  The plan says step 3 may
    "tighten" it by including `allow_self_signup`, but ANDing a condition onto a
    refusal LOOSENS it — an uncapped multi-account deployment is an unbounded
    vendor bill whether or not strangers can register, because everyone who
    already has an account can spend it."""
    for signup_open in (True, False):
        with pytest.raises(Exception) as caught:
            Settings(_env_file=None, auth_provider="supabase",
                     supabase_url="https://p.supabase.co", supabase_anon_key="k",
                     usage_monthly_document_cap=0, allow_self_signup=signup_open)
        assert "unbounded vendor bill" in str(caught.value)


def test_the_availability_route_never_disagrees_with_the_post(anon, supabase, monkeypatch):
    """The sign-in screen has no session and cannot read the gated /api/config,
    so it asks here — and a GET that said "open" while the POST refused would put
    a registration form in front of a user who cannot use it."""
    assert anon.get("/api/auth/signup").json()["enabled"] is True
    assert _signup(anon, email="one@broker.np").status_code == 202

    monkeypatch.setattr(get_settings(), "allow_self_signup", False)
    closed = anon.get("/api/auth/signup").json()
    assert closed["enabled"] is False and closed["detail"]
    assert _signup(anon, email="two@broker.np").status_code == 403


def test_the_availability_route_names_no_provider_and_no_account(anon, supabase):
    body = anon.get("/api/auth/signup").text
    assert set(anon.get("/api/auth/signup").json()) == {"enabled", "detail"}
    for leak in ("supabase", "project.supabase.co", "anon-key", NEWCOMER_EMAIL):
        assert leak not in body.lower()


def test_signup_is_public_and_everything_else_is_still_not(anon, supabase):
    """It has to be — a stranger has no session.  The gate is unchanged for the
    rest: /api/auth/session still answers 401 to the same client."""
    assert _signup(anon, email="public@broker.np").status_code == 202
    assert anon.get("/api/auth/session").status_code == 401


# --------------------------------------------------------------------------- #
# The route contract
# --------------------------------------------------------------------------- #
def test_signup_never_returns_a_session(anon, supabase):
    """202 and no cookie.  Confirmation is what completes a registration, so
    there is no session to issue — and a route that sometimes returns one is a
    route whose clients get it wrong."""
    r = _signup(anon, email="nosession@broker.np")
    assert r.status_code == 202
    assert "token" not in r.text and "access_token" not in r.text
    assert not r.cookies.get(auth.COOKIE_NAME)
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")


def test_a_session_from_the_provider_is_discarded_and_never_forwarded(anon, supabase):
    """With "Confirm email" OFF, Supabase hands back a full session at signup.
    It is dropped here: the anti-abuse case for confirmation rests on a new
    account being unusable until the address is proved."""
    supabase.signup = lambda body: _FakeResponse(200, {
        "access_token": "a-real-supabase-session", "refresh_token": "r",
        "user": {"id": "unconfirmed-1", "email": body["email"],
                 "email_confirmed_at": "2026-08-13T09:00:00Z",
                 "identities": [{"identity_id": "id-1"}]}})
    r = _signup(anon, email="noconfirm@broker.np")
    assert r.status_code == 202
    assert "a-real-supabase-session" not in r.text
    assert not r.cookies.get(auth.COOKIE_NAME)


def test_a_project_with_confirmation_off_is_reported_to_the_operator(anon, supabase, caplog):
    """The primary anti-abuse control lives in a dashboard this repository cannot
    assert, so the code says so the first time it sees the evidence: a session in
    the signup response means unverified addresses can spend the OCR budget."""
    supabase.signup = lambda body: _FakeResponse(200, {
        "access_token": "session", "user": {"id": "u", "email": body["email"],
                                            "identities": [{"identity_id": "i"}]}})
    with caplog.at_level("WARNING"):
        assert _signup(anon, email="warned@broker.np").status_code == 202
    assert any("Confirm email" in r.getMessage() for r in caplog.records), caplog.text


def test_the_body_is_an_email_and_a_password_and_nothing_else(anon, supabase):
    """`extra="forbid"` is the enforcement.  A role, a quota or an owner_key in
    the body is a 422 rather than a field somebody has to remember to ignore."""
    for extra in ({"role": "admin"}, {"owner_key": NEWCOMER},
                  {"monthly_document_cap": 100_000}, {"is_admin": True}):
        r = anon.post("/api/auth/signup",
                      json={"email": "x@broker.np", "password": "a-password", **extra})
        assert r.status_code == 422, f"{extra} was accepted: {r.text[:200]}"
    assert supabase.signups == [], "a rejected body must not reach the provider"


def test_an_already_registered_address_is_answered_exactly_as_a_new_one(anon, supabase):
    """THE ORACLE TEST.  Byte for byte, status and body, or this endpoint becomes
    a way to test whether any address someone is curious about has an account
    here."""
    fresh = _signup(anon, email="brand-new@broker.np")
    auth.reset_signup_limiter()
    taken = _signup(anon, email=NEWCOMER_EMAIL)
    assert fresh.status_code == taken.status_code == 202
    assert fresh.json() == taken.json()
    assert fresh.text == taken.text


def test_an_already_registered_address_is_not_counted_differently(anon, supabase):
    """...and it costs the same limiter budget, or the difference in how many
    tries you have left is the oracle instead."""
    _signup(anon, email=NEWCOMER_EMAIL)
    db = SessionLocal()
    try:
        assert db.query(SignupAttempt).count() == 1
    finally:
        db.close()


def test_a_weak_password_is_the_one_refusal_whose_text_is_forwarded(anon, supabase):
    """It is about what the caller just typed and says nothing about the address
    — and it is forwarded rather than reworded so the DEPLOYMENT'S policy
    (length, leaked-password checking) is what the user is told."""
    supabase.signup = lambda body: _FakeResponse(422, {
        "error_code": "weak_password",
        "msg": "Password should be at least 8 characters."})
    r = _signup(anon, email="weak@broker.np", password="short")
    assert r.status_code == 400 and r.json()["code"] == "WEAK_PASSWORD"
    assert "at least 8 characters" in r.json()["detail"]


def test_a_refused_password_does_not_spend_the_callers_registration_budget(anon, supabase):
    """A user fixing a rejected password must not be locked out for an hour.
    Nothing was created, so nothing is counted — the identity provider's own
    signup rate limit is what bounds refused attempts."""
    supabase.signup = lambda body: _FakeResponse(
        422, {"error_code": "weak_password", "msg": "Password should be at least 8 characters."})
    for _ in range(5):
        assert _signup(anon, email="fumbling@broker.np", password="x").status_code == 400
    db = SessionLocal()
    try:
        assert db.query(SignupAttempt).count() == 0
    finally:
        db.close()
    supabase.signup = None
    assert _signup(anon, email="fumbling@broker.np").status_code == 202


def test_junk_that_is_not_an_address_costs_no_outbound_call(anon, supabase):
    for junk in ("not-an-address", "two@at@signs.np", "@broker.np", "user@", "a b@c.np"):
        r = _signup(anon, email=junk)
        assert r.status_code == 400 and r.json()["code"] == "INVALID_EMAIL"
    assert supabase.signups == []


def test_an_outage_is_502_and_is_not_counted(anon, supabase):
    """The caller did nothing to earn it — and it is not a definite answer about
    the account either, which is why the sentence does not claim the
    registration failed."""
    supabase.fail = OSError("connection refused")
    r = _signup(anon, email="outage@broker.np")
    assert r.status_code == 502 and "server problem" in r.text
    db = SessionLocal()
    try:
        assert db.query(SignupAttempt).count() == 0
    finally:
        db.close()


def test_sign_ups_disabled_at_the_provider_is_our_misconfiguration_not_a_bad_address(
        anon, supabase):
    """This deployment says registration is open and the provider says it is
    closed.  503 rather than 400 says whose problem it is."""
    supabase.signup = lambda body: _FakeResponse(422, {
        "error_code": "signup_disabled", "msg": "Signups not allowed for this instance"})
    r = _signup(anon, email="closed@broker.np")
    assert r.status_code == 503
    assert "configuration problem" in r.json()["detail"]


def test_the_providers_rate_limit_is_NOT_forwarded(anon, supabase):
    """THE ORACLE THIS ROUTE USED TO BE, closed.  GoTrue applies a per-ADDRESS
    cooldown to the mail it sends and only sends for an address that is NEW, so
    its 429 means "a confirmation email just went out for this address" — which
    is "this address was not already registered".  Two requests seconds apart
    read it straight off: 429 for a new address, 202 both times for a registered
    one (obfuscated, no mail, so no cooldown).

    It collapses into the same 202, byte for byte, as `POST /api/auth/password-
    reset` has always done with the identical signal.  Our own limiter is keyed
    on the CALLER and is the only thing on this route that ever says "wait"."""
    supabase.signup = lambda body: _FakeResponse(
        429, {"msg": "For security purposes, you can only request this after 47 seconds."})
    limited = _signup(anon, email="fast@broker.np")
    supabase.signup = None
    normal = _signup(anon, email="unhurried@broker.np")

    assert limited.status_code == normal.status_code == 202
    assert limited.json() == normal.json() == _SIGNUP_ACCEPTED
    assert limited.text == normal.text
    # ...and nothing about the provider's cooldown survives in the envelope.
    assert "Retry-After" not in limited.headers
    assert "47" not in limited.text and "security purposes" not in limited.text


def test_a_delivery_failure_is_not_reported_to_the_caller(anon, supabase, caplog):
    """The SHARPER half of the same trap, and it needs only ONE request.  GoTrue
    attempts a confirmation send only for an address that is new, so with SMTP
    broken a new address is a 5xx and a registered one is still a clean 202 —
    the cleanest existence oracle available on this route.

    It is answered exactly as an accepted registration is, and logged at ERROR,
    which is where an operator sees it and a stranger does not.  The mirror of
    `test_a_delivery_failure_is_not_reported_to_the_caller` in the reset suite."""
    supabase.signup = lambda body: _FakeResponse(
        500, {"error_code": "unexpected_failure", "msg": "Error sending confirmation email"})
    with caplog.at_level("ERROR"):
        broken = _signup(anon, email="undeliverable@broker.np")
    supabase.signup = None
    normal = _signup(anon, email="deliverable@broker.np")

    assert broken.status_code == normal.status_code == 202
    assert broken.json() == normal.json() == _SIGNUP_ACCEPTED
    assert broken.text == normal.text
    assert "SMTP" not in broken.text and "500" not in broken.text
    assert any("did not complete a signup" in rec.getMessage()
               for rec in caplog.records), caplog.text


def test_every_outcome_the_caller_sees_as_202_costs_the_same_budget(
        anon, supabase, monkeypatch):
    """THE BUDGET ORACLE, closed with the status code and not after it.  Making
    the answers identical is only half: if a collapsed 429 or a failed send cost
    no limiter row, how many tries the caller has left would still vary by
    whether the address already existed — the oracle re-entering through the
    budget after the front door was shut.

    One caller per outcome, because three accepted registrations an hour is the
    limit and the point here is the cost of the fourth, not the ceiling."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    cooldown = lambda body: _FakeResponse(                                    # noqa: E731
        429, {"msg": "For security purposes, you can only request this after 9 seconds."})
    undeliverable = lambda body: _FakeResponse(                               # noqa: E731
        500, {"error_code": "unexpected_failure", "msg": "Error sending confirmation email"})

    for ip, provider, email in (("198.51.100.1", None, "created@broker.np"),
                                ("198.51.100.2", None, NEWCOMER_EMAIL),
                                ("198.51.100.3", cooldown, "cooldown@broker.np"),
                                ("198.51.100.4", undeliverable, "undeliverable2@broker.np")):
        supabase.signup = provider
        r = _signup(anon, email=email, headers={"x-forwarded-for": ip})
        assert r.status_code == 202, f"{ip}: {r.text[:200]}"
        assert _signup_rows(ip) == 1, (
            f"an outcome answered 202 cost {_signup_rows(ip)} rows, not one — the "
            f"remaining-tries count is an account-existence oracle again")


def test_a_rejected_anon_key_is_an_outage_and_NOT_a_202(anon, supabase):
    """The limit of the collapse, asserted so it is not widened by accident.
    401/403 is OUR key or OUR project — identical for every address, so it says
    nothing about any of them — and it is a misconfiguration an operator must see
    rather than one to swallow behind a cheerful 202."""
    supabase.signup = lambda body: _FakeResponse(401, {"message": "Invalid API key"})
    assert _signup(anon, email="badkey@broker.np").status_code == 502
    assert _signup_rows() == 0


def test_a_200_we_cannot_read_is_not_a_registration_we_assert(anon, supabase):
    supabase.signup = lambda body: _FakeResponse(200, None, text="<html>gateway</html>")
    assert _signup(anon, email="unreadable@broker.np").status_code == 502


# --------------------------------------------------------------------------- #
# The limiter: it counts successes, and it is not the login throttle
# --------------------------------------------------------------------------- #
def _signup_rows(client_key: str | None = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(SignupAttempt)
        if client_key:
            q = q.filter(SignupAttempt.client_key == client_key)
        return q.count()
    finally:
        db.close()


def test_the_hourly_limit_counts_accepted_registrations(anon, supabase):
    """The property the login throttle cannot provide: ten thousand VALID
    registrations record zero failures and would never be slowed."""
    for i in range(auth._SIGNUP_MAX_PER_HOUR):
        assert _signup(anon, email=f"burst{i}@broker.np").status_code == 202
    blocked = _signup(anon, email="burst-over@broker.np")
    assert blocked.status_code == 429 and blocked.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert int(blocked.headers["Retry-After"]) > 0
    assert supabase.signups[-1]["email"] == "burst2@broker.np", (
        "a limited signup must be refused BEFORE the provider is called")


def test_the_daily_limit_catches_a_script_that_waits_out_the_hour(anon, supabase):
    """Two windows because they answer different questions: the hourly one stops
    a burst, the daily one stops a patient script spacing its registrations an
    hour apart."""
    db = SessionLocal()
    try:
        for hours in range(2, 22, 2):                    # 10 rows, none within the hour
            db.add(SignupAttempt(client_key="testclient",
                                 created_at=datetime.now(timezone.utc)
                                 - timedelta(hours=hours)))
        db.commit()
        assert auth.signup_retry_after(db, "testclient") > 0
    finally:
        db.close()
    assert _signup(anon, email="patient@broker.np").status_code == 429


def test_callers_are_limited_independently(anon, supabase, monkeypatch):
    """The same reasoning as the login throttle's: one caller's registrations
    must not stop a different caller's, or the limiter is a denial of service
    built out of a security control."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    noisy = {"x-forwarded-for": "203.0.113.7"}
    quiet = {"x-forwarded-for": "203.0.113.8"}
    for i in range(auth._SIGNUP_MAX_PER_HOUR):
        assert _signup(anon, email=f"noisy{i}@broker.np", headers=noisy).status_code == 202
    assert _signup(anon, email="noisy-over@broker.np", headers=noisy).status_code == 429
    assert _signup(anon, email="quiet@broker.np", headers=quiet).status_code == 202


def test_an_unreadable_counter_refuses_the_signup(anon, supabase, monkeypatch):
    """FAIL CLOSED, exactly as the login throttle does.  An unreadable counter
    that answered "go ahead" would turn a database fault into unlimited account
    creation, and every account created can spend real money at the vendors."""
    def _explode(db, client):
        raise auth.SignupLimitUnavailable("counter is gone")

    monkeypatch.setattr(auth, "signup_retry_after", _explode)
    r = _signup(anon, email="unreadable-counter@broker.np")
    assert r.status_code == 503
    assert supabase.signups == [], "a signup was sent while the limiter was blind"


def test_recording_prunes_to_the_LONGEST_window(anon, supabase):
    """Pruning at the hourly window would delete the rows the daily limit is made
    of, and the daily limit would quietly stop existing — which is exactly the
    trap a `kind` column on `login_attempt` walks into, since that table prunes
    at five minutes."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.add(SignupAttempt(client_key="aged-out", created_at=now - timedelta(hours=25)))
        db.add(SignupAttempt(client_key="still-counting", created_at=now - timedelta(hours=2)))
        db.commit()
        auth.record_signup(db, "fresh")
        remaining = {r.client_key for r in db.query(SignupAttempt).all()}
    finally:
        db.close()
    assert remaining == {"still-counting", "fresh"}


def test_a_failed_counter_write_does_not_undo_a_registration(anon, supabase):
    """By the time it is called the provider has already created the account and
    this app cannot roll that back, so a failed count must not tell the caller
    their registration failed."""
    db = SessionLocal()
    db.close()                                   # a closed session: writes fail
    auth.record_signup(db, "some-caller")        # must not raise


# --------------------------------------------------------------------------- #
# Non-interference with the login throttle — asserted in both directions
# --------------------------------------------------------------------------- #
def test_a_successful_signup_does_not_clear_the_login_failure_window(anon, supabase):
    """THE ATTACK THE PLAN NAMES.  A correct password calls `clear_failures`; if a
    signup shared that key and cleared on success, an attacker would interleave a
    registration between password guesses and guess for ever."""
    for _ in range(auth._MAX_FAILURES - 1):
        anon.post("/api/auth/login", json={"username": NEWCOMER_EMAIL, "password": "guess"})
    assert _signup(anon, email="interleaved@broker.np").status_code == 202
    blocked = anon.post("/api/auth/login",
                        json={"username": NEWCOMER_EMAIL, "password": "guess"})
    assert blocked.status_code == 401                     # the tenth failure lands
    after = anon.post("/api/auth/login", json={"username": NEWCOMER_EMAIL,
                                               "password": NEWCOMER_PW})
    assert after.status_code == 429, (
        "a signup reset the login failure window — the two limiters are sharing state")


def test_a_signup_writes_nothing_into_the_login_failure_window(anon, supabase):
    before = _login_rows()
    assert _signup(anon, email="separate@broker.np").status_code == 202
    assert _login_rows() == before


def test_signups_do_not_count_against_the_login_throttle(anon, supabase):
    """The reverse direction: registering repeatedly must not lock the same
    caller out of signing in."""
    for i in range(auth._SIGNUP_MAX_PER_HOUR):
        _signup(anon, email=f"login-after{i}@broker.np")
    r = anon.post("/api/auth/login", json={"username": NEWCOMER_EMAIL,
                                           "password": NEWCOMER_PW})
    assert r.status_code == 200, r.text


def test_a_successful_login_does_not_clear_the_signup_window(anon, supabase):
    """`clear_failures` deletes `login_attempt` rows and can reach nothing else.
    If it could, signing in would be a way to buy another three registrations."""
    for i in range(auth._SIGNUP_MAX_PER_HOUR):
        assert _signup(anon, email=f"cleared{i}@broker.np").status_code == 202
    assert anon.post("/api/auth/login", json={"username": NEWCOMER_EMAIL,
                                              "password": NEWCOMER_PW}).status_code == 200
    assert _signup(anon, email="after-login@broker.np").status_code == 429


def test_the_suites_own_throttle_reset_cannot_empty_the_signup_window(anon, supabase):
    """conftest calls `auth.reset_throttle()` around EVERY test.  A shared table
    would mean the signup limiter was emptied ~2000 times a run and no test could
    ever observe it holding."""
    assert _signup(anon, email="survives@broker.np").status_code == 202
    auth.reset_throttle()
    assert _signup_rows() == 1


def test_the_two_limiters_are_two_tables():
    """Stated as a property, because "we will remember not to share the key" is
    not one."""
    assert SignupAttempt.__tablename__ != LoginAttempt.__tablename__
    assert auth._SIGNUP_DAY_SECONDS > auth._FAILURE_WINDOW_SECONDS


def _login_rows() -> int:
    db = SessionLocal()
    try:
        return db.query(LoginAttempt).count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# What a brand-new account is allowed
# --------------------------------------------------------------------------- #
def test_a_brand_new_account_inherits_the_multi_account_default_cap(anon, supabase):
    """Step 1 made the deployment default per PROVIDER; this is the assertion
    that a self-registered account actually lands on a sane number.  200 documents
    a month, from `DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP`, with no row anywhere."""
    assert _signup(anon, email="capped@broker.np").status_code == 202
    db = SessionLocal()
    try:
        _, user_id = supabase.registered["capped@broker.np"]
        assert db.get(AccountQuota, user_id) is None
        assert metering.resolved_cap(db, user_id) == DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP == 200
        assert metering.quota_exceeded(db, user_id) is None      # nothing spent yet
    finally:
        db.close()


def test_the_new_account_sees_that_cap_the_moment_it_signs_in(anon, supabase):
    """Through the route the account itself reads, because a cap it cannot see is
    a cap it will only discover by being refused."""
    _signup(anon, email="sees-cap@broker.np", password="a-long-enough-password")
    r = anon.post("/api/auth/login", json={"username": "sees-cap@broker.np",
                                           "password": "a-long-enough-password"})
    assert r.status_code == 200, r.text
    anon.headers["Authorization"] = f"Bearer {r.json()['token']}"
    assert anon.get("/api/usage").json()["monthly_document_cap"] == 200


def test_signup_writes_no_row_this_app_owns(anon, supabase):
    """ABSENCE IS THE NORMAL STATE.  A quota row written at signup would freeze
    today's default onto every account that ever registers, so it could never be
    changed for the accounts that already exist — and it would make signup depend
    on a second write that can fail AFTER the provider has created the account, a
    partial state this app cannot roll back because it does not own the account.
    """
    db = SessionLocal()
    try:
        before = {model: db.query(model).count()
                  for model in (AccountQuota, AccountRole, AccountDisabled, AccountSeen)}
    finally:
        db.close()

    assert _signup(anon, email="no-rows@broker.np").status_code == 202

    db = SessionLocal()
    try:
        after = {model: db.query(model).count()
                 for model in (AccountQuota, AccountRole, AccountDisabled, AccountSeen)}
        assert after == before, "signup wrote a row into a table this app owns"
        assert db.query(SignupAttempt).count() == 1, "the limiter row is the only write"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# "Did X register?" — the question step 2 left open, and how far it is closed
# --------------------------------------------------------------------------- #
def test_an_account_that_has_only_signed_in_is_visible_to_the_administrator(anon, supabase):
    """Step 2's listing is the union of the `owner_key` values in this app's own
    tables, so an account that owned nothing was invisible.  With registration
    open that becomes "did this person get in?", asked on day one and answerable
    by nobody.  A sign-in record closes it."""
    _signup(anon, email="only-signed-in@broker.np", password="a-long-enough-password")
    assert anon.post("/api/auth/login",
                     json={"username": "only-signed-in@broker.np",
                           "password": "a-long-enough-password"}).status_code == 200
    _, user_id = supabase.registered["only-signed-in@broker.np"]

    db = SessionLocal()
    try:
        assert user_id in accounts.known_owner_keys(db)
        row = next(a for a in accounts.list_accounts(db, limit=200)["accounts"]
                   if a["owner_key"] == user_id)
        # The label an admin needs to act on a support ticket: the account has no
        # job, so the audit trail could never have supplied one.
        assert row["display_label"] == "only-signed-in@broker.np"
        assert row["label_source"] == "sign-in"
        assert row["last_sign_in"] and row["jobs"] == 0
        # ...and a cap set for it is not a cap for nobody.
        assert accounts.account_detail(db, user_id)["seen"] is True
    finally:
        db.close()


def test_an_account_that_registered_and_never_signed_in_is_still_invisible(anon, supabase):
    """THE HONEST LIMIT of the position taken here, asserted rather than left to
    be discovered.  The record is written at sign-in, never from a signup
    response: with confirmation on, the provider answers an already-registered
    address with an obfuscated user whose id belongs to nobody, so a row written
    there could be keyed on a fabrication and a stranger could fill the table
    with them.  "Registered but never confirmed" is therefore not visible here —
    which is the same fact as "the confirmation link was never followed"."""
    assert _signup(anon, email="never-arrived@broker.np").status_code == 202
    _, user_id = supabase.registered["never-arrived@broker.np"]
    db = SessionLocal()
    try:
        assert user_id not in accounts.known_owner_keys(db)
    finally:
        db.close()


def test_the_sign_in_record_never_fails_a_sign_in(anon, supabase, monkeypatch):
    """Best effort, and it must stay that way: a caller who has proved their
    password is not refused because a metadata upsert lost a race with a second
    tab.  Driven through the REAL function against a session that cannot write,
    so what is asserted is the try/except in `record_sign_in` and not a stub."""
    broken = SessionLocal()
    broken.close()                                   # a closed session: writes fail
    accounts.record_sign_in(broken, NEWCOMER, display=NEWCOMER_EMAIL)   # must not raise

    # ...and a live sign-in whose record cannot be written is still a 200 with a
    # token.  Only this table is broken — the deny-list read just above it in the
    # handler is untouched, so what is being asserted is this write and not the
    # handler's other fail-closed arms.
    class _NotATable:
        pass

    monkeypatch.setattr(accounts, "AccountSeen", _NotATable)
    r = anon.post("/api/auth/login", json={"username": NEWCOMER_EMAIL,
                                           "password": NEWCOMER_PW})
    assert r.status_code == 200 and r.json()["token"], r.text


def test_signing_in_again_updates_the_label_and_not_the_first_sighting(anon, supabase):
    """An email can change; ownership does not change with it, and neither does
    when this deployment first saw the account."""
    db = SessionLocal()
    try:
        accounts.record_sign_in(db, NEWCOMER, display="old@broker.np")
        first = db.get(AccountSeen, NEWCOMER).first_seen_at
        accounts.record_sign_in(db, NEWCOMER, display="new@broker.np")
        db.expire_all()
        row = db.get(AccountSeen, NEWCOMER)
        assert row.display == "new@broker.np"
        assert row.first_seen_at == first
        assert row.last_seen_at >= first
    finally:
        db.query(AccountSeen).filter(AccountSeen.owner_key == NEWCOMER).delete()
        db.commit()
        db.close()


def test_an_empty_principal_never_becomes_an_account(anon, supabase):
    """`job_visible_to` has the equivalent guard: "" is what a caller with no
    identity looks like, and a row keyed on it must never become everybody's."""
    db = SessionLocal()
    try:
        accounts.record_sign_in(db, "", display="nobody@broker.np")
        assert db.get(AccountSeen, "") is None
    finally:
        db.close()
