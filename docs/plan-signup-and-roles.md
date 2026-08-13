# Plan — self-service signup, password reset, and the admin/member role

**Status: every step is implemented.** See the "Order of work" table at the end
for what each step covers, and the four "Where step N departed from this plan"
sections for the decisions each changed — those, not the body above them, are the
design of record wherever the two disagree.

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
| `POST /api/auth/signup` | yes — add to `_PUBLIC_API_PATHS` | `POST /auth/v1/signup` | 202 + "check your email", **never a session** — *landed with step 3, plus a `GET` on the same path so the sign-in screen knows whether to offer it; see departures 4 and 7* |
| `POST /api/auth/password-reset` | yes — add to `_PUBLIC_API_PATHS` | `POST /auth/v1/recover` | 202, **always**, regardless of whether the email exists — *landed with step 4, plus a `GET` on the same path, and "always" was extended to cover the provider's own 429 and its delivery failures; see departures 1 and 2 there* |
| `POST /api/auth/password-reset/confirm` | yes — add to `_PUBLIC_API_PATHS` | `PUT /auth/v1/user` with the recovery token | 200 + "sign in with your new password" — *landed with step 4* |

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

### Where step 1 departed from this plan

Two decisions, taken when the code was read against the plan and approved before
implementation. Both are now the design of record; this section is what a reader
of step 3 needs.

**1. The deployment default is per PROVIDER, not one number.**
`usage_monthly_document_cap` is now `int | None`, where `None` means "take the
default for the auth provider" (`Settings.resolved_monthly_document_cap`):
`local` resolves to 0 = unlimited, `supabase` to
`DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP` (200). An explicit value overrides either.

Because this plan asserts both *"lower it to a real number"* and *"0 ... Correct
for a single-operator install"*, and a single number cannot honour both. A flat
lowered default would cap every existing `local` install on upgrade — and
**retroactively within the current calendar month**, because
`documents_this_month` counts `UsageEvent` rows already written since
`month_start()`. An operator upgrading on the 20th having already extracted more
than the cap is blocked the instant the process boots, with no admin able to
raise it until step 2 and only a `.env` edit plus a restart as the remedy. The
`local` provider also cannot self-register at all (`auth.verify_token` checks the
token subject against the one configured username), so it never carries the risk
the cap exists to bound. Per-provider resolution makes the upgrade a no-op for
every single-operator install while giving the multi-account provider a real
number.

**2. The boot refusal is gated on `auth_provider == "supabase"`, not on
`allow_self_signup`.** That flag is **not** added, and step 3 still owes it.

The plan gates the refusal on a new `allow_self_signup` field, but its only
step-1 reader would be the validator, which lives in `config.py` —
and `tests/test_config_is_consumed.py` requires a reader *outside* `config.py`.
The behaviour the flag names is step 3. Adding it here would have meant an
exemption in that ratchet or a fake reader, both of which are the thing the
ratchet exists to catch. `supabase` is by this codebase's own description the
only provider under which the app is multi-user at all, so it is the condition
the refusal was reaching for. Step 3 adds `allow_self_signup` alongside the route
that obeys it, and may tighten this refusal to include it.
*(Closed by step 3: the flag exists, its reader is
`main._self_signup_refusal`, and this refusal was deliberately left alone —
including the flag would have loosened it, not tightened it. See departure 1
there. Step 3 added a separate refusal instead: the flag cannot be on under
`local`.)*

Note what the refusal can and cannot fire on: only an **explicit** `0` reaches
it, since unset resolves to 200. It therefore refuses a deliberate choice and
never an upgrade.

