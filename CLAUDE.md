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

Serves the API *and* the SPA on http://localhost:8000. Requires `EASYCUSTOMS_AUTH_USERNAME` / `AUTH_PASSWORD` / `AUTH_SECRET` (see `.env.example`) — without them the app fails closed and every route answers 401.

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

`.claude/launch.json` points `runtimeExecutable` at a venv under `F:/cld-easy-customs-xml-v2.0.1/` — a path from an earlier checkout. Fix it before relying on the preview launcher.

## The one architectural rule

**The LLM is never the final authority.** OCR + extraction produce only *raw facts with page evidence*; every customs decision — HS11, COO, gross weight, bank/payment codes, freight, supplementary units, valuation, XML — is deterministic Python resolved against the official reference data. When adding a feature, the question "does this let a model decide a declared value?" is the one that matters.

Concretely:
- `app/reference/store.py` is the *only* HS / country / bank / payment-term / customs-office / procedure authority. It loads the real source files in `backend/reference_data/`; never hand-maintain a parallel list.
- LLM-supplied HS11 is rejected outright (8-digit hints only), then completed from the official DB and split into `Commodity_code`(8) + `Precision_1`(3).
- `app/xml/composer.py` is a pure serializer over a *validated* `MergedDeclaration`. It never calls OCR/LLM and never reads raw extraction.
- `app/numbers.py` is the single conversion boundary between untrusted text and authoritative numbers. Money/weight/quantity maths is `Decimal`, never float.

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

`app/extraction/service.py` dispatches by provider (`openai` default → `langroid` → `offline`) and **always** runs the deterministic validator afterward. A live provider whose key or library is missing falls back to offline with a warning rather than failing to boot — which is why the demo and the test suite work with no keys.

Packing lists are parsed deterministically wherever OCR yields a readable table, cross-checked against the document's own printed totals, and only the pages the parser cannot own reach the LLM.

## Rules that bite

- **`docs/allocation-spec.md` is authoritative** for weight/carton allocation, the override ladder and the reconciliation invariants. It and `rules/weight_carton.py`, `rules/packing_match.py`, `rules/description_weight.py`, `extraction/table_parser.py`, `extraction/validator.py`, `review/packing_view.py` are one unit — **change the spec in the same commit that changes the behaviour.** The net-to-gross ratio drifted between 0.3 and 0.7 for months precisely because the rules lived somewhere with no connection to the code.
- **Item order is invoice order**, never resorted by packing/HS/value/weight.
- **Reconciliation is exact**: item gross and package sums equal the authorised totals (Decimal, largest-remainder apportionment); every net is strictly below its gross.
- **Packing match is by product identity**, never row number.
- **Rule-vs-sample conflicts are versioned config flags, not silent guesses** — the ADR table in `README.md`, implemented in `app/config.py` (`default_net_to_gross_ratio`, `pair_divide_by_two`, `cost_allocation_basis`, `default_unknown_payment_terms_to_lc`). Each is recorded in the audit trail.
- **A setting must be read by the behaviour it describes.** `tests/test_config_is_consumed.py` fails on any `Settings` field never read outside `config.py`. Add a flag in the same commit as the code that obeys it.
- **Warn mode is the default** (`xml_strict_blocking=False`): blocking cases warn and still produce XML so the reviewer can test it in real ASYCUDA. Don't assume a blocker stops generation.
- **Role mismatch parks a document** in `ROLE_REVIEW_REQUIRED`; review and finalize refuse until the reviewer accepts or rejects it. Having a stored extraction is not sufficient to be read — `services.declarable_documents` is the gate.

## Auth, schema, tests

Auth is **middleware, not a per-route dependency** (`app/main.py` → `app/auth.py`), so a route added later is protected by the fact that it exists; `tests/test_auth.py` asserts this. Tokens are stateless HMAC-SHA256, 24h, returned in the body *and* as an HttpOnly cookie (the cookie is what makes PDF `<iframe>` and download links work).

`tests/conftest.py` configures an operator account, patches `TestClient.__init__` to attach a token, and gives each run its own temp SQLite database. A test that wants an anonymous client does `client.headers.pop("Authorization", None)`.

SQLite builds itself at startup and needs no migration. **Postgres does not**: `app/database.py` refuses to serve a database whose schema is not at head, because `create_all` cannot ALTER an existing table. A Postgres DB created before migrations existed needs `alembic stamp fa843d18c61e` before its first upgrade.

`tests/test_repo_hygiene.py` is a ratchet that fails if customer documents (`.pdf`, `.xls`, images) become tracked files. Its backlog list is empty; keep it there. Everything in `backend/sample_data/` is synthetic, generated by `scripts/generate_sample_documents.py`.

## In-flight, not wired up

The working tree carries a Supabase/Next.js-shaped experiment — `supabase/` (migrations for `users` / `transactions` / `document_generations` with RLS), a root `package.json` of `@supabase/*` deps, `.env.local` with `NEXT_PUBLIC_*` keys, and `backend/test_db.py`. **None of it is imported by the FastAPI app**, which is still SQLAlchemy against SQLite/Postgres. Treat it as a parallel prototype until something connects the two; don't assume `supabase/migrations/` describes the running schema (`backend/alembic/versions/` does).
