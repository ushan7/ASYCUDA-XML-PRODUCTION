# Plan — self-service signup, password reset, and the admin/member role

**Status: plan only. Nothing here is implemented.**

Follows `docs/ADR-001-identity-and-tenancy.md`, which decides that the account is
the tenant and that a platform admin manages accounts and quotas and cannot read
declarations. This document does not reopen either.

## Phase 0 — a prerequisite, not part of this work

`tests/test_tenant_isolation.py` currently fails four assertions covering one
defect: `GET /api/jobs/{job_id}/xml` and
`GET /api/jobs/{job_id}/brand-model-size.xls` resolve their artifact by
`job_id` alone and never call `services.job_visible_to`. Any authenticated user
with a job id downloads that job's declaration.

**Nothing below ships before that is fixed and the suite is green.** Opening
self-service registration while a cross-account read exists converts "an
attacker needs an account" into "an attacker makes an account".

---

# a. Self-service signup and password reset

The division of labour is fixed by `app/auth_supabase.py` and does not change:
**Supabase owns the password, its hashing, email confirmation and the reset
email. This app owns the session** — its own HMAC token and HttpOnly cookie,
because the evidence `<iframe>` and the XML / `.xls` download links are plain
navigations that carry no header. The browser never talks to Supabase, with one
named exception below.

Only the **anon key** is used, as today. **No step in this plan introduces the
service-role key into this process**; that constraint is load-bearing and is
what rules out one of the role options in part (b).

## Routes added

All three are `local`-provider-hostile by design: under `auth_provider=local`
they return **501** with "this deployment has a single configured account". A
single-operator install has no registration and must not grow one.

| Route | Public? | Calls Supabase | Returns |
| --- | --- | --- | --- |
| `POST /api/auth/signup` | yes — add to `_PUBLIC_API_PATHS` | `POST /auth/v1/signup` | 202 + "check your email", **never a session** |
| `POST /api/auth/password-reset` | yes — add to `_PUBLIC_API_PATHS` | `POST /auth/v1/recover` | 202, **always**, regardless of whether the email exists |
| `POST /api/auth/password-reset/confirm` | yes — add to `_PUBLIC_API_PATHS` | `PUT /auth/v1/user` with the recovery token | 200 + "sign in with your new password" |

Three details that are decisions, not implementation:

**Signup does not sign you in.** It returns 202 and no cookie. Email
confirmation is required (see config below), so there is no session to issue
yet, and a route that sometimes returns a session and sometimes does not is a
route whose clients get it wrong.

**Password reset always answers 202.** Answering 404 for an unknown email turns
the endpoint into an account-existence oracle for any address someone wants to
test. Supabase's own response is ignored for the purpose of the status code;
failures are logged, not returned. This mirrors the existing login handler,
which gives one message for both halves of a bad credential.

**`POST /api/auth/signup` never accepts a role, a quota or an `owner_key`.**
The request body is `{email, password}` and nothing else — `extra="forbid"`, as
every other request model here does. `owner_key` comes from Supabase's response
and from nowhere else, exactly as `services.create_job` takes it only from a
named principal.

### The one place the browser touches Supabase, and the trade being made

A reset link arrives in an email. Whatever it points at, the recovery token
transits the user's browser. There is no arrangement in which it does not, short
of this app minting its own reset tokens — which means owning reset, which
contradicts letting Supabase own it.

**Recommended: the implicit (hash-fragment) flow.** Supabase's email link
redirects to the SPA at `/#access_token=…&type=recovery`. The SPA reads the
fragment, immediately strips it with `history.replaceState`, and posts
`{access_token, new_password}` to `POST /api/auth/password-reset/confirm`. That
route calls Supabase server-side. The recovery token never becomes this app's
session and is never stored.

The trade, stated plainly: for a few seconds the token is in a URL fragment that
JavaScript can read — the thing `auth_supabase.py` avoids for the *session*
token. It is acceptable here and not there, because this token's entire power is
"set this one account's password, once, within the hour", it cannot read a
declaration, and it is spent immediately. A fragment is also not sent to servers
and does not appear in access logs or `Referer`.

**The alternative, if that trade is later judged too expensive:** switch the
Supabase project to the PKCE flow, where the redirect carries `?code=…` in the
*query string*, which reaches our server directly and never needs JavaScript.
The cost is that PKCE requires the `code_verifier` generated when the flow
started — server-side here — so it needs a short-lived store keyed to the reset
request. That is a new table and new expiry logic to avoid a token-in-fragment
that is already single-use. Not worth it now; revisit if the SPA ever stops
being served same-origin.

## Supabase configuration that must change

Each of these is currently either off or set for a project used only for the
password grant.

| Setting | Required value | Why it is not optional |
| --- | --- | --- |
| Auth → Providers → Email → **Enable Sign Ups** | ON | `POST /auth/v1/signup` returns 422 while it is off |
| **Confirm email** | ON | This is the primary anti-abuse control. An unconfirmed account cannot sign in — Supabase's password grant returns 400, which `auth_supabase.sign_in` already maps to "wrong credentials" — so an unverified account cannot reach a single vendor call |
| **Site URL** + **Redirect URLs** allow-list | the app origin, exactly | This is the open-redirect control. A wildcard here sends recovery tokens wherever an attacker asks |
| **SMTP** | a real provider (SES / Postmark / …) | Supabase's built-in sender is rate-limited to a handful of messages per hour and is explicitly not for production. Left unchanged, signup appears to work and the confirmation emails silently do not arrive |
| Auth → **Rate limits** (signup, email) | set explicitly | Supabase's own per-hour caps, independent of ours — defence in depth against a limiter of ours that is misconfigured behind a proxy |
| **Password policy**: minimum length + **leaked-password protection** | ON | HIBP checking is a toggle and is the highest-value password control available for the cost |
| Service-role key | **stays out of this deployment** | Unchanged. Nothing above needs it |

`.env.example` gains no new secret — the anon key and URL already exist.

## What stops signup abuse — and the answer to "does the login throttle cover it?"

**No. It does not, and reusing it verbatim would make things worse.** Three
independent reasons:

1. **It is not middleware.** `auth.throttle_retry_after` and
   `auth.record_failure` are called from the body of the `login` handler
   (`app/main.py`). A new route inherits nothing. This is the opposite of the
   login *gate*, which is middleware precisely so a new route is covered by the
   fact that it exists — the throttle never got that treatment because there was
   only ever one public endpoint.

2. **It counts failures; signup abuse is a stream of successes.** `_MAX_FAILURES
   = 10` over `_FAILURE_WINDOW_SECONDS = 300`, and a row is written only when
   `verify_login` returns `None`. Ten thousand scripted registrations, all
   valid, record zero failures and are never slowed.

3. **Naive reuse would actively weaken the login throttle.** A successful login
   calls `auth.clear_failures(db, client)`, wiping that client's window. If
   signup shared the key and cleared on success, an attacker could interleave a
   signup between password guesses to reset their guessing budget indefinitely.

### What to add instead

A **success-counting** limiter, structurally parallel to the failure one and
sharing none of its state:

- Same storage shape as `LoginAttempt` (a database table, not a per-process
  dict — for the reason already documented in `app/auth.py`: N processes
  otherwise allow N times the intended rate). Either a new `SignupAttempt`
  table or a `kind` column on the existing one; a new table is cleaner, because
  `clear_failures` and the window-pruning delete must not touch signup rows.
- Keyed on `auth.client_key(request)` — reused as-is, so the
  `trusted_proxy_hops` reasoning carries over unchanged. **Inherited caveat
  worth stating in the deployment checklist:** if `trusted_proxy_hops` is 0
  behind a load balancer, every caller collapses into one bucket and the
  limiter becomes a self-inflicted denial of service. Already true of login;
  signup makes it more visible.