**Still owed by step 2, and worth stating because the code ships without it:**
`account_quota.updated_by` has no writer. The route that sets a cap and names its
setter is step 2, so today a row can only be inserted by hand — which is why that
column is nullable rather than `default=""`. NULL says nobody recorded a setter;
an empty string would read as a setter whose name was lost.
*(Closed by step 2: `PUT /api/admin/accounts/{owner_key}/quota` is the writer.)*

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
| `GET /api/admin/accounts` | one row per account: `owner_key`, display email, job **count**, status counts, documents this month, spend, current cap — *see departure 1: there is no account list and no email column, so this is "accounts this deployment has seen" with a derived label* |
| `GET /api/admin/accounts/{owner_key}/usage` | `metering.summary(db, owner_key)` — the same shape `/api/usage` returns to the account itself |
| `PUT /api/admin/accounts/{owner_key}/quota` | set or clear the `account_quota` row; writes an audit record naming the admin — *see departure 2: `AuditEvent.job_id` is a non-nullable FK, so the record is the row plus a structured log line* |
| `POST /api/admin/accounts/{owner_key}/disable` | adds the account to a **local** deny-list checked at login. Not a Supabase user-disable, which needs the service-role key — *see departure 3: also checked before an extraction, because a login-only check does not stop a session that already exists* |
| `POST /api/admin/accounts/{owner_key}/enable` | *added by step 2 (departure 4): a deny-list that can only be added to is a trap* |

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

- ~~Extend `tests/test_tenant_isolation.py` with a third account holding
  `role='admin'` and run the **same parametrized sweep**: every job-scoped route
  answers 404 to an admin who does not own the job. This is the assertion that
  makes ADR-001's position enforceable rather than aspirational.~~ Landed with
  step 2 — `carla_the_admin`, 24 routes, plus the cookie-only download paths and
  the dashboard listing.
- ~~`job_visible_to` takes `(job, principal)` and no role, asserted by
  signature.~~ Landed, and with it a source scan of `job_visible_to`, `get_job`
  and `list_jobs` for any reference to the role at all.
- ~~A non-admin gets 404 on every `/api/admin/` route.~~ Landed, route by route,
  against a table checked against the app's own routing table — plus the control
  (an admin gets 200 on each) and the anonymous case (401 from the middleware,
  which never reaches the role check).
- Landed with step 2 and worth naming, because each is a property rather than a
  case: the role is **not** a claim in the session token (the payload's key set is
  asserted); revoking it takes effect on the **next request** with the same token;
  reading it costs **one** `account_role` lookup that does **not grow with the
  page**; a job route reads that table **zero** times; and the listing carries no
  declaration content even for an account whose job has one.
- ~~Quota resolution: per-account row wins over the deployment default; absence
  falls back to it; `<= 0` is unlimited at both levels.~~ Landed with step 1 in
  `tests/test_account_quota.py`, which also asserts that a row's NULL cap and its
  0 cap are different answers, and that one account's row and one account's usage
  move nothing about another's.
- ~~The signup limiter counts successes, and a successful signup does **not**
  clear the login failure window.~~ Landed with step 3 in `tests/test_signup.py`,
  and in both directions: a signup does not clear the failure window (asserted by
  driving nine failures, a successful registration, then the tenth failure and a
  throttled login), a successful login does not clear the signup window, the
  suite's own `reset_throttle` cannot empty it, and the two are asserted to be two
  tables. Plus: the limiter refuses **before** the provider is called, fails
  closed on an unreadable counter, prunes to the longest of its two windows, and
  does not count a refused attempt.
- ~~Boot refuses when self-signup is on and the deployment cap is `<= 0`.~~
  Landed with step 1, gated on `auth_provider == "supabase"` instead — see
  "Where step 1 departed from this plan". Step 3 may tighten it to include
  `allow_self_signup` when that flag arrives with the route that obeys it.

Note that `test_every_job_scoped_route_is_listed_here` will not fire for the
admin routes — it keys on `{job_id}` in the path, and none of them have one.
That is correct, and worth knowing before someone reads a green suite as
coverage of the admin router. *(Step 2 closed that gap the same way: the admin
routes have their own closed table in `tests/test_admin_role.py`, checked against
the app's routing table, plus `test_no_admin_route_is_job_scoped` — which is the
note explaining why the two sweeps are not the same sweep.)*

## Where step 2 departed from this plan

Five decisions, taken when the code was read against the plan and reported before
implementation. All are now the design of record; this section is what a reader of
step 3 needs.

