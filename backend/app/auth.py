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
    """A verified token's claims.

    Two identifiers, and keeping them apart is the whole point:

      ``user_id``  WHO OWNS WHAT.  Stable for the life of the account — a
                   Supabase ``auth.users.id``, or the configured username on the
                   local provider.  This is what ``Job.owner_key`` holds and
                   what every access check compares.
      ``username`` WHAT TO CALL THEM.  An email or login name, for the screen
                   and for the audit trail.  It can change; ownership must not
                   change with it.

    Collapsing the two is how a later email change silently hands someone else's
    declarations to nobody, or how an audit correction quietly grants access.
    """
    username: str
    issued_at: int
    expires_at: int
    user_id: str = ""

    def principal(self) -> str:
        """The access key. Falls back to the display name for tokens minted
        before user_id existed, which on the local provider are the same value."""
        return self.user_id or self.username


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


@dataclass(frozen=True)
class Identity:
    """A verified sign-in: who they are, and what to call them."""
    user_id: str
    display: str


class LoginUnavailable(Exception):
    """The credentials could not be CHECKED — distinct from being wrong.

    Reporting an outage as a bad password sends an operator to reset a password
    that was never the problem, and hides the fault that is.
    """


def verify_login(username: str, password: str) -> Identity | None:
    """Check credentials against whichever provider holds the accounts.

    Returns None for "wrong", raises LoginUnavailable for "could not tell".
    """
    if get_settings().auth_provider == "supabase":
        from . import auth_supabase

        try:
            identity = auth_supabase.sign_in(username.strip(), password)
        except auth_supabase.SupabaseAuthUnavailable as e:
            raise LoginUnavailable(str(e)) from e
        if identity is None:
            return None
        return Identity(user_id=identity.user_id, display=identity.email)

    if not verify_credentials(username, password):
        return None
    # The local account's id IS its configured name: one account, and the token
    # names the CONFIGURED spelling rather than the typed one, because the
    # comparison is case-insensitive and ownership must not vary by keystroke.
    name = configured_username() or username.strip()
    return Identity(user_id=name, display=name)


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