- Counts **accounts created**, not failures, and is **never cleared on
  success** — success is the thing being counted.
- Suggested opening values: 3 per IP per hour, 10 per IP per day. Tune from the
  first month of real traffic; these are a starting point, not a finding.
- Fails **closed**, like `throttle_retry_after`: an unreadable counter answers
  503, not "go ahead".

Three further layers, in order of how much they actually protect:

- **Email confirmation** (above) is the real control. Rate limiting slows
  account creation; confirmation makes the created accounts useless.
- **Quota is the economic backstop.** Every extraction spends real money at
  Mistral and OpenAI. The limit that matters is not how many accounts exist but
  how much vendor spend an account can reach before anyone looks — which is
  exactly what the next section is about, and why it must land before signup.
- **A domain allow/deny list** is available and is deliberately *not*
  recommended as a security control. It is a commercial decision (do we sell to
  free-mail addresses?) and blocking disposable-mail domains is an unwinnable
  list to maintain.

## How a brand-new account gets its initial quota

**Current state, and this is a gap the plan has to close.**
`usage_monthly_document_cap` is a single deployment-wide `Settings` field
(`app/config.py`) read by `metering.quota_exceeded(db, owner_key)`. There is no
per-account quota anywhere in the schema. Two consequences:

- A new account's quota is "the same number as everybody else's", recorded
  nowhere.
- There is no way to raise one account's limit without raising everyone's — even
  though the message the reviewer is already shown promises exactly that: *"or
  when an administrator raises the limit."* That sentence is currently untrue.
- **The default is `0`, meaning unlimited.** Correct for a single-operator
  install. Combined with open registration it is an uncapped vendor bill.

### The change

A new `account_quota` table, one alembic revision in
`backend/alembic/versions/` — **not** a case in `_migrate_sqlite`, which is the
frozen pre-baseline ladder, and not a `create_all`-only column. Both SQLite and
Postgres run the same revision. (`CLAUDE.md` records what happened the last time
a column was added outside a revision: it existed on fresh SQLite files and was
missing on every existing one, and the app started and then died on the first
query naming it.)

```
account_quota
  owner_key             PK, str(160)   -- the same key Job.owner_key holds
  monthly_document_cap  int, nullable  -- NULL = follow the deployment default
  note                  text           -- why, for the admin who set it
  updated_at, updated_by
```

`metering.quota_exceeded` resolves in this order, and nothing else changes:

1. the account's row, if one exists;
2. otherwise `settings.usage_monthly_document_cap`;
3. `<= 0` still means unlimited, at either level.

**A brand-new account gets no row, and therefore the deployment default.** That
is deliberate, for two reasons. Writing a row at signup freezes the default at
whatever it was on the day each user registered, so it could never be changed
for existing accounts. And it makes signup depend on a second write that can
fail *after* Supabase has already created the account — a partial state this app
cannot roll back, because it does not own the account. A row is written only
when an admin sets a specific cap.

**Hard prerequisite:** `usage_monthly_document_cap` must be lowered to a real
number before registration opens. Enforce it rather than document it — extend
the existing `Settings` validator (the one that already refuses
`auth_provider=supabase` without `SUPABASE_URL`) to refuse boot when
self-signup is enabled and the cap is `<= 0`. Note that `allow_self_signup`, as
a new `Settings` field, must be *read by the behaviour it names in the same
commit*, or `tests/test_config_is_consumed.py` fails — which is the intended
pressure.

No backfill is needed for the counter itself: `metering.documents_this_month`
counts `UsageEvent` rows with `operation == "ocr"`, and a new account has none.

---

# b. The admin / member role

Scope is set by ADR-001 and is not reopened here: **an admin manages accounts
and quotas and cannot read declarations.**

## Where the role lives

**Recommendation: locally — an `account_role` table in this app's database, not
Supabase `app_metadata`.**

