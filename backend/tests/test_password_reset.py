"""Password reset — the link request, the confirm, and the oracle they must not be.

`docs/plan-signup-and-roles.md` step 4.  Four properties:

  * **One 202 for every outcome, including the provider's 429.**  This is where
    the route is stricter than signup, and deliberately: GoTrue applies a
    per-ADDRESS cooldown to the mail it sends, so forwarding that status lets a
    prober ask twice seconds apart and read "an email actually went out for this
    address" off the difference.  Our limiter is keyed on the CALLER, so it is the
    only thing that ever tells anybody to wait.
  * **The budget is not an oracle either.**  A request costs the same whatever the
    provider answered, or "how many tries do I have left" says what the status
    code refused to.
  * **A third limiter, sharing state with neither of the other two.**  Resets must
    not exhaust a caller's registration budget and registrations must not stop
    somebody recovering an account; both directions are asserted, as is the fact
    that neither suite's reset hook can empty this window.
  * **No session, ever.**  Not from the request half, not from the confirm half,
    and no `account_seen` row — that record is proof-of-sign-in, and a reset is
    not one.

Everything here runs against a stubbed provider.  NOTHING in this suite reaches
the real Supabase project, and the email loop itself — the half that lives in a
dashboard rather than in this repository — cannot be verified from here.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import accounts, auth
from app.config import Settings, get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import (AccountQuota, AccountRole, AccountSeen, LoginAttempt,
                        PasswordResetAttempt, SignupAttempt)

# Distinct from every other module's principals: the suite shares one database
# for the whole run.
HOLDER = "5127a900-0000-4000-8000-0000000000b1"
HOLDER_EMAIL, HOLDER_PW = "holder@broker.np", "pw-holder-long-enough"

# A JWT-shaped string. Never verified here — Supabase decides what is real; this
# only has to survive the route's shape check.
RECOVERY = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJob2xkZXIifQ.c2lnbmF0dXJl"


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


@pytest.fixture()
def supabase(monkeypatch):
    """auth_provider=supabase with the HTTP calls stubbed.

    `state.recover` and `state.update` may be replaced with callables to drive any
    provider answer; the defaults accept the request and accept the new password.

    Note what is NOT set here: `allow_self_signup`.  Password reset is gated on
    the provider alone, and a fixture that quietly opened registration would hide
    the fact.
    """
    import httpx

    init_db()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "supabase")
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")

    state = types.SimpleNamespace(recovers=[], updates=[], recover=None, update=None,
                                  fail=None)

    def fake_post(url, *, params=None, headers=None, json=None, timeout=None):
        if state.fail is not None:
            raise state.fail
        if url.endswith("/auth/v1/recover"):
            state.recovers.append({"json": json, "headers": headers})
            if state.recover is not None:
                return state.recover(json)
            return _FakeResponse(200, {})
        if url.endswith("/auth/v1/token"):
            # The password grant. Nothing here signs in successfully — this suite
            # is about the routes that exist BECAUSE somebody cannot — but the
            # non-interference tests drive real failed sign-ins through it, and a
            # sign-in that 502s would never reach the failure counter.
            return _FakeResponse(400, {"error": "invalid_grant"})
        raise AssertionError(f"unexpected POST to {url}")

    def fake_put(url, *, headers=None, json=None, timeout=None):
        if state.fail is not None:
            raise state.fail
        if url.endswith("/auth/v1/user"):
            state.updates.append({"json": json, "headers": headers})
            if state.update is not None:
                return state.update(json)
            return _FakeResponse(200, {"id": HOLDER, "email": HOLDER_EMAIL})
        raise AssertionError(f"unexpected PUT to {url}")

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "put", fake_put)
    auth.reset_password_reset_limiter()
    yield state
    auth.reset_password_reset_limiter()


@pytest.fixture()
def anon():
    """A stranger on the network: no token, no cookie."""
    init_db()
    client = TestClient(app, raise_server_exceptions=False)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    return client


def _ask(client, email=HOLDER_EMAIL, **kw):
    return client.post("/api/auth/password-reset", json={"email": email}, **kw)


def _confirm(client, token=RECOVERY, password="a-long-enough-new-password", **kw):
    return client.post("/api/auth/password-reset/confirm",
                       json={"access_token": token, "password": password}, **kw)


def _reset_rows(client_key: str | None = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(PasswordResetAttempt)
        if client_key:
            q = q.filter(PasswordResetAttempt.client_key == client_key)
        return q.count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Which deployments offer it, and which cannot
# --------------------------------------------------------------------------- #
def test_the_single_account_provider_does_not_offer_reset(anon):
    """501 on all three, and nothing reaches an identity provider.  `local` is one
    account whose password IS backend/.env; there is no address to mail and the
    remedy is editing that file."""
    assert _ask(anon).status_code == 501
    assert _ask(anon).json()["code"] == "PASSWORD_RESET_UNSUPPORTED"
    assert _confirm(anon).status_code == 501
    assert anon.get("/api/auth/password-reset").json()["enabled"] is False


def test_reset_is_available_with_registration_CLOSED(anon, supabase):
    """THE DEPARTURE FROM THE PLAN'S SHAPE, asserted rather than described.  A
    deployment that creates accounts by hand still has users who forget passwords,
    and `allow_self_signup` is off by DEFAULT — gating reset on it would switch
    reset off almost everywhere."""
    monkeypatch_off = get_settings()
    assert monkeypatch_off.allow_self_signup is False, "the fixture must not open signup"
    assert anon.get("/api/auth/signup").json()["enabled"] is False
    assert anon.get("/api/auth/password-reset").json()["enabled"] is True
    assert _ask(anon).status_code == 202
    assert len(supabase.recovers) == 1


def test_the_availability_route_never_disagrees_with_the_post(anon, supabase, monkeypatch):
    """One implementation behind both (`main._password_reset_refusal`), so a GET
    that said "open" while the POST answered 501 could not happen."""
    assert anon.get("/api/auth/password-reset").json()["enabled"] is True
    assert _ask(anon, email="agrees@broker.np").status_code == 202

    monkeypatch.setattr(get_settings(), "auth_provider", "local")
    closed = anon.get("/api/auth/password-reset").json()
    assert closed["enabled"] is False and closed["detail"]
    assert _ask(anon, email="agrees2@broker.np").status_code == 501


def test_the_availability_route_names_no_provider_and_no_account(anon, supabase):
    body = anon.get("/api/auth/password-reset").text
    assert set(anon.get("/api/auth/password-reset").json()) == {"enabled", "detail"}
    for leak in ("supabase", "project.supabase.co", "anon-key", HOLDER_EMAIL):
        assert leak not in body.lower()


def test_both_halves_are_public_and_everything_else_is_still_not(anon, supabase):
    """They have to be — somebody who cannot sign in is the only person who needs
    either.  The gate is unchanged for the rest."""
    assert _ask(anon, email="public@broker.np").status_code == 202
    assert _confirm(anon).status_code == 200
    assert anon.get("/api/auth/session").status_code == 401
    assert anon.get("/api/config").status_code == 401


# --------------------------------------------------------------------------- #
# The request half is nobody's oracle
# --------------------------------------------------------------------------- #
def test_an_unknown_address_is_answered_exactly_as_a_known_one(anon, supabase):
    """THE ORACLE TEST.  Byte for byte, or this endpoint becomes a way to test
    whether any address someone is curious about has an account here."""
    known = _ask(anon, email=HOLDER_EMAIL)
    unknown = _ask(anon, email="nobody-at-all@broker.np")
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert known.text == unknown.text


def test_the_providers_address_cooldown_is_NOT_forwarded(anon, supabase):
    """THE DIFFERENCE FROM SIGNUP, and the reason for it.  GoTrue's 429 on this
    endpoint means "an email was recently sent to THIS address" — forwarding it
    lets a prober ask twice seconds apart and read account existence off the
    second answer.  It collapses into the same 202 as everything else."""
    supabase.recover = lambda body: _FakeResponse(
        429, {"msg": "For security purposes, you can only request this after 47 seconds."})
    limited = _ask(anon, email="cooldown@broker.np")
    supabase.recover = None
    normal = _ask(anon, email="fresh@broker.np")
    assert limited.status_code == normal.status_code == 202
    assert limited.json() == normal.json()
    assert "Retry-After" not in limited.headers
    assert "47" not in limited.text


def test_a_delivery_failure_is_not_reported_to_the_caller(anon, supabase, caplog):
    """The sharpest version of the same trap: Supabase only ATTEMPTS a send for an
    address that exists, so a 500 `error_sending_recovery_email` surfaced to the
    caller would be the cleanest oracle in the app.  It is logged at ERROR — where
    an operator sees it and a stranger does not — and answered 202."""
    supabase.recover = lambda body: _FakeResponse(
        500, {"error_code": "error_sending_recovery_email", "msg": "SMTP refused"})
    with caplog.at_level("ERROR"):
        r = _ask(anon, email="undeliverable@broker.np")
    assert r.status_code == 202
    assert "SMTP" not in r.text and "500" not in r.text
    assert any("did not send a password-reset email" in rec.getMessage()
               for rec in caplog.records), caplog.text


def test_a_refusal_from_the_provider_is_also_202(anon, supabase):
    """400 / 422 for an address GoTrue will not accept is still one answer: which
    addresses a provider considers deliverable is a map of who is a customer."""
    supabase.recover = lambda body: _FakeResponse(
        400, {"error_code": "validation_failed", "msg": "Unable to validate email address"})
    assert _ask(anon, email="refused@broker.np").status_code == 202


def test_junk_that_is_not_an_address_costs_no_outbound_call(anon, supabase):
    """Safe to be specific about: a syntactic check cannot tell a registered
    address from an unregistered one."""
    for junk in ("not-an-address", "two@at@signs.np", "@broker.np", "user@", "a b@c.np"):
        r = _ask(anon, email=junk)
        assert r.status_code == 400 and r.json()["code"] == "INVALID_EMAIL"
    assert supabase.recovers == []
    assert _reset_rows() == 0, "junk spent a caller's reset budget"


def test_the_body_is_an_email_and_nothing_else(anon, supabase):
    for extra in ({"password": "x"}, {"owner_key": HOLDER}, {"redirect_to": "https://evil.np"}):
        r = anon.post("/api/auth/password-reset",
                      json={"email": HOLDER_EMAIL, **extra})
        assert r.status_code == 422, f"{extra} was accepted: {r.text[:200]}"
    assert supabase.recovers == []


def test_a_disabled_account_can_still_ask_and_still_cannot_sign_in(anon, supabase):
    """The deny-list is NOT consulted here, on purpose: refusing a barred address
    differently is the oracle again.  It is consulted where it matters — at
    sign-in — so a reset buys a disabled account nothing."""
    db = SessionLocal()
    try:
        accounts.disable(db, HOLDER, reason="testing", actor="pytest-admin")
        db.commit()
    finally:
        db.close()
    try:
        assert _ask(anon, email=HOLDER_EMAIL).status_code == 202
        assert _confirm(anon).status_code == 200
        db = SessionLocal()
        try:
            assert accounts.disabled_reason(db, HOLDER), "the bar was lifted by a reset"
        finally:
            db.close()
    finally:
        db = SessionLocal()
        try:
            accounts.enable(db, HOLDER)
            db.commit()
        finally:
            db.close()


def test_an_outage_is_502_and_is_not_counted(anon, supabase):
    """The caller did nothing to earn it, and an outage is the same answer for
    every address — so declining to count it tells a prober nothing."""
    supabase.fail = OSError("connection refused")
    r = _ask(anon, email="outage@broker.np")
    assert r.status_code == 502 and "server problem" in r.text
    assert _reset_rows() == 0


def test_a_rejected_anon_key_is_an_outage_not_a_202(anon, supabase):
    """401/403 is OUR key or OUR project, identical for every address, so it says
    nothing about any of them — and it is a misconfiguration an operator must see
    rather than one to swallow behind a cheerful 202."""
    supabase.recover = lambda body: _FakeResponse(401, {"message": "Invalid API key"})
    assert _ask(anon, email="badkey@broker.np").status_code == 502


def test_no_session_is_ever_issued_by_the_request_half(anon, supabase):
    r = _ask(anon)
    assert r.status_code == 202
    assert "token" not in r.text and "access_token" not in r.text
    assert not r.cookies.get(auth.COOKIE_NAME)
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")


# --------------------------------------------------------------------------- #
# The redirect, which is configuration and never a request
# --------------------------------------------------------------------------- #
def test_no_redirect_is_sent_when_none_is_configured(anon, supabase):
    """The normal state.  Omitting it lets Supabase use the project's Site URL,
    which is right whenever one deployment owns one project — and it means this
    step changes nothing in a dashboard."""
    assert get_settings().password_reset_redirect_url == ""
    _ask(anon)
    assert "redirect_to" not in supabase.recovers[-1]["json"]


def test_a_configured_redirect_is_sent_and_comes_only_from_config(anon, supabase,
                                                                  monkeypatch):
    """A caller-supplied redirect would mail a recovery token wherever the caller
    asked.  `extra="forbid"` already refuses one in the body; this asserts the
    value that IS sent came from the deployment."""
    monkeypatch.setattr(get_settings(), "password_reset_redirect_url",
                        "https://declare.example.np")
    _ask(anon)
    assert supabase.recovers[-1]["json"]["redirect_to"] == "https://declare.example.np"


def test_a_redirect_that_supabase_would_silently_ignore_is_refused_at_boot():
    """Supabase falls back to Site URL without saying so for anything it cannot
    parse, so a typo does not fail — it quietly mails every user a link to another
    origin."""
    for bad in ("declare.example.np", "/reset", "javascript:alert(1)"):
        with pytest.raises(Exception) as caught:
            Settings(_env_file=None, password_reset_redirect_url=bad)
        assert "absolute http" in str(caught.value)
    assert Settings(_env_file=None,
                    password_reset_redirect_url="https://ok.example.np").\
        password_reset_redirect_url == "https://ok.example.np"


# --------------------------------------------------------------------------- #
# The confirm half — honest, because the only secret is the caller's own token
# --------------------------------------------------------------------------- #
def test_a_valid_token_sets_the_password_and_returns_no_session(anon, supabase):
    r = _confirm(anon)
    assert r.status_code == 200 and r.json()["code"] == "PASSWORD_UPDATED"
    assert "token" not in r.text and "access_token" not in r.text
    assert not r.cookies.get(auth.COOKIE_NAME)
    assert auth.COOKIE_NAME not in r.headers.get("set-cookie", "")
    # ...and the token went to Supabase as a Bearer, with the anon key alongside.
    sent = supabase.updates[-1]
    assert sent["headers"]["Authorization"] == f"Bearer {RECOVERY}"
    assert sent["headers"]["apikey"] == "anon-key"
    assert sent["json"] == {"password": "a-long-enough-new-password"}


def test_the_account_is_decided_by_the_token_and_never_by_the_body(anon, supabase):
    """Nothing in the request names an account.  A body that tried to would be a
    422, and there is no field for it even to try."""
    for extra in ({"email": "victim@broker.np"}, {"owner_key": HOLDER},
                  {"user_id": HOLDER}, {"role": "admin"}):
        r = anon.post("/api/auth/password-reset/confirm",
                      json={"access_token": RECOVERY, "password": "a-password", **extra})
        assert r.status_code == 422, f"{extra} was accepted: {r.text[:200]}"
    assert supabase.updates == []


def test_an_expired_or_spent_token_is_refused_with_one_sentence(anon, supabase):
    """Expired, already used, or never ours: the remedy is the same and which it
    was is not something the holder of a dead link can act on."""
    for status in (401, 403):
        supabase.update = lambda body, s=status: _FakeResponse(
            s, {"error_code": "otp_expired", "msg": "Token has expired or is invalid"})
        r = _confirm(anon)
        assert r.status_code == 400 and r.json()["code"] == "INVALID_TOKEN"
        assert "expired or has already been used" in r.json()["detail"]


def test_a_weak_new_password_is_the_one_refusal_whose_text_is_forwarded(anon, supabase):
    """It is about what the caller just typed and names no account — and it is
    forwarded rather than reworded so the DEPLOYMENT'S policy is what they read."""
    supabase.update = lambda body: _FakeResponse(422, {
        "error_code": "weak_password",
        "msg": "Password should be at least 12 characters."})
    r = _confirm(anon, password="short")
    assert r.status_code == 400 and r.json()["code"] == "WEAK_PASSWORD"
    assert "at least 12 characters" in r.json()["detail"]


