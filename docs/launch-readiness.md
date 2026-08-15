# Launch readiness

What has to hold before real customs brokers file declarations built by this app.
Written from the code, not from memory — every claim below names the thing that
enforces it, so you can check rather than trust.

`docs/deploy-staging.md` is how you *stand a box up*. This is the shorter list of
what must be **true** before you let somebody who is not you use it.

---

## 1. The boot refusals — the checks that already ran

The process will not start if any of these is wrong, so a running server has
already passed them. They exist because each failure would otherwise arrive as
something other than an error. All of them live in
`config.Settings._check_auth_provider_config` and are keyed on
`EASYCUSTOMS_AUTH_PROVIDER=supabase` — **the multi-account provider, i.e. the
shape a real deployment has.** Under `local` (one account, the demo, a broker's
own laptop) they do not apply, and the tests run under `local`, which is why the
suite is green without any of this.

| Refused at boot | What it prevents |
| --- | --- |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` unset | every sign-in answering 502 and looking like a credential problem |
| `EASYCUSTOMS_AUTH_SECRET` unset | the session key is generated **per process**, so two uvicorn workers sign with different keys and each rejects the other's sessions as forged — at random, on every other request |
| `USAGE_MONTHLY_DOCUMENT_CAP=0` | an unbounded Mistral + OpenAI bill payable by anyone who can obtain an account. The failure arrives as an invoice weeks later |
| a live OCR/extraction provider with **no key** | the one that matters most — see §4 |
| `ALLOW_SELF_SIGNUP=true` under `local` | a registration form on a deployment where a second account could never sign in |
| `PASSWORD_RESET_REDIRECT_URL` that is not an absolute `http(s)` origin | Supabase ignores anything else *silently* and mails every user a link to the project's Site URL instead |

**What no refusal covers, and you must therefore check by hand:**

- A key that is **present but wrong, expired, or out of quota.** That is not a
  misconfiguration, and it fails loudly — the document goes to `FAILED` with
  `EXTRACTION_FAILED` in the job's audit trail. Confirm it before launch with
  `python scripts/live_smoke_test.py`, which hits both vendors for real.
- `EASYCUSTOMS_EXTRACTION_PROVIDER=langroid` with the **library** not installed.
  Still a warning and a silent fall back to offline: `pip install langroid` is
  not a setting, so a settings validator cannot ask. Don't select `langroid`.
- `TRUSTED_PROXY_HOPS`. A wrong value never errors. Too low and every caller
  behind your proxy shares one throttle bucket; too high and nobody is throttled
  at all. The only proof is triggering a limiter from two different addresses.

---

## 2. A downloaded XML is **not** evidence that nothing was flagged

`xml_strict_blocking` is **false** by default (`config.py`), and that is a
deliberate rule, not an oversight: blocking cases warn and the XML is still
generated, so the reviewer can test it in real ASYCUDA.

So the artifact you can download exists in two states that look identical:

- built from a declaration with no blockers, and
- built **over** unresolved blockers, with `xml_built_with_blockers = true` on
  the declaration (`services.py`, `declaration/models.py`).

`GET /api/jobs/{id}/xml` returns bytes and says nothing about which. **The flag
is on the declaration, not the file.**

> **Tell every broker this in the onboarding, in these words:** a downloaded XML
> means the composer ran, not that the declaration is clean. The screen lists the
> unresolved cases; the file does not carry them.

To check a specific job:

```bash
curl -s -b cookies.txt https://customs.aindalabs.com/api/jobs/<JOB_ID>/declaration | python -m json.tool | grep -A20 -e xml_built_with_blockers -e blocking_errors
```

A small set of codes (`WARN_MODE_HARD_CODES`, e.g. anything that would declare a
zero gross weight on every line) still blocks generation even in warn mode. Set
`EASYCUSTOMS_XML_STRICT_BLOCKING=true` only if you have decided that no broker
should ever receive a file over blockers — it changes what the product does.

---

## 3. The document cap, and raising it for one account

Two levels, and only one of them should move after launch:

- **Deployment default** — `EASYCUSTOMS_USAGE_MONTHLY_DOCUMENT_CAP`, or 200 a
  month when unset under `supabase` (`DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP`).
  Changing this changes it for **everyone**, including accounts that do not exist
  yet.
- **One account** — an `account_quota` row, which wins over the default
  (`metering.resolved_cap`).

A new account gets **no row** on purpose, so the default can still be changed for
accounts that already exist. Raise one account instead:

```bash
curl -X PUT https://customs.aindalabs.com/api/admin/accounts/<OWNER_KEY>/quota -b admin-cookies.txt -H 'content-type: application/json' -d '{"monthly_document_cap": 1000, "note": "paid tier, ticket #42"}'
```

`monthly_document_cap` has **no default in the request body** — `null` means
"follow the deployment default", `0` means unlimited for this account, `n` means
n. Omitting the field is a 422 rather than a guess. `note` is stored on the row
and `updated_by` records who set it — fill the note in, because it is what the
next administrator reads before changing it back.

Finding the `owner_key` for `alice@broker.np`: `GET /api/admin/accounts` lists
accounts this deployment has **seen** (signed in at least once) with a
`display_label` from the sign-in record. An account that registered and never
confirmed its email is **not** in that list — which is the same fact as "the
confirmation link was never followed".

Admin routes need the `admin` role, which no route can grant. The first one comes
from a shell on the box:

```bash
cd backend && python scripts/grant_admin.py --list
```

An admin manages accounts and quotas and **cannot read anyone's declarations,
documents, XML or job audit trail** — same 404 as any other stranger
(`docs/ADR-001-identity-and-tenancy.md`). If a broker needs support on a specific
job, you need them on a screen share; there is no impersonation route and adding
one is an ADR amendment, not a feature.

---

## 4. Which extractor actually ran

**This is the failure that looks like success.** With a live provider selected and
its key missing, the fallback to the offline reader (pypdf text layer, heuristic
extraction) is **per extraction and logged at WARNING**. The process boots clean,
answers `/api/health`, accepts the document, and hands the reviewer a
complete-looking ASYCUDA declaration assembled from facts nothing read off the
paperwork — every deterministic rule downstream running perfectly on invented
input. No 500, no failed job, nothing red.

The boot refusal in §1 closes the *missing key* case under `supabase`. Check
anyway, because the check does not depend on the refusal being right:

**In the server journal** — the fallback announces itself:

```bash
sudo journalctl -u easycustoms-api --since "1 hour ago" | grep -i "falling back to offline"
```

Nothing returned is the answer you want.

**In the job's audit trail** — `DOCUMENT_EXTRACTED` carries the provider that
actually ran (`services.py`), and it must say `mistral` / `openai`, never
`offline`:

```bash
curl -s -b cookies.txt https://customs.aindalabs.com/api/jobs/<JOB_ID>/audit | python -m json.tool | grep -B2 -A4 DOCUMENT_EXTRACTED
```

Note the constraint: that route is **job-scoped and owner-only**. An admin gets a
404 on someone else's job, so for a customer's job the journal is your only
route — this is a deliberate consequence of §3, not a gap to route around.

Naming `offline` outright (`EASYCUSTOMS_OCR_PROVIDER=offline`) still boots
everywhere, on purpose: an explicit choice is not a misconfiguration. **Do not
launch with it.**

---

## 5. When a user cannot receive email

This deployment holds only Supabase's **anon key**; the service-role key is
deliberately absent, so *this app cannot read the user list, resend a
confirmation, or set anybody's password.* All of that happens in the Supabase
dashboard (Auth → Users), by a human with dashboard access.

**First, know what you cannot learn from the API.** Both public routes answer the
same thing for every accepted outcome, on purpose:

- `POST /api/auth/password-reset` answers **202 always** — including for an
  address with no account, the provider's per-address send cooldown, and a
  delivery failure.
- `POST /api/auth/signup` answers **202** for the same set — created, already
  registered, the provider's 429, and a confirmation email that failed to send.

Each is an account-existence oracle if it distinguishes them, so the API will
never tell you whether that user exists or whether mail went out. **The log will**
— that is where the difference was moved to:

```bash
sudo journalctl -u easycustoms-api --since today | grep -E "did not send a password-reset email|did not complete a signup|accepted a password-reset request"
```

| What you find | What it means | What to do |
| --- | --- | --- |
| `did not send a password-reset email` / `did not complete a signup` (ERROR) | the provider accepted the request and the mail did not go | Supabase → Auth → Emails / SMTP. **The user was told a link is on its way** |
| `accepted a password-reset request` (INFO) and the user still has nothing | the mail was sent and lost downstream | spam folder, then the corporate mail filter. Check Supabase's own auth logs |
| nothing at all for that time | the request never reached the provider | our caller-keyed limiter refused it first (5/hour, 20/day per caller — a whole office behind one NAT address shares it), or they never submitted |

**Set SMTP to a real provider before launch.** Supabase's built-in sender is
rate-limited to a handful of messages an hour and is explicitly not for
production; left alone, signup appears to work and the confirmation emails
silently do not arrive.

**The manual route, when a broker simply cannot receive mail:** create or fix the
account in the Supabase dashboard with **Confirm email** already set, and give
them the password out of band. Do not switch the project's "Confirm email" off to
work around one user — that is the primary anti-abuse control, and turning it off
makes every unverified address able to spend this deployment's vendor budget. The
app detects it and logs a WARNING naming the setting, which is the only reason
you would ever find out.

---

## 6. Residuals — known, accepted, and worth saying out loud

- **A distributed attacker mailbombing one address** is not bounded by our
  limiters, which are keyed on the caller. What bounds it is Supabase's own
  per-address rate limit (Auth → Rate limits). Set it explicitly.
- **A password reset issues no session**, by design. A recovery token proves
  control of a mailbox; the user signs in afterwards like anybody else. If a
  broker reports "I set a new password and it didn't log me in", that is correct
  behaviour.
- **Refused signups are not counted** against the caller's registration budget,
  so somebody fumbling a password is not locked out for an hour. What bounds that
  direction is the provider's own signup rate limit — the same dashboard row.
- **Supabase row-level security does not protect this app.** The backend connects
  to Postgres as a privileged role and bypasses RLS entirely. Job isolation is
  Python (`services.job_visible_to`) and is tested as Python.
