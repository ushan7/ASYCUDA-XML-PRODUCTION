# Easy Customs — ASYCUDA World XML Generator

A full-stack web application that turns role-specific shipment documents
(commercial invoice, packing list, air waybill, banking/LC) into a valid **Nepal
ASYCUDA World SAD import declaration XML**.

It implements the architecture from the *Developer Architecture and Langroid
Extraction Design Report*:

> **The upload box selects the extractor. Langroid/OCR converts untrusted document
> text into evidence-backed raw facts. Deterministic backend services resolve
> customs authority, reconcile totals, and generate the XML. The LLM is never the
> final authority for HS11, COO, shipment totals, bank/payment codes, freight,
> supplementary units, or XML.**

The pipeline reproduces the reference sample declaration **item-for-item**: on the
bundled 119-line shipment it computes the same 199 kg / 12-package HAWB totals,
the same USD 2,108.89 → NPR 307,391.81 valuation, CIF 474,575.07, bank code
`11020000`, terms `200`, and **111 of 119 items are byte-identical** to the
reference XML (the 8 differences are the documented rule-vs-sample ADR conflicts —
see below).

---

## Architecture

```
 Frontend (role-specific upload boxes)
        │  invoice · packing · air waybill · banking  (+insurance/COO optional)
        ▼
 FastAPI intake  →  immutable document registry + hashing
        ▼
 OCR (Mistral | offline pypdf)  →  versioned page envelope   ← untrusted text
        ▼
 Extraction (Langroid ChatAgent + forced ToolMessage | offline)  →  evidence-backed RAW facts
        ▼                                                   (validator: evidence, role, gross-label, numeric)
 ┌───────────────── Deterministic rule services ─────────────────┐
 │ invoice authority → banking → AWB authority → packing match   │
 │ → HS cascade (official DB gate) → COO → [CRITICAL REVIEW]     │
 │ → weight/carton allocation → supplementary units             │
 │ → freight/insurance → valuation                              │
 └──────────────────────────────────────────────────────────────┘
        ▼
 Merged declaration  →  blocking validation  →  ASYCUDA XML (lxml)
```

The LLM/OCR layer only produces **raw facts with page evidence**. Every customs
decision (HS11, COO, gross-weight authority, bank/terms, supplementary units,
freight, valuation, XML) is deterministic Python resolved against the official
reference data.

---

## What's in the box

- **Backend** — FastAPI + SQLAlchemy (Postgres or SQLite), Pydantic v2.
- **Reference data** — the real authoritative files: 6,550 official Nepal HS
  codes with tariff units, 248 country Alpha-2 codes, 279 banks with SWIFT,
  30 ASYCUDA payment-term codes, 71 NECAS customs offices (NNSW codification
  DB, scraped 2026-08-01), the 17 Box-1 declaration-model lines, 55 extended +
  182 national procedure codes (NECAS ANNEX 1/3), 9 transport modes and the
  11 Incoterms 2020.
- **Allocation rules** — weight and carton allocation, the override ladder and the
  reconciliation invariants are specified in
  [`docs/allocation-spec.md`](docs/allocation-spec.md). That file is authoritative;
  change it in the same commit that changes the behaviour.
- **Sea and land shipments** — a bill of lading (or sea waybill / consignment
  note) uploaded in the transport-document box is classified as such, supplies
  the gross weight and packages exactly like a house air waybill, and makes
  Field 9 print `B/L NO: … B/L DATE: …` **instead of** the `MAWB NO: … HAWB: …`
  line. Its number and date are read from the B/L, or from the invoice, which
  states them on most such jobs; the reviewer can override which document the
  declaration says it travelled on.
- **EXIM / IEC codes** — every invoice is searched for them, in the party
  blocks and anywhere else on the page (a labelled-value scan runs under the
  extractor and reports what it filled). The importer's code is a hard XML
  blocker, so a missing one is named plainly in the review.
- **Nine deterministic rule engines** — invoice authority, HAWB/MAWB authority,
  packing match, weight/carton allocation, HS resolver, COO, supplementary
  units, banking, freight.