def issue_token(username: str, *, user_id: str | None = None,
                now: int | None = None) -> tuple[str, Session]:
    """Return ``(token, session)`` for a 24h (configurable) session.

    ``sub`` is the USER ID, not the display name, so a later email change cannot
    orphan the jobs that account owns.  ``dsp`` carries the name to show and to
    write on audit rows.  ``user_id`` defaults to ``username`` because on the
    local provider they are the same single account.
    """
    issued = int(time.time() if now is None else now)
    expires = issued + token_ttl_seconds()
    subject = (user_id or username).strip()
    payload = _b64e(json.dumps(
        {"sub": subject, "dsp": username, "iat": issued, "exp": expires},
        separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return (f"{payload}.{_sign(payload)}",
            Session(username=username, issued_at=issued, expires_at=expires,
                    user_id=subject))


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
        subject, issued, expires = claims["sub"], int(claims["iat"]), int(claims["exp"])
        # Tokens minted before `dsp` existed carry only `sub`, which on the
        # local provider WAS the username.
        display = claims.get("dsp") or subject
    except Exception:
        return None
    if not isinstance(subject, str) or not subject:
        return None
    if int(time.time() if now is None else now) >= expires:
        return None
    if get_settings().auth_provider == "local":
        # One account, named in the environment: a token outlives its account,
        # so rotating the username in .env must not leave the previous
        # operator's session working until it expires.
        #
        # There is no equivalent check for Supabase, and inventing one here
        # would be worse than none: the token is signed by THIS server and its
        # subject is a stable account id, so the meaningful question is whether
        # that account is still active — which is a live question for Supabase
        # to answer, not something this signature can encode. Until it is asked
        # per request, a deactivated Supabase user keeps access for the
        # remainder of their token's lifetime (24h by default). Same window the
        # local provider has always had for a changed password.
        current = configured_username()
        if not current or _user_key(subject) != _user_key(current):
            return None
    return Session(username=display, issued_at=issued, expires_at=expires,
                   user_id=subject)


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
    """Empty the FAILURE window — a test hook, and the manual unlock for an
    operator who has locked themselves out.  Opens its own session when not
    given one.

    Deliberately does not touch ``signup_attempt``: the suite calls this between
    every test (tests/conftest.py), and a signup limiter it emptied would be a
    limiter no test could ever observe holding.
    """
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


# --------------------------------------------------------------------------- #
# Signup limiter — the same shape as the throttle above, and NONE of its state.
#
# The login throttle does not cover self-service registration, and reusing it
# would make things worse.  Three reasons, each independent:
#
#   1. It is not middleware.  `throttle_retry_after` / `record_failure` are
#      called from the body of the login handler, so a new public route inherits
#      nothing from them.
#   2. It counts FAILURES.  A row is written only when `verify_login` returns
#      None, so ten thousand scripted registrations — every one of them valid —
#      record zero failures and are never slowed.
#   3. Sharing the key would WEAKEN it.  A correct password calls
#      `clear_failures`, wiping that caller's window.  If a signup shared the key
#      and cleared on success, an attacker would interleave a registration
#      between password guesses to reset their guessing budget indefinitely.
#
# So: a success counter, in its own table, never cleared on success — success is
# the thing being counted — and keyed on the same `client_key` so
# `trusted_proxy_hops` cannot be right for one limiter and wrong for the other.
#
# WHAT IT COUNTS is a signup this app ACCEPTED, which is a superset of accounts
# created and never fewer: with email confirmation on, Supabase deliberately
# answers an already-registered address exactly as it answers a new one, so
# "an account was created" is not a fact this process can know.  A refused
# signup (a password the policy rejects, an address that will not validate) is
# NOT counted — the caller has created nothing, and locking someone out for an
# hour because they typed a short password twice is a limiter doing damage
# rather than work.  What bounds that direction is the identity provider's own
# signup rate limit, which the deployment checklist says to set.
# --------------------------------------------------------------------------- #
_SIGNUP_MAX_PER_HOUR = 3
_SIGNUP_MAX_PER_DAY = 10
_SIGNUP_HOUR_SECONDS = 3600
_SIGNUP_DAY_SECONDS = 86_400


class SignupLimitUnavailable(RuntimeError):
    """The signup count could not be read, so the limit cannot be enforced."""


def signup_retry_after(db, client: str) -> int:
    """Seconds before this caller may register again, or 0 to go ahead.

    Two windows, because they answer different questions: the hourly one stops a
    burst, the daily one stops a patient script that spaces its registrations an
    hour apart.  The caller waits for whichever is longer.

    Raises SignupLimitUnavailable rather than returning 0 when the store cannot
    be read — FAIL CLOSED, exactly as `throttle_retry_after` does.  An unreadable
    counter that answered "go ahead" would turn a database fault into unlimited
    account creation, and every one of those accounts can spend real money at
    Mistral and OpenAI.
    """
    from .models import SignupAttempt

    now = datetime.now(timezone.utc)
    day_cutoff = now - timedelta(seconds=_SIGNUP_DAY_SECONDS)
    try:
        stamps = [_aware(s) for s in db.scalars(
            select(SignupAttempt.created_at)
            .where(SignupAttempt.client_key == client,
                   SignupAttempt.created_at >= day_cutoff)
            .order_by(SignupAttempt.created_at))]
    except Exception as e:
        raise SignupLimitUnavailable(str(e)) from e

    wait = 0.0
    within_hour = [s for s in stamps if s >= now - timedelta(seconds=_SIGNUP_HOUR_SECONDS)]
    if len(within_hour) >= _SIGNUP_MAX_PER_HOUR:
        # Until the OLDEST signup in the hour ages out, which is when this caller
        # drops back below the limit.
        wait = max(wait, _SIGNUP_HOUR_SECONDS - (now - within_hour[0]).total_seconds())
    if len(stamps) >= _SIGNUP_MAX_PER_DAY:
        wait = max(wait, _SIGNUP_DAY_SECONDS - (now - stamps[0]).total_seconds())
    return max(1, int(wait)) if wait > 0 else 0


def record_signup(db, client: str) -> None:
    """Count one accepted registration, and prune the window while we are here.

    Never raises, for the same reason `record_failure` does not: by the time this
    is called the identity provider has already created the account, and this app
    cannot roll that back — so failing the request here would tell a caller their
    registration failed when it did not.  Logged at ERROR instead, because a
    limiter that has silently stopped counting is the whole control.

    Pruning uses the LONGEST window.  Pruning at the hourly one would delete the
    rows the daily limit is made of, and the daily limit would quietly stop
    existing — which is the trap a `kind` column on `login_attempt` would have
    walked straight into, since that table prunes at five minutes.
    """
    from .models import SignupAttempt

    try:
        db.add(SignupAttempt(client_key=client))
        db.execute(sql_delete(SignupAttempt).where(
            SignupAttempt.created_at
            < datetime.now(timezone.utc) - timedelta(seconds=_SIGNUP_DAY_SECONDS)))
        db.commit()
    except Exception as e:
        db.rollback()
        log.error("could not record a signup for %s — the signup limiter is not "
                  "counting: %s", client, e)


def reset_signup_limiter(db=None) -> None:
    """Empty the signup window — a test hook, and the manual unlock for a real
    caller who has been limited by a misconfigured `trusted_proxy_hops` (behind a
    load balancer at hops=0 every caller shares one bucket).  Opens its own
    session when not given one."""
    from .database import SessionLocal
    from .models import SignupAttempt

    session = db or SessionLocal()
    try:
        session.execute(sql_delete(SignupAttempt))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if db is None:
            session.close()