**1. `GET /api/admin/accounts` cannot list accounts, and "display email" does not
exist.** The plan asks for "one row per account: `owner_key`, display email, job
count, …". There is no account list in this app: identity lives in Supabase and
enumerating it needs the admin API, i.e. the service-role key this plan rules out
in part (a). And no column holds an email — `Job.created_by` is `""` for every job
the API creates (`main.create_job` passes only `actor` and `owner_key`) and
`"demo"` for a seeded one; the display name reaches the database in exactly one
place, `AuditEvent.actor`.

So the route reports **accounts this deployment has seen** — the union of the
`owner_key` values in its own tables — and says so in a `scope` field of the
payload rather than only in a docstring. `display_label` is derived best-effort
from the most recent human audit actor of that account's jobs and is `null` when
nothing named a person. **An account that registers and never uploads anything is
invisible to this route**, which step 3 should know before it opens registration:
the first question after a signup ("did X register?") is not one this API can
answer. What it *can* answer is the question that matters for a quota: an account
that has hit its cap has `usage_event` rows, so it is always listed — and is on
the first page, because the listing is ordered by documents extracted this month
rather than by `owner_key`.

**2. There is nowhere to write "an audit record naming the admin".** The plan's
route table says the quota route "writes an audit record naming the admin".
`AuditEvent.job_id` is a **non-nullable foreign key** to `customs_job`, and an
account-level action has no job — and SQLite does not enforce foreign keys (no
`PRAGMA foreign_keys=ON`), so a fabricated job id would have passed the whole test
suite and failed on Postgres.

What ships instead: attribution on the rows themselves —
`account_quota.updated_by`/`updated_at`/`note`, `account_role.granted_by`,
`account_disabled.disabled_by`/`reason` — plus a `WARNING`-level structured log
line per admin action carrying the request id and the acting principal's stable
id. The row names the human (matching `AuditEvent.actor`, because the column
exists to be read by the next administrator); the log line names the account.

**Stated limitation, not a silent one: that is current-state attribution, not an
action history.** A second quota change overwrites who set the first. A history
needs its own table with its own integrity rules, and inventing one was not part
of this step.

**3. A deny-list checked only at login does not stop a disabled account.**
Sessions are stateless HMAC tokens with a 24h lifetime — `auth.verify_token`'s own
docstring already records that a deactivated Supabase user keeps access until its
token expires. A login-only check would therefore do nothing about the account
that is *currently* burning the Mistral and OpenAI budget, which is the case the
route exists for.

So the deny-list is consulted in three places: at sign-in, on
`POST /api/jobs/{id}/documents/{doc}/extract` (one primary-key read on the one
route that is about to buy OCR), and in `require_admin`. It is deliberately **not**
consulted on every request — that is the per-request database read this plan
rejects for the role in the very next section, and taking it for the deny-list
while refusing it for the role would be incoherent. The remaining window is
documented on `models.AccountDisabled`, asserted by
`test_a_disabled_account_keeps_its_existing_session_on_other_routes`, and the
remedy for an urgent revocation is rotating `EASYCUSTOMS_AUTH_SECRET`, which ends
every open session at once.

At sign-in the deny-list is read **only after the credentials verify**, so the
public login route does not become an account-existence oracle, and a caller who
has proved the password is told `403 ACCOUNT_DISABLED` with the reason rather than
being left to retry a password that was never wrong. That refusal is not counted
against the failure throttle — nothing was guessed — and an unreadable deny-list
answers 503, fail-closed, exactly as `throttle_retry_after` does.

**4. `POST …/enable` was added.** The plan lists only `disable`. A deny-list that
can only be added to turns one mis-click into a permanently barred paying
customer, fixable only with SQL against a live database. Disabling your **own**
account is refused for the same reason: there may be no second administrator to
undo it, and under the `local` provider there certainly is not.