The case *for* `app_metadata` is real: it rides along in the JWT Supabase
already returns at sign-in, so it costs nothing to read, and it is writable only
with the service-role key, so a user cannot self-promote.

It loses on four counts:

1. **Writing it requires the service-role key.** `app/auth_supabase.py` states
   why that key is deliberately absent from this process — it would turn any
   code-execution bug into control of the whole auth database. An admin console
   that grants a role must write `app_metadata`, so it needs that key. The only
   alternative is granting roles by hand in the Supabase dashboard, which is a
   manual step, not a feature. **This is the decisive argument**: the constraint
   it violates is one this codebase already made on purpose.
2. **It splits authorization across two systems.** `owner_key` — the value the
   role has to line up with — is a local column, and `CLAUDE.md` is explicit
   that isolation here is Python and tested as Python because Supabase's RLS
   does not protect this backend at all. Putting the role in Supabase means
   answering "who can do what" requires reading two systems that no single test
   can cover.
3. **The `local` provider has no `app_metadata`.** The single-operator
   deployment would need a second, different mechanism — so the local one gets
   built either way.
4. **It is not actually cheaper at read time.** The Supabase JWT is exchanged
   for *our* token at login and then discarded; a claim would have to be copied
   across regardless. So the only question is where the source of truth lives,
   and the local one is the one this process can write.

The single genuine advantage of `app_metadata` — a user cannot self-promote — is
preserved by the obvious means: the role is read server-side from the table, and
is never accepted from a request body, a header, or a client-supplied claim.

```
account_role
  owner_key   PK, str(160)
  role        str(20)   -- 'admin'; absence means 'member'
  granted_at, granted_by
```

Absence means `member`. No row is written for ordinary users, so there is no
signup-time write and no default to migrate. The first admin is inserted by
hand, once, documented in the deployment runbook — bootstrapping a privileged
role through a route that must itself be privileged has no non-silly answer.

## How it reaches the auth middleware without a per-request network call

There is no network call in either candidate — the choice is between a token
claim and a local database read.

**(i) Stamp it into our own session token at login.** `auth.issue_token` gains a
`rol` claim, HMAC-signed with the rest; `verify_token` returns it on `Session`.
Free at request time. **Rejected**, for one reason: the claim is stale for up to
the token TTL (24 hours). Granting late is harmless; *revoking* late is not, and
revocation is the direction that matters for a privileged role. A stateless
token with a 24h TTL has no revocation story, which is fine for identity and not
fine for privilege.

**(ii) Read it from the local table.** One primary-key lookup against a table
with a handful of rows, in the database every request already uses. Revocation
is immediate.

**Recommendation: (ii) — but in a `require_admin` route dependency, not in the
middleware.**

Putting it in `_require_login` would make all ~1500 concurrent reviewers pay a
lookup on every request to serve a capability that matters on about five routes.
Putting it in a dependency on the admin router puts the cost exactly where the
capability is used.

More importantly, it keeps the layers doing one job each. The middleware answers
*is there a session*. `principal_dep` answers *whose data is this*. Neither
should learn *what may they do* — a role available in `principal_dep` is a role
in scope at every job route, which is precisely how it ends up in a predicate
where ADR-001 says it must not be.

For the SPA's own use (whether to draw the admin nav item), extend
`GET /api/auth/session` to report the role from the same lookup. One read per
page load, always current. Deliberately **not** a claim in the token labelled
"display hint" — a value that is present and authoritative-looking will
eventually be treated as authoritative by someone reading the code later.

## Which routes it gates

New, all under `/api/admin/`, all carrying `Depends(require_admin)`, all
returning 404 (not 403) to a non-admin, for the same reason job routes do:

| Route | What it returns |
| --- | --- |
| `GET /api/admin/accounts` | one row per account: `owner_key`, display email, job **count**, status counts, documents this month, spend, current cap |
| `GET /api/admin/accounts/{owner_key}/usage` | `metering.summary(db, owner_key)` — the same shape `/api/usage` returns to the account itself |
| `PUT /api/admin/accounts/{owner_key}/quota` | set or clear the `account_quota` row; writes an audit record naming the admin |
| `POST /api/admin/accounts/{owner_key}/disable` | adds the account to a **local** deny-list checked at login. Not a Supabase user-disable, which needs the service-role key |

Every one of those reads account and usage metadata. **None reads a
declaration, a document, an extraction, an XML artifact or a job audit trail.**

And explicitly, the routes it does **not** gate — no existing route changes
behaviour: `/api/jobs`, `/api/jobs/{id}/**`, the document file route, the XML
and `.xls` downloads and `/api/usage` all keep scoping to `principal` with no
role branch anywhere.

## What would touch `job_visible_to` — and why this is safe

**Nothing in this plan modifies `services.job_visible_to`, `services.get_job` or
the `list_jobs` predicate.** That is the design constraint, not an outcome. The
three places someone will be tempted:

1. **`GET /api/admin/accounts` showing per-account job information.** It returns
   *counts*, from a separate aggregate (`select(func.count()).where(Job.owner_key
   == …)`), and reads no job content — not the summary, not the parties, not the
   totals. Safe because it never loads a `Job` row to display. The residual
   disclosure, stated honestly rather than waved away: an admin learns how many
   declarations an account has and when it was active. That is the minimum
   needed to do quota and abuse work, and it is not nothing.

2. **An admin "open this user's job to debug it" button.** Out of scope, by
   decision. ADR-001 specifies what this would have to be instead: an explicit,
   customer-granted, time-boxed, per-job grant, audited on the read and visible
   to the customer, implemented as a grant *row* that `job_visible_to` looks up
   — never a role test. If that is ever built it amends ADR-001 in the same
   commit.

3. **`SYSTEM_PRINCIPAL`.** The existing bypass, for startup recovery and cascade
   invalidation — callers with no HTTP request behind them. **An admin session
   must never be given it.** If an admin route ever seems to need it, that is
   the signal the route is doing something this ADR forbids, not a reason to
   widen the constant.

## Tests that must land with it

- Extend `tests/test_tenant_isolation.py` with a third account holding
  `role='admin'` and run the **same parametrized sweep**: every job-scoped route
  answers 404 to an admin who does not own the job. This is the assertion that
  makes ADR-001's position enforceable rather than aspirational.
- `job_visible_to` takes `(job, principal)` and no role, asserted by signature.
- A non-admin gets 404 on every `/api/admin/` route.
- Quota resolution: per-account row wins over the deployment default; absence
  falls back to it; `<= 0` is unlimited at both levels.
- The signup limiter counts successes, and a successful signup does **not**
  clear the login failure window.
- Boot refuses when self-signup is on and the deployment cap is `<= 0`.

Note that `test_every_job_scoped_route_is_listed_here` will not fire for the
admin routes — it keys on `{job_id}` in the path, and none of them have one.
That is correct, and worth knowing before someone reads a green suite as
coverage of the admin router.

## Order of work

The sequence is not arbitrary; each step exists because the next one is unsafe
without it.

| # | Step | Why here |
| --- | --- | --- |
| 0 | Fix the two unscoped download routes; `test_tenant_isolation.py` green | Registration must not open over a known cross-account read |
| 1 | `account_quota` + resolution order; lower the deployment default; boot refusal | The spend limit has to exist before accounts can create themselves |
| 2 | `account_role` + `require_admin` + admin routes | Somebody must be able to *raise* a cap before there are users hitting one |
| 3 | Signup + confirmation + the success-counting limiter | Only now is a self-registered account both capped and supportable |
| 4 | Password reset | The smallest piece, and the only one that needs an SPA route; nothing else depends on it |

Doing 3 before 1 and 2 produces a user who registers, hits the cap, and finds
that nobody in the system has the power to help them.
