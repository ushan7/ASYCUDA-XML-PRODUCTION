"""Login gate — one operator account from the environment, 24h sessions.

The account lives in backend/.env; tests/conftest.py configures it for the
suite and makes every TestClient authenticated by default, so the clients here
strip the header when they want to be anonymous.
"""
import time

import pytest

from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.database import init_db
from app.main import app

GOOD = {"username": "pytest-operator", "password": "pytest-password"}


@pytest.fixture()
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def anon(client):
    """Same client with no credentials — a stranger on the network."""
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    return client


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
PROTECTED = [
    ("GET", "/api/config"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/reference/hs?q=9021"),
    ("GET", "/api/reference/banks"),
    ("GET", "/api/jobs/any-id"),
    ("GET", "/api/jobs/any-id/critical-review"),
    ("POST", "/api/jobs/any-id/finalize"),
    ("GET", "/api/jobs/any-id/xml"),
    ("GET", "/api/jobs/any-id/brand-model-size.xls"),
    ("GET", "/api/jobs/any-id/audit"),
    ("GET", "/api/jobs/any-id/documents/any-doc/file"),
    ("GET", "/api/auth/session"),
    # The admin surface is inside the SAME gate, so an anonymous caller is
    # answered 401 by the middleware and never reaches `require_admin` — which is
    # what makes the admin routes' 404-to-a-member a refusal that reveals nothing
    # either way (every /api path answers 401 unauthenticated, real or not).
    ("GET", "/api/admin/accounts"),
    ("GET", "/api/admin/accounts/any-owner/usage"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_route_refuses_an_anonymous_caller(anon, method, path):
    r = anon.request(method, path, json={} if method == "POST" else None)
    assert r.status_code == 401, f"{method} {path} answered {r.status_code} without a login"
    if r.headers.get("content-type", "").startswith("application/json"):
        assert r.json()["code"] == "AUTH_REQUIRED"


def test_the_refusal_never_leaks_data(anon):
    """A 401 body must not carry the answer it refused to give."""
    r = anon.get("/api/config")
    assert "reference_counts" not in r.text and "llm_model" not in r.text


def test_health_and_the_spa_shell_stay_public(anon):
    """The healthcheck is liveness only, and the shell has to load to draw the
    login form — everything it renders still comes from the gated /api/*."""
    assert anon.get("/api/health").status_code == 200
    shell = anon.get("/")
    assert shell.status_code == 200 and "<div id=\"root\">" in shell.text


def test_the_public_surface_is_a_closed_set():
    """Every path outside the gate, named here so a fourth is a decision somebody
    made rather than a line somebody added.

    Registration has to be public — a stranger has no session — which is why the
    route is written to be an account-existence oracle for nobody
    (tests/test_signup.py) and why it is refused outright unless the deployment
    turns it on.
    """
    from app.main import _PUBLIC_API_PATHS

    assert set(_PUBLIC_API_PATHS) == {"/api/health", "/api/auth/login", "/api/auth/signup"}


def test_registration_is_refused_rather_than_hidden_on_this_deployment(anon):
    """The suite runs the single-account provider, where registration cannot
    exist: 501, and the sign-in screen is told not to offer it."""
    assert anon.get("/api/auth/signup").json()["enabled"] is False
    r = anon.post("/api/auth/signup", json={"email": "a@b.np", "password": "a-password"})
    assert r.status_code == 501 and r.json()["code"] == "SIGNUP_UNSUPPORTED"


def test_new_api_routes_are_protected_by_default():
    """The gate is middleware, not a per-route dependency, so a route added
    without any auth wiring is still closed."""
    from app.main import _needs_login

    class _Req:
        def __init__(self, path, method="GET"):
            self.method = method

            class _U:
                pass
            self.url = _U()
            self.url.path = path

    assert _needs_login(_Req("/api/some/brand/new/route"))
    assert _needs_login(_Req("/api/jobs", "POST"))
    assert not _needs_login(_Req("/api/jobs", "OPTIONS"))   # CORS preflight
    assert not _needs_login(_Req("/index.html"))


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #
def test_login_returns_a_token_and_a_cookie(anon):
    r = anon.post("/api/auth/login", json=GOOD)
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "pytest-operator"
    assert body["token"] and body["expires_at"] > time.time()
    cookie = r.cookies.get(auth.COOKIE_NAME)
    assert cookie == body["token"]
    # HttpOnly so page scripts cannot read it; Lax so a cross-site POST cannot
    # ride it. Not Secure here because the test request is plain http.
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw and "samesite=lax" in raw


def test_the_session_lasts_one_day(anon):
    body = anon.post("/api/auth/login", json=GOOD).json()
    assert get_settings().auth_token_ttl_hours == 24.0
    assert 23.9 * 3600 < body["expires_in_seconds"] <= 24 * 3600


def test_the_cookie_alone_opens_the_gate(anon):
    """The evidence <iframe> and the XML / .xls download links are plain
    navigations — no JavaScript, so no Authorization header."""
    anon.post("/api/auth/login", json=GOOD)          # client keeps the cookie
    assert "Authorization" not in anon.headers
    assert anon.get("/api/auth/session").status_code == 200
    assert anon.get("/api/config").status_code == 200


def test_the_bearer_token_alone_opens_the_gate(anon):
    token = anon.post("/api/auth/login", json=GOOD).json()["token"]
    anon.cookies.clear()
    r = anon.get("/api/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["username"] == "pytest-operator"


def test_wrong_password_is_refused(anon):
    r = anon.post("/api/auth/login", json={**GOOD, "password": "nearly-right"})
    assert r.status_code == 401 and r.json()["code"] == "BAD_CREDENTIALS"
    assert not r.cookies.get(auth.COOKIE_NAME)


def test_unknown_username_is_refused_the_same_way(anon):
    """Same code, same sentence: a prober must not learn which half was wrong."""
    bad_user = anon.post("/api/auth/login", json={"username": "someone-else",
                                                 "password": "pytest-password"})
    bad_pw = anon.post("/api/auth/login", json={**GOOD, "password": "nope"})
    assert bad_user.status_code == bad_pw.status_code == 401
    assert bad_user.json() == bad_pw.json()


def test_username_is_case_and_space_tolerant_password_is_not(anon):
    assert anon.post("/api/auth/login", json={**GOOD, "username": " Pytest-Operator "}).status_code == 200
    assert anon.post("/api/auth/login", json={**GOOD, "password": " pytest-password"}).status_code == 401


def test_logout_clears_the_cookie(anon):
    anon.post("/api/auth/login", json=GOOD)
    assert anon.get("/api/auth/session").status_code == 200
    anon.post("/api/auth/logout")
    assert anon.get("/api/auth/session").status_code == 401


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def test_an_expired_token_is_refused(anon):
    """After 24h the operator signs in again — the whole point of the TTL."""
    token, session = auth.issue_token("pytest-operator",
                                      now=int(time.time()) - int(auth.token_ttl_seconds()) - 1)
    assert session.expires_at <= time.time()
    assert auth.verify_token(token) is None
    r = anon.get("/api/config", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_a_token_valid_a_second_before_expiry_still_works(anon):
    token, _ = auth.issue_token("pytest-operator",
                                now=int(time.time()) - int(auth.token_ttl_seconds()) + 5)
    assert anon.get("/api/config", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_a_tampered_token_is_refused(anon):
    token, _ = auth.issue_token("pytest-operator")
    payload, _, sig = token.partition(".")
    for forged in (f"{payload}.{'a' * len(sig)}",         # wrong signature
                   f"{payload}x.{sig}",                    # payload edited
                   payload,                                # signature dropped
                   "", "not-a-token"):
        assert auth.verify_token(forged) is None
        assert anon.get("/api/config",
                        headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_an_unsigned_payload_cannot_be_forged(anon):
    """The claims are readable but not writable — the classic 'alg: none'."""
    import base64
    import json
    claims = {"sub": "pytest-operator", "iat": int(time.time()),
              "exp": int(time.time()) + 99999}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    assert auth.verify_token(f"{payload}.") is None
    assert auth.verify_token(f"{payload}.{payload}") is None


def test_a_token_for_a_retired_account_stops_working(monkeypatch):
    """Rotating the username in .env must not leave yesterday's token valid."""
    token, _ = auth.issue_token("pytest-operator")
    assert auth.verify_token(token) is not None
    monkeypatch.setattr(auth, "configured_username", lambda: "someone-new")
    assert auth.verify_token(token) is None


# --------------------------------------------------------------------------- #
# Fail closed / brute force
# --------------------------------------------------------------------------- #
def test_no_configured_account_means_nobody_gets_in(anon, monkeypatch):
    """An unconfigured deployment is visibly broken, never quietly open."""
    monkeypatch.setattr(auth, "credentials_configured", lambda: False)
    monkeypatch.setattr(auth, "configured_username", lambda: None)
    monkeypatch.setattr(auth, "configured_password", lambda: None)
    assert not auth.verify_credentials("pytest-operator", "pytest-password")
    r = anon.post("/api/auth/login", json=GOOD)
    assert r.status_code == 503 and "backend/.env" in r.json()["detail"]
    assert anon.get("/api/config").status_code == 401


def test_placeholder_credentials_do_not_count_as_configured(monkeypatch):
    """The .env.example values must not become a shipped default account."""
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_username", "your_login_username_here")
    monkeypatch.setattr(settings, "auth_password", "your_login_password_here")
    assert not auth.credentials_configured()
    monkeypatch.setattr(settings, "auth_username", "  ")
    assert auth.configured_username() is None


def test_repeated_failures_are_throttled(anon):
    for _ in range(10):
        assert anon.post("/api/auth/login", json={**GOOD, "password": "guess"}).status_code == 401
    blocked = anon.post("/api/auth/login", json={**GOOD, "password": "guess"})
    assert blocked.status_code == 429 and blocked.json()["code"] == "TOO_MANY_ATTEMPTS"
    assert int(blocked.headers["Retry-After"]) > 0
    # ...and the throttle does not lock out the real password forever
    auth.reset_throttle()
    assert anon.post("/api/auth/login", json=GOOD).status_code == 200


def test_a_successful_login_clears_the_failure_count(anon):
    for _ in range(9):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"})
    assert anon.post("/api/auth/login", json=GOOD).status_code == 200
    for _ in range(9):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"})
    assert anon.post("/api/auth/login", json=GOOD).status_code == 200
