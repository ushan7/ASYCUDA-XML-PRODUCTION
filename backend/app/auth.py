"""Session authentication — one operator account, configured from the env.

The deployment is a single-operator tool (one broker's workstation, one server
on the office LAN), so the account lives in ``backend/.env`` rather than in a
users table:

    EASYCUSTOMS_AUTH_USERNAME=...
    EASYCUSTOMS_AUTH_PASSWORD=...
    EASYCUSTOMS_AUTH_SECRET=...        # signs tokens; see below

Login exchanges those credentials for a signed token that is valid for
``auth_token_ttl_hours`` (24h by default).  The token is stateless — an
HMAC-SHA256 signature over ``{"sub", "iat", "exp"}`` — so nothing is stored
server side and a restart cannot lose a session; the SAME reason means a token
cannot be revoked before it expires except by changing the secret or the
password.  For a one-operator tool that trade is worth the missing session
table.  Format is JWT-shaped but deliberately not JWT: no dependency, one
algorithm, no ``alg`` field for an attacker to negotiate down to ``none``.

FAIL CLOSED — when no username/password is configured, no token can ever be
issued and every protected route answers 401.  The alternative ("no
credentials configured, so let everyone in") would silently publish a
declaration workspace, holding an importer's invoices and party details, to
anyone who can reach the port.  A misconfigured deployment must be visibly
broken, not quietly open.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from .config import get_settings

log = logging.getLogger("easycustoms.auth")

# HttpOnly cookie carrying the same token as the Authorization header.  Needed
# because the reviewer's browser fetches evidence and downloads by NAVIGATION —
# the PDF <iframe>, the XML and .xls <a href> links — where no JavaScript runs
# and no header can be attached.
COOKIE_NAME = "ec_session"

# The secret used when EASYCUSTOMS_AUTH_SECRET is unset: random per process, so
# tokens simply stop verifying after a restart (the operator logs in again).
# Deriving it from the password instead would survive restarts but would put a
# brute-forceable key behind every issued token.
_EPHEMERAL_SECRET = secrets.token_hex(32)
_warned_ephemeral = False


@dataclass(frozen=True)
class Session:
    """A verified token's claims."""
    username: str
    issued_at: int
    expires_at: int


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _configured(value: str | None) -> str | None:
    """Blank and the .env.example placeholders count as NOT configured."""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower().startswith("your_"):
        return None
    return v


def configured_username() -> str | None:
    return _configured(get_settings().auth_username)


def configured_password() -> str | None:
    # Not stripped: leading/trailing spaces are legitimate password characters
    # and silently trimming one turns a correct password into a rejected one.
    pw = get_settings().auth_password
    return None if (pw is None or not pw.strip() or pw.strip().lower().startswith("your_")) else pw


def credentials_configured() -> bool:
    return bool(configured_username()) and bool(configured_password())


def token_ttl_seconds() -> int:
    return int(get_settings().auth_token_ttl_hours * 3600)


def _secret() -> bytes:
    global _warned_ephemeral
    configured = _configured(get_settings().auth_secret)
    if configured:
        return configured.encode("utf-8")
    if not _warned_ephemeral:
        _warned_ephemeral = True
        log.warning("EASYCUSTOMS_AUTH_SECRET is not set — login tokens are signed with a "
                    "per-process key, so every restart (including each uvicorn --reload) "
                    "signs everyone out. Set it in backend/.env to keep sessions across restarts.")
    return _EPHEMERAL_SECRET.encode("utf-8")


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
def _user_key(name: str | None) -> bytes:
    """A username reduced to the bytes the constant-time comparison sees."""
    return unicodedata.normalize("NFC", (name or "").strip()).casefold().encode("utf-8")


def verify_credentials(username: str, password: str) -> bool:
    """Constant-time check of a submitted username + password.

    Both comparisons always run: returning early on an unknown username leaks,
    through response timing, which half of the pair was wrong.
    """
    expected_user = configured_username()
    expected_pw = configured_password()
    if not expected_user or not expected_pw:
        return False
    # The username is typed by a human (phones append a space, some keyboards
    # capitalise the first letter); the password is compared byte for byte.
    #
    # BOTH sides go to bytes first.  hmac.compare_digest accepts str only when
    # both operands are ASCII-only and raises TypeError otherwise, so a
    # non-ASCII username — an accented character, entirely plausible for a
    # Nepali or European operator — used to escape this function as a 500 on
    # the UNAUTHENTICATED login route, before record_failure could even count
    # the attempt.  NFC first so two spellings of the same accented name that
    # compare equal on screen also compare equal here.
    user_ok = hmac.compare_digest(_user_key(username), _user_key(expected_user))
    pw_ok = hmac.compare_digest((password or "").encode("utf-8"),
                                expected_pw.encode("utf-8"))
    return user_ok and pw_ok


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload_b64: str) -> str:
    return _b64e(hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest())


