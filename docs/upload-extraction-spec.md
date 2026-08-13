# Upload & Extraction — Complete Functional & Technical Specification

**Audience:** a developer re-implementing this stage in another language/stack.
**Scope:** everything from "the reviewer picks a file" to "the job has a validated raw extraction
per document" — the upload boxes, every gate a file passes, OCR, the extraction schema, the
deterministic table parser, the LLM fallback, the validators, role mismatch, retries, cost, and
every state a document can be in.

**Companion documents:** `docs/critical-review-spec.md` (Step 2) and `docs/detailed-review-spec.md`
(Step 3). This stage produces the *only* input those two ever read.

Source of truth in the reference implementation:

| Concern | File |
| --- | --- |
| Upload gates, document lifecycle, extraction orchestration | `backend/app/services.py` |
| HTTP routes, body-size limit, security headers | `backend/app/main.py` |
| Photo → PDF conversion and its gates | `backend/app/images.py` |
| Where documents live (local dir or S3) | `backend/app/storage.py` |
| OCR envelope + provider protocol | `backend/app/ocr/base.py` |
| Mistral OCR (live) / offline pypdf | `backend/app/ocr/mistral.py`, `offline.py`, `service.py` |
| Raw extraction schema (the LLM contract) | `backend/app/extraction/common_models.py` |
| Provider dispatch + post-extraction stamping | `backend/app/extraction/service.py` |
| Deterministic table parser | `backend/app/extraction/table_parser.py` |
| LLM extractor: windowing, repair loop, gates | `backend/app/extraction/openai_extractor.py` |
| Evidence/anchor/role validators | `backend/app/extraction/validator.py`, `manifest.py` |
| Vendor layout memory / field profiles | `backend/app/extraction/layout_memory.py`, `field_profiles.py` |
| Deterministic reference backfill | `backend/app/extraction/document_refs.py` |
| Queue mode (SQS) | `backend/app/queueing.py`, `backend/worker.py` |
| Cost metering and quota | `backend/app/metering.py` |
| Roles, statuses, provenance | `backend/app/domain/enums.py` |

---

## 0. The governing rules of this stage

1. **OCR and the LLM produce raw facts with page evidence — never decisions.** No resolved HS11, no
   country code, no gross-weight authority, no bank code, no XML value comes out of this stage.
   Every numeric value leaves as a **raw string**; conversion to `Decimal` happens later.
2. **The declared role is server metadata, chosen by the upload box.** The extractor may *report* a
   role mismatch; it may never redefine the role.
3. **Fail at upload, not three steps later.** Every rejection is made while the reviewer is still
   looking at the box they dropped the file in — before any paid OCR round. A gate that fires after
   the bill is a bug.
4. **The uploaded file is immutable** and identified by its SHA-256. That is what makes a retry free:
   the OCR envelope is persisted the moment it is paid for and reused forever after.
5. **The model never authorises its own output.** Every payload passes a deterministic validator;
   failures are fed back for a *bounded* repair loop.
6. **What was not read off the document is marked as such.** Extraction provenance is a column on the
   row, not an operator's memory of which button they pressed.

---

## 1. The stage at a glance

```
reviewer picks a file (or photos, or drags onto a box)
        │
        ▼  client-side mirror gate  (§3.1) — costs nothing, catches the obvious
        │
        ▼  POST /documents/{role}     multipart
        │
        ├─ validate_upload            (§3.2)  empty / size / markup-PDF / page ceiling / type
        ├─ photos → single PDF        (§3.3)  resolution, pixel and count gates
        ├─ duplicate gate             (§3.4)  byte-identical file in the same role box
        ├─ store bytes + row          status = UPLOADED
        │
        ▼  reviewer presses **Continue**  →  POST /documents/{id}/extract   (per document, parallel)
        │
        ├─ quota check                (§8.2)  before anything is bought
        ├─ claim: UPLOADED|FAILED → EXTRACTING   (guarded UPDATE — the exclusivity primitive)
        ├─ OCR                        (§4)  Mistral live, or offline pypdf; envelope persisted at once
        ├─ extraction                 (§5)  deterministic parser first, LLM for the residue
        ├─ deterministic validators   (§6)  evidence, anchors, sums, role
        ├─ status = EXTRACTED | ROLE_REVIEW_REQUIRED | FAILED
        └─ invalidate everything derived from the OLD evidence  (§7.4)
        │
        ▼  all required roles EXTRACTED and no role decision pending
        │
        ▼  the client computes Critical Review automatically
```

---

## 2. Roles, statuses and provenance

### 2.1 Declared roles

| Role | Required | Absent → |
| --- | --- | --- |
| `INVOICE` | **yes** | nothing can be declared |
| `PACKING_LIST` | no | item weights split from the authorised gross by quantity share, cartons by weight share (exact-sum reconciled) |
| `AIR_WAYBILL` (also Bill of Lading) | no | reviewer enters total gross, total cartons and the MAWB/HAWB/B/L number manually |
| `BANKING` | no | bank, payment-term and transaction-reference fields are manual entries |
| `INSURANCE` | no | premium entered manually |
| `CERTIFICATE_OF_ORIGIN` | no | — |

**Only the invoice is compulsory.** Every other absence degrades to a deterministic fallback plus a
named manual entry, and each one raises its own warning on the Critical Review screen
(`PACKING_DOC_MISSING`, `TRANSPORT_DOC_MISSING`, `BANKING_DOC_MISSING`).

**A role may hold several documents.** A shipment can carry two invoices; the upload index
(`upload_index_within_role`, 0-based) distinguishes them. A combined "Invoice cum Packing List" PDF
may legitimately be attached to two boxes — which is why the duplicate gate is scoped *per role*.

### 2.2 Document status machine

```
                 ┌──────────┐
   upload ─────► │ UPLOADED │ ◄──── server restart recovery
                 └────┬─────┘
                      │ claim (guarded UPDATE)
                      ▼
                ┌────────────┐
                │ EXTRACTING │
                └──┬──────┬──┘
       success     │      │   exception
        ┌──────────┘      └──────────┐
        ▼                            ▼
  role_match?                    ┌────────┐
   ├─ true  → EXTRACTED          │ FAILED │ ── Continue retries (OCR reused)
   └─ false → ROLE_REVIEW_REQUIRED└────────┘
                    │
      reviewer decides
        ├─ accept → EXTRACTED
        └─ reject → ROLE_REJECTED   (evidence kept, contributes nothing)
```

Other statuses defined but not produced by this path: `OCR_COMPLETE`, `FIELD_REVIEW_REQUIRED`.

### 2.3 Job status alongside

`UPLOADING` → (first extraction) → `EXTRACTION_COMPLETE` → `CRITICAL_REVIEW_REQUIRED` → …
Removing the last extracted document drops the job back to `UPLOADING`.

### 2.4 Extraction provenance — a column, not a memory

| Value | Meaning | Gate |
| --- | --- | --- |
| `OCR` | read from the document's own bytes. **The normal path.** | — |
| `BUNDLED_DEMO` | a fixture shipped in `backend/sample_data`, seeded by the demo button | none needed — it ships with the code, no request can alter it — but it is **marked** everywhere the job appears |
| `CLIENT_FIXTURE` | extraction values supplied **in the HTTP request** | refused unless `EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS=true` |

