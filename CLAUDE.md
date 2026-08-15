# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Easy Customs** turns role-specific shipment documents (commercial invoice, packing list, air waybill / bill of lading, banking-LC) into a valid Nepal **ASYCUDA World SAD import declaration XML**. FastAPI backend + a zero-build single-file React SPA that the backend serves itself.

## Commands

All backend commands run from `backend/`.

```bash
cd backend && python -m venv .venv && .venv/Scripts/activate && pip install -r requirements.txt
```

```bash
cd backend && uvicorn app.main:app --reload
```

Serves the API *and* the SPA on http://localhost:8000. On the default `local` auth provider it requires `EASYCUSTOMS_AUTH_USERNAME` / `AUTH_PASSWORD` / `AUTH_SECRET` (see `.env.example`) — without them the app fails closed and every route answers 401. Under `EASYCUSTOMS_AUTH_PROVIDER=supabase` the accounts live in Supabase instead, so those two credential variables are not the credential source; `AUTH_SECRET` still signs this app's own session token.

```bash
cd backend && pytest -q
```

```bash
cd backend && pytest tests/test_allocation_spec.py::test_name -q
```

```bash
cd backend && alembic upgrade head
```

```bash
docker compose up --build
```

```bash
cd backend && python scripts/live_smoke_test.py
```

Verifies Mistral OCR + OpenAI keys hit the real APIs end to end.

Queue mode (`EASYCUSTOMS_QUEUE_PROVIDER=sqs`) adds `python worker.py` (consumer) and `python producer.py --list` (manual enqueue for testing).

## The one architectural rule

**The LLM is never the final authority.** OCR + extraction produce only *raw facts with page evidence*; every customs decision — HS11, COO, gross weight, bank/payment codes, freight, supplementary units, valuation, XML — is deterministic Python resolved against the official reference data. When adding a feature, the question "does this let a model decide a declared value?" is the one that matters.

Concretely:
- `app/reference/store.py` is the *only* HS / country / bank / payment-term / customs-office / procedure authority. It loads the real source files in `backend/reference_data/`; never hand-maintain a parallel list.
- LLM-supplied HS11 is rejected outright (8-digit hints only), then completed from the official DB and split into `Commodity_code`(8) + `Precision_1`(3).
- `app/xml/composer.py` is a pure serializer over a *validated* `MergedDeclaration`. It never calls OCR/LLM and never reads raw extraction.
- `app/numbers.py` is the single conversion boundary between untrusted text and authoritative numbers. Money/weight/quantity maths is `Decimal`, never float.

**And the rules themselves are frozen.** Every customs rule here — allocation, reconciliation, item order, the reference-data lookups, the ADR config flags — is an authoritative business requirement, not an implementation detail that happens to be written this way. A refactor changes how a rule is *expressed*, never what it *decides*. A rule that looks wrong is reported by name and left in place; it is never silently corrected, because a quiet "fix" and a regression are indistinguishable by the time anyone notices. Changing one takes explicit approval first, and updates `docs/allocation-spec.md` in the same commit.

The same same-commit rule covers the three screen specs — a change to Critical Review, Detailed Review or upload/extraction behaviour updates `docs/critical-review-spec.md`, `docs/detailed-review-spec.md` or `docs/upload-extraction-spec.md` with it. Where they describe weights and cartons they defer to `docs/allocation-spec.md`, which stays authoritative on any conflict.

## Pipeline

`app/pipeline.py` is the spine. Resolution always replays from the **stored raw extraction** — never re-OCRing — so Critical Review and Finalize run the same deterministic steps in dependency order:

```
invoice authority → banking → AWB authority → packing match
  → HS → COO → [Critical Review gate]
  → weight/carton allocation → supplementary units → freight/insurance
  → merged declaration → validation → XML
```

Confirming reviewed totals re-runs allocation onward only. A `review_fingerprint` rejects finalize if the underlying evidence changed since the review was computed.

Layer map:

| Path | Role |
| --- | --- |
| `app/main.py` | routes, login middleware, body-size limit, security headers, static SPA |
| `app/services.py` | job/document lifecycle, locking, upload validation, finalize orchestration |
| `app/ocr/` | page-envelope abstraction; `mistral` (live) and `offline` (pypdf) |
| `app/extraction/` | raw Pydantic schema, deterministic table parser, provider extractors, evidence validator |
| `app/rules/` | the nine deterministic engines |
| `app/review/` | Critical Review, reviewer item mutations, packing view |
| `app/declaration/` | builder (valuation), validator, merged models |
| `app/xml/` | ASYCUDA composer (lxml) + brand/model/size `.xls` export |
| `app/storage.py` | document bytes: a local directory or an S3 bucket, dispatched on the key |
| `app/accounts.py` | the platform role, the login deny-list, per-account quota writes, the sign-in record, account/usage **metadata** — never job content |
| `app/metering.py` | per-extraction token/page counts, cost only where a rate is configured, monthly document cap |
| `app/logging_setup.py` | JSON log lines, request-id / principal context, secret scrubbing |

`app/extraction/service.py` dispatches by provider (`openai` default → `langroid` → `offline`) and **always** runs the deterministic validator afterward. A live provider whose key or library is missing falls back to offline with a warning rather than failing to boot — which is why the demo and the test suite work with no keys.

**One exception, narrowed on purpose (2026-08-13): under `auth_provider=supabase`, a live provider with a MISSING KEY is refused at boot** (`config._check_auth_provider_config`, the third refusal on that discriminator). The fallback is per-extraction and logged at WARNING, so a keyless deployment boots clean, accepts the document and produces a complete-looking declaration out of facts nothing read off the paperwork — every deterministic rule downstream running perfectly on invented input, with no 500 and nothing red. Step 5 of `docs/deploy-staging.md`, a human reading the journal for the word `offline`, was the whole control between that and a broker filing it. **The demo and the test suite are unaffected because both run under `local`**, the default, which this does not touch — and naming `offline` outright still boots on every provider, because an explicit choice is not a misconfiguration. Scope: `supabase` only; a MISSING key only. A key that is present but expired or out of quota is a different failure and already surfaces as one (the vendor call raises → document `FAILED`), and the langroid **library** case stays a warning — `pip install langroid` is not a setting. `config.live_ocr_key_missing` / `live_extraction_key_missing` are the single predicate the refusal and both fallbacks share, for the reason `configured` is (`tests/test_live_provider_keys.py` runs the real dispatch to assert they cannot drift).

Packing lists are parsed deterministically wherever OCR yields a readable table, cross-checked against the document's own printed totals, and only the pages the parser cannot own reach the LLM.

## Rules that bite

- **`docs/allocation-spec.md` is authoritative** for weight/carton allocation, the override ladder and the reconciliation invariants. It and `rules/weight_carton.py`, `rules/packing_match.py`, `rules/description_weight.py`, `extraction/table_parser.py`, `extraction/validator.py`, `review/packing_view.py` are one unit — **change the spec in the same commit that changes the behaviour.** The net-to-gross ratio drifted between 0.3 and 0.7 for months precisely because the rules lived somewhere with no connection to the code.
- **Item order is invoice order**, never resorted by packing/HS/value/weight.
- **Reconciliation is exact**: item gross and package sums equal the authorised totals (Decimal, largest-remainder apportionment); every net is strictly below its gross.
- **Packing match is by product identity**, never row number.
- **Rule-vs-sample conflicts are versioned config flags, not silent guesses** — the ADR table in `README.md`, implemented in `app/config.py` (`default_net_to_gross_ratio`, `pair_divide_by_two`, `cost_allocation_basis`, `default_unknown_payment_terms_to_lc`). Each is recorded in the audit trail.
- **A setting must be read by the behaviour it describes.** `tests/test_config_is_consumed.py` fails on any `Settings` field never read outside `config.py`. Add a flag in the same commit as the code that obeys it.
- **A job-scoped route must state what it does about ownership.** `tests/test_tenant_isolation.py::test_every_job_scoped_route_is_listed_here` reads the app's own routing table and fails when a `{job_id}` route exists that its sweep does not exercise. `services.job_visible_to` was never wrong; what was not guaranteed is that every route *asks* it. Two never did — `/api/jobs/{job_id}/xml` and `/api/jobs/{job_id}/brand-model-size.xls` took `principal` via `Depends` and never read it, so any authenticated caller holding a job id downloaded another user's finished declaration XML and `.xls` (fixed in 86bd148).
- **Warn mode is the default** (`xml_strict_blocking=False`): blocking cases warn and still produce XML so the reviewer can test it in real ASYCUDA. Don't assume a blocker stops generation.
- **Role mismatch parks a document** in `ROLE_REVIEW_REQUIRED`; review and finalize refuse until the reviewer accepts or rejects it. Having a stored extraction is not sufficient to be read — `services.declarable_documents` is the gate.

