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
# Login throttle — a single account with no lockout is one long guessing run.
# In-process and best effort (per worker, cleared by a restart): enough to make
# online guessing impractically slow without adding a store to a stateless app.
# --------------------------------------------------------------------------- #
_MAX_FAILURES = 10
_FAILURE_WINDOW_SECONDS = 300

_failures: dict[str, list[float]] = {}


def _recent(client: str, now: float) -> list[float]:
    stamps = [t for t in _failures.get(client, []) if now - t < _FAILURE_WINDOW_SECONDS]
    if stamps:
        _failures[client] = stamps
    else:
        _failures.pop(client, None)
    return stamps


def throttle_retry_after(client: str) -> int:
    """Seconds the caller must wait, or 0 when it may attempt a login now."""
    now = time.time()
    stamps = _recent(client, now)
    if len(stamps) < _MAX_FAILURES:
        return 0
    return max(1, int(_FAILURE_WINDOW_SECONDS - (now - stamps[0])))


def record_failure(client: str) -> None:
    now = time.time()
    _failures.setdefault(client, []).append(now)
    _recent(client, now)


def clear_failures(client: str) -> None:
    _failures.pop(client, None)


def reset_throttle() -> None:
    """Test hook — the failure memory is process-wide."""
    _failures.clear()