**5. No route grants a role, and there is a script instead.** The plan says the
first admin is inserted by hand "documented in the deployment runbook"; there is
no runbook in `docs/`. `backend/scripts/grant_admin.py` is that documentation —
`--list`, a dry run, `--apply`, and `--revoke` — following
`scripts/reassign_job_owner.py`, the other operation that changes who may see
what. Deliberately no `PUT /api/admin/accounts/{owner_key}/role`: the first
administrator cannot be granted through a route that requires an administrator,
and once the bootstrap is out of band, an HTTP path that lets one admin session
mint another has no reason to exist. `tests/test_admin_role.py` asserts that
`app/main.py` never references the grant at all.

**Not built, and not owed by this step:** an admin SCREEN in the SPA. Step 2's
scope is the role, the gate and the routes; `GET /api/auth/session` reports the
role so a later nav item has something to read. The administrative surface today
is the API.

**No new `Settings` field was added**, so `tests/test_config_is_consumed.py` needed
no exemption. Nothing about the role is configurable: a deployment-wide
`admin_owner_keys` env var would be a role that cannot be revoked without a
restart, and a role in config is a role no test of the table can cover.

## Where step 3 departed from this plan

Eight decisions, taken when the code was read against the plan and reported
before implementation. All are now the design of record; this section is what a
reader of step 4 needs.

**1. "Tightening the boot refusal to include `allow_self_signup`" would have
LOOSENED it, and was not done.** The step-1 section invites step 3 to add the
flag to the refusal that stops an uncapped multi-account deployment booting.
ANDing a condition onto a refusal makes it fire in *fewer* cases: `supabase and
cap == 0 and allow_self_signup` stops refusing the moment registration is off,
and an uncapped multi-account deployment is an unbounded vendor bill whether or
not strangers can register — everyone who already holds an account can spend it.
The refusal stays gated on the provider alone.

What `allow_self_signup` *did* get is a refusal of its own: **`true` under the
`local` provider is refused at boot.** That provider verifies the token subject
against its one configured username (`auth.verify_token`), so a second account
could not sign in even if something created it. Answering 501 for ever would
leave an operator believing they had opened registration — the same shape as the
OCR quality gate that was configured, documented and never ran.

**2. The route's reader of the flag is real, and there is no `_EXEMPT` entry.**
`main._self_signup_refusal` reads `settings.allow_self_signup` and is the single
implementation both the POST and the GET use, so the two can never disagree about
whether registration is open.

**3. "Counts accounts created" is not knowable, so it counts ACCEPTED
registrations.** With email confirmation on, Supabase answers an
already-registered address with an obfuscated user object *specifically* so the
caller cannot tell it apart from a new one — and this app keeps that property
rather than unwinding it. So "an account was created" is not a fact this process
has. The limiter counts what it can honestly observe: a signup this app accepted.
That is a superset of accounts created and never fewer, so it errs towards
limiting more.

**Refused attempts are deliberately not counted**, which the plan does not
discuss. A user who types a password the policy rejects has created nothing, and
spending their 3-per-hour budget on it means being locked out for an hour for a
typo. The cost is stated rather than hidden: refused attempts are unbounded by
*this* app, and what bounds them is the provider's own signup rate limit — which
is why "set Auth → Rate limits explicitly" moved from a nice-to-have in the plan
to a row in the deployment table in `README.md`.

**4. One 202 body, always, and the decision table that produces it.** The plan
says signup returns "202 + check your email" and stops there. Supabase has at
least six distinguishable answers, and forwarding them would have made the route
an account-existence oracle by the back door — including through the *message*,
where "check your email" and "your account is ready" tell a prober whether an
address was new. What ships:

| Supabase | this app | why |
| --- | --- | --- |
| 200, no session (confirmation on) | 202, constant body | the intended path |
| 200, session present (confirmation OFF) | 202, **same** body, `WARNING` logged | the difference is the operator's problem, not a fact for the caller |
| 422 already registered | 202, **same** body | the oracle case, asserted byte for byte |
| 422 weak password | 400 + the provider's own sentence | the one refusal that is about what the caller typed |
| 422 signups disabled | 503 | our flag says open, their project says closed: a misconfiguration the caller cannot fix |
| 429 | 429 + `Retry-After` | theirs, passed on as ours |
| anything else / unreadable / unreachable | 502, **not counted** | not a definite answer about the account |