## Auth, schema, tests

Auth is **middleware, not a per-route dependency** (`app/main.py` → `app/auth.py`), so a route added later is protected by the fact that it exists; `tests/test_auth.py` asserts this. Tokens are stateless HMAC-SHA256, 24h, returned in the body *and* as an HttpOnly cookie (the cookie is what makes PDF `<iframe>` and download links work).

**The public surface is a closed set** — `/api/health`, `/api/auth/login`, `/api/auth/signup`, `/api/auth/password-reset`, `/api/auth/password-reset/confirm` — asserted as a set in `tests/test_auth.py`, so the next one is a decision somebody makes rather than a line somebody adds. Every member is public for the same reason: the caller has no session and the point of the route is that they cannot get one yet. Each therefore has to be written so it tells a stranger nothing, and each is refused outright on a deployment that cannot offer it.

Registration is off unless `allow_self_signup` says otherwise, is refused at boot under the `local` provider (one account, so a second could never sign in), and answers **one 202 body for every accepted outcome** — created, already registered, created on a project whose email confirmation is off, the provider's own `429`, and a confirmation email that failed to send — because a public endpoint that distinguishes them is an account-existence oracle for any address a stranger cares to test. Its limiter counts **successes** in its own `signup_attempt` table and shares nothing with the login throttle: that one counts failures and a correct password clears the window, so a shared key would let an attacker interleave a registration between password guesses to reset their guessing budget for ever (`tests/test_signup.py` asserts both directions).

Password reset is **not** gated on `allow_self_signup` — a deployment that creates accounts by hand still has users who forget passwords, and that flag is off by default — only on the provider. `POST /api/auth/password-reset` answers **202 for everything**, and unlike signup that includes the provider's own `429` and its delivery failures: GoTrue applies a per-*address* send cooldown and only attempts a send for an address that exists, so either one, surfaced, is an existence oracle two requests deep. Its limiter (`password_reset_attempt`, a **third** table for the same reason there is a second) counts every request that reached the provider whatever it answered, because counting only the ones that produced an email would put the oracle back in the remaining-tries. The confirm route answers honestly — the only secret in it is the token the caller brought — takes `{access_token, password}` and no account identifier, and issues **no session**: a recovery token proves control of a mailbox, and this app's session is minted from a password grant. `tests/test_password_reset.py` asserts each of those.

**Signup collapses the same three channels, and it did not always.** It forwarded the provider's `429` as a 429 — the residual reported during the password-reset work and deferred rather than changed inside a commit scoped to reset — until it was closed against the same pattern. Closing it surfaced the second channel: a confirmation email that fails to send was a `5xx` from GoTrue and so a `502` here, while an already-registered address stayed a clean `202`. **That one is sharper than the rate limit, because GoTrue's send condition on `/signup` is the inverse of the one on `/recover`** — a *new* address is the one that gets a mail — so a single request distinguished them, where the 429 needed two. Both now answer exactly what an accepted registration answers, with the provider's status in the log at `INFO`/`ERROR` (`auth_supabase.sign_up`; `tests/test_signup.py::test_the_providers_rate_limit_is_NOT_forwarded`, `::test_a_delivery_failure_is_not_reported_to_the_caller`). The limit of the collapse: a transport failure or a refusal of *our own* anon key is still a `502`, because those are identical for every address and are misconfigurations an operator has to see.

