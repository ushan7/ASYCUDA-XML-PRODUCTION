"""Sign-in against Supabase Auth, proxied through this server.

The browser never talks to Supabase.  It posts to /api/auth/login exactly as it
always has, this module exchanges those credentials for a Supabase identity, and
the app then issues its OWN session token and HttpOnly cookie (app/auth.py).

That indirection is deliberate and is the reason the rest of the app is
unchanged:

  * the SPA is served same-origin by this app, and its session lives in an
    HttpOnly cookie *because* the evidence iframe and the XML/.xls download
    links are plain navigations that carry no header.  Supabase's browser SDK
    would put a token somewhere JavaScript can read, which is both a weaker
    place for it and unusable by those two flows;
  * every request keeps verifying one locally-signed token, so the auth
    middleware costs no network call — a per-request round trip to an external
    service on the path of 1500 concurrent reviewers is not a thing to add
    lightly;
  * the login throttle, the audit actor and job ownership all keep working
    against one shape of session, whoever issued the credentials.

What Supabase owns in exchange: the password, its hashing, email verification,
password reset, and per-account lockout — every one of which is real work to
build safely and none of which belongs in a customs declaration tool.

Only the ANON key is used.  The password grant is what that key is for.  The
service-role key is deliberately absent from this process: nothing in the
request path needs to act as an administrator, and holding one here would turn
any code-execution bug into control of the whole auth database.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import get_settings

log = logging.getLogger("easycustoms.auth")

# Supabase's own endpoint for "exchange an email and password for a session".
_TOKEN_PATH = "/auth/v1/token"


class SupabaseAuthUnavailable(Exception):
    """Supabase could not be reached, or answered in a way we cannot read.

    Distinct from "wrong password" on purpose: one is the caller's problem and
    the other is ours, and reporting an outage as a credential failure sends an
    operator to reset a password that was never wrong.
    """


@dataclass(frozen=True)
class SupabaseIdentity:
    user_id: str          # auth.users.id — the UUID job ownership is keyed by
    email: str            # display + audit label; never the ownership key


def sign_in(email: str, password: str) -> SupabaseIdentity | None:
    """Verified identity, or None when the credentials are wrong.

    Raises SupabaseAuthUnavailable for anything that is not a clean yes or no.
    """
    import httpx

    settings = get_settings()
    base = settings.supabase_url.rstrip("/")
    try:
        response = httpx.post(
            f"{base}{_TOKEN_PATH}",
            params={"grant_type": "password"},
            headers={"apikey": settings.supabase_anon_key,
                     "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=settings.supabase_timeout_seconds,
        )
    except Exception as e:
        raise SupabaseAuthUnavailable(f"{type(e).__name__}: {e}") from e

    if response.status_code in (400, 401, 403):
        # Supabase says no. It distinguishes "wrong password" from "email not
        # confirmed" in the body; the caller is told neither, for the same
        # reason the local provider gives one message for both halves of a
        # failed sign-in — which of the two was wrong is not a stranger's
        # business. It is logged here so an operator can tell them apart.
        log.info("supabase refused a sign-in (%s): %.200s",
                 response.status_code, response.text)
        return None
    if response.status_code != 200:
        raise SupabaseAuthUnavailable(f"unexpected status {response.status_code}")

    try:
        user = response.json()["user"]
        user_id = str(user["id"])
        email_out = str(user.get("email") or email)
    except Exception as e:
        # A 200 we cannot read is NOT a successful sign-in. Treating it as one
        # would admit a caller on the strength of a response we did not
        # understand.
        raise SupabaseAuthUnavailable(f"unreadable sign-in response: {e}") from e
    if not user_id:
        raise SupabaseAuthUnavailable("sign-in response carried no user id")
    return SupabaseIdentity(user_id=user_id, email=email_out)