- **Live extraction by default** — Mistral OCR + OpenAI structured extraction
  (with a Langroid ChatAgent/ToolMessage option and an offline no-key fallback).
  All providers emit the same raw schema and feed the same downstream rules.
- **Frontend** — single-file React SPA (zero build) with the mandatory
  role-specific upload boxes, critical-review gate, validation, and XML download.
- **Tests** — the regression catalog + golden XML, plus a case for every rule
  conflict and silent-failure mode found in audit (`cd backend && pytest -q`).
- **One-click demo** — the bundled sample shipment runs the whole pipeline offline.

---

## Quick start

### Option A — run it (SQLite)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# REQUIRED — the login account (see "Signing in" below); without it nobody
# can sign in and every API route answers 401:
export EASYCUSTOMS_AUTH_USERNAME=admin
export EASYCUSTOMS_AUTH_PASSWORD=...
export EASYCUSTOMS_AUTH_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
# Optional — enable LIVE extraction (else it falls back to offline):
export EASYCUSTOMS_MISTRAL_API_KEY=...   # or MISTRAL_API_KEY
export EASYCUSTOMS_LLM_API_KEY=...         # or OPENAI_API_KEY
uvicorn app.main:app --reload
```

Open **http://localhost:8000**, **sign in** with those credentials, then click **⚡ Load Sample Shipment (demo)** →
**Compute Critical Review** → fill the reviewer-only values (manifest /
Field 18 / Field 21 / insurance as applicable) →
**Finalize & Build XML** → **Download XML**. The demo uses bundled fixtures
(no keys needed); uploading your own PDFs runs live Mistral OCR + OpenAI
extraction when keys are set.

### Option B — Postgres + Docker

```bash
docker compose up --build
# backend on http://localhost:8000, Postgres on 5432
```

The stack runs `alembic upgrade head` in a one-shot `migrate` service and the
API waits for it, because on Postgres the app **verifies** its schema instead of
building it. `create_all` cannot ALTER an existing table, so a Postgres database
that built itself would silently lack any column added later; the app refuses to
start instead, naming the command that fixes it.

Outside Docker, migrations are an explicit step:

```bash
cd backend && alembic upgrade head
```

An existing Postgres database created *before* migrations existed needs
`alembic stamp fa843d18c61e` first — upgrading it directly would try to create
tables it already has. SQLite is unaffected: it still builds itself at startup
and needs no migration command.

### Run the tests

```bash
cd backend
pytest -q
```

---

## Signing in

The workspace holds importers' invoices, party details and finished
declarations, so **every route is behind a login**. One operator account is
configured from the environment (`backend/.env`):

```ini
EASYCUSTOMS_AUTH_USERNAME=admin
EASYCUSTOMS_AUTH_PASSWORD=a-long-password
EASYCUSTOMS_AUTH_SECRET=<python -c "import secrets; print(secrets.token_hex(32))">
EASYCUSTOMS_AUTH_TOKEN_TTL_HOURS=24        # one working day (default)
```

* **`POST /api/auth/login`** exchanges the username + password for a token that
  is valid for **24 hours**; after that the password is asked for again. The
  token comes back in the response body *and* as an **HttpOnly** cookie —
  the cookie is what makes the evidence-PDF `<iframe>` and the XML / `.xls`
  download links work, since a plain navigation cannot send a header. API
  clients can use `Authorization: Bearer <token>` instead.
* **`GET /api/auth/session`** reports who is signed in and until when;
  **`POST /api/auth/logout`** drops the cookie.
* Tokens are stateless (HMAC-SHA256 over `{sub, iat, exp}`) — nothing to store,
  nothing to lose on restart. The flip side: a token cannot be revoked early
  except by changing `EASYCUSTOMS_AUTH_SECRET` or the username/password, which
  invalidates **all** open sessions.
* `EASYCUSTOMS_AUTH_SECRET` unset ⇒ a random key per process, so every restart
  (including each `uvicorn --reload`) signs everyone out.
* **Fails closed.** With no username/password configured, no token can be
  issued and every protected route answers `401` — a deployment that skipped
  this step is visibly broken, never quietly open. Ten failed sign-ins from one
  address within five minutes are throttled (`429`).
* Unauthenticated by design: `GET /api/health` (liveness only, no data) and the
  static SPA shell — it has to load to draw the login form. Everything the
  shell renders comes from `/api/*`, which is gated, and so are `/docs`,
  `/redoc` and `/openapi.json`.

The gate is **middleware, not a per-route dependency**, so a route added later
is protected by the fact that it exists (`tests/test_auth.py` asserts this).
In the test suite, `tests/conftest.py` configures an account and makes every
`TestClient` authenticated; a test that wants an anonymous client does
`client.headers.pop("Authorization", None)`.

---

## Live extraction — Mistral OCR + OpenAI (default)

Live extraction is the **default**: uploaded PDFs are OCR'd by Mistral and
extracted into the raw schema by OpenAI. `pip install -r requirements.txt`
already includes `openai` and `mistralai`. Set your keys and run:

```bash
export EASYCUSTOMS_MISTRAL_API_KEY=...     # or MISTRAL_API_KEY
export EASYCUSTOMS_LLM_API_KEY=...          # or OPENAI_API_KEY
uvicorn app.main:app --reload
```

Verify your keys hit the real APIs end to end:

```bash
python scripts/live_smoke_test.py
```

**Providers** (`EASYCUSTOMS_EXTRACTION_PROVIDER`):

- `openai` *(default)* — `app/extraction/openai_extractor.py`. Asks the model
  for JSON matching each role's raw Pydantic schema, then validates the JSON
  **and** the OCR evidence, with a bounded repair loop feeding errors back. The
  model returns only evidence-backed raw facts; every customs decision stays in
  the deterministic rule layer.
- `langroid` — `app/extraction/langroid_agents.py`, the report's design: one
  fresh `ChatAgent` + `Task` per unit, one forced role-specific `ToolMessage`,
  a bounded repair loop, and a typed `ResultTool` returned only after backend
  validation. `pip install langroid` and pin a tested version to enable.
- `offline` — pypdf + fixtures/heuristics, no keys.

**Automatic fallback:** if a live provider is selected but its key/library is
missing, the app logs a warning and falls back to the offline extractor, so it
always boots and the bundled demo + test-suite keep working with no keys. The
header badges in the UI show `live` vs `offline fallback`. The one-click demo
uses bundled fixtures + offline OCR (deterministic, no keys, no API cost even
when keys are set); uploading your own PDFs triggers live extraction.

**Rate limits:** LLM calls are capped by one global gate
(`EASYCUSTOMS_LLM_CONCURRENCY`, default 4) because they share one OpenAI
tokens-per-minute budget and 429 each other on multi-page documents. A document
running against a time budget (the packing list) also gets one **reserved** slot,
so undeadlined work can never starve it into aborting. The extractor retries
rate-limit/transient API errors with exponential backoff, and a failed document
can be retried from the UI without re-running OCR (the stored OCR envelope is
reused).

**Packing lists** are parsed deterministically wherever the OCR gives a readable
table — weights, cartons, batch and expiry included — and the parsed sums are
cross-checked against the document's own printed totals before they are trusted.
Only the pages the parser cannot own reach the LLM. If the time budget expires
part-way, the rows already extracted are **kept and used**; items not among them
take their quantity share and are named individually in the review warnings. See
[docs/allocation-spec.md](docs/allocation-spec.md) §5b.

**After updating the frontend:** browsers may keep serving a stale cached copy
of the SPA (uploads then appear stuck at `uploaded — pending` because the old
page never calls the extract endpoint). Hard-refresh once (`Ctrl+F5`) after
pulling frontend changes; responses now carry `Cache-Control: no-cache` so this
is only needed for copies cached before that header existed.

---

## API

Every route below requires a session (see **Signing in**) — send the login
cookie or `Authorization: Bearer <token>`.

| Method & path | Purpose |
| --- | --- |
| `POST /api/auth/login` | `{"username","password"}` → 24h token (body + HttpOnly cookie). **Public.** |
| `GET  /api/auth/session` | Who is signed in and until when (`401` = not signed in). |
| `POST /api/auth/logout` | Drop the session cookie. |
| `GET  /api/jobs` | Newest-first job listing for the dashboard (status, roles, review summary, XML flag; `limit`/`offset`). |
| `POST /api/jobs` | Create a job. |
| `POST /api/jobs/demo` | Seed a job with the bundled sample shipment. |
| `POST /api/jobs/{id}/documents/{role}` | Upload a role-specific file (+ optional raw fixture). Upload only — extraction is a separate step (fixtures extract immediately). |
| `POST /api/jobs/{id}/documents/{doc_id}/extract` | Run OCR + extraction for an uploaded document (also retries `FAILED`; stored OCR is reused on retry). |
| `POST /api/jobs/{id}/documents/{doc_id}/role-decision` | Answer a role mismatch: `{"accept": true}` uses the document as declared, `false` excludes it from the declaration. |
| `GET  /api/jobs/{id}` | Job + document status, role-match, warnings. |
| `GET  /api/jobs/{id}/critical-review` | Compute control totals (item count, goods total, gross, packages). |
| `POST /api/jobs/{id}/regime` | Durable per-job regime/office/transport selections (Boxes 1, 25/26, 27, 30, 37, A) — reference-gated; `null` reverts a field to the deployment default. |
| `GET  /api/reference/customs-offices` etc. | Reference tables for the pickers: `customs-offices`, `declaration-models`, `extended-procedures`, `national-procedures`, `transport-modes`, `incoterms` (plus the existing `hs`, `banks`, `payment-terms`). |
| `POST /api/jobs/{id}/finalize` | Confirm/override totals → allocate → validate → build XML. |
| `GET  /api/jobs/{id}/declaration` | Canonical merged declaration JSON. |
| `GET  /api/jobs/{id}/xml` | Download the ASYCUDA XML. |
| `GET  /api/jobs/{id}/audit` | Provenance timeline. |
| `GET  /api/config` | Providers, reference counts, ADR flags. |

`roles`: `INVOICE`, `PACKING_LIST`, `AIR_WAYBILL`, `BANKING`, `INSURANCE`,
`CERTIFICATE_OF_ORIGIN`. Interactive docs at `/docs`.

---

## Rule conflicts → ADRs (versioned config)

The project resources contain internally inconsistent rules and legacy sample
output. Each is handled by a **versioned config flag** rather than a silent
guess, and recorded in the audit trail. Defaults follow the *dedicated rule
files* except where a conflict has since been resolved against the reference
declaration (ADR-003); set the env var to match legacy sample behaviour.

| ADR | Conflict | Config flag (default) |
| --- | --- | --- |
| ADR-003 | **Resolved 2026-07-21 in favour of 0.7.** Every item in the reference declaration satisfies `Net_weight_itm = 0.7 x Gross_weight_itm` (`1.064 = 0.7 x 1.52`); the earlier rule text said 0.3. 0.7 is now the code default, not an override. | `EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO=0.7` |
| ADR-004 | Pair qty: rule divides PCS/2 vs sample keeps PCS | `EASYCUSTOMS_PAIR_DIVIDE_BY_TWO=true` |
| ADR-006 | Unknown payment terms silently → LC | `EASYCUSTOMS_DEFAULT_UNKNOWN_PAYMENT_TERMS_TO_LC=false` |
| freight | Item cost basis: value share (sample) vs gross-weight (rule) | `EASYCUSTOMS_COST_ALLOCATION_BASIS=value` |
| ADR-009 | Sample XML values conflict with newer rules | Sample used as layout reference only; rules + ADRs control calculations. |
| ADR-011 | **2026-08-01.** `Place_of_loading/Name` is emitted **empty** (the hand-exported sample carries `KATHMANDU`): ASYCUDA derives the name from the code on import, and the UNCTAD XML SAD spec discards informational elements. Do not "fix" the empty element back. Also since 2026-08-01: office/declaration-model/procedure/transport values are per-job reviewer selections (reference-gated); Box 25/26 transport modes have **no default** — finalize blocks until the reviewer picks them. | — |

With the defaults, 8 of 119 demo items differ from the reference XML: 6 pair
quantities (ADR-004), 1 HS code whose sample HS8 is absent from the official DB
(the DB-gate rule correctly completes via a 6-digit prefix), and 1 freight
rounding-residual item. Setting `EASYCUSTOMS_PAIR_DIVIDE_BY_TWO=false` aligns the
pair items with the legacy sample.

---

## Project layout

```
easy-customs-xml/
  backend/
    app/
      main.py                 FastAPI app + endpoints + login gate + static frontend
      auth.py                 one env-configured account, 24h signed tokens
      config.py               env-driven settings + rule-set flags
      database.py  models.py  SQLAlchemy engine + ORM
      numbers.py              Decimal parsing/formatting (money never float)
      reference/store.py      HS / countries / banks / terms authority
      ocr/                    base envelope, offline (pypdf), mistral
      extraction/             raw schema, validator, offline + langroid agents
      rules/                  the 9 deterministic engines
      review/critical_review.py
      declaration/            builder (valuation), validator, models
      xml/composer.py         deterministic ASYCUDA serializer (lxml)
      pipeline.py services.py demo.py
    reference_data/           HS xlsx, countries xlsx, banks csv, terms csv
    sample_data/              sample PDFs + reconstructed raw fixtures + sample XML
    tests/                    pytest regression suite + golden XML
  frontend/index.html         zero-build React SPA
  docker-compose.yml  .env.example  README.md
```

---

## Design guarantees

- **Item order** = invoice order, never reordered by packing/HS/value/weight.
- **HS** — every accepted code is an official 11-digit DB record; LLM HS11 is
  rejected (8-digit hints only), split into `Commodity_code`(8)+`Precision_1`(3).
- **Gross weight** — HAWB authority; chargeable/volumetric/net never used as gross.
- **COO** — per item, exporter fallback with warning, blocks on unresolved;
  `NA` = Namibia, never a null marker.
- **Reconciliation** — item gross and package sums equal the authorised totals
  exactly (Decimal, largest-remainder apportionment); every net < gross.
- **Net weight ladder** — reviewer pin > invoice-printed weight > invoice
  quantity in a mass unit (a line sold as `500 KG` states its own net) >
  description unit conversion > packing-list net > `0.7 x gross`. The invoice
  sources cross-check each other: the chosen net is compared against every
  other available reading, and a disagreement beyond 10x is reported, never
  resolved silently.
- **Packing match** — by normalized product identity, never by row number:
  exact name, then product code, then a scored description similarity that is
  gated on measurements agreeing and reported to the reviewer. Repeated rows are
  summed first; a shared carton is divided, never duplicated.
- **Critical review** — the declaration-level control point: it confirms the
  invoice roster, shipment authority (HAWB/MAWB or bill of lading, gross,
  packages, package type,
  weight unit), parties (EXIM 13–15 alnum blocker), manifest, Fields 18/21/40,
  bank/payment pair, banking transaction reference, freight and insurance
  before any XML (the Declarant block is always emitted empty). Field 9 and the
  first-item banking texts are composed deterministically from the reviewed
  values in the locked reference-XML format; Field 40 follows the
  same-weight→MAWB / different-weight→HAWB / single-AWB→MAWB / B/L-shipment→B/L
  rule and is stamped on every item. Confirming re-runs allocation only, never OCR
  or extraction; a `review_fingerprint` rejects finalize if evidence changed
  (stale-review protection).
- **Role mismatch** — when extraction reports that a document is not the role
  its upload box declares, the document is parked in `ROLE_REVIEW_REQUIRED` and
  the review/finalize endpoints refuse until the reviewer accepts it (used as
  declared) or rejects it (excluded from the declaration, evidence kept). It is
  never read on the strength of merely carrying an extraction.
- **XML** — built only from a validated declaration; the composer never calls
  OCR/LLM and never reads raw extraction.
#   A S Y C U D A - X M L - P R O D U C T I O N  
 