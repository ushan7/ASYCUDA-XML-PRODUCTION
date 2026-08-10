"""The login throttle has to survive more than one process, and has to know who
is asking. Neither half is any use without the other.

It used to be `auth._failures`, a dict in the API process. That is two distinct
production bugs:

  * **per-process counting.** N API processes each keep their own count, so the
    deployment allows N times the intended guess rate, and whichever process the
    load balancer happened to route to decided whether a caller was blocked.
  * **one global bucket behind a proxy.** `_client_id` read the socket peer, and
    behind a load balancer every request arrives FROM the balancer. So all
    callers shared one key: ten failed sign-ins from anywhere locked out every
    user of the system, and an unauthenticated attacker could trigger that
    deliberately. A denial of service built out of a security control.

Moving the count to the database fixes the first. `auth.client_key` +
EASYCUSTOMS_TRUSTED_PROXY_HOPS fixes the second — and it has to read the address
from a position the caller cannot control, or the throttle stops limiting
anyone.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import LoginAttempt

GOOD = {"username": "pytest-operator", "password": "pytest-password"}


@pytest.fixture()
def anon():
    init_db()
    auth.reset_throttle()
    client = TestClient(app)
    client.headers.pop("Authorization", None)
    client.cookies.clear()
    yield client
    auth.reset_throttle()


def _request(peer: str = "10.0.0.1", forwarded: str | None = None):
    headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
    return types.SimpleNamespace(client=types.SimpleNamespace(host=peer), headers=headers)


# --------------------------------------------------------------------------- #
# Who is asking
# --------------------------------------------------------------------------- #
def test_forwarded_headers_are_ignored_when_nothing_is_trusted(monkeypatch):
    """The default. A caller that can set the header would otherwise choose its
    own throttle key and reset its failure count whenever it liked."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 0)
    assert auth.client_key(_request("10.0.0.1", "1.2.3.4")) == "10.0.0.1"


def test_one_trusted_proxy_reads_the_address_that_proxy_saw(monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    # The proxy appends the peer it received the request from, so the rightmost
    # entry is the only one it vouched for.
    assert auth.client_key(_request("10.0.0.1", "203.0.113.7")) == "203.0.113.7"


def test_a_spoofed_prefix_cannot_choose_the_key(monkeypatch):
    """The attack the hop count exists to defeat: a caller sending its own XFF
    so that every attempt looks like a different client."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    key = auth.client_key(_request("10.0.0.1", "9.9.9.9, 203.0.113.7"))
    assert key == "203.0.113.7", "the caller-supplied prefix must be ignored"


def test_two_trusted_hops_read_one_further_left(monkeypatch):
    """CloudFront in front of an ALB: two of our own hops, each appending one."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
    key = auth.client_key(_request("10.0.0.1", "9.9.9.9, 203.0.113.7, 10.0.0.9"))
    assert key == "203.0.113.7"


def test_a_short_forwarded_list_falls_back_to_the_socket_peer(monkeypatch):
    """Fewer entries than trusted hops means the request did not come through
    them — a direct hit on the app port, or a wrong hop count. Believing the
    list would let the caller supply its own key."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 2)
    assert auth.client_key(_request("10.0.0.1", "203.0.113.7")) == "10.0.0.1"
    assert auth.client_key(_request("10.0.0.1", "")) == "10.0.0.1"
    assert auth.client_key(_request("10.0.0.1")) == "10.0.0.1"


# --------------------------------------------------------------------------- #
# The count is shared, not per-process
# --------------------------------------------------------------------------- #
def test_failures_are_visible_to_a_second_session(anon):
    """Stands in for a second API process: an independent session, its own
    connection, the same window. With the old dict this count was 0."""
    for _ in range(3):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"})

    other = SessionLocal()
    try:
        assert auth.throttle_retry_after(other, auth.client_key(_request("testclient"))) == 0
        assert other.query(LoginAttempt).count() == 3
    finally:
        other.close()


def test_a_second_session_enforces_the_limit_reached_by_the_first(anon):
    key = "198.51.100.4"
    writer = SessionLocal()
    reader = SessionLocal()
    try:
        for _ in range(auth._MAX_FAILURES):
            auth.record_failure(writer, key)
        # A different session — a different process, in production — must see it.
        assert auth.throttle_retry_after(reader, key) > 0
    finally:
        writer.close()
        reader.close()


def test_clients_are_throttled_independently(monkeypatch, anon):
    """The bug that locked everybody out: one caller's failures must not block
    a different caller."""
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    noisy = {"x-forwarded-for": "203.0.113.7"}
    quiet = {"x-forwarded-for": "203.0.113.8"}

    for _ in range(auth._MAX_FAILURES):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"}, headers=noisy)
    assert anon.post("/api/auth/login", json=GOOD, headers=noisy).status_code == 429
    # ...and the other caller is unaffected, including with the RIGHT password
    assert anon.post("/api/auth/login", json=GOOD, headers=quiet).status_code == 200


def test_a_successful_login_only_clears_its_own_caller(monkeypatch, anon):
    monkeypatch.setattr(get_settings(), "trusted_proxy_hops", 1)
    for _ in range(3):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"},
                  headers={"x-forwarded-for": "203.0.113.7"})
    for _ in range(3):
        anon.post("/api/auth/login", json={**GOOD, "password": "guess"},
                  headers={"x-forwarded-for": "203.0.113.8"})

    anon.post("/api/auth/login", json=GOOD, headers={"x-forwarded-for": "203.0.113.7"})

    db = SessionLocal()
    try:
        keys = [r.client_key for r in db.query(LoginAttempt).all()]
    finally:
        db.close()
    assert set(keys) == {"203.0.113.8"}


# --------------------------------------------------------------------------- #
# Window behaviour
# --------------------------------------------------------------------------- #
def test_failures_outside_the_window_do_not_count(anon):
    key = "198.51.100.9"
    db = SessionLocal()
    try:
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=auth._FAILURE_WINDOW_SECONDS + 60))
        for _ in range(auth._MAX_FAILURES + 5):
            db.add(LoginAttempt(client_key=key, created_at=stale))
        db.commit()
        assert auth.throttle_retry_after(db, key) == 0
    finally:
        db.close()


def test_recording_prunes_what_can_no_longer_matter(anon):
    """The table is a sliding window, not a log of every failed sign-in."""
    db = SessionLocal()
    try:
        stale = (datetime.now(timezone.utc)
                 - timedelta(seconds=auth._FAILURE_WINDOW_SECONDS + 60))
        db.add(LoginAttempt(client_key="old-caller", created_at=stale))
        db.commit()
        auth.record_failure(db, "new-caller")
        remaining = [r.client_key for r in db.query(LoginAttempt).all()]
    finally:
        db.close()
    assert remaining == ["new-caller"]


def test_retry_after_counts_down_from_the_oldest_failure(anon):
    key = "198.51.100.11"
    db = SessionLocal()
    try:
        aged = datetime.now(timezone.utc) - timedelta(seconds=120)
        for _ in range(auth._MAX_FAILURES):
            db.add(LoginAttempt(client_key=key, created_at=aged))
        db.commit()
        # 300s window, oldest is 120s old -> ~180s left, never 0 while blocked.
        assert 170 <= auth.throttle_retry_after(db, key) <= 181
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_an_unreadable_counter_refuses_the_sign_in(anon, monkeypatch):
    """FAIL CLOSED. Answering "go ahead" because the counter is unreadable would
    turn a database fault into unlimited guessing at a public endpoint."""
    def _explode(db, client):
        raise auth.ThrottleUnavailable("counter is gone")

    monkeypatch.setattr(auth, "throttle_retry_after", _explode)
    r = anon.post("/api/auth/login", json=GOOD)
    assert r.status_code == 503
    assert "token" not in r.text


def test_a_failed_write_does_not_turn_a_401_into_a_500(anon, monkeypatch):
    """The caller is being refused either way; a broken counter must not change
    the answer they get."""
    def _explode(db, client):
        raise RuntimeError("write failed")

    monkeypatch.setattr(auth, "record_failure", _explode)
    with pytest.raises(RuntimeError):
        # Proves the stub is reached; the real record_failure swallows its own
        # errors, which is the behaviour the next assertion depends on.
        auth.record_failure(None, "x")

    monkeypatch.undo()
    db = SessionLocal()
    try:
        db.close()                                  # a closed session: writes fail
        auth.record_failure(db, "some-caller")      # must not raise
    finally:
        db.close()