**The third channel is the budget, and it is the one that reopens quietly.** `auth.record_signup` runs on `outcome.accepted`, so every outcome the caller sees as `202` costs exactly one row — collapsed `429` and failed send included. A collapsed answer that cost no budget would put *how many tries do I have left* back in a prober's hands as the oracle the status code had just been made constant to close, which is the rule `record_password_reset` states from the other direction. Refusals stay uncounted (`400` for a weak password or an address that will not validate): those are about what the caller typed and distinguish no address from any other. `::test_every_outcome_the_caller_sees_as_202_costs_the_same_budget` is the assertion; the cost of the collapse, stated rather than hidden, is that with SMTP broken the route reports success and no mail arrives — the same trade `docs/deploy-staging.md` already names for confirmation email.

**Who may see what is decided once**, in `services.job_visible_to`. The decision behind it is `docs/ADR-001-identity-and-tenancy.md`, the tenancy record: the USER is the tenant — there are no organizations, firms or memberships, and both isolation and quota are per-user. Read it before writing anything that widens visibility.

**The `admin` role is not a data privilege, and `job_visible_to` does not know it exists.** An admin manages accounts, quotas and usage metadata (`app/accounts.py`, the `/api/admin/*` routes, `Depends(require_admin)`); an admin reading someone else's job, documents, XML, `.xls` or declaration gets the same 404 anybody else does, asserted route by route in `tests/test_tenant_isolation.py`. `if role == "admin": return True` in that predicate is the "unowned job is visible to everyone" branch with a different condition — ADR-001 argues it out, and any support-access feature is an explicit, expiring, customer-created grant ROW plus an ADR amendment, never a role test. The role is a row in `account_role` (absence means member), read server-side per request on the admin routes only; it is deliberately **not** a claim in the session token, because a stateless 24h token cannot be revoked. The first admin comes from `scripts/grant_admin.py` — no route grants a role.

`tests/conftest.py` configures an operator account, patches `TestClient.__init__` to attach a token, and gives each run its own temp SQLite database. A test that wants an anonymous client does `client.headers.pop("Authorization", None)`.

SQLite builds *and migrates* itself at startup: `create_all` raises a new file at the current models, then `_apply_sqlite_migrations` stamps it and runs any outstanding revisions (a pre-alembic file is stamped at the baseline first, an existing one just upgrades). **Postgres does not**: `app/database.py` refuses to serve a database whose schema is not at head, because `create_all` cannot ALTER an existing table. A Postgres DB created before migrations existed needs `alembic stamp fa843d18c61e` before its first upgrade.

Both backends therefore run the **same** alembic revisions — a new column goes in `backend/alembic/versions/` and nowhere else. `_migrate_sqlite` is the frozen pre-baseline ladder; do not add a case to it. That freeze was once load-bearing in the wrong direction: it said new columns belong in a revision, but nothing on the SQLite path ran one, so the first column added after it existed on fresh files and was missing on every existing one — the app started and then died on the first query naming it.

`tests/test_repo_hygiene.py` is a ratchet that fails if customer documents (`.pdf`, `.xls`, images) become tracked files. Its backlog list is empty; keep it there. Everything in `backend/sample_data/` is synthetic, generated by `scripts/generate_sample_documents.py`.

## Supabase is the identity provider, and only that

Under `EASYCUSTOMS_AUTH_PROVIDER=supabase` (default: `local`), `/api/auth/login` hands the credentials to Supabase's password grant **server-side** (`app/auth_supabase.py`) and this app then issues its *own* HMAC token and HttpOnly cookie. The browser never talks to Supabase — the evidence `<iframe>` and the XML/`.xls` download links are plain navigations that carry no header, so the session has to live in a cookie a browser SDK could not write. Only the **anon/publishable key** is used; the service-role key is deliberately absent from this process.

What it is *not*: Supabase's row-level security does **not** protect this app. The backend connects to Postgres as a privileged role and bypasses RLS entirely, so job isolation is Python (`services.job_visible_to`) and tested as Python. And `supabase/migrations/` still does not describe the running schema — those are the Supabase-side `users` / `transactions` / `document_generations` tables; `backend/alembic/versions/` is the app's own, on both SQLite and Postgres.