def test_a_token_that_is_not_even_token_shaped_costs_no_outbound_call(anon, supabase):
    for junk in ("not-a-token", "one.part", ".b.c", "a..c", "   "):
        r = _confirm(anon, token=junk)
        assert r.status_code == 400 and r.json()["code"] == "INVALID_TOKEN"
    assert supabase.updates == []


def test_a_provider_rate_limit_on_the_confirm_IS_passed_on(anon, supabase):
    """Forwarded here, unlike in the request half, and the difference does not
    generalise: this caller already holds a valid token for a specific account, so
    a wait tells them nothing they did not already know."""
    supabase.update = lambda body: _FakeResponse(
        429, {"msg": "For security purposes, you can only request this after 12 seconds."})
    r = _confirm(anon)
    assert r.status_code == 429 and r.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert r.headers["Retry-After"] == "12"


def test_an_outage_on_the_confirm_is_502(anon, supabase):
    supabase.fail = OSError("connection refused")
    assert _confirm(anon).status_code == 502


def test_an_unreadable_answer_is_not_a_password_we_claim_to_have_changed(anon, supabase):
    supabase.update = lambda body: _FakeResponse(200, None, text="<html>gateway</html>")
    # A 200 is a 200 even with an unreadable body — GoTrue answers 200 only after
    # the write. What must never happen is a NON-200 becoming a success.
    assert _confirm(anon).status_code == 200
    supabase.update = lambda body: _FakeResponse(503, None, text="<html>gateway</html>")
    assert _confirm(anon).status_code == 502