**5. Confirmation is a dashboard toggle this repository cannot assert, so the
code detects it and says so.** The plan lists "Confirm email = ON" as the primary
anti-abuse control and leaves it as documentation. A session in the signup
response *is* the evidence that it is off, so `auth_supabase.sign_up` reads it,
discards the session, and logs a `WARNING` naming the exact dashboard setting.
The 202 the caller sees is unchanged — see 4.

**6. The SPA got the signup form; it is the one part with no test.** Step 2 shipped
its admin surface as API-only by explicit decision, but registration with no way
to reach it is not a feature a customer has. `LoginScreen` asks
`GET /api/auth/signup` and draws a "Create an account" panel only when the
deployment says it is open — so a broker's own machine never offers something it
would refuse. There is no SPA test harness in this repo, so this was verified by
running the app and driving the screen, not by the suite.

**7. `GET /api/auth/signup` was added.** The plan lists only the POST. The sign-in
screen has no session and therefore cannot read `/api/config`, which is gated, so
without a public signal the SPA would have to either always offer registration or
never. It is the same path, so `_PUBLIC_API_PATHS` does not grow twice, and it
reports one boolean and a sentence — never the provider's name, never a count.

**8. The decision step 2 left open is CLOSED, and here is its limit.** Step 2
recorded that the admin listing is the union of `owner_key` values in this app's
own tables, so an account that registers and never uploads is invisible — and
handed step 3 the choice of closing that or accepting it.

Closed, by writing an `account_seen` row on a successful **sign-in**. Two
questions become answerable that were not: *did this person get in?* and *which
`owner_key` is `alice@broker.np`, so I can raise her cap?* The second is the one
that matters — step 2 shipped a route to raise one account's quota, keyed on a
UUID that nothing in the product surfaced unless that account had already created
a job **and** an audit row naming a human. `account_seen.display` is the first
column in this schema to hold an email address; it is account metadata, ADR-001
already grants an admin exactly that, and the same value has always been written
to `AuditEvent.actor` on every human action. What changes is that it becomes
answerable per account instead of inferred. The listing reports
`label_source: "sign-in" | "audit"` so a fact is never presented with the same
authority as a guess.

**Written at sign-in and never at signup**, which is what makes it safe: with
confirmation on, the provider's anti-enumeration answer carries a user id that
belongs to nobody, so a row written from a signup response could be keyed on a
fabrication and a stranger could fill the table with them. A sign-in is proof —
the password verified and the id came from a real session.

**The residual, asserted rather than left to be discovered
(`test_an_account_that_registered_and_never_signed_in_is_still_invisible`):** an
account that registered and never confirmed its email has never signed in and is
still not listed. That is not a gap so much as the same fact seen from the other
side — "the confirmation link was never followed" — and closing it would require
the admin API and the service-role key this plan rules out in part (a).

The write is best-effort and cannot fail a sign-in: a caller who has proved their
password is not refused because a metadata upsert lost a race with a second tab.

**Not built, and not owed by this step:** an admin screen in the SPA (still), and
anything belonging to step 4. `POST /api/auth/password-reset` and its confirm
route do not exist, and the hash-fragment flow the plan recommends for them is
untouched. *(Both landed with step 4, which also closed a token this step was
leaving in the address bar: a confirmed signup redirects here as
`#access_token=…&type=signup` and the SPA ignored it — see departure 8 there.)*

## Where step 4 departed from this plan

Nine decisions, taken when the code was read against the plan and reported before
implementation. All are now the design of record.

**1. THE PUBLIC SURFACE GREW BY TWO, AND HERE IS THE DECISION.** `CLAUDE.md` and
`tests/test_auth.py` record the public paths as a closed set precisely so a
fourth is decided rather than added. The two added are
`POST|GET /api/auth/password-reset` and `POST /api/auth/password-reset/confirm`,
both public because the person who needs them cannot sign in by definition. What
each tells a stranger: the request half, nothing about any address (one 202 for
every outcome) plus its own caller's limiter state; the confirm half, whether the
token *it was given* is still valid, which the caller brought with them; the GET,
which provider this deployment runs, which the signup POST's 501-vs-403 split
already told anybody who asked.