Rules to reproduce exactly:

* A caller that supplies a fixture **without stating a provenance** does not get the benefit of the
  doubt — it is downgraded to `CLIENT_FIXTURE` and therefore gated. A forgotten argument must surface
  as a refusal, never as a supplied value laundered as read-from-the-document.
* If no fixture was supplied, the row records `OCR` regardless of what the caller asked for.
* A non-`OCR` provenance writes an `EXTRACTION_SEEDED` audit event naming the document.
* `is_demo` on a job is **derived** from its documents, never stored: a flag and a provenance are two
  things that can disagree, and the one the UI reads would be the wrong one.

> Why this exists: a seeded extraction and a real one are the same JSON in the same column. Without
> the provenance column, the only trace that a legally binding declaration was built from values
> nobody read off paper is an operator's recollection.

---

## 3. The upload path

### 3.1 Client-side mirror gate

Catching a wrong pick in the browser costs nothing; catching it server-side costs a round trip;
catching it at extraction costs a **paid OCR run that can only fail**. The client therefore mirrors
the server's rules — it does not replace them.

| Check | Message |
| --- | --- |
| `size == 0` | "…is empty (0 bytes) — re-export or re-scan the document, then attach it again." |
| `size > 25 MB` | names the actual size and the limit, suggests splitting or a lower scan resolution |
| HEIC/HEIF by extension or MIME | "share or export it as JPEG… (or set iPhone Camera → Formats → Most Compatible)" |
| not PDF and not JPEG/PNG | "attach a PDF, or a JPEG/PNG photo of the document" |

