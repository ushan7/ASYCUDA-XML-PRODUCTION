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
# ...and for "create an account".  Both are anon-key endpoints; neither needs
# the service-role key, which is why registration can live here at all.
_SIGNUP_PATH = "/auth/v1/signup"


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


# --------------------------------------------------------------------------- #
# Registration
#
# Supabase owns the account, the password policy, the confirmation email and the
# link in it.  This function's whole job is to ask, and to reduce whatever comes
# back to something a public endpoint can answer without becoming an
# ACCOUNT-EXISTENCE ORACLE for any address a stranger wants to test.
#
# That is why "already registered" is reported as ACCEPTED rather than as an
# error.  It is also Supabase's own behaviour when email confirmation is on: it
# answers a taken address with an obfuscated user object, precisely so the caller
# cannot tell.  With confirmation off it says so outright (422
# user_already_exists) — and this function flattens that difference rather than
# letting a dashboard toggle decide whether the app leaks the customer list.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SignupOutcome:
    """What became of a registration request, in the only terms a caller needs.

    ``accepted`` means "answer 202" — the account was created, OR the address was
    already registered and saying so is not this app's business.  The two are
    deliberately the same value; nothing downstream can tell them apart, which is
    the property, not a limitation to be worked around.
    """
    accepted: bool
    # Whether a confirmation email is what completes this registration.  False on
    # an accepted signup means the project has email confirmation OFF and the
    # account is already usable — a misconfiguration worth shouting about, never
    # a difference the caller is shown.
    confirmation_required: bool = True
    # Set only when `accepted` is False: which refusal this was, in OUR
    # vocabulary rather than the vendor's.
    code: str = ""
    # Safe to show the caller ONLY for a password-policy refusal, which says
    # nothing about the address.  Empty for everything else.
    message: str = ""
    retry_after: int = 0


def _error_fields(response) -> tuple[str, str]:
    """``(code, message)`` from a Supabase error body, defensively.

    GoTrue has used ``error_code``, ``code``, ``error`` and ``error_description``
    across versions, and a proxy in front of it may return no JSON at all.  An
    unreadable error body must produce a generic refusal, never an exception —
    the request has already failed and the caller is owed an answer.
    """
    try:
        body = response.json()
    except Exception:
        return "", ""
    if not isinstance(body, dict):
        return "", ""
    code = str(body.get("error_code") or body.get("code") or body.get("error") or "")
    message = str(body.get("msg") or body.get("message")
                  or body.get("error_description") or "")
    return code.strip().lower(), message.strip()


def _retry_after(response, message: str) -> int:
    header = (getattr(response, "headers", None) or {}).get("retry-after") or ""
    try:
        return max(0, int(str(header).strip()))
    except (TypeError, ValueError):
        pass
    # GoTrue's own wording: "For security purposes, you can only request this
    # after 47 seconds."  A number in the sentence is better than no number.
    import re
    found = re.search(r"(\d+)\s*second", message or "")
    return int(found.group(1)) if found else 0


def sign_up(email: str, password: str) -> SignupOutcome:
    """Ask Supabase to create an account.  Raises SupabaseAuthUnavailable for
    anything that is not a definite answer.

    NO SESSION IS EVER RETURNED from here, even when Supabase issues one (which
    it does when email confirmation is off).  A route that sometimes signs you in
    and sometimes does not is a route whose clients get it wrong, and the whole
    anti-abuse case for confirmation rests on a new account being unusable until
    the address is proved.
    """
    import httpx

    settings = get_settings()
    base = settings.supabase_url.rstrip("/")
    try:
        response = httpx.post(
            f"{base}{_SIGNUP_PATH}",
            headers={"apikey": settings.supabase_anon_key,
                     "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=settings.supabase_timeout_seconds,
        )
    except Exception as e:
        raise SupabaseAuthUnavailable(f"{type(e).__name__}: {e}") from e

    status = response.status_code
    if status in (200, 201):
        try:
            body = response.json()
        except Exception as e:
            # Same rule as sign_in: a 200 we cannot read is not an outcome we
            # assert.  The caller is told it could not be completed and may
            # retry — which, if the account WAS created, answers 202 next time.
            raise SupabaseAuthUnavailable(f"unreadable signup response: {e}") from e
        if not isinstance(body, dict):
            raise SupabaseAuthUnavailable("signup response was not an object")
        user = body.get("user") if isinstance(body.get("user"), dict) else body
        # A session in the body means the project has "Confirm email" OFF: the
        # account is immediately usable and the primary anti-abuse control the
        # deployment checklist calls for is not in force.  The token is discarded
        # here and never leaves this function.
        confirmed_already = bool(body.get("access_token")) or bool(
            (user or {}).get("email_confirmed_at"))
        if (user or {}).get("identities") == []:
            # Supabase's anti-enumeration shape for an address that is already
            # registered.  Logged, because an operator investigating "I never got
            # the email" needs to know, and NEVER returned — the caller is told
            # exactly what a new registration is told.
            log.info("supabase answered a signup with an obfuscated user "
                     "(the address is already registered)")
        if confirmed_already:
            log.warning("a signup completed WITHOUT email confirmation — this Supabase "
                        "project has 'Confirm email' off, so the account is usable "
                        "immediately and unverified addresses can spend this "
                        "deployment's OCR budget. Turn it on in Auth -> Providers -> "
                        "Email.")
        return SignupOutcome(accepted=True, confirmation_required=not confirmed_already)

    if status in (400, 422):
        code, message = _error_fields(response)
        haystack = f"{code} {message}".lower()
        if "already" in haystack or "exists" in haystack or "registered" in haystack:
            # Reported as success on purpose — see the module note above.
            log.info("supabase refused a signup as already registered (%s); answering "
                     "the caller exactly as a new registration is answered", status)
            return SignupOutcome(accepted=True)
        if "password" in haystack:
            # The ONE refusal whose text is safe to forward: it is about the
            # password the caller just chose and says nothing about the address.
            # Forwarded rather than reworded so the deployment's own policy —
            # length, HIBP leaked-password checking — is what the user is told.
            return SignupOutcome(accepted=False, code="WEAK_PASSWORD",
                                 message=message or "That password does not meet this "
                                                    "site's password policy.")
        if ("signup" in haystack and "disabled" in haystack) or "not allowed" in haystack:
            # OUR flag says registration is open and the identity provider says it
            # is closed.  A misconfiguration, and the caller cannot fix it.
            log.error("supabase refused a signup because sign-ups are disabled for the "
                      "project, while EASYCUSTOMS_ALLOW_SELF_SIGNUP is on — enable "
                      "Auth -> Providers -> Email -> Enable Sign Ups, or turn the flag off")
            return SignupOutcome(accepted=False, code="SIGNUP_UNAVAILABLE")
        log.info("supabase refused a signup (%s): %.200s", status, message or code)
        return SignupOutcome(accepted=False, code="INVALID_EMAIL")

    if status == 429:
        _, message = _error_fields(response)
        log.warning("supabase rate-limited a signup (429): %.200s", message)
        return SignupOutcome(accepted=False, code="TOO_MANY_ATTEMPTS",
                             retry_after=_retry_after(response, message))

    # 401/403 means OUR anon key is wrong or the project moved; 5xx is theirs.
    # Neither is the caller's fault and neither is a definite answer about the
    # account, so it is an outage, not a refusal.
    raise SupabaseAuthUnavailable(f"unexpected status {status}")