**The `GET` is not foldable into `GET /api/auth/signup`**, which the plan might
have suggested since both answer "should the sign-in screen draw this link".
Registration is gated on `allow_self_signup`, reset on the provider, and that flag
is **off by default** — one answer for both would hide password reset on every
deployment that never opened registration, which is most of them. Confirm is not
foldable into the request path either: a route that means two things depending on
which fields arrived is the defect this plan names for "sometimes returns a
session".

**No path for the landing page.** `main._needs_login` already returns False for
everything outside `/api/`, so the reset screen is a client-side branch of a
shell that was always public.

**2. THE PROVIDER'S 429 IS AN ORACLE HERE, AND IS NOT FORWARDED.** The plan says
reset "always answers 202" and then leaves the provider's own statuses
unenumerated. GoTrue applies a per-**address** cooldown to the mail it sends, so
a prober who asks twice seconds apart reads *"an email actually went out for this
address"* off the second answer. It collapses into the same 202. Our limiter is
keyed on the **caller** (`auth.client_key`) and is the only thing that ever tells
anybody to wait.

**A delivery failure is not surfaced either, which is the sharper version of the
same trap.** Supabase only *attempts* a send for an address that exists, so a
500 `error_sending_recovery_email` reported to the caller would be the cleanest
existence oracle in the app. Everything except a transport failure or a refusal of
**our own credentials** — both identical for every address — answers 202 and goes
to the log at `ERROR`. The cost, stated rather than hidden: with SMTP broken the
route reports success and no mail arrives. That is the same trade the deployment
checklist already names for the confirmation email.

**3. A FINDING ABOUT STEP 3, REPORTED AND DELIBERATELY NOT FIXED HERE.**
`POST /api/auth/signup` forwards the provider's 429 with its `Retry-After`
(`main.signup`, asserted by `test_a_provider_rate_limit_is_passed_on_as_one`),
which is the same address-keyed signal. Two requests seconds apart: a new or
unconfirmed address answers 429 (the confirmation mail just went out and the
cooldown applies), an already-registered-and-confirmed one answers 202 both times
(obfuscated, no mail, so no cooldown). It is cheaper than it looks, because a
refused signup is deliberately not counted, so the second probe is free against
our own limiter. Left in place on a decision taken when it was reported: changing
merged, asserted behaviour inside a commit scoped to password reset is how a
security control gets altered by a commit nobody reviewed for it. The fix is
collapsing that branch into the same 202.

**4. A THIRD LIMITER, AND THE PLAN DISCUSSES NONE.** The plan spends sixty lines
on the signup limiter and says nothing about reset, which is a public endpoint
that spends an outbound email per call. `password_reset_attempt` is its own table
for the reason `signup_attempt` is: three resets must not exhaust a caller's
registration budget, three registrations must not stop somebody recovering an
account (the direction that locks a real customer out of the only route back in),
and `reset_signup_limiter` must not empty a window nobody asked it about. 5/hour
and 20/day — wider than signup's because a registration creates an account that
can spend money at Mistral and OpenAI and a reset request sends one email.

**It counts every request that reached the provider, whatever came back** — not
signup's rule, and the difference is the point. Counting only the ones that
produced an email would make *how many tries you have left* vary by whether the
address exists, which is the oracle re-entering through the budget after the
status code closed the front door. Only an outage goes uncounted, which is
address-independent.

**The residual it cannot cover, named rather than papered over:** a per-caller key
does nothing about a distributed attacker mailbombing one victim. What bounds that
is the provider's per-address send cooldown, which is why Auth → Rate limits
(email) is now a row in the deployment table rather than a footnote.

**5. THE CONFIRM ROUTE IS NOT RATE-LIMITED, ON PURPOSE.** There is no secret to
guess: the token is provider-signed, so a wrong one is refused by cryptography and
a right one is already the caller's. Counting attempts would lock a user out of
finishing a reset because they mistyped a new password. What is left is the
outbound call an invalid token costs, which a JWT-shape check makes free for
anything not token-shaped and the provider's own limits bound beyond that. Stated
as an accepted residual rather than left to be discovered
(`test_the_confirm_is_deliberately_not_counted`).