`accept="application/pdf,.pdf,image/jpeg,image/png,.jpg,.jpeg,.png"`.
Photo short edge must be ≥ **1000 px** (mirror of the server's `min_photo_px`).

### 3.2 Server upload gate (`validate_upload`)

Runs in this order and returns `"pdf"` (stored as-is) or `"image"` (converted):

| # | Check | Error code |
| --- | --- | --- |
| 1 | non-empty | `EMPTY_DOCUMENT_UPLOAD` |
| 2 | `<= max_upload_mb` (default 25 MB) | `DOCUMENT_TOO_LARGE` |
| 3 | `%PDF-` appears in the first 1024 bytes → it is a PDF | — |
| 3a | …but the file **begins with `<`** after stripping BOM/whitespace → refuse | `UNSUPPORTED_DOCUMENT_TYPE` |
| 3b | page ceiling (`extraction_max_pages`, default 150) read from the page tree | `DOCUMENT_TOO_MANY_PAGES` |
| 4 | JPEG/PNG by magic bytes → accept as `"image"` | — |
| 4a | …but only if a **live OCR provider** is configured | `PHOTO_NEEDS_LIVE_OCR` |
| 5 | HEIC by `ftyp` brand | `UNSUPPORTED_DOCUMENT_TYPE` (with the iPhone instructions) |
| 6 | anything else — sniffed for a better message | `UNSUPPORTED_DOCUMENT_TYPE` |

**Rule 3a is a security gate, not pedantry.** The PDF spec tolerates preamble junk before the header,
but the stored upload is served back to the reviewer **inline on the app's own origin**. A file that
is a document to this gate and a script to their browser is the whole attack. A real PDF never opens
with `<`, so refusing that costs nothing.

**Rule 4a exists because the offline provider is pypdf text-layer extraction** — an image-derived PDF
has no text layer, so accepting the photo would burn an extraction run that can only come back empty.

**The page ceiling is enforced here, before spend.** The same limit also exists inside the LLM
extractor, but that one runs *after* the OCR bill is paid and only on one provider path — so an
oversized PDF was fully OCR'd, charged for, and only then refused; on the offline provider it was
never refused at all. Reading the page tree costs a rounding error against the call it gates.
It is re-checked once more just before OCR, for documents stored before the gate existed.

Sniffed kinds for a better refusal message: GIF/BMP/TIFF/WebP → "an image in an unsupported format
(re-save it as JPEG or PNG)"; `PK\x03\x04` → "an Office document (docx/xlsx/pptx) or zip archive";
`\xd0\xcf\x11\xe0` → "a legacy Office document (doc/xls)".

### 3.3 Photos → one PDF

Two upload routes exist because a photo is a **page**, not a document:

```
POST /api/jobs/{id}/documents/{role}          file          one PDF, or one photo
POST /api/jobs/{id}/documents/{role}/photos   files[]       several photos = ONE document
```

> Uploading the pages of one document as separate uploads declares three invoices: the rows of each
> are emitted again, tripling items and totals. This is the document-boundary failure the split
> route exists to prevent.

**The photo stage (client).** Photos gather in a visible tray before upload — thumbnails, page
numbers, reorder arrows (← →), per-photo remove, "Add photos" and "Take another". Low-resolution
photos are outlined red with their pixel size. Confirming sends them in the displayed order.

Two hidden file inputs are needed, not one: `capture="environment"` **forces** the camera on Android,
so the gallery picker must be its own input to keep the library reachable. On desktop the camera
input degrades to a normal file dialog.

**Server gates in `photos_to_pdf`** — these need the decoded pixels, so they cannot live in
`validate_upload`:

| Check | Default | Error code |
| --- | --- | --- |
| photo count per document | `max_photos_per_document` = 40 | `TOO_MANY_PHOTOS` |
| pixel count (read from the **header**, before decode) | `max_photo_pixels` = 80,000,000 | `PHOTO_TOO_LARGE` |
| decodes at all (`img.load()` — catches truncated files) | — | `UNREADABLE_IMAGE` |
| short edge after EXIF orientation | `min_photo_px` = 1000 | `PHOTO_RESOLUTION_TOO_LOW` |

* The pixel cap is a **memory-exhaustion** defence: Pillow only *errors* above ~179 M pixels and
  merely warns between ~89 M and that, decoding anyway. A 25 MB PNG can sit in that band at ~170 M
  pixels ≈ 500 MB as RGB, roughly tripled by the alpha path.
* Alpha channels are flattened onto white — `img2pdf` refuses alpha, and PDF viewers/OCR would
  render transparent regions black.
* EXIF rotation is applied `ifvalid`: a malformed orientation tag is ignored, not fatal.
* The resolution gate is a correctness gate, not a quality preference: **a blurry 640 px photo
  extracts confidently wrong numbers into a legal declaration**, which is worse than a refusal.

The digest is taken over the **original photo bytes in page order**, so it is deterministic across
converter versions and identical to the single-file digest when the set holds one photo — the same
photo attached via either route trips the same duplicate gate.

Display name: the single filename, or `"{first-stem} +{n-1} photos.pdf"`.

### 3.4 Duplicate gate

A byte-identical file **in the same role box** is refused: `DUPLICATE_DOCUMENT_UPLOAD`, naming the
existing document and its status.

> Several documents per role IS supported. But every extra copy of the *same* file contributes its
> rows again — 2N items and a doubled goods total, with nothing downstream able to tell the copies
> apart.

Scoped to the role, because a combined "Invoice cum Packing List" PDF legitimately fills two boxes.

**Retry semantics.** If the only twin is a `FAILED` row, re-picking the same file is the retry the UI
offers: the existing row is **reset** to `UPLOADED` rather than a second row being added, so the
stored OCR envelope survives and the retry costs no re-OCR. A *live* twin always wins over a failed
one — a job stored before this gate existed may hold both, and resetting the failed row there would
add a second extraction of a file already extracted.

### 3.5 What gets stored

* **Always PDF bytes.** `validate_upload` proved it, or the photo converter just produced it.
* **The client's MIME type is never stored.** It is the browser's guess from the file extension, i.e.
  attacker-influenced. Persisting it once meant the evidence viewer served it back as the response
  media type — an "invoice" declaring `text/html` ran as script as the signed-in operator. The column
  defaults to `application/pdf` and the serving route hard-codes it plus `X-Content-Type-Options:
  nosniff`.
* A converted photo keeps its camera filename for display, but the **stored** file is named `.pdf`
  so the storage directory tells the truth.
* Storage is local-directory by default or S3. **Dispatch is on the key, never on the current
  setting** — an `s3://` key is fetched from S3 and anything else is a local path, whatever
  `storage_backend` says today. That is what lets a deployment turn S3 on without orphaning
  documents it already holds. Bytes, not paths, cross this boundary.

### 3.6 Request body limit

An ASGI-level middleware bounds request bodies: **Content-Length first** (an honest client is refused
before it sends a byte), then a **running total** for clients that understate it or use chunked
transfer-encoding. Limit = `max(64 MB, max_upload_mb × 2)`. Refusal is `413` with
`Connection: close`.

This has to live at the ASGI boundary because the multipart body is consumed *before* the route
function runs — so `max_upload_mb` alone could never gate it.

---

## 4. OCR

### 4.1 The envelope

OCR output is **untrusted document data**: versioned, never overwritten in place, and only the pages
a role needs are handed to an extractor.

```
OcrDocument {
  document_id, declared_role, source_sha256,
  ocr_provider, ocr_model, ocr_schema_version = "ocr-v1", created_at,
  pages: [ OcrPage { page_no ≥ 1, plain_text, markdown,
                     blocks: [ OcrBlock { block_id, block_type, text, bbox? } ] } ]
}
```

Helpers every consumer uses: `page_text_map() -> {page_no: text}` and `full_text()`.

**The provider protocol takes BYTES, not a path.** A provider that opens a file makes "the document
is on this machine's disk" a requirement of the extraction layer — false the moment documents live in
S3, and false by construction in queue mode, where the worker is not the machine that stored the
upload.

### 4.2 Providers

| Provider | Model | When |
| --- | --- | --- |
| `mistral` | `mistral-ocr-latest` | default; requires `MISTRAL_API_KEY` |
| `offline` | `pypdf-textlayer` | no key configured, or a fixture replay |

**Fallback rule:** if `mistral` is selected but no key is set, the app logs a warning and falls back
to offline **rather than failing to boot**. This is why the bundled demo and the whole test suite
work with no keys at all.

**Exception — `auth_provider=supabase` refuses to boot instead.** See §5.1: the fallback below is
unchanged, but on the multi-account provider a *missing* key stops the process rather than quietly
downgrading the reader.

Mistral flow: `files.upload` → `files.get_signed_url` → `ocr.process(document_url=…)`, with every HTTP
call bounded by `timeout_ms` (default 120 s) — without it a stalled connection hangs the whole
extraction silently.

Two privacy rules in that flow, both worth copying:

* **The vendor is told the document ID and nothing else.** It used to receive the server's absolute
  storage path, disclosing the deployment's directory layout and the job UUID to a third party for no
  benefit — the file name is only a label on their side.
* **The uploaded file is deleted from the vendor's file store in a `finally`.** The upload *persists*
  there; the signed URL is needed only for the one `process()` call. Without the delete, every
  invoice, packing list and **banking document** (bank references, SWIFT text, payment amounts)
  accumulates in the vendor account forever, outliving any deletion performed in this app — and one
  leaked OCR key yields the whole historical corpus rather than just API credit. Best effort: a
  failed cleanup logs loudly but must not fail an extraction whose OCR is already paid for.

Offline provider: pypdf `extract_text()` per page, one block per non-blank line. An unreadable file
yields a single empty page rather than raising — good enough for text PDFs, which is the common case
for invoices, packing lists, AWBs and SWIFT prints.

### 4.3 The envelope is persisted the moment it is paid for

```
doc.ocr = envelope; db.commit()          # BEFORE the LLM phase
```

Three things follow: a crash or restart in the LLM phase costs no re-OCR; a retry reuses it; and a
document with OCR but no `raw_extraction` is *visibly* mid-flight rather than indistinguishable from
one that never started.

---

## 5. Extraction

### 5.1 Provider dispatch

| Provider | Behaviour |
| --- | --- |
| fixture supplied | offline extractor replays it verbatim — **no LLM call** |
| `openai` (default) | direct structured output + schema validation + repair loop |
| `langroid` | the ChatAgent + forced ToolMessage design |
| `offline` | pypdf heuristics, no keys |

A live provider whose key or library is missing **falls back to offline with a warning** rather than
failing.

### 5.1a A missing key is a boot refusal under `supabase`

The fallback above is decided **per extraction**, in `ocr/service.get_ocr_provider` and
`extraction/service._run_provider`. Nothing at boot used to ask the question, so a deployment with no
keys started clean, answered `/api/health`, accepted the document, and produced a **complete-looking
ASYCUDA declaration out of facts nothing read off the paperwork** — every deterministic rule
downstream working perfectly on invented input. No 500, no failed job, nothing red; the only tells
are a `WARNING` nobody greps for and `provider: offline` in the audit trail. Step 5 of
`docs/deploy-staging.md` was the whole control between that and a broker filing it.

So `config.Settings._check_auth_provider_config` refuses the boot when **all three** hold:

| | |
| --- | --- |
| `auth_provider` | `supabase` — the multi-account provider, i.e. the shape a real deployment has |
| provider named | `ocr_provider=mistral`, or `extraction_provider` in {`openai`, `langroid`} |
| its key | **absent** — unset, blank, or a placeholder (`***`, `your_…_here`) |

The message names the variable to set for each half at once, and the escape hatch.

**What this does not do.**

* **It does not change the fallback.** `local` still warns and downgrades, which is the documented
  zero-setup path — and the reason the bundled demo and the test suite run with no keys at all.
* **It does not refuse `offline`.** Named outright it boots on every provider; an explicit choice is
  not a misconfiguration. What is refused is *claiming* a live provider and not giving it a key.
* **It does not validate the key.** One that is present but expired, revoked or out of quota is a
  different failure and already behaves like one: the live provider is constructed, the vendor call
  raises, and `services.extract_document` marks the document `FAILED` with
  `EXTRACTION_FAILED (…)`, writes `DOCUMENT_EXTRACTION_FAILED` to the audit trail, and re-raises.
  Only *absence* is silent, and only absence is refused. Boot-time credential validation against a
  third party would be a different feature with its own failure mode.
* **It does not cover the langroid LIBRARY.** `langroid` with a key but no `pip install langroid`
  still falls back with a warning: that is not a variable anyone can set in `.env`, and importing an
  optional dependency inside a settings validator would make every boot depend on it. Recorded here
  as a residual rather than half-covered.

`config.live_ocr_key_missing()` / `live_extraction_key_missing()` are the **single predicate** the
refusal and both fallbacks consult — the `configured`/`auth_secret` rule applied to vendor keys, so a
value one treats as a usable key cannot be one the other silently treats as missing.
`tests/test_live_provider_keys.py` runs the real dispatch functions across the provider × key matrix
and asserts the branch taken is the one the refusal predicts.

### 5.2 The raw extraction schema — the entire LLM contract

Every numeric value is a raw string. Nothing here is a final customs value.

**Shared building blocks**

```
Evidence      { page_no ≥ 1, label?, quote (1..500 chars), block_id? }
RawNumber     { value_raw?, unit_raw?, evidence? }
RawMoney      { amount_raw?, currency_raw?, evidence? }
PartyRaw      { name_raw?, address_raw?, exim_code_raw?, country_raw?, … }
RoleValidation{ expected_role, matches_expected_role = true,
                detected_title_raw?, detected_document_kind?, reason?, evidence[] }
```

`quote` is documented to the model as *"the SHORTEST distinctive fragment of the OCR line that proves
the value — about 12 words, never a whole paragraph."* An over-long quote is **truncated, never
rejected**: `max_length` alone turned a model that quoted a whole table row into a schema
`ValidationError`, costing a full repair round (a resend of every row in the window) to fix a value
that was already correct. Downstream only matches the quote's first 40 characters.

**Per-role payloads**

| Role | Model | Top-level fields |
| --- | --- | --- |
| INVOICE | `InvoiceChunkRaw` | `role_validation, page_numbers[], header, rows[], totals, sub_invoices[], page_complete, warnings[], page_numeric_locales` |
| PACKING_LIST | `PackingListChunkRaw` | `role_validation, packing_list_number_raw, packing_list_date_raw, invoice_references_raw[], invoice_date_raw, lc_reference_raw, lc_date_raw, exporter, importer, country_of_final_destination_raw, rows[], total_gross_weight, total_net_weight, total_packages, total_quantity, total_volume, dimensions[], page_complete, warnings[], page_numeric_locales` |
| AIR_WAYBILL | `AirWaybillExtractionRaw` | `role_validation, forms[], warnings[]` |
| BANKING | `BankingExtractionRaw` | `role_validation, document_kind_raw, swift_message_type_raw, sender_bic_raw, receiver_bic_raw, issuing_or_applicant_bank_name_raw, bank_swift_raw, reference_number_raw, issue_or_value_date_raw, amount, applicant, beneficiary, payment_terms_raw, draft_tenor_raw, invoice_references_raw[], freight_mentions[], exchange_rate_mentions_raw[], warnings[]` |
| INSURANCE | `InsuranceExtractionRaw` | `role_validation, policy_number_raw, insurer_raw, invoice_value, sum_insured, incidental_cost, premium, exchange_rate_raw, invoice_references_raw[], warnings[]` |

Two schema-level rules the field descriptions carry into the prompt:

* **`sub_invoices`** — one entry per distinct printed invoice in the upload, in page order, each with
  its own number/date/totals and `first_page_no`. *"Never merge different invoices' numbers, dates or
  totals into one entry."* Document-level `totals` is only for an explicitly printed combined grand
  total.
* **`forms`** — one form per distinct transport document: a master AWB, a house AWB, a bill of lading,
  a delivery order and a tracking page each get their **own** form. *"Never merge different documents
  into one form; every weight/pieces value must come from that form's own pages."* This is what makes
  the downstream authority ladder (HAWB > TRUE_DO > TRACKING > SINGLE_AWB) possible at all.

**Fields the code stamps, never the model:**

* `page_numeric_locales` — per-page `"EU"`/`"US"` hints from `detect_numeric_locale`, used downstream
  to resolve `1.600`-style separator ambiguity.
* Deterministic reference backfill (`document_refs`) — the parties' EXIM/IEC codes on an invoice, and
  the bill-of-lading number/date of a sea/land shipment. **Only ever into a field the extractor left
  empty**, and every fill is reported as a warning so the reviewer can see where the value came from.

### 5.3 Deterministic-first: the table parser

Mistral OCR emits markdown tables, so most goods rows are machine-parseable with no model at all.

```
header row  →  column map
data line   →  cells
row CONFIRMED  ⟺  it self-verifies
```

For invoices, self-verification is the arithmetic `qty × unit_price == line_total`, locale-aware. **A
confirmed row therefore cannot be numerically wrong in a way that matters.**

One narrow repair is allowed inside that gate: when the printed quantity contradicts the money while
`total / price` is a clean positive integer, the quantity is derived from the money and the
correction is reported in the parser notes — rather than disowning a whole page to the LLM over one
misread digit.

**Page ownership is all-or-nothing.** A page is parser-owned only when *every* suspicious line on it
(anything carrying an identity token or a qty|UOM cell pair that is not a banner or header) became a
confirmed row. Any leftover — split rows, merged cells, garbled fragments — sends the **whole page**
to the LLM, so parser and model never share a page and merging stays trivially ordered.

One class of leftover is excused rather than disowning its page: a **value-less continuation
fragment** (the previous row's batch/COO breakdown printed as its own line, every quantity and money
column empty) can never be a goods row — and handing its page to the LLM is how such a fragment once
gained invented values and became a phantom declaration item.

If no header-derived column map exists anywhere in the document, the parser **stands down entirely**
and the historical LLM path runs unchanged.

Hard-won parser rules (from an adversarial audit — each produced a wrong number before it existed):

* **A row carrying data is never a header.** `| 6-10 | LEATHER GOODS | 50 | CTNS | … |` matched the
  header words, replaced the working column map mid-page, and silently deleted itself from a page the
  parser still owned. Any pure-numeric cell, or a qty|UOM data cell, disqualifies a header candidate.
* **A per-unit rate column maps to nothing.** `UNITS PER CARTON`, `PCS/CTN`, `N.W./CTN`, `KG/PC` are
  rates; mapping one to qty/ctn/weight declares a per-carton figure as the row's total.
* **The document's own printed totals are the gate.** A parse whose line values sum to something that
  matches none of the printed totals is rejected — and the *reason* is carried forward (see below).

**Stand-down reasons are never discarded.** When the parser rejects its own parse and the LLM path
takes over, the concrete reason ("parsed line values sum X matches none of the printed invoice
totals") is attached to the payload as `TABLE_PARSER: stood down and the LLM extracted these rows
instead — …`. Without it, a document whose column map was provably wrong looked, from the review
screen, exactly like one the parser had simply never tried.

**Vendor layout memory.** Every successful parser run records its header-derived column map in a
`vendor_layout` row keyed by (role, header signature). When a future document's header is unreadable
(bad scan), the parser retries with the remembered maps instead of standing down. Safety does not
rest on the store: **every row still has to arithmetic-verify**, so a wrong remembered layout can
never fabricate rows — it simply fails to confirm and the LLM path takes over. A store failure
degrades to "no memory", never to a broken extraction.

Two design notes worth copying: the store is a **row per key** (a whole-file read-modify-write meant
two processes each started from the same snapshot and the second write silently discarded what the
first learned), and `POSITIONAL` entries are skipped on read — they could only have been written by a
parse that itself came from memory, and that key matches every headerless document of the role rather
than one vendor's table: a self-confirming entry that gets stronger each time it is wrong.

**The packing-column escape hatch.** A packing list whose printed totals refuse to reconcile is the
one shape where the deterministic layer genuinely cannot tell which column is which. A model is asked
to propose column roles, the document is **re-parsed** under them, and the proposal is kept **only if
the document's own totals then close**. Confirmed rows alone are not enough — a role pointing at a
text column yields no weights and reconciles vacuously. The accepted case is logged as
`COLUMN_ROLES_RESOLVED: … No value came from the model.`

### 5.4 The LLM path

**Windowing.** Long INVOICE / PACKING_LIST documents are extracted in page windows so a single
response stays small — a 21-page invoice in one shot risks output truncation, and one repair round
then re-requests everything.

| Setting | Default | Note |
| --- | --- | --- |
| `extraction_chunk_page_threshold` | 6 | chunk when pages exceed this |
| `extraction_chunk_page_size` | 4 | pages per window |
| `extraction_chunk_page_size_packing` | **2** | packing pages are dense tables whose rows rarely straddle a page break, so 4+2 mostly bought re-sending each page up to three times — on the one document that has a time budget to lose |
| `extraction_max_repair_rounds` | 2 | bounded repair |
| `extraction_max_pages` | 150 | `0` disables; a 500-page PDF at a 4-page window is 125 calls per `/extract`, repeatable on every retry |

**One small document-level call.** In parser-first mode the LLM is asked only for header / totals /
`sub_invoices` / `role_validation` (invoice) or the packing header and every `total_*` field. It sees
**every** page, with middle pages trimmed to their top/bottom zones where headers and totals print —
because a bundled upload's interior invoices print their headers on interior pages.

**Concurrency and backoff.** All extractions share one tokens-per-minute budget, so calls pass
through a global semaphore (`llm_concurrency`, default 4) with exponential backoff on 429 and
transient errors (`8, 16, 32, 60` s). The SDK's own retries are disabled — our loop handles
transients, and without that a hung request looks like "extracting…" for the SDK default 600 s × 3.
Per-request timeout `llm_timeout_seconds` = 180 s.

**One reserved priority slot.** A deadlined extraction may take an extra slot on top of the gate.
The gate is global and the budget is not: a packing list with 240 seconds to live was spending them
queued behind an invoice extraction that has all the time in the world, and then aborting.

**The packing time budget** (`packing_extraction_budget_seconds`, default 240 s) spans **OCR +
reasoning**, so OCR eats into it. It is passed in as a deadline; the extractor stops launching new
calls and repair rounds past it, with a `_DRAIN_GRACE` of 15 s bounding how far an in-flight call can
overshoot. Three outcomes, and the difference between them matters downstream:

| Outcome | Marker | Allocation effect |
| --- | --- | --- |
| finished in time | — | packing evidence used normally |
| **hard abort**, nothing usable | `PACKING_EXTRACTION_OVER_BUDGET` | all packing evidence dropped; gross split by **quantity share**, cartons by weight share, totals reconciled exactly |
| **partial** — some windows completed | `PACKING_EXTRACTION_PARTIAL` | the extracted rows **ARE used**; items not among them take the quantity share and are **named individually** |

> PARTIAL is a third state, not a milder over-budget. Collapsing it into either "present" or "absent"
> is what makes it dangerous. Discarding a whole packing list because its last window ran late threw
> away rows already extracted **and already paid for**, and left allocation with no evidence at all —
> the long wait followed by a proportional split that this whole design exists to end.

Tokens from windows that completed before the clock ran out are still recorded: an aborted packing
list is the most expensive thing this application does, and dropping its usage would make it cost
nothing on the report.

**Deterministic post-extraction gates**, run in dependency order on every path (parser, chunked or
single-window):

1. `attribute_row_descriptions` — put each printed goods name on the row that owns it. **Runs first**,
   because identity-token harvesting pulls GTINs out of `description_raw`, so every later gate must
   already be looking at descriptions on the right rows.
2. `neutralize_invented_fragment_values` — strip provably-invented fragment values.
3. `ground_row_values` — every claimed money value must appear in that page's own OCR.
4. `reconcile_row_duplicates` — stripped rows no longer count toward over-counting.
5. `reconcile_invoice_sum` — cross-check against the printed totals, post-strip.
6. `flag_truncated_value_columns` — pages whose value columns the OCR lost.
7. `flag_incomplete_pages` — only now, missing-row detection.

---

## 6. Deterministic validation

The validator runs **after every provider, always**, before the payload is trusted. On failure the
errors are fed back to the extractor for a bounded repair round — **the model never authorises its
own output.**

### 6.1 What is checked

| Check | Failure |
| --- | --- |
| the role matches | `role_validation.matches_expected_role = false` → `ROLE_REVIEW_REQUIRED` |
| every evidence page is in document scope | `evidence page N is outside document scope` |
| every evidence quote actually appears in that page's OCR | quote-not-found error |
| gross-weight evidence is not mislabelled | forbidden labels: `chargeable`, `c.w`, `cw`, `volumetric`, `dimensional`, `vol`, `net` |
| numeric tokens parse | parse error |
| **page completeness** — a page whose OCR prints qty\|UOM cells must contribute ≥ 1 extracted row | `PAGE_ROWS_MISSING: page N prints goods-table rows … extract EVERY row on that page` |
| **row completeness** — every goods-row anchor the OCR provably prints is covered | `ROW_ANCHOR_MISSING: page N prints goods row … extract that row` |
| **packing sums** judged on the WHOLE document | see below |

Two independent detectors back the page-completeness check: a raw-text `qty|UOM` regex **and** a
cell-based scan that survives OCR bold markers around cells. `| **42021900000** | PCS |` defeated the
plain regex and let a whole last page vanish silently.

**Packing sums are whole-document only.** A page window holds part of the rows and sometimes all of
the totals, so a per-window mismatch means nothing. `validate_packing(..., whole_document=True)` runs
once, after merging.

**House-page hints.** A page mentioning `delivery order`, `house air waybill`, `house awb`, `house
bill` (or matching the HAWB token pattern `H.A.W.B` / `H/AWB` / `H-AWB` / `H AWB` — never `mawb`,
which has no leading `h`) holds **house-level consignment values that must become their own form**,
never be merged into the master AWB.

### 6.2 `review_required`

`bool(errors) or not role_match or bool(warnings)` — surfaced on the document card as an expandable
"N note(s)" list.

---

## 7. The extraction run

### 7.1 The claim — the exclusivity primitive

```sql
UPDATE document SET status='EXTRACTING'
 WHERE id = :id AND status IN (:claim_from)
```

`rowcount == 0` → refuse with `EXTRACTION_ALREADY_RUNNING`, reporting the status the **database**
holds (the in-memory one is the stale snapshot that lost the race).

> The endpoint's own status check reads outside any transaction, so a double-submit, a stale tab or a
> second browser each passed it and started their own paid OCR + LLM round on the same file — and the
> slower one then overwrote the faster one's rows. Exactly one of the racing `UPDATE`s can match.

`claim_from` differs by mode:

| Caller | `claim_from` | Why |
| --- | --- | --- |
| interactive | `(UPLOADED, FAILED)` | an extraction starts from those, never over another run's claim |
| queue worker | `(EXTRACTING,)` | the **producer** took the claim before sending the message, so `EXTRACTING` is the expected state at pickup, and a redelivered message (previous worker died) must be able to take over |

`FAILED` is deliberately **not** in the worker's set: a failed attempt deletes its own message, so the
retry is the reviewer's Continue — never an SQS redelivery racing the human being invited to press it.

### 7.2 Commit discipline

The audit event for the start is written and **committed before** the slow calls. Extractions run
concurrently and SQLite allows a single writer, so the write lock must not span OCR/LLM work. The
document is then `refresh`ed because the guarded UPDATE deliberately bypassed the ORM.

### 7.3 Failure

Any exception → `status = FAILED`, `warnings = ["EXTRACTION_FAILED (<Type>): <first 300 chars>"]`,
audit `DOCUMENT_EXTRACTION_FAILED`, commit, re-raise. The route turns it into a **502** with the
reason — an upstream OCR/LLM fault, not an opaque 500.

**A refused claim is a 409, not a 502.** Reporting it as FAILED would tell the reviewer their
document broke while it is in fact extracting.

### 7.4 Success — and what it invalidates

New evidence supersedes everything derived from the old evidence:

```
with job_lock(job):
    superseded = job.critical_review or job.declaration
    _invalidate_derived(job)                # review, declaration, XML + .xls artifacts
    _reset_extraction_derived_state(job, role, cause)   # role-scoped overlay reset
    job.status = EXTRACTION_COMPLETE
    audit DOCUMENT_EXTRACTED  (+ DERIVED_STATE_INVALIDATED when superseded)
```

> Every reviewer mutation already invalidated; extraction did not. So a document extracted after
> finalize left the stored review, declaration and XML artifact untouched, and `GET /jobs/{id}/xml`
> kept serving the superseded file at 200 — to a reviewer who very likely uploaded that document
> *because* the first result was wrong.

**The lock is taken briefly, at the end.** It deliberately does not span the OCR/LLM calls: those
commit mid-flight so SQLite's single writer is not held across them, and the client extracts documents
in parallel. It covers the one window that matters — this delete racing a concurrent finalize writing
the very artifact being deleted.

The role-scoped overlay reset is specified in `docs/detailed-review-spec.md` §8.8. Summary: INVOICE
evidence discards every item channel; AWB/PACKING evidence discards the shipment override and the
weight/carton pins only; explicit HS selections are folded into name-keyed history rather than
re-bound by position.

### 7.5 Interrupted runs

**Single-process mode:** on startup, every document still in `EXTRACTING` is returned to `UPLOADED`
(not `FAILED` — nothing failed and nothing is lost, since the OCR envelope was committed as soon as it
was paid for) with the note *"the server restarted while this document was being extracted… press
Continue to run it again (the stored OCR is reused, no re-scan)."*

**Queue mode disables that sweep** — redelivery *is* the crash recovery. A worker heartbeats the
message's visibility while it runs; the only undeleted messages are those whose worker died mid-run.
After `maxReceiveCount` such deliveries the queue moves the message to the DLQ, so the DLQ holds
exactly the poison documents that kill workers.

### 7.6 Queue mode contract

```
producer (POST /extract, EASYCUSTOMS_QUEUE_PROVIDER=sqs)
    claim the document (committed)  →  send ONE message {v, kind, job_id, document_id}
    send failed?  →  release the claim on the spot  →  502
worker
    EXTRACTING     → it is ours: run it (claim_from=(EXTRACTING,))
    anything else  → done elsewhere / failed / released / job deleted: delete the message, touch nothing
    success, refusal, skip, extraction failure → delete the message
```

* **The message carries references only** — never file bytes. Documents live in shared storage, jobs
  in the shared database, and an SQS body is capped at 256 KB anyway.
* **A message dies with its attempt.** Auto-retrying a failure here would race the human who is being
  shown a retry button: two live messages, two concurrent paid extractions, last writer silently
  winning. Transient vendor hiccups are already retried inside the pipeline.
* The message schema is **versioned** (`MESSAGE_V`) so a worker can refuse what it does not understand
  instead of guessing.
* Known, accepted residue: if the heartbeat itself fails while the worker is alive and mid-run, a
  second worker can take the document over and both commit — a duplicate spend, last writer wins,
  logged loudly on both sides.
* Switching the flag requires stopping the API **and** every worker first. A document left
  `EXTRACTING` by a crash while the queue was off has no message and no worker will ever release it.

The client needs no change for queue mode: it already polls any document it sees in `EXTRACTING` and
continues by itself when the claim is released.

---

## 8. The API surface

### 8.1 Routes

| Route | Purpose |
| --- | --- |
| `POST /api/jobs` | create an empty job → `{job_id, status}` |
| `POST /api/jobs/demo` | seed the bundled sample shipment (provenance `BUNDLED_DEMO`) |
| `POST /api/jobs/{id}/documents/{role}` | attach one PDF or one photo (multipart `file`) |
| `POST /api/jobs/{id}/documents/{role}/photos` | attach several photos as ONE document (multipart `files[]`) |
| `POST /api/jobs/{id}/documents/{doc_id}/extract` | run (or queue) the extraction |
| `POST /api/jobs/{id}/documents/{doc_id}/role-decision` | `{accept, reason}` — answer a role mismatch |
| `DELETE /api/jobs/{id}/documents/{doc_id}` | detach a document |
| `GET\|HEAD /api/jobs/{id}/documents/{doc_id}/file` | serve the stored upload inline (the evidence viewer) |
| `GET /api/jobs/{id}` | job + per-document `{document_id, role, file, status, role_match, warnings, provenance}` |
| `GET /api/config` | providers, live-readiness, models, ADR flags, reference counts |
| `GET /api/usage` | this account's spend this calendar month |

**Upload response:** `{document_id, role, role_match, status, warnings}`.
**Extract response:** the same shape, plus `queued: true` in queue mode.

**`HEAD` on the file route is not decoration.** The evidence panel frames that URL, and a frame
reports nothing back — its load event fires for a refusal exactly as for a PDF. The panel asks `HEAD`
first and renders the reason instead of an empty grey rectangle. Without the method registered it
answered 404 (the SPA mount at `/` swallows the 405) and every document was reported unavailable.

**Remote objects are streamed through the app, not redirected** to a presigned URL. A redirect is
cheaper and is the obvious optimisation if egress ever shows up on the bill — but the page's CSP is
`default-src 'self'`, so a redirect to the bucket's origin is refused by the browser unless that
origin is added to `frame-src`. Widening the CSP of the page that holds the finalize button, to save
bandwidth on a few-MB PDF, is the wrong default trade. Streaming also keeps the bytes behind this
app's own login.

Documents stay viewable in **every** job state, including after finalize — post-clearance audits
arrive months later. They just cannot be changed.

### 8.2 Cost metering and quota

* **The quota is checked before the claim and before anything is bought** — the last point at which
  refusing costs nothing. Over the cap → `429` with
  `{status:"QUOTA_EXCEEDED", code:"USAGE_LIMIT_REACHED", detail:"…"}`.
* The cap is **this account's own** (`metering.resolved_cap`): its `account_quota` row when it has
  one, otherwise the deployment default, and `<= 0` is unlimited at either level.
* The detail is a **sentence, not a bool**: "This account has extracted N documents this calendar
  month, which is its limit of M. Extraction resumes at the start of next month, or when an
  administrator raises the limit." A limit is only actionable if it says what it was — and that
  second remedy is real: `PUT /api/admin/accounts/{owner_key}/quota` is what an administrator raises
  it with, and the account is findable in `GET /api/admin/accounts` because it is ordered by
  documents extracted this month.
* **One line before the quota gate, a disabled account is refused**: `403` with
  `{status:"REFUSED", code:"ACCOUNT_DISABLED", detail:"…"}`. Same reasoning, one step further — a
  session that was issued before the operator disabled the account keeps working until its 24h token
  expires, so a deny-list checked only at sign-in would do nothing about the account spending this
  deployment's vendor budget right now. One primary-key read, on the one route about to buy OCR.
  Every other route keeps serving that account its own jobs until the token expires.
* OCR spend is recorded **next to the paid call and only for a real one** — the offline provider and
  fixture replays cost nothing, and a report that counted them would make the demo look like spend.
  Recorded: provider, operation, model, job, document, calls, pages.
* Extraction spend records calls and prompt / cached / completion tokens, including on a budget abort.
* The owner is read fresh from the job, not passed down: the extractor and OCR provider have no
  business knowing about users, and the queue worker never saw the request that created the job.
* `estimated_cost_usd` is null until vendor rates are configured, and `unpriced_events` counts calls
  no rate could price — a non-zero value means the figure shown is a **floor**, not a total. Token and
  page counts are always exact; money never guesses.

---

## 9. The upload screen

### 9.1 Layout

A header row — **⚡ Load Sample Shipment (demo)**, **New empty job**, the job pill, and a
`"{filled} of {present} documents extracted"` counter (counted, not inferred from scanning six boxes)
— above a grid of one box per role.

### 9.2 Box states

| State | Pill | Box shows |
| --- | --- | --- |
| empty | the role's fallback tag | file input + 🖼 Add photos + 📷 Take photo |
| uploading | `uploading…` + spinner | the file name |
| stored, not yet extracted | `uploaded — pending` | file name, ✕ remove |
| extracting | `extracting…` + spinner | file name, engine name |
| extracted | `extracted` | 📄 file name (opens the viewer), ✕ remove |
| role mismatch | `role mismatch — decide below` | the decision panel (§9.4) |
| failed | `failed — retry` | ✕ remove + the file input, so Continue can retry |
| rejected | `rejected — not declared` | note that the upload and its read text are kept |
| reviewer-confirmed role | `role confirmed by reviewer` | as extracted |

**A box shows one document but a role may hold several.** The one displayed is chosen in this order:
an unanswered role mismatch → any live document → the last one. An unanswered mismatch **outranks a
healthy sibling** — otherwise the card keeps showing the good document and the decision the backend is
waiting for has no button anywhere on the page.

### 9.3 Drag & drop

A box accepts a drop only when it can take a file (not busy, empty or holding a dead document).
One PDF → uploaded directly. Several files, all images → routed to the **photo stage** so the reviewer
sets page order and confirms they are one document. A mixed drop is refused with the reason.

### 9.4 The role-mismatch decision

Rendered inside the box as a question, never a badge:

> **{file}** does not look like a **{role}**. Declaring it as one would put its rows, totals and
> parties on the SAD. Check the document, then decide:
> `[👁 View the document]  [It is a {role} — use it]  [Wrong box — exclude it]`

* **Accept** → `EXTRACTED`, used as declared (the extractor was wrong).
* **Reject** → `ROLE_REJECTED`, excluded from the declaration; the upload and its OCR are kept.
* Either way everything derived is invalidated and the reviewer goes back through Critical Review.
* Answering when the document is not awaiting a decision → `DOCUMENT_ROLE_NOT_IN_REVIEW`.

> This used to be a coloured pill and nothing else: the status gated no endpoint, and the raw-grouping
> step read any document carrying a `raw_extraction`. So a packing list dropped into the INVOICE box
> became the goods roster — its rows priced, its totals declared, its parties on the SAD — and the
> only sign was a badge on an upload card. `declarable_documents` is now the single gate: having a
> stored extraction is **not** sufficient to be read.

### 9.5 Continue

```
Continue → Extract N documents        (or "Extract / retry" when any is FAILED)
```

* Extracts every `UPLOADED` or `FAILED` document **concurrently** (`Promise.allSettled`); the backend
  gates global LLM concurrency and retries 429s, so total wall time ≈ the slowest document, not the
  sum.
* Each box updates as its own document finishes.
* Failures are collected and reported together, with the reminder that a retry reuses the stored
  document text.
* **Auto-continue:** when every required role is genuinely `EXTRACTED` and nothing is left to answer,
  Critical Review is computed automatically. Counting a rejected, undecided or still-running document
  as "ok" earns a refusal from the review endpoint, shown as a red error — so the client uses exactly
  the backend's own gate.
* A tab that did **not** start the run still continues by itself: it polls any document it sees in
  `EXTRACTING` every 4 s and computes the review when the claim is released. Without this, a reload
  during extraction (or an extraction started in another tab) leaves a page that never updates, which
  reads as "stuck" and invites a second, duplicate Continue.

### 9.6 Removing a document

`✕ remove` detaches it. The confirmation names the cost when the document was already extracted:
*"Its extracted rows and values are discarded; the review recomputes without them."*

* Refused while an extraction is running (`DOCUMENT_EXTRACTION_RUNNING`).
* Deletes the row, invalidates derived state, runs the role-scoped overlay reset, deletes the stored
  file best-effort, and audits `DOCUMENT_REMOVED`.
* Job status falls back to `EXTRACTION_COMPLETE` if any extracted document remains, else `UPLOADING`.

This is also the "remove the existing document first if you meant to replace it" step that the
duplicate-upload refusal instructs.

### 9.7 Gates before Critical Review (`_require_extracted`)

In order, each with an actionable message:

| Condition | Code |
| --- | --- |
| any document `EXTRACTING` | `EXTRACTION_IN_PROGRESS` — "the review would be computed without its rows, weights and parties" |
| any document `UPLOADED` or `FAILED` | `DOCUMENTS_NOT_EXTRACTED` |
| any document `ROLE_REVIEW_REQUIRED` | `DOCUMENT_ROLE_UNCONFIRMED`, naming each file |

The in-flight check matters as much as the others: an extraction still running carries no
`raw_extraction`, so letting the review through would declare the shipment as if that document did not
exist — and the reviewer would have no reason to doubt a review that computed without complaint.

---

## 10. Configuration

| Setting | Env var | Default |
| --- | --- | --- |
| OCR provider | `EASYCUSTOMS_OCR_PROVIDER` | `mistral` |
| OCR model | `EASYCUSTOMS_MISTRAL_OCR_MODEL` | `mistral-ocr-latest` |
| OCR key | `EASYCUSTOMS_MISTRAL_API_KEY` / `MISTRAL_API_KEY` | — |
| OCR timeout | `EASYCUSTOMS_MISTRAL_OCR_TIMEOUT_SECONDS` | 120 |
| Extraction provider | — | `openai` |
| LLM key | `EASYCUSTOMS_OPENAI_API_KEY` / `OPENAI_API_KEY` | — |
| LLM model | — | `gpt-4o-mini` (any structured-output-capable model) |
| LLM concurrency | `EASYCUSTOMS_LLM_CONCURRENCY` | 4 |
| LLM timeout | `EASYCUSTOMS_LLM_TIMEOUT_SECONDS` | 180 |
| Repair rounds | — | 2 |
| Packing budget | `EASYCUSTOMS_PACKING_EXTRACTION_BUDGET_SECONDS` | 240 |
| Page ceiling | `EASYCUSTOMS_EXTRACTION_MAX_PAGES` | 150 (`0` disables) |
| Chunk threshold / size / packing size | — | 6 / 4 / 2 |
| Deterministic parser | — | on |
| Vendor layout memory | — | on |
| Max upload | `EASYCUSTOMS_MAX_UPLOAD_MB` | 25 |
| Min photo short edge | `EASYCUSTOMS_MIN_PHOTO_PX` | 1000 |
| Max photo pixels | `EASYCUSTOMS_MAX_PHOTO_PIXELS` | 80,000,000 |
| Max photos per document | `EASYCUSTOMS_MAX_PHOTOS_PER_DOCUMENT` | 40 |
| Fixture uploads | `EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS` | **false** |
| Monthly document cap | `EASYCUSTOMS_USAGE_MONTHLY_DOCUMENT_CAP` | 0 = unlimited |
| Queue provider | `EASYCUSTOMS_QUEUE_PROVIDER` | `off` (`sqs` requires a queue URL) |

`GET /api/config` publishes `ocr_provider`, `extraction_provider`, `ocr_live`, `extraction_live`,
`llm_model` and the reference counts, so the UI can tell the reviewer whether a real run is possible.

---

## 11. Message catalogue

### 11.1 Upload refusals (all blocking, scope `DOCUMENT`)

| Code | Cause |
| --- | --- |
| `EMPTY_DOCUMENT_UPLOAD` | 0 bytes, or a photo set with no photos |
| `DOCUMENT_TOO_LARGE` | over `max_upload_mb` (also checked after photo merge) |
| `UNSUPPORTED_DOCUMENT_TYPE` | not a PDF/JPEG/PNG; HEIC; a markup file with a PDF header further in; a PDF sent to the photo route |
| `PHOTO_NEEDS_LIVE_OCR` | a photo on a server running offline text-layer OCR |
| `DOCUMENT_TOO_MANY_PAGES` | over `extraction_max_pages` |
| `TOO_MANY_PHOTOS` | over `max_photos_per_document` |
| `PHOTO_TOO_LARGE` | over `max_photo_pixels` |
| `UNREADABLE_IMAGE` | damaged or truncated image |
| `PHOTO_RESOLUTION_TOO_LOW` | short edge under `min_photo_px` |
| `DUPLICATE_DOCUMENT_UPLOAD` | byte-identical file already in that role box |
| `FIXTURE_UPLOADS_DISABLED` | supplied extraction values with the flag off |

### 11.2 Extraction-run outcomes

| Code | Meaning |
| --- | --- |
| `EXTRACTION_ALREADY_RUNNING` | the claim was lost to a concurrent run (409) |
| `EXTRACTION_FAILED (<Type>)` | stored on the row; the route answers 502 |
| `EXTRACTION_INTERRUPTED` | the process restarted mid-run; returned to `UPLOADED` |
| `USAGE_LIMIT_REACHED` | monthly document cap, this account's own (429) |
| `ACCOUNT_DISABLED` | the account is on the operator's deny-list (403) |
| `DOCUMENT_ROLE_NOT_IN_REVIEW` | a role decision on a document not awaiting one |
| `DOCUMENT_EXTRACTION_RUNNING` | removal attempted mid-extraction |

### 11.3 Extraction-quality markers (carried into the review)

| Marker | Meaning |
| --- | --- |
| `PACKING_EXTRACTION_OVER_BUDGET` | hard abort — quantity-share fallback |
| `PACKING_EXTRACTION_PARTIAL` | some windows landed — extracted rows used, the rest named |
| `TABLE_PARSER: stood down …` | the deterministic parse was rejected, with the reason |
| `COLUMN_ROLES_RESOLVED` | packing column roles proposed by a model and accepted only because the printed totals then closed |
| `PAGE_ROWS_MISSING` / `ROW_ANCHOR_MISSING` | completeness gates fired |
| `PACKING_AUTHORITY_PARTIAL` | shipment totals come from a packing list whose extraction did not finish — the figure read as the document total may be an interior subtotal |

### 11.4 Audit events

`DOCUMENT_UPLOADED`, `DOCUMENT_UPLOAD_RETRIED`, `EXTRACTION_SEEDED`,
`DOCUMENT_EXTRACTION_STARTED`, `DOCUMENT_EXTRACTED`, `DOCUMENT_EXTRACTION_FAILED`,
`DOCUMENT_EXTRACTION_INTERRUPTED`, `DOCUMENT_ROLE_CONFIRMED`, `DOCUMENT_ROLE_REJECTED`,
`DOCUMENT_REMOVED`, `DERIVED_STATE_INVALIDATED`, `REVIEW_STATE_RESET`.

The audit trail is written by the server as the work happens, never by the client — it is the record
of what actually ran.

---

## 12. Acceptance checklist

1. Every rejection happens at upload, before any paid call — nothing reaches OCR that a cheap check
   could have refused.
2. The page ceiling is enforced from the PDF page tree at upload, and again before OCR.
3. A file whose bytes begin with markup is refused even when a PDF header appears later.
4. Photos convert to one multi-page PDF in the reviewer's chosen order; a photo set is never several
   documents.
5. The stored content type is a server fact; the client's MIME type is never persisted or reflected.
6. A byte-identical file in the same role box is refused; re-picking after a failure resets the
   existing row instead of adding a second.
7. The OCR envelope is committed the moment it is paid for; retries never re-OCR.
8. The vendor file store is cleaned up after every OCR call.
9. Extraction is claimed with a status-guarded UPDATE; a lost race is a 409, never a FAILED.
10. The deterministic parser owns a page only when every suspicious line on it confirmed; its
    stand-down reason always reaches the reviewer.
11. Arithmetic verification gates every parsed row, including rows parsed from remembered layouts.
12. The packing budget produces three distinguishable outcomes, and PARTIAL keeps the rows it got.
13. Every payload passes the deterministic validator; repair rounds are bounded.
14. Evidence quotes are truncated, never rejected.
15. `page_numeric_locales` and the reference backfill are stamped by code, never by the model, and
    each backfill is reported.
16. A role mismatch blocks the review until answered; a rejected document contributes nothing
    anywhere.
17. A successful extraction invalidates the stored review, declaration and XML artifacts, and runs
    the role-scoped overlay reset.
18. Extraction provenance is recorded on the row, and a fixture without a stated provenance is
    downgraded and gated.
19. The quota is checked before the claim; spend is recorded only for real paid calls.
20. In queue mode the producer claims before sending, the message carries references only, and a
    message dies with its attempt.