def test_a_reset_writes_no_row_this_app_owns(anon, supabase):
    """No `account_seen` in particular: that record is deliberately
    proof-of-SIGN-IN, and controlling a mailbox is not one.  The sign-in that
    follows is what writes it."""
    db = SessionLocal()
    try:
        before = {model: db.query(model).count()
                  for model in (AccountQuota, AccountRole, AccountSeen)}
    finally:
        db.close()

    assert _ask(anon, email="no-rows@broker.np").status_code == 202
    assert _confirm(anon).status_code == 200

    db = SessionLocal()
    try:
        after = {model: db.query(model).count()
                 for model in (AccountQuota, AccountRole, AccountSeen)}
        assert after == before, "a password reset wrote a row into a table this app owns"
    finally:
        db.close()


def test_the_confirm_is_deliberately_not_counted(anon, supabase):
    """Stated as a test because it is a decision, not an omission.  There is no
    secret to guess — the token is provider-signed — so there is no budget to
    exhaust, and counting it would lock a user out of finishing a reset because
    they mistyped a new password."""
    for _ in range(auth._RESET_MAX_PER_HOUR + 3):
        assert _confirm(anon).status_code == 200
    assert _reset_rows() == 0


# --------------------------------------------------------------------------- #
# The limiter: counted whatever the provider said, and sharing nothing
# --------------------------------------------------------------------------- #
def test_the_hourly_limit_counts_requests(anon, supabase):
    for i in range(auth._RESET_MAX_PER_HOUR):
        assert _ask(anon, email=f"burst{i}@broker.np").status_code == 202
    blocked = _ask(anon, email="burst-over@broker.np")
    assert blocked.status_code == 429 and blocked.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert int(blocked.headers["Retry-After"]) > 0
    assert supabase.recovers[-1]["json"]["email"] == \
        f"burst{auth._RESET_MAX_PER_HOUR - 1}@broker.np", (
        "a limited request must be refused BEFORE the provider is called")