**6. `redirect_to` IS CONFIGURATION THE PLAN NEVER MENTIONS.** Without one,
GoTrue uses the project's Site URL — a single value for the whole project — so a
second deployment sharing it mails its users a link to the first one's origin.
`password_reset_redirect_url` is unset by default (send nothing, Site URL
decides), so this step changes nothing in a dashboard. It is **never** read from a
request — a caller-supplied redirect is an open redirect that mails a recovery
token wherever the caller asked — and a value Supabase would silently ignore is
refused at boot, because the failure mode is not an error but every user getting a
link to another origin.

**7. THE FRAGMENT IS READ AT MODULE SCOPE, BEFORE THE SESSION CHECK.** The plan's
landing flow assumes the visitor is signed out. `Shell` asks `/api/auth/session`
first and renders the workspace when it succeeds, so a signed-in user clicking a
reset link would have got the workspace with a live token still in the address bar.
`EMAIL_LINK` is evaluated once at script parse — before the first render, before
the session fetch, and before `writeJobUrl` could `replaceState` the fragment away
without having read it — and the reset screen takes precedence over both the
workspace and the sign-in form. A successful reset also drops whatever session the
browser held, because a password change is the moment to prove the new one.

**8. THE PLAN NEVER SAYS WHAT AN ERROR FRAGMENT DOES, AND IT IS THE COMMON CASE.**
A link older than the hour, or one already clicked, is not a token at all: GoTrue
redirects with `#error=access_denied&error_code=otp_expired&…`. Unhandled, the
user lands on a bare sign-in screen with no explanation. It now shows the
provider's own sentence.

**And the same handler closes a live token this app was already leaving in the
address bar.** Step 3's confirmation links land here as `#access_token=…&type=signup`;
the SPA ignored them, so a Supabase access token sat in the URL after every email
confirmation, ready to be copied into a bug report. The fragment is stripped
unconditionally for every kind, and `type=signup` now says "your email address is
confirmed, sign in".

**9. THE DENY-LIST IS NOT CONSULTED ON EITHER ROUTE.** Refusing a barred address
differently is the oracle again. It is consulted where it decides something — at
sign-in — so a disabled account can reset its password and still cannot get in,
asserted directly. Nor does a reset write an `account_seen` row: that record is
deliberately proof-of-**sign-in** (departure 8 of step 3), and controlling a
mailbox is not one. The sign-in that follows writes it.

**Not built, and not owed by this step:** an admin screen in the SPA (still). No
`Settings` field was added other than `password_reset_redirect_url`, whose reader
is `auth_supabase.request_password_reset`, so `tests/test_config_is_consumed.py`
needed no exemption. `services.job_visible_to` is untouched, and neither new table
is read by it.

## Order of work

The sequence is not arbitrary; each step exists because the next one is unsafe
without it.

| # | Step | Why here | Status |
| --- | --- | --- | --- |
| 0 | Fix the two unscoped download routes; `test_tenant_isolation.py` green | Registration must not open over a known cross-account read | **done** (`86bd148`) |
| 1 | `account_quota` + resolution order; lower the deployment default; boot refusal | The spend limit has to exist before accounts can create themselves | **done** — see "Where step 1 departed from this plan" |
| 2 | `account_role` + `require_admin` + admin routes | Somebody must be able to *raise* a cap before there are users hitting one | **done** — see "Where step 2 departed from this plan" |
| 3 | Signup + confirmation + the success-counting limiter | Only now is a self-registered account both capped and supportable | **done** — see "Where step 3 departed from this plan" |
| 4 | Password reset | The smallest piece, and the only one that needs an SPA route; nothing else depends on it | **done** — see "Where step 4 departed from this plan" |

Doing 3 before 1 and 2 produces a user who registers, hits the cap, and finds
that nobody in the system has the power to help them.
