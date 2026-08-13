"""Many accounts, and each one sees only its own declarations.

Until now this app had exactly ONE account, in two environment variables. That
is what "1500 concurrent users" could not mean: they would share a login and
every one of them would see every other's commercial invoices, party details and
finalized declarations.

Two changes make it real, and they are separable on purpose:

  * IDENTITY moves to a provider. `auth_provider=supabase` proxies sign-in
    server-side and gets back a stable user id; `local` keeps the single env
    account for a broker's own machine and for this suite.
  * AUTHORIZATION stops treating "unowned" as "everyone's". That branch was
    correct with one account and is a disclosure with two.

The isolation asserted here is PYTHON isolation. Supabase's row-level security
does not protect this backend at all — it connects to Postgres as a privileged
role and bypasses RLS — so a policy in the database would not catch a regression
in any of these.
"""
from __future__ import annotations

import json
import types

import pytest
from fastapi.testclient import TestClient

from app import auth, auth_supabase, services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Job

USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture()
def supabase(monkeypatch):
    """auth_provider=supabase, with the HTTP call stubbed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "supabase")
    monkeypatch.setattr(settings, "supabase_url", "https://project.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")

    accounts = {("a@broker.np", "pw-a"): USER_A, ("b@broker.np", "pw-b"): USER_B}
    state = types.SimpleNamespace(calls=[], fail=None)

    def fake_post(url, *, params=None, headers=None, json=None, timeout=None):
        state.calls.append({"url": url, "params": params, "headers": headers,
                            "json": json, "timeout": timeout})
        if state.fail is not None:
            raise state.fail
        user_id = accounts.get((json["email"], json["password"]))
        if user_id is None:
            return FakeResponse(400, {"error": "invalid_grant"})
        return FakeResponse(200, {"access_token": "supabase-jwt",
                                  "user": {"id": user_id, "email": json["email"]}})

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    return state


@pytest.fixture()
def anon():
    init_db()
    auth.reset_throttle()
    client = TestClient(app)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    yield client
    auth.reset_throttle()


# --------------------------------------------------------------------------- #
# The token carries an ID and a NAME, and they are not the same field
# --------------------------------------------------------------------------- #
def test_the_token_subject_is_the_user_id_not_the_display_name(supabase):
    """An email can change; the jobs that account owns must not change hands
    when it does."""
    token, session = auth.issue_token("a@broker.np", user_id=USER_A)
    assert session.user_id == USER_A
    assert session.username == "a@broker.np"

    verified = auth.verify_token(token)
    assert verified.user_id == USER_A
    assert verified.username == "a@broker.np"
    assert verified.principal() == USER_A


def test_a_token_minted_before_user_ids_existed_still_verifies():
    """Those carry only `sub`, which on the local provider WAS the username —
    so it is both the id and the name, and nobody is signed out by the upgrade."""
    token, _ = auth.issue_token("pytest-operator")
    verified = auth.verify_token(token)
    assert verified.user_id == "pytest-operator"
    assert verified.username == "pytest-operator"


def test_ownership_reads_the_id_and_the_audit_trail_reads_the_name():
    session = auth.Session(username="a@broker.np", issued_at=0, expires_at=0,
                           user_id=USER_A)
    assert services.principal_of(session) == USER_A


# --------------------------------------------------------------------------- #
# Provider dispatch
# --------------------------------------------------------------------------- #
def test_the_local_provider_is_its_own_identity():
    identity = auth.verify_login("pytest-operator", "pytest-password")
    assert identity is not None
    # One account, so its id IS its configured name — which is why a database
    # written under `local` needs no remapping until Supabase is switched on.
    assert identity.user_id == "pytest-operator"


def test_the_local_provider_still_refuses_a_wrong_password():
    assert auth.verify_login("pytest-operator", "nope") is None


def test_supabase_sign_in_returns_the_user_id(supabase):
    identity = auth.verify_login("a@broker.np", "pw-a")
    assert identity.user_id == USER_A
    assert identity.display == "a@broker.np"


def test_supabase_uses_the_password_grant_and_the_anon_key(supabase):
    auth.verify_login("a@broker.np", "pw-a")
    call = supabase.calls[-1]
    assert call["url"] == "https://project.supabase.co/auth/v1/token"
    assert call["params"] == {"grant_type": "password"}
    assert call["headers"]["apikey"] == "anon-key"
    assert call["timeout"] == get_settings().supabase_timeout_seconds


def test_supabase_refusal_is_a_wrong_password_not_an_outage(supabase):
    assert auth.verify_login("a@broker.np", "wrong") is None


def test_an_unreachable_supabase_is_not_reported_as_a_wrong_password(supabase):
    """Reporting an outage as a bad password sends the operator to reset one
    that was never the problem, and hides the fault that is."""
    supabase.fail = OSError("connection refused")
    with pytest.raises(auth.LoginUnavailable):
        auth.verify_login("a@broker.np", "pw-a")


def test_a_200_we_cannot_read_is_not_a_successful_sign_in(supabase, monkeypatch):
    """Admitting a caller on the strength of a response we did not understand
    is the one failure mode worth being paranoid about here."""
    import httpx
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: FakeResponse(200, None, text="<html>gateway</html>"))
    with pytest.raises(auth_supabase.SupabaseAuthUnavailable):
        auth_supabase.sign_in("a@broker.np", "pw-a")


def test_supabase_without_configuration_is_refused_at_boot():
    from app.config import Settings

    with pytest.raises(Exception) as caught:
        Settings(_env_file=None, auth_provider="supabase")
    assert "SUPABASE_URL" in str(caught.value)


# --------------------------------------------------------------------------- #
# EASYCUSTOMS_AUTH_SECRET is a boot refusal on this provider.
#
# Unset, auth._secret generates one PER PROCESS, so the shipped
# easycustoms-api.service (`--workers 2`) signs sessions with two different keys
# and each worker treats the other's token as a forgery.  The symptom is a
# browser signed in on roughly every other request — which looks like anything
# except a missing line in .env, and costs somebody an afternoon.
#
# `_env_file=None` stops backend/.env being read; conftest still puts a secret in
# os.environ, so "unset" has to be said explicitly as an init kwarg (the
# highest-priority source) rather than by omission.
# --------------------------------------------------------------------------- #
#
# The vendor keys are part of the shape because a supabase deployment naming a
# LIVE extraction/OCR provider without one is now refused at boot as well
# (tests/test_live_provider_keys.py) — these tests are about the auth secret, so
# they say the rest of the deployment is complete rather than tripping over it.
_SUPABASE = {"auth_provider": "supabase",
             "supabase_url": "https://project.supabase.co",
             "supabase_anon_key": "anon",
             "mistral_api_key": "sk-mistral", "llm_api_key": "sk-openai"}


def test_supabase_without_an_auth_secret_is_refused_at_boot():
    from app.config import Settings

    with pytest.raises(Exception) as caught:
        Settings(_env_file=None, auth_secret=None, **_SUPABASE)
    message = str(caught.value)
    assert "EASYCUSTOMS_AUTH_SECRET" in message
    assert "secrets.token_hex" in message, "the refusal has to say how to make one"


def test_the_env_example_placeholder_does_not_count_as_a_secret():
    """The realistic way to get this wrong is copying .env.example and filling in
    every line but this one.  `config.configured` is the single predicate both
    this refusal and auth._secret use, so a value one accepts the other cannot
    silently treat as absent."""
    from app.config import Settings

    with pytest.raises(Exception) as caught:
        Settings(_env_file=None, auth_secret="your_random_64_hex_secret_here", **_SUPABASE)
    assert "EASYCUSTOMS_AUTH_SECRET" in str(caught.value)

    with pytest.raises(Exception):
        Settings(_env_file=None, auth_secret="   ", **_SUPABASE)


def test_supabase_with_an_auth_secret_boots():
    from app.config import Settings

    s = Settings(_env_file=None, auth_secret="0" * 64, **_SUPABASE)
    assert s.auth_secret == "0" * 64


def test_a_single_operator_deployment_still_boots_without_one():
    """The path this refusal must not break: `local` is the broker's laptop —
    one process, `uvicorn --reload`, no proxy — where an unset secret means "sign
    in again after a restart".  That is comprehensible, documented, and the
    zero-setup path that makes this app runnable with no configuration at all.

    The residual is deliberate and recorded rather than closed: `local` with
    --workers 2 behind a proxy has the identical bug, and the unit file carries
    the warning for it."""
    from app.config import Settings

    assert Settings(_env_file=None, auth_provider="local", auth_secret=None
                    ).auth_secret is None


# --------------------------------------------------------------------------- #
# End to end: two users, two dashboards
# --------------------------------------------------------------------------- #
def _login(client, email, password):
    r = client.post("/api/auth/login", json={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_two_users_do_not_see_each_others_jobs(supabase, anon):
    """The property the whole change exists for."""
    token_a = _login(anon, "a@broker.np", "pw-a")
    anon.headers["Authorization"] = f"Bearer {token_a}"
    job_a = anon.post("/api/jobs").json()["job_id"]

    anon.cookies.clear()
    token_b = _login(anon, "b@broker.np", "pw-b")
    anon.headers["Authorization"] = f"Bearer {token_b}"
    job_b = anon.post("/api/jobs").json()["job_id"]

    # B cannot reach A's job by id — 404, not 403: an id that exists but belongs
    # to someone else must be indistinguishable from one that does not.
    assert anon.get(f"/api/jobs/{job_a}").status_code == 404
    assert anon.get(f"/api/jobs/{job_b}").status_code == 200

    # ...and the reverse
    anon.headers["Authorization"] = f"Bearer {token_a}"
    assert anon.get(f"/api/jobs/{job_b}").status_code == 404
    assert anon.get(f"/api/jobs/{job_a}").status_code == 200


def test_a_job_is_owned_by_the_user_id_not_the_email(supabase, anon):
    token = _login(anon, "a@broker.np", "pw-a")
    anon.headers["Authorization"] = f"Bearer {token}"
    job_id = anon.post("/api/jobs").json()["job_id"]

    db = SessionLocal()
    try:
        assert db.get(Job, job_id).owner_key == USER_A
    finally:
        db.close()


def test_the_dashboard_listing_is_scoped_too(supabase, anon):
    """The listing is separate SQL from the per-job check. One that disagreed
    would show a user another's shipment totals and party names in the summary,
    while refusing to open them."""
    token_a = _login(anon, "a@broker.np", "pw-a")
    anon.headers["Authorization"] = f"Bearer {token_a}"
    anon.post("/api/jobs")

    anon.cookies.clear()
    token_b = _login(anon, "b@broker.np", "pw-b")
    anon.headers["Authorization"] = f"Bearer {token_b}"
    assert anon.get("/api/jobs").json()["total"] == 0


def test_an_outage_is_502_and_is_not_counted_against_the_throttle(supabase, anon):
    """The caller did nothing to earn a failure — locking them out for an
    outage on our side would compound it."""
    supabase.fail = OSError("connection refused")
    r = anon.post("/api/auth/login", json={"username": "a@broker.np", "password": "pw-a"})
    assert r.status_code == 502
    assert "not your password" in r.text

    db = SessionLocal()
    try:
        from app.models import LoginAttempt
        assert db.query(LoginAttempt).count() == 0
    finally:
        db.close()


def test_a_wrong_password_is_still_counted(supabase, anon):
    anon.post("/api/auth/login", json={"username": "a@broker.np", "password": "wrong"})
    db = SessionLocal()
    try:
        from app.models import LoginAttempt
        assert db.query(LoginAttempt).count() == 1
    finally:
        db.close()


def test_the_local_provider_refuses_a_token_naming_another_account():
    """One account, named in the environment. A token outlives its account, so
    rotating the username in .env must not leave the previous operator's session
    working until it expires — and a token minted for some other subject
    entirely must never verify."""
    token, _ = auth.issue_token("someone-else", user_id="someone-else")
    assert auth.verify_token(token) is None


def test_supabase_does_not_apply_the_single_account_check(supabase):
    """That check exists because `local` has exactly one account. Applying it
    under a provider with many would refuse every user but one."""
    token, _ = auth.issue_token("b@broker.np", user_id=USER_B)
    assert auth.verify_token(token).user_id == USER_B