def issue_token(username: str, *, now: int | None = None) -> tuple[str, Session]:
    """Return ``(token, session)`` for a 24h (configurable) session."""
    issued = int(time.time() if now is None else now)
    expires = issued + token_ttl_seconds()
    payload = _b64e(json.dumps({"sub": username, "iat": issued, "exp": expires},
                               separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return (f"{payload}.{_sign(payload)}",
            Session(username=username, issued_at=issued, expires_at=expires))


def verify_token(token: str | None, *, now: int | None = None) -> Session | None:
    """The token's claims, or None when it is absent, malformed, forged or
    expired.  Callers get no detail about WHICH — a probe learns nothing."""
    if not token or "." not in token:
        return None
    payload_b64, _, signature = token.partition(".")
    try:
        if not hmac.compare_digest(signature, _sign(payload_b64)):
            return None
        claims = json.loads(_b64d(payload_b64))
        username, issued, expires = claims["sub"], int(claims["iat"]), int(claims["exp"])
    except Exception:
        return None
    if not isinstance(username, str) or not username:
        return None
    if int(time.time() if now is None else now) >= expires:
        return None
    # A token outlives its account: rotating the username in .env must not
    # leave the previous operator's session working until it expires.
    current = configured_username()
    if not current or _user_key(username) != _user_key(current):
        return None
    return Session(username=username, issued_at=issued, expires_at=expires)


def token_from_request(request) -> str | None:
    """Bearer header first (API clients, the SPA's fetches), then the cookie
    (browser NAVIGATION: the evidence iframe and the download links)."""
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(COOKIE_NAME)


def authenticate_request(request) -> Session | None:
    return verify_token(token_from_request(request))


# --------------------------------------------------------------------------- #
# Who is asking — the key the throttle counts against.
#
# The socket peer is the honest answer only when nothing sits in front of this
# process.  Behind a load balancer every request arrives from the balancer, so
# keying on it collapses every caller into ONE bucket: ten failed sign-ins from
# anywhere lock out everybody, and an unauthenticated attacker can do that on
# purpose.  That is a denial of service built out of a security control.
#
# X-Forwarded-For fixes it and cannot simply be trusted: a caller that can set
# the header would otherwise pick its own throttle key and reset its count at
# will.  What makes it safe is knowing how many hops in front of us are OURS.
# Each trusted proxy APPENDS the address it received the request from, so with
# `hops` trusted proxies the real client is the hops-th entry FROM THE RIGHT;
# everything to the left of it is caller-supplied and worthless.
# --------------------------------------------------------------------------- #
def client_key(request) -> str:
    """The throttle identity of a caller: an IP, from a source we trust."""
    peer = (request.client.host if request.client else "") or "unknown"
    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        # Nothing trusted in front: the socket peer is the only truthful answer,
        # and the header is entirely attacker-controlled.
        return peer
    forwarded = request.headers.get("x-forwarded-for") or ""
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if len(parts) < hops:
        # Fewer entries than there are trusted proxies: this request did not
        # come through them (a direct hit on the app port, or a misconfigured
        # hop count). Falling back to the peer is the safe read — believing a
        # short list would let a caller supply its own key.
        return peer
    return parts[-hops]


# --------------------------------------------------------------------------- #
# Login throttle — a public endpoint with no lockout is one long guessing run.
#
# Stored in the database, not in a dict, because a per-process count is not a
# rate limit on a deployment with more than one process: N processes allow N
# times the intended rate, and whichever one a load balancer happens to pick
# decides whether the caller is blocked.
# --------------------------------------------------------------------------- #
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 300


class ThrottleUnavailable(RuntimeError):
    """The failure count could not be read, so the limit cannot be enforced."""


def _aware(value: datetime) -> datetime:
    """Timestamps are written in UTC; SQLite hands them back without a tzinfo."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def throttle_retry_after(db, client: str) -> int:
    """Seconds the caller must wait, or 0 when it may attempt a login now.

    Raises ThrottleUnavailable rather than returning 0 when the store cannot be
    read.  Answering "go ahead" on a database fault would turn an outage into
    unlimited password guessing against a public endpoint — and this app cannot
    serve a signed-in user without the database anyway, so there is nothing to
    protect by letting the login through.
    """
    from .models import LoginAttempt

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=_FAILURE_WINDOW_SECONDS)
    try:
        stamps = list(db.scalars(
            select(LoginAttempt.created_at)
            .where(LoginAttempt.client_key == client, LoginAttempt.created_at >= cutoff)
            .order_by(LoginAttempt.created_at)))
    except Exception as e:
        raise ThrottleUnavailable(str(e)) from e
    if len(stamps) < _MAX_FAILURES:
        return 0
    # Time until the OLDEST failure in the window ages out, which is when the
    # caller drops back below the limit.
    elapsed = (now - _aware(stamps[0])).total_seconds()
    return max(1, int(_FAILURE_WINDOW_SECONDS - elapsed))


def record_failure(db, client: str) -> None:
    """Count one failed attempt, and prune the window while we are here.

    Never raises: the caller is being told 401 either way, and a failure to
    write the counter must not turn a wrong password into a 500.  It is logged,
    because a throttle that has silently stopped counting is worth knowing about.
    """
    from .models import LoginAttempt

    try:
        db.add(LoginAttempt(client_key=client))
        # Anything older than the window can never affect a decision again.
        # Pruning on write keeps the table a sliding window instead of a
        # permanent log of every failed sign-in anyone has ever made.
        db.execute(sql_delete(LoginAttempt).where(
            LoginAttempt.created_at
            < datetime.now(timezone.utc) - timedelta(seconds=_FAILURE_WINDOW_SECONDS)))
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("could not record a failed sign-in for %s — the login throttle is "
                  "not counting: %s", client, e)


def clear_failures(db, client: str) -> None:
    """Forget this caller's failures after a correct password.  Never raises:
    the sign-in has already succeeded and must not be undone by a cleanup."""
    from .models import LoginAttempt

    try:
        db.execute(sql_delete(LoginAttempt).where(LoginAttempt.client_key == client))
        db.commit()
    except Exception as e:
        db.rollback()
        log.warning("could not clear failed sign-ins for %s: %s", client, e)


def reset_throttle(db=None) -> None:
    """Empty the window — a test hook, and the manual unlock for an operator who
    has locked themselves out.  Opens its own session when not given one."""
    from .database import SessionLocal
    from .models import LoginAttempt

    session = db or SessionLocal()
    try:
        session.execute(sql_delete(LoginAttempt))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if db is None:
            session.close()