def test_an_address_with_no_account_costs_the_same_budget(anon, supabase):
    """THE BUDGET ORACLE, closed.  If only the requests that produced an email
    were counted, how many tries you have left would tell you which addresses have
    accounts — after the status code had just been made constant."""
    supabase.recover = lambda body: _FakeResponse(200, {})       # "sent"
    _ask(anon, email="exists@broker.np")
    supabase.recover = lambda body: _FakeResponse(
        429, {"msg": "For security purposes, you can only request this after 9 seconds."})
    _ask(anon, email="cooldown@broker.np")
    supabase.recover = lambda body: _FakeResponse(
        500, {"error_code": "error_sending_recovery_email"})
    _ask(anon, email="undeliverable@broker.np")
    assert _reset_rows() == 3


def test_the_daily_limit_catches_a_script_that_waits_out_the_hour(anon, supabase):
    db = SessionLocal()
    try:
        for hours in range(1, 21):                     # 20 rows, none within the hour
            db.add(PasswordResetAttempt(client_key="testclient",
                                        created_at=datetime.now(timezone.utc)
                                        - timedelta(hours=hours, minutes=5)))
        db.commit()
        assert auth.password_reset_retry_after(db, "testclient") > 0
    finally:
        db.close()
    assert _ask(anon, email="patient@broker.np").status_code == 429


def test_callers_are_limited_independently(anon, supabase, monkeypatch):
    """One caller's requests must not stop a different caller's, or the limiter is
    a denial of service built out of a security control."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    noisy = {"x-forwarded-for": "203.0.113.17"}
    quiet = {"x-forwarded-for": "203.0.113.18"}
    for i in range(auth._RESET_MAX_PER_HOUR):
        assert _ask(anon, email=f"noisy{i}@broker.np", headers=noisy).status_code == 202
    assert _ask(anon, email="noisy-over@broker.np", headers=noisy).status_code == 429
    assert _ask(anon, email="quiet@broker.np", headers=quiet).status_code == 202


def test_an_unreadable_counter_refuses_the_request(anon, supabase, monkeypatch):
    """FAIL CLOSED, exactly as the other two limiters do.  A counter that answered
    "go ahead" while blind would turn a database fault into an unmetered mail relay
    pointed at this deployment's own sending reputation."""
    def _explode(db, client):
        raise auth.PasswordResetLimitUnavailable("counter is gone")

    monkeypatch.setattr(auth, "password_reset_retry_after", _explode)
    r = _ask(anon, email="unreadable-counter@broker.np")
    assert r.status_code == 503
    assert supabase.recovers == [], "a reset was sent while the limiter was blind"


def test_recording_prunes_to_the_LONGEST_window(anon, supabase):
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.add(PasswordResetAttempt(client_key="aged-out", created_at=now - timedelta(hours=25)))
        db.add(PasswordResetAttempt(client_key="still-counting",
                                    created_at=now - timedelta(hours=2)))
        db.commit()
        auth.record_password_reset(db, "fresh")
        remaining = {r.client_key for r in db.query(PasswordResetAttempt).all()}
    finally:
        db.close()
    assert remaining == {"still-counting", "fresh"}


def test_a_failed_counter_write_does_not_undo_a_request(anon, supabase):
    """By the time it is called an email may already be in flight and this app
    cannot recall it, so a failed count must not report a failure."""
    db = SessionLocal()
    db.close()                                       # a closed session: writes fail
    auth.record_password_reset(db, "some-caller")    # must not raise


# --------------------------------------------------------------------------- #
# Non-interference with the other two limiters — asserted in every direction
# --------------------------------------------------------------------------- #
def test_the_three_limiters_are_three_tables():
    """Stated as a property, because "we will remember not to share the key" is
    not one."""
    assert len({PasswordResetAttempt.__tablename__, SignupAttempt.__tablename__,
                LoginAttempt.__tablename__}) == 3


def test_resets_do_not_spend_the_registration_budget(anon, supabase, monkeypatch):
    """A shared table would mean somebody recovering an account could not then be
    invited to create a second one — and, worse, the reverse below."""
    monkeypatch.setattr(get_settings(), "allow_self_signup", True)
    for i in range(auth._RESET_MAX_PER_HOUR):
        assert _ask(anon, email=f"spend{i}@broker.np").status_code == 202
    db = SessionLocal()
    try:
        assert db.query(SignupAttempt).count() == 0
    finally:
        db.close()


def test_registrations_do_not_stop_somebody_recovering_an_account(anon, supabase,
                                                                  monkeypatch):
    """The direction that matters.  If the two shared a window, a caller who had
    used up their registrations could not reset a password — locking a real
    customer out of the only route back into their account."""
    db = SessionLocal()
    try:
        for _ in range(auth._SIGNUP_MAX_PER_DAY):
            auth.record_signup(db, "testclient")
    finally:
        db.close()
    assert _ask(anon, email="still-recoverable@broker.np").status_code == 202
    auth.reset_signup_limiter()


def test_neither_other_reset_hook_can_empty_this_window(anon, supabase):
    """conftest calls `auth.reset_throttle()` around every test, and
    `reset_signup_limiter` is called by the signup fixture.  A window either of
    them emptied would be a window no test could observe holding."""
    assert _ask(anon, email="survives@broker.np").status_code == 202
    auth.reset_throttle()
    auth.reset_signup_limiter()
    assert _reset_rows() == 1


def test_a_reset_writes_nothing_into_the_login_failure_window(anon, supabase):
    db = SessionLocal()
    try:
        before = db.query(LoginAttempt).count()
    finally:
        db.close()
    assert _ask(anon, email="separate@broker.np").status_code == 202
    assert _confirm(anon).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(LoginAttempt).count() == before
    finally:
        db.close()


def test_a_reset_does_not_clear_the_login_failure_window(anon, supabase):
    """The attack shape the plan names for signup, checked here too: if a reset
    cleared the caller's failures, an attacker would interleave one between
    password guesses and guess for ever."""
    for _ in range(auth._MAX_FAILURES):
        anon.post("/api/auth/login", json={"username": HOLDER_EMAIL, "password": "guess"})
    assert _ask(anon, email="interleaved@broker.np").status_code == 202
    assert _confirm(anon).status_code == 200
    blocked = anon.post("/api/auth/login",
                        json={"username": HOLDER_EMAIL, "password": "guess"})
    assert blocked.status_code == 429, (
        "a password reset cleared the login failure window — the limiters share state")
    auth.reset_throttle()
