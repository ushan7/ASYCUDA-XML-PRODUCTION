"""Supabase connectivity check — proves the URL and ANON key this app is
configured with actually reach the project's auth endpoints.

Run from the backend directory:

    python scripts/check_supabase_connection.py

WHY THE ANON KEY, AND WHY THERE IS NO SERVICE-ROLE KEY HERE.  This script used
to require SUPABASE_SERVICE_KEY out of backend/.env — the same file the API
process reads — which made a connectivity check into an instruction to put a
service-role key in the API's environment.  app/auth_supabase.py argues at
length why that key is deliberately absent from this deployment: nothing in the
request path acts as an administrator, and holding one turns any code-execution
bug in a process that parses uploaded PDFs into control of the whole auth
database.  A diagnostic must not be the reason a credential exists.

The anon key is also the RIGHT key, not merely the safer one.  What this script
is for is answering "will sign-in work when I start the app?", and sign-in is
`POST /auth/v1/token` with the anon key (app/auth_supabase.py).  Checking with a
service key would prove that a credential the app does not hold can reach a
project — which is true of a misconfigured deployment too.

WHY IT NO LONGER SELECTS FROM `document_generations`.  That table comes from
supabase/migrations/, which describes a prototype schema this backend has never
read; the app's own schema is alembic's.  Two things followed.  It is enabled
for row-level security with `USING (auth.uid() = user_id)`, so an anon caller
matches no rows and the check would pass on an empty result either way.  And a
NEW project — which is what docs/deploy-staging.md tells a staging operator to
create — does not have that table at all, so the old script would have reported
a correctly configured project as broken and sent somebody off to create tables
this app does not use.

VALUES COME FROM THE APP'S OWN SETTINGS, not from a second dotenv read.  The
previous version read bare SUPABASE_URL out of os.environ, so a deployment that
set EASYCUSTOMS_SUPABASE_URL (which wins) was checked at one project and ran
against another.  Reading through app.config means this checks exactly the
values app/auth_supabase.py will use, aliases, precedence and all.

Read-only: `GET /auth/v1/settings` creates no account and sends no email.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings          # noqa: E402

SETTINGS_PATH = "/auth/v1/settings"
TIMEOUT_SECONDS = 15.0


def main() -> int:
    import httpx

    settings = get_settings()
    url = settings.supabase_url.strip().rstrip("/")
    key = settings.supabase_anon_key.strip()

    print("auth provider :", settings.auth_provider)
    if not url or not key:
        print("\n[!] No Supabase URL / anon key is configured.")
        print("    Set SUPABASE_URL and SUPABASE_ANON_KEY in backend/.env")
        print("    (see the 'Who holds the accounts' block in .env.example).")
        print("    The service-role key is NOT one of them and must not be set here.")
        return 1
    print("supabase url  :", url)
    # Never the key itself: this output gets pasted into chat messages and issues.
    print("anon key      : configured")

    print(f"\n>> GET {SETTINGS_PATH} ...")
    try:
        response = httpx.get(f"{url}{SETTINGS_PATH}",
                             headers={"apikey": key, "Authorization": f"Bearer {key}"},
                             timeout=TIMEOUT_SECONDS)
    except Exception as e:
        print(f"\n[!] Could not reach the project: {type(e).__name__}: {e}")
        return 1

    if response.status_code == 401:
        # ASCII only in printed strings: the Windows console is cp1252 here and
        # turns an em-dash into a literal '?'.
        print("\n[!] 401 - the project answered, but rejected this key.")
        print("    Check SUPABASE_ANON_KEY against Project Settings -> API.")
        return 1
    if response.status_code >= 400:
        print(f"\n[!] The project answered {response.status_code}.")
        print("    Check SUPABASE_URL against Project Settings -> API.")
        return 1

    try:
        body = response.json()
    except Exception:
        body = {}

    # ASCII only in printed strings: the Windows console is cp1252 here and
    # turns an em-dash into a literal '?'.
    print("\nOK - the project is reachable and accepted the anon key.")
    print("     Sign-in, signup and password reset all use this same key.")

    # Free, and worth printing: these are two of the dashboard settings
    # docs/deploy-staging.md has to list as unverifiable from the app side.
    # Reported, never enforced - this script decides nothing.
    if isinstance(body, dict):
        if "disable_signup" in body:
            disabled = bool(body["disable_signup"])
            print(f"\n     Sign-ups         : {'DISABLED' if disabled else 'enabled'}")
            if disabled:
                print("       -> /auth/v1/signup will 422, whatever ALLOW_SELF_SIGNUP says.")
        if "mailer_autoconfirm" in body:
            autoconfirm = bool(body["mailer_autoconfirm"])
            print(f"     Confirm email    : {'OFF (autoconfirm)' if autoconfirm else 'on'}")
            if autoconfirm:
                print("       -> accounts are usable immediately and never receive a")
                print("          confirmation mail. That toggle is the primary anti-abuse")
                print("          control for open registration; turn it on.")
    print("\n     Not checked here: SMTP, Site URL and the Redirect URLs allow-list.")
    print("     Nothing outside a real mailbox can check those - see")
    print("     docs/deploy-staging.md, 'What cannot be verified until it is deployed'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
