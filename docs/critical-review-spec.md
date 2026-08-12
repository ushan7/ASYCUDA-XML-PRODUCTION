# Critical Review — Complete Functional & Technical Specification

**Audience:** a developer re-implementing this screen in another language/stack.
**Scope:** everything the Critical Review section is, every field on it, where each field's value
comes from, what validates it, what it produces, and what blocks the declaration.

Source of truth in the reference implementation (Python/FastAPI + React SPA):

| Concern | File |
| --- | --- |
| Review model, build, merge, Field 9 composition | `backend/app/review/critical_review.py` |
| Item add/delete/edit/pin/HS/COO overlay | `backend/app/review/item_mutations.py` |
| Derived packing-authority view | `backend/app/review/packing_view.py` |
| Orchestration (build → preview → finalize) | `backend/app/pipeline.py` |
| HTTP routes | `backend/app/main.py` |
| Job lifecycle, locking, persistence, audit | `backend/app/services.py` |
| Final blocking validation | `backend/app/declaration/validator.py` |
| Declaration/valuation assembly | `backend/app/declaration/builder.py` |
| XML serialization | `backend/app/xml/composer.py` |
| Weight-unit conversion boundary | `backend/app/units.py` |
| Date normalization | `backend/app/dates.py` |
| Reference tables (HS, banks, offices, procedures…) | `backend/app/reference/store.py` + `backend/reference_data/` |
| Weight/carton allocation rules | `docs/allocation-spec.md` |

---

## 0. The one rule that governs this entire screen

> **The LLM is never the final authority.**
> OCR + extraction produce only *raw facts with page evidence*. Every value that reaches the XML is
> either (a) the output of a deterministic Python rule resolved against official reference data, or
> (b) an explicit entry made by the human reviewer **on this screen**.

Three consequences your implementation must preserve:

1. **Recomputing Critical Review never re-reads documents.** It replays the deterministic rules from
   the *stored raw extraction*. No OCR, no LLM call, no cost. This is why the reviewer can press
   "Recompute" freely.
2. **Every reference-coded value is validated against the official table**, both at write time (the
   side-channel endpoints) and again at finalize (the validator). A code that is not in the table is
   rejected — never "accepted quietly" or auto-corrected.
3. **A rule that looks wrong is reported, never silently fixed.** These are customs rules, not
   implementation details.

---

## 1. Where Critical Review sits

```
upload documents (only the invoice is compulsory)
        │
        ▼
OCR → extraction → deterministic validator            (per document, cached)
        │
        ▼
┌──────────────────── resolve_context (replays from stored extraction) ────────────────────┐
│ invoice authority → banking → AWB/BL authority → packing match → HS → COO                │
└──────────────────────────────────────────────────────────────────────────────────────────┘
        │
        ▼
  ***  CRITICAL REVIEW GATE  ***   ← this document
        │  (human confirms declaration-level control values)
        ▼
weight/carton allocation → supplementary units → freight/insurance
        → merged declaration → blocking validation → ASYCUDA XML + brand/model/size .xls
```

Critical Review is a **gate**, not a form that stores data. Nothing on it is persisted as "the
review's answers" except two explicitly durable side-channels (§8). The reviewer's answers travel
in the **finalize request body**.

### 1.1 The three kinds of value on this screen

| Kind | Meaning | Examples |
| --- | --- | --- |
| **Direct XML values** | copied verbatim into one XML element | manifest number, party names, Field 18/21, bank code |
| **Derived XML values** | inputs to a deterministic composition | Field 9, Field 40, first-item banking texts, package kind, weight unit → kg |
| **Review / audit values** | provenance & lock metadata, never reach XML | resolution states, candidate lists, reasons, fingerprint |

---

## 2. Lifecycle & state machine

```
  ┌─────────────────────────┐
  │ documents EXTRACTED     │
  └───────────┬─────────────┘
              │  GET/POST /critical-review
              ▼
  ┌─────────────────────────┐   side-channel mutations (§8, §9)
  │ CRITICAL_REVIEW_REQUIRED│◄──────────────────────────────────┐
  └───────────┬─────────────┘   each one recomputes the review, │
              │                 bumps a revision, invalidates   │
              │                 declaration + XML artifacts     │
              │  POST /finalize (body = reviewer answers)       │
              ▼                                                  │
      fingerprint match? ── no ──► REVIEW_STALE (409) ───────────┘
              │ yes
              ▼
  merge_confirmation → allocation → validation
              │
      ┌───────┴────────┐
      ▼                ▼
  XML_READY     VALIDATION_BLOCKED
  (0 blockers)  (blockers present; in warn mode the XML is still
                 built and labelled "test XML")
```

### 2.1 Job statuses relevant here

`CRITICAL_REVIEW_REQUIRED`, `VALIDATION_BLOCKED`, `XML_READY` — these three are the **item-mutable
states**. Item add/delete/edit is refused (HTTP 409 `JOB_STATE_NOT_REVIEWABLE`) in any other state.

### 2.2 The review fingerprint (stale-review lock)

Every computed review carries `review_fingerprint` = `sha256(json.dumps(basis, sort_keys=True))`.

**Basis keys — exactly these, nothing else:**

```
items            = invoice_item_count
total            = calculated_goods_total
currency         = goods_currency
invoices         = ["<number>|<date>", …]      (zip of invoice_numbers, invoice_dates)
gross            = gross_weight
packages         = total_packages
hawb, mawb, bl, bl_date, doc_type
authority        = shipment_authority_type
bank             = [bank_code, bank_name, swift_code]
terms            = payment_term_code
bank_ref         = [bank_reference, bank_amount, bank_date]
field40          = field_40_previous_document
freight          = manual_freight_amount
item_revision    = item_mutation_revision
regime_revision  = regime_revision
```

Rules:

* The client echoes `review_fingerprint` back in the finalize body.
* If it differs from the freshly computed one, finalize is **refused** with
  `{"status": "REVIEW_STALE", "critical_review": <fresh review>}` at HTTP **409**, the job is put
  back into `CRITICAL_REVIEW_REQUIRED`, and the client must re-verify.
* The fingerprint covers **computed defaults only**, not what the reviewer typed. Typing in the form
  never stales it; changing the underlying evidence, item set, or regime selection does.
* `item_mutation_revision` and `regime_revision` are in the basis on purpose: a count-neutral swap
  (delete a row, add a manual one at the same price) leaves every other key identical, and would
  otherwise pass a stale fingerprint check.
* Legacy `hs_overrides` (keyed by item sequence number) **require** a fingerprint. Without one,
  finalize raises `HS_OVERRIDES_REQUIRE_FINGERPRINT` — sequences shift on add/delete, so an
  SN-keyed override could otherwise land on the wrong item.

### 2.3 Concurrency

Every mutating operation takes a **job lock** (Postgres advisory lock / in-process lock for SQLite),
re-reads the job under the lock, does its work, and **commits before releasing**. Implement the
equivalent: two browser tabs finalizing the same job must serialize, not interleave.

---

## 3. API surface

### 3.1 The review itself

```
GET  /api/jobs/{job_id}/critical-review
POST /api/jobs/{job_id}/critical-review     (identical behaviour)
```

*Not idempotent-read:* it **recomputes and commits**. It stores the result on the job
(`job.critical_review`), sets status `CRITICAL_REVIEW_REQUIRED`, and writes a
`CRITICAL_REVIEW_BUILT` audit event. Returns the full `CriticalReview` object (§4).

Refuses (409, structured `blocking_errors`) if required documents are not extracted, or a document
sits in `ROLE_REVIEW_REQUIRED`.

### 3.2 Finalize (where the reviewer's answers are submitted)

```
POST /api/jobs/{job_id}/finalize      body = CriticalReviewConfirmation (§5) + extras
```

Extras accepted in the same body: `exchange_rate`, `hs_overrides` (legacy, SN-keyed).
Legacy aliases folded in: `insurance_national` → `manual_insurance_amount`,
`freight_override` → `manual_freight_amount`.

Response codes:

| Condition | HTTP | Body |
| --- | --- | --- |
| XML built, no blockers | 200 | declaration (`ready_for_xml: true`) |
| XML built **with** blockers (warn mode) | 200 | declaration, `xml_built_with_blockers: true`, `blocking_errors[]` |
| Blocked and no XML built | 409 | declaration with `blocking_errors[]` |
| Stale fingerprint | 409 | `{status:"REVIEW_STALE", critical_review}` |
| Reported item count ≠ actual | 409 | `{status:"ITEM_COUNT_MISMATCH", expected:N}` |

### 3.3 Side-channel writes (durable, outside the finalize body)

```
POST   /api/jobs/{id}/regime                    regime/office/transport selections (§8.1)
POST   /api/jobs/{id}/shipment-totals           reviewer gross/packages authority (§8.2)
POST   /api/jobs/{id}/items                     add a goods row (§9.3)
DELETE /api/jobs/{id}/items/{item_id}           delete a goods row (§9.4)
PATCH  /api/jobs/{id}/items/{item_id}           edit invoice fields / pins (§9.5)
POST   /api/jobs/{id}/items/hs-review           set HS on one row (§9.6)
POST   /api/jobs/{id}/items/hs-review-range     set HS on an SN range (§9.6)
POST   /api/jobs/{id}/items/coo-all             stamp one COO on every row (§9.7)
POST   /api/jobs/{id}/items/brand-model-size    export-only BRAND/MODEL/SIZE (§9.8)
```

Every one of these returns `{"status":"ok", …, "critical_review": <freshly recomputed review>}` so
the client re-renders from the server rather than patching local state.

### 3.4 Reference lists (read-only, local, network-free)

| Endpoint | Contents | Used by |
| --- | --- | --- |
| `GET /api/reference/customs-offices` | 71 NECAS offices `{code,name}` | Box A, Box 29 |
| `GET /api/reference/declaration-models` | 17 lines `{type,code,description}` | Box 1 |
| `GET /api/reference/extended-procedures` | ANNEX 1, 55 rows, annex order | Box 37 (extended) |
| `GET /api/reference/national-procedures` | ANNEX 3, 182 rows | Box 37 (national) |
| `GET /api/reference/transport-modes` | 01–09 | Box 25 / Box 26 |
| `GET /api/reference/incoterms` | Incoterms 2020 (11) | delivery terms |
| `GET /api/reference/banks` | `{code,name,swift}` | bank picker |
| `GET /api/reference/payment-terms` | ASYCUDA terms table | payment-term picker |
| `GET /api/reference/hs?q=&limit=` | HS search, prefix or description | Detailed Review |
| `GET /api/config` | ADR flags, defaults, reference counts | boot |

---

## 4. Data contract — `CriticalReview` (server → client)

All monetary/weight/count values are **strings**, formatted server-side to a fixed precision. Never
send floats. Blank string = "not derivable / not entered", it is not zero.

### 4.1 Declaration summary

| Field | Type | Format | Meaning |
| --- | --- | --- | --- |
| `invoice_item_count` | int | | goods lines after the mutation overlay is applied |
| `total_number_of_forms` | int | | `1 + ceil(max(n-1, 0) / 3)` — SAD main form holds 1 item, each continuation sheet 3 |
| `calculated_goods_total` | str | `%.2f` | Σ line totals (Decimal) |
| `goods_currency` | str | | invoice currency; **mixed currencies is a hard blocker** |
| `gross_weight` | str | `%.4f` or `""` | authority gross, in `weight_unit_evidence`'s unit |
| `gross_weight_source` | str | enum | see §4.6 authority types |
| `total_packages` | str | `%.2f` or `""` | authority package count |
| `packages_source` | str | enum | |
| `package_type` | str | code | default `CT` |
| `package_type_name` | str | | derived from code |
| `reviewed_packing_weight_unit` | str | code | normalized evidence unit, else `KGM` |
| `weight_unit_evidence` | str | | the unit **as printed** on the source document |
| `weight_unit_missing` | bool | | true → an extra confirmation checkbox appears and finalize gates on it |

### 4.2 Declaration identity & regime

`manifest_no` (always `""` from the server — customs assigns it), `declaration_type`,
`gen_procedure_code`, `customs_office_code`, `customs_office_name`,
`extended_customs_procedure`, `national_customs_procedure`, `regime_revision`.

### 4.3 Invoices

| Field | Type | Notes |
| --- | --- | --- |
| `invoice_numbers` | `str[]` | in authority order |
| `invoice_dates` | `str[]` | normalized `DD-MMM-YYYY`, parallel array |
| `invoice_roster` | `RosterEntry[]` | `{number, date, currency, item_count, source_file, pages[]}` |
| `field_9_invoice_transport_document` | str | the **auto-composed** Field 9 text (§7.2) |

`invoice_roster` handles multi-invoice attachments: each bundled sub-invoice maps to its own page
span (its first page → the page before the next sub-invoice starts).

### 4.4 Parties

```
exporter / importer : PartyReview {
    name, address, exim_code, country_code,
    country_resolution_state: "RESOLVED" | "INVALID" | "ABSENT"
}
importer_exim_valid : bool     # regex ^[A-Za-z0-9]{13,15}$ (full match)
```

### 4.5 Shipment authority

| Field | Notes |
| --- | --- |
| `hawb_no`, `mawb_no` | air waybill numbers |
| `bill_of_lading_no`, `bill_of_lading_date` | B/L; date normalized `DD-MMM-YYYY` |
| `bill_of_lading_source` | `TRANSPORT_DOCUMENT` \| `INVOICE` \| `""` |
| `transport_doc_type` | `"AWB"` \| `"BL"` — decides the first Field 9 line (§7.1) |
| `transport_doc_type_reason` | human-readable explanation of how the default was reached |
| `shipment_authority_type` | see below |
| `shipment_candidates` | `{form_id, decision, awb_number, gross_weight, packages, selected, reasons[]}[]` |
| `gross_weight_source_doc`, `package_count_source_doc` | logical form id of the selected authority |
| `mixed_source` | true when weight and packages came from different documents |
| `gross_weight_authority`, `carton_authority` | per-value provenance `{document, type, value, unit, label, confidence, reasons[]}` |

### 4.6 Authority-type enumeration

`HAWB`, `TRUE_DO` (delivery order), `TRACKING`, `SINGLE_AWB`, `MAWB`, `MIXED_DO_PACKING`,
`BILL_OF_LADING`, `PACKING_TOTALS`, `REVIEWER_OVERRIDE`, `UNKNOWN`.

The ladder (highest first): **HAWB > TRUE_DO > TRACKING > SINGLE_AWB > packing totals**.
A master AWB's weight/count is *never* auto-used when a house-level document exists — a MAWB may
carry consolidated airline-level cargo. `REVIEWER_OVERRIDE` (§8.2) sits above all of them.

### 4.7 Transport

`field_18_transport_identity`, `field_21_transport_identity`, `field_40_previous_document`,
`field_40_reason`, `field_40_candidates[]`, `border_mode`, `inland_mode_of_transport`,
`border_nationality`, `border_office_code`, `border_office_name`, `place_of_loading_code`,
`location_of_goods`, `container_flag`.

`field_40_candidates` = `[{source: "HAWB"|"MAWB"|"B/L", value, suggested: bool}]`.

### 4.8 Incoterm, bank, payment, banking reference, freight, insurance

See §6.4 – §6.8 for the field-by-field rules.

### 4.9 Review/audit block

| Field | Notes |
| --- | --- |
| `evidence` | `EvidenceItem[]` — `{field, normalized_value, raw_value, source_role, source_file, page_number, quote}`, read from stored extractions only |
| `warnings` | `ValidationMessage[]` — `{code, severity, scope, document_id, item_sequence, field, message, remediation}` |
| `review_fingerprint` | §2.2 |
| `packing_view` | derived packing-authority JSON, or `null` (§10) |
| `item_details` | `ItemDetailRow[]` — the Detailed Review preview (§9.1) |
| `item_mutation_revision` | overlay revision counter |

---

## 5. Data contract — `CriticalReviewConfirmation` (client → server)

### 5.1 Merge semantics — get this exactly right

```
pick(conf_value, computed_default):
    if conf_value is null/absent  ->  computed_default     # "keep what you computed"
    else                          ->  str(conf_value).strip()   # explicit, even if ""
```

**`null`/absent ≠ `""`.** `null` means "keep the computed default"; `""` means "the reviewer
deliberately blanked this". Getting these confused is the classic bug: a form that always sends
every key as a string can never say "keep your default".

Booleans are plain booleans, except `container_flag` which is `bool | null` and follows the same
null-means-default rule.

### 5.2 Fields

```
# shipment totals
confirmed_gross_weight, confirmed_total_packages, reviewed_packing_weight_unit
manual_shipment_authority_confirmed : bool
reported_item_count : int|null            # mismatch → ITEM_COUNT_MISMATCH refusal
mixed_source_reason : str|null

# identity & transport
manifest_no, package_type
hawb_no, mawb_no, bill_of_lading_no, bill_of_lading_date, transport_doc_type
field_18_transport_identity, field_21_transport_identity
field_40_previous_document, field_40_confirmed : bool
field_9_text, field_9_override : bool

# regime & offices (also settable durably via POST /regime)
declaration_type, gen_procedure_code
customs_office_code, customs_office_name
border_office_code, border_office_name
extended_customs_procedure, national_customs_procedure
border_mode, inland_mode_of_transport, border_nationality
place_of_loading_code, location_of_goods, container_flag

# parties
exporter_name, exporter_address, exporter_exim_code, exporter_country_code
importer_name, importer_address, importer_exim_code, importer_country_code

# incoterm
incoterm, delivery_place, invoice_values_exclude_freight_insurance : bool

# bank & payment
bank_code, bank_name, swift_code, payment_term_code, mode_of_payment
bank_reference, bank_amount, bank_currency, bank_date

# costs
manual_freight_amount, manual_insurance_amount

# lock
review_fingerprint
```

### 5.3 Merge results (`ReviewedValues`) — the single input to the build

Type conversions applied during merge:

| Output | Rule |
| --- | --- |
| `gross_weight_kg` | `parse_decimal(gross) × WEIGHT_UNIT_TO_KG[unit]` — **always converted to kg here** |
| `total_packages` | `parse_decimal(packages)` |
| `weight_unit` | upper-cased; `""` → `KGM` |
| `package_type_code` | upper-cased; `""` → `CT`; name looked up from the code |
| `bank_amount` | `parse_decimal(text)` if text non-empty, else `None` |
| `freight_amount` | `parse_decimal(text)` if text `!= ""`, else `None` (→ deterministic rule runs) |
| `insurance_amount` | `parse_decimal(text)` or `Decimal("0")`; **never** `None` |
| `field_9_override` | `conf.field_9_override AND field_9_text is non-empty after stripping XML-illegal control chars` |
| `transport_doc_type` | explicit `AWB`/`BL` wins; else re-derived from the **merged** numbers (§7.1) |
| `customs_office_name` | derived from the code via the reference; explicit name is the escape hatch only |
| `border_office_code` | explicit `""` = "same as clearance office" |

Every reviewer entry that differs from the computed default is recorded in the audit event
`CRITICAL_REVIEW_LOCKED` as `{field: {from, to}}`, including the exchange rate.

---

## 6. Field-by-field specification

Layout: seven sections, plus a read-only KPI strip and an evidence panel. A "jump to" index sits
above them. Every section renders the same three-part control pattern: **label → control →
provenance/state chip**.

---

### 6.0 KPI strip (read-only)

| Tile | Value | Sub-label |
| --- | --- | --- |
| Items / forms | `invoice_item_count` / `total_number_of_forms` | goods lines / SAD forms |
| Total invoice | `calculated_goods_total` | `goods_currency` |
| Gross weight | `gross_weight` or `—` | `gross_weight_source` · `weight_unit_evidence` + 📄 deep-link |
| Packages | `total_packages` or `—` | `packages_source` + 📄 deep-link |

The 📄 button opens the source document in a pinned side panel, **on the page the value was printed
on**. Implement this: it is what makes the review verifiable rather than trusted.

Below the strip: the warnings list, grouped by severity. Item-scope warnings are collapsed into a
`<details>` summary that points at the Detailed Review rows.

---

### 6.1 Section 1 — Customs office & regime

Declaration identity. **None of it comes from document evidence** — it is a reviewer/deployment
choice validated against the NECAS reference tables.

| # | Label | Control | Seeded from | Validation | Feeds |
| --- | --- | --- | --- | --- | --- |
| 1.1 | **Declaration form (Box 1)** | single `<select>` of the 17 model lines, value = `"{type} {code}"` | stored selection → `settings.declaration_type` + `declaration_gen_procedure_code` (`IM 4`) | pair must be in `declaration_models` → else blocking `DECLARATION_MODEL_INVALID` | `Type_of_declaration`, `Declaration_gen_procedure_code`; also sets `Sad_flow` = `"E"` for `EX`/`PEX`, else `"I"` |
| 1.2 | **Customs clearance office (Box A)** | searchable combo (code or name) | stored selection → `settings.customs_office_code` (`TIA00`) | must be in `customs_offices` → blocking `CUSTOMS_OFFICE_INVALID` | `Customs_clearance_office_code` / `_name` |
| 1.3 | **Border office (Box 29)** | checkbox "differs from clearance office" + combo | defaults to the clearance office | must be in `customs_offices` when set | `Border_office/Code` + `/Name`; empty falls back to the clearance office |
| 1.4 | **Extended procedure (Box 37)** | `<select>`, **filtered to the Box-1 general-procedure digit** | stored → `settings.extended_customs_procedure` (`4000`) | must be in ANNEX 1 → `PROCEDURE_INVALID`; first digit must equal `gen_procedure_code` → `PROCEDURE_TYPE_MISMATCH` | `Tarification/Extended_customs_procedure` on **every item** |
| 1.5 | **National procedure (Box 37)** | searchable combo over 182 rows (search the exemption text too) | stored → `settings.national_customs_procedure` (`000`) | must be in ANNEX 3 → `PROCEDURE_INVALID` | `Tarification/National_customs_procedure` on every item. **Sets the duty treatment.** |
| 1.6 | **Location of goods (Box 30)** | text | `settings.location_of_goods` (`TIA...IM/GODOWN`) | free text | `Transport/Location_of_goods` |
| 1.7 | **Place of loading code (Box 27)** | text, upper-cased | `settings.place_of_loading_code` (`NPKTM`) | free text | `Place_of_loading/Code`. **`Place_of_loading/Name` is deliberately emitted EMPTY** — ASYCUDA derives it from the code on import |
| 1.8 | **Container flag** | checkbox | `false` | — | `Transport/Container_flag` = `"true"`/`"false"` |
| — | **Border nationality** | not on the form; carried in the payload | `settings.border_nationality` (`NP`) | must be valid alpha-2 | `Border_information/Nationality` |

**UX rule implemented:** when `border_mode == "04"` (containerised truck) and the container flag is
off, show a nudge — a suggestion, not an auto-tick.

**💾 Save regime as job defaults** button → `POST /regime` (§8.1). Optional: finalize accepts the
form values one-shot. Saving makes them survive reloads and seed every future recompute.

**ANNEX 1 note:** code `9100` appears twice in the source file. Both rows are kept. Your `<option>`
keys must therefore include the index, not just the code.

---

### 6.2 Section 2 — Shipment totals & packages

The four values here are the **authority** that item weights and cartons reconcile to, exactly.

| # | Label | Control | Seeded from | Rules | Feeds |
| --- | --- | --- | --- | --- | --- |
| 2.1 | **Confirm / override gross weight** | text (decimal) | `review.gross_weight` | must be `> 0` at finalize → blocking `SHIPMENT_GROSS_REQUIRED` | `Valuation/Weight/Gross_weight` and `Total/Total_weight`, formatted `%.4f`; the allocation basis for every item's gross |
| 2.2 | **Weight unit** | `<select>` KGM / G / LB / OZ / T | normalized evidence unit, else `KGM` | conversion table below | multiplies 2.1 into kilograms — **the single conversion boundary** |
| 2.3 | **Confirm / override total packages** | text (decimal) | `review.total_packages` | `> 0` → else blocking `SHIPMENT_PACKAGES_REQUIRED`; must be a whole multiple of `0.01` when set via §8.2 | `Nbers/Total_number_of_packages`; the carton allocation authority |
| 2.4 | **Package type (all items)** | `<select>` | `settings.package_type_default` = `CT` | must be one of the 7 codes → blocking `PACKAGE_TYPE_UNSUPPORTED` | `Packages/Kind_of_packages_code` + `_name` on every item |
| 2.5 | **Transport document (Field 9)** | `<select>` AWB / BL | derived (§7.1) | — | decides the first line of Field 9 |
| 2.6 | **HAWB number** | text | `ship.hawb_number` | at least one of 2.6/2.7/2.8 required → blocking `TRANSPORT_REFERENCE_REQUIRED` | Field 9 line 1, Field 40 candidate |
| 2.7 | **MAWB number** | text | `ship.mawb_number` | same | same |
| 2.8 | **Bill of Lading number** | text | B/L doc → invoice's printed B/L → manual | same | Field 9 B/L line, Field 40 candidate |
| 2.9 | **B/L date** | text `DD-MMM-YYYY` | B/L doc or invoice | normalized leniently, unparseable passes through unchanged | Field 9 B/L line |
| 2.10 | **Manual shipment-authority confirmation** | checkbox, **only rendered when `weight_unit_missing`** | `false` (reset on every recompute) | unticked + unit missing → blocking `WEIGHT_UNIT_UNCONFIRMED` | gate only |

**Weight unit table (`WEIGHT_UNIT_TO_KG`) — kilograms per unit:**

```
KGM = 1            G  = 0.001         MG = 0.000001
LB  = 0.45359237   OZ = 0.028349523125    T = 1000
```

Aliases recognised when reading documents: `KG/KGS/KILO/KILOGRAM(S)/KILOGRAMME(S)` → `KGM`;
`GM/GMS/GR/GRAM(S)/GRAMME(S)` → `G`; `LBS/POUND(S)` → `LB`; `OUNCE(S)` → `OZ`;
`TON(S)/TONNE(S)/MT/METRICTON(S)` → `T`.

> **The rule that matters:** a unit that is **present but unrecognised DISQUALIFIES its source**.
> The caller falls through to the next authority and warns. It is *never* silently assumed to be
> kilograms. A silent `factor = 1` is what once turned `500 G` per unit into 500 kg and pushed a
> consignment's net weight above its gross.
> A **blank** unit means "the document did not say", which on a customs weight column means
> kilograms — and the caller records that assumption.

**⚖ Apply totals to items** button → `POST /shipment-totals` (§8.2). Until pressed, the totals shown
are the documents' own. After pressing, `shipment_authority_type` becomes `REVIEWER_OVERRIDE` and
those figures are the final authority for every later recompute.

> **Unit trap to replicate:** the Step-2 form's figure is typed in the *shipment's* unit, but the
> pin-conflict banner's figure is always kilograms. The banner therefore posts `weight_unit: "KGM"`
> explicitly. Posting a kg figure under a shipment unit of `LB` stores a *smaller* authority than
> intended and the block never clears; under `T` it stores 1000× and reaches Field 35.

---

### 6.3 Section 3 — Manifest & transport references

| # | Label | Control | Seeded | Rules | Feeds |
| --- | --- | --- | --- | --- | --- |
| 3.1 | **Manifest number** | text | always `""` | blank → warning `MANIFEST_MISSING` (never a blocker; customs assigns it) | `Identification/Manifest_reference_number` |
| 3.2 | **Field 18 — transport identity (arrival)** | text | `""` | blank → warning `FIELD_18_EMPTY` | `Departure_arrival_information/Identity` |
| 3.3 | **Field 21 — border transport identity** | text | `""` | blank → warning `FIELD_21_EMPTY` | `Border_information/Identity` |
| 3.4 | **Mode at border (Box 25)** | picker over modes 01–09 | **`""` deliberately — NO default** | unselected → warning at review, **blocking `TRANSPORT_MODE_REQUIRED`** at finalize | `Border_information/Mode` |
| 3.5 | **Inland mode (Box 26)** | picker over modes 01–09 | **`""` deliberately** | same | `Inland_mode_of_transport` |
| 3.6 | **Incoterm** | `<select>` Incoterms 2020 + passthrough option for an extracted legacy value | `inv.incoterm` | free; `CIF`/`CIP` + unticked 8.3 → warning `DOUBLE_COUNT_RISK` | `Delivery_terms/Code` and per-item `IncoTerms/Code`; **empty falls back to `"C&F"`** |
| 3.7 | **Delivery place** | text | `inv.incoterm_place` → `settings.place_of_loading_name` (`KATHMANDU`) | — | `Delivery_terms/Place`, per-item `IncoTerms/Place` |
| 3.8 | **Field 40 — previous document** | text + candidate chips + **confirmation checkbox** | derived (§7.3) | any transport reference present + unconfirmed → **blocking `FIELD_40_UNCONFIRMED`**; empty on an air shipment → blocking `FIELD_40_MISSING`; empty on a B/L shipment → warning only | `Previous_doc/Summary_declaration` on **every item** |

**Transport modes (`transport_modes.csv`):**
`01 By Air · 02 By Truck · 03 By Train · 04 By Containerised Truck · 05 By Cart · 06 By Riksaw ·
07 By Animal Cart · 08 By Tractor · 09 Other`

**Mode suggestion chips** (suggestions only — one click fills the field, nothing is auto-applied):
* B/L shipment and Box 25 empty → offer `02 — By Truck` (Nepal is landlocked, so sea cargo crosses
  the border by road).
* AWB present and Box 25 empty → offer `01 — By Air`.

**Field 40 confirmation is cleared on every recompute — by design.** The reviewer is being
re-asked, not nagged: Field 40 is stamped on every item, so it must be a deliberate choice each
time the underlying numbers could have moved.

---

### 6.4 Section 4 — Authoritative invoices & Field 9

**Invoice roster** (read-only table): `number · date · currency · item_count · source_file · pages`.
It locks the item count, item order and invoice totals. If something here is wrong, the fix is
"correct the document and re-extract", never "type over it below".

Warnings raised here:
* `INVOICE_CURRENCY_MISSING` — no currency found.
* `INVOICE_REF_DUPLICATE` — two refs share a number but print different dates. (This is *not* the
  duplicate-upload guard: identical `(number, date)` pairs are merged upstream. Uploading the same
  invoice twice raises `INVOICE_DUPLICATE_DOCUMENT`, which is a hard blocker that warn mode may
  never bypass — every goods row would be declared twice and the duty overstated 2×.)

**Field 9 box** — a `<textarea>` with two modes:

| Mode | Behaviour |
| --- | --- |
| **auto-composed** (default) | the box shows a **live client-side recomposition** from the current form values, so it tracks MAWB/HAWB/B/L/bank edits as they are typed. Chip: "auto-composed". |
| **manually edited** | the moment the reviewer types, `field_9_override = true` and the text is used **verbatim** in the XML. Chip: "manually edited", plus a **↺ Reset to auto-composed** button. |

Implementation requirements:
* Your client-side preview must be a **faithful mirror** of the server's `compose_field9` (§7.2),
  including the B/L branch and the same lenient date normalization — otherwise the reviewer approves
  one string and the XML carries another.
* On submit, the server strips XML-illegal C0 control characters from the override text. If it is
  empty **after** stripping, the override degrades to recomposition — an accidental blank must never
  silently drop the MAWB/HAWB/invoice/LC references.
* A manual override **survives a recompute** (it is reviewer intent, not a computed default).

---

### 6.5 Section 5 — Exporter & importer

Render as **two separate panels**, stacked or clearly delineated — never as one eight-field grid.
Side by side, "Exporter address" and "Importer address" sit at the same height and are told apart
only by the first word of the label, which is exactly how a consignee ends up in a consignor box.

| Field | Control | Rules | Feeds |
| --- | --- | --- | --- |
| Exporter name | textarea | | `Exporter_name` — **name and address are joined as `name\naddress`** |
| Exporter address | textarea | | (same element) |
| Exporter EXIM / code | text | | `Exporter_code` |
| Exporter country (alpha-2) | text + state chip | must be valid alpha-2 → blocking `EXPORTER_COUNTRY_INVALID`; unresolved → warning `EXPORTER_COUNTRY_UNRESOLVED` | `Export_country_code`, `Trading_country`, `Country_first_destination`; **the controlled COO fallback for every item without its own country of origin** |
| Importer name | textarea | | `Consignee_name` (name + `\n` + address) |
| Importer address | textarea | | (same element) |
| Importer EXIM | text + valid/invalid chip | `^[A-Za-z0-9]{13,15}$` full match → otherwise **blocking `IMPORTER_EXIM_INVALID`** | `Consignee_code` |
| Importer country (alpha-2) | text + state chip | valid alpha-2 → else blocking `IMPORTER_COUNTRY_INVALID` | `Consignee_country_code` |

State chips render `country_resolution_state` (`RESOLVED` / `INVALID` / `ABSENT`) and
`importer_exim_valid`. Show the verdict **on the field it judges**, before finalize does.

---

### 6.6 Section 6 — Bank, payment terms & banking reference

| # | Label | Control | Seeded | Rules | Feeds |
| --- | --- | --- | --- | --- | --- |
| 6.1 | **Bank code** | combobox searching **code, name or SWIFT**; picking a row fills all three as an exact pair | `banking.bank_code` | code+name must be **both blank or an exact reference pair** → `BANK_PAIR_INCOMPLETE` / `BANK_PAIR_MISMATCH` (blocking) | `Financial/Bank/Code` |
| 6.2 | **Bank name** | text | `banking.bank_name` | (see 6.1) | `Financial/Bank/Name` |
| 6.3 | **SWIFT** | text | `banking.swift_raw`, upper-cased | identification only — **not** serialized into the XML | — |
| 6.4 | **Payment-term code** | `<select>` over the terms table + passthrough option for an extracted-but-unknown value | `banking.terms_code` | must exist in the table → blocking `PAYMENT_TERM_INVALID`; blank → warning `PAYMENT_TERMS_UNRESOLVED` | `Financial/Terms/Code` + `Description`; **and the prefix Field 9 uses** |
| 6.5 | **Mode of payment** | text | `"CASH"` | free text; `""` → `"CASH"` | `Financial/Mode_of_payment` |
| 6.6 | **Extracted terms wording** | **read-only** text | `banking.payment_terms_raw` or invoice's | — | evidence only |
| 6.7 | **Reference no. (LC / TT / …)** | text | `banking.reference_number` → invoice's LC reference | reference present but amount or date missing → warning `BANK_REFERENCE_INCOMPLETE` | Field 9 last line, first-item texts |
| 6.8 | **Amount** | text | `banking.amount` `%.2f` | | first-item texts |
| 6.9 | **Currency** | text | `banking.currency`, upper-cased | | first-item text currency symbol |
| 6.10 | **Date** | text `DD-MMM-YYYY` | `banking.value_date_raw` normalized | | Field 9 (`DD-MMM-YYYY`) and first-item `Previous_document_reference` (**`DD/MM/YYYY`**) |

**Bank resolution states** (chip on 6.1): `RESOLVED` / `AMBIGUOUS` / `ABSENT`.
**Payment resolution states**: `RESOLVED` / `REVIEW_REQUIRED` / `ABSENT`.

BIC matching uses the 8-character base. Unknown payment terms return `REVIEW_REQUIRED` and **never**
silently default to L/C — unless the deployment flag `default_unknown_payment_terms_to_lc` is on,
in which case they become `200` **with an explicit warning `PAYMENT_TERMS_DEFAULTED`** recorded in
the audit trail (this is ADR-006).

**Payment code → Field 9 prefix map** (this is the *only* place this mapping lives):

```
200 → "LC NO"     400 → "TT REF"    700 → "CAD REF"
906 → "DA REF"    600 → "DAP REF"   999 → "FOC"
anything else → "BANK REF"
```

Common codes from `terms_of_payments_and_codes.csv`: `000 None`, `100 Under Credit`,
`200 Under Letter of Credit (L/C)`, `300 Barter`, `400 Bank Draft T.T. System`,
`500 Credit Card`, `600 Document Against Payment (DAP)`, `700 Adv. Payment Cash Against Docs (CAD)`,
`906 Draft Against Acceptance`, `999 No CSF` — plus the deposit/guarantee variants (`202`, `204`,
`402`, `404`, …). Ship the whole table; do not hand-maintain a subset.

---

### 6.7 Section 7 — Freight & insurance

| # | Label | Control | Seeded | Rules | Feeds |
| --- | --- | --- | --- | --- | --- |
| 7.1 | **Document freight candidates** | clickable chips | `freight_candidates[]` | a chip with `comparable == false` is **disabled and marked ⚠** | fills 7.2 on click |
| 7.2 | **Freight amount** (labelled with the **invoice** currency) | text | highest comparable candidate | `""` → fall back to the deterministic rule; **explicit `0.00` is a legitimate answer and wins** | `Gs_external_freight` (foreign + national); allocated across items exactly |
| 7.3 | **Insurance premium amount** (labelled with the **national** currency) | text | `""` | non-negative | `Gs_insurance`; added to `Total_cost` **as-is** |
| 7.4 | **Invoice values exclude freight & insurance** | checkbox | `false` | unticked + incoterm CIF/CIP + non-zero cost → warning `DOUBLE_COUNT_RISK` | audit + validator only |

**Freight candidate shape:**
```
{ source: "INVOICE" | "BANKING_SWIFT" | "AWB",
  amount: "%.2f", currency: "<as printed>",
  comparable: bool,        # currency == invoice currency
  detail: "<human explanation>" }
```

**Freight selection ladder (`select_freight`):**
1. **Reviewer's manual amount wins** — including an explicit `0.00`. Source `MANUAL_OVERRIDE`.
2. Otherwise: the **highest** candidate *among those denominated in the invoice currency*
   (rule: highest, to avoid undervaluation).
3. A candidate in another currency is **never auto-selected**. It is reported with both currencies
   named (`FREIGHT_CURRENCY_MISMATCH`) for the reviewer to convert and type in.
   *Why:* `max()` over mixed currencies hands the winner to valuation, which multiplies by the
   invoice currency's rate — turning EUR 4,708 into USD 4,708 silently, and biasing the choice
   toward whichever unit is stronger.
4. Nothing usable → `0.00`, source `MISSING_ZERO`, warning `FREIGHT_MISSING`.

**The AWB's own contribution** is its bottom-line `Total Prepaid` / `Total Collect` box — weight
charge **plus** all other charges — then scaled to this consignment by gross weight. Ladder:
`Total Prepaid` → `Total Collect` → `weight + valuation + tax + other` → the extractor's raw
`freight_amount`. (Real failure this fixes: EUR 4,653.00 weight charge taken instead of EUR 4,708.00
total prepaid = 4,653.00 + 55.00 AWC.)

**Insurance is the one asymmetric field.** Freight is converted at the exchange rate; **insurance is
not** — `total_cost = external_freight_national + insurance_national`. The input therefore must be
labelled with the national currency (`insurance_currency`, default `NPR`) that the server consumes.
An unlabelled box directly beneath a freight box labelled in USD is how a reviewer types the premium
in USD and understates the customs value by the whole exchange rate.

**Exchange rate is deliberately not on this form.** The job carries the configured rate so valuation
computes normally, but ASYCUDA auto-fills the NRB rate when the XML is uploaded. If the rate is
overridden in the finalize body it is recorded in the audit trail as an override.

---

### 6.8 Evidence panel (collapsed `<details>`)

`Field · Value · Source (role · file) · Page`, built only from **stored raw extractions** — never a
re-OCR. Fields harvested:

* **INVOICE**: invoice number, invoice date, currency, incoterm, payment terms, exporter EXIM,
  importer EXIM, B/L number, B/L date, plus each bundled sub-invoice's number and date with its own
  first page.
* **AIR_WAYBILL**: per logical form — HAWB, MAWB, B/L number/date, `gross_weight[form_id]` with the
  quote it was read from.
* **BANKING**: SWIFT/BIC, bank name, reference number, value date, amount + currency, payment terms.

---

## 7. Derived-value composition rules (exact algorithms)

### 7.1 `transport_doc_type(mawb, hawb, bl, authority_type) -> "AWB" | "BL"`

```
if authority_type == "BILL_OF_LADING":                        return "BL"
if bl.strip() and not (mawb.strip() or hawb.strip()):         return "BL"
                                                              return "AWB"
```

Meaning: a bill of lading uploaded **instead of** an air waybill / delivery order makes this a B/L
shipment. An air shipment whose invoice happens to quote a B/L number stays an AWB shipment — the
B/L line is still printed, just underneath. The reviewer can override the choice; on merge, an
explicit `AWB`/`BL` wins, otherwise the rule re-runs against the reviewer's **edited** numbers (so
typing a B/L number over an empty AWB pair gets the B/L Field 9 without saying so twice).

### 7.2 `compose_field9(...) -> str` — `Traders/Financial/Financial_name`

```
lines = []
kind  = explicit doc_type  or  transport_doc_type(...)

bl_line = ""
if bill_of_lading:
    bl_line = "B/L NO:{bl}"
    if bl_date: bl_line += " B/L DATE:{DD-MMM-YYYY}"

if kind == "BL" and (bl_line or not (mawb or hawb)):
    if bl_line: lines.append(bl_line)            # the B/L REPLACES the air line
else:
    if mawb or hawb: lines.append("MAWB NO:{mawb} HAWB:{hawb}".strip())
    if bl_line:      lines.append(bl_line)       # printed underneath

for (number, date) in invoice_refs:
    lines.append("INVOICE NO:{number} DT: {DD-MMM-YYYY}")

if bank_reference:
    lines.append("{PREFIX}:{bank_reference}, DT:{DD-MMM-YYYY}")

return "\n".join(lines)
```

Note the exact spacing: `"INVOICE NO:… DT: …"` has a space after `DT:`; the bank line
`"…, DT:…"` does not. Reproduce it verbatim.

Example (air):
```
MAWB NO:160-08566795 HAWB:KCNKTM0057
INVOICE NO:2026-209-1 DT: 24-FEB-2026
LC NO:0018283210508FU, DT:24-FEB-2026
```
Example (sea/land): first line becomes `B/L NO:HLCUBOM240012345 B/L DATE:24-FEB-2026`.

Safety valve: a `"BL"` choice with no B/L number but an AWB number present falls through to the air
line rather than emitting no transport reference at all.

### 7.3 Field 40 default (`derive_field40`)

```
no AWB forms and no HAWB/MAWB number:
    B/L present   -> B/L number, "Bill of Lading shipment — the B/L number is used."
    otherwise     -> "",         "No transport document reference found — Field 40 left empty."

both HAWB and MAWB present:
    compare the FORMS' gross weights (never the scored HAWB/MAWB labels):
        equal      -> MAWB number   ("same gross weight — MAWB number used")
        different  -> HAWB number   (lower gross = house consignment)
        incomparable -> HAWB number

single air waybill -> that number ("Single air waybill — its (master) number used")
```

Comparing the *forms' weights* rather than the classifier's labels is deliberate: a mis-classified
document can invert HAWB/MAWB, and this is a real Field 40 failure that was corrected on 2026-07-18.

Candidates offered to the reviewer: every non-empty one of `HAWB`, `MAWB`, `B/L`, with
`suggested: true` on the derived default. **The reviewer must always confirm** — the system
suggests, the user decides, and their choice is final.

### 7.4 First-item banking texts (`compose_first_item_texts`)

Populated on **item 1 only**; every other item emits the elements empty.

```
if not bank_reference and bank_amount is None:  return ("", "")

word = first word of the payment prefix        # LC / TT / CAD / DA / DAP / FOC / BANK
sym  = "$" if currency in (USD, US$, $) else "{CURRENCY} "
amt  = "%.2f" % bank_amount   (or "")

Previous_document_reference = "DATE:{DD/MM/YYYY},({sym}{amt} OF {word})"      # or "({word})" with no amount
Free_text_1                 = "{PREFIX}:{bank_reference},{sym}{amt}"          # trailing comma stripped
```

Example: `DATE:24/02/2026,($7023.17 OF LC)` and `LC NO:0018283210508FU,$7023.17`.

Note the **two different date formats**: Field 9 uses `DD-MMM-YYYY`, this one uses `DD/MM/YYYY`.

### 7.5 Date normalization

`fmt_dd_mmm_yyyy` accepts `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`, `YYYYMMDD`, `YYMMDD` (SWIFT tag
style), `24 FEB 2026`, `FEB 24, 2026`, and composite SWIFT printouts like `260405 2026 Apr 05`
(the leading tag is parsed; if the trailing text also parses it must agree). Day-first is the
convention for ambiguous `DD/MM` vs `MM/DD`.

**An unrecognised string is returned unchanged, never guessed.** Provenance survives review.

### 7.6 Numbers

All money/weight/quantity arithmetic is `Decimal`, never float. `parse_decimal` is the single
conversion boundary between untrusted text and authoritative numbers (it handles locale separators
per page). Quantizers: `q2` (2dp), `q4` (4dp). Apportionment uses **largest-remainder** so item sums
equal the authority exactly.

---

## 8. Durable side-channels (persisted outside the finalize body)

### 8.1 `POST /api/jobs/{id}/regime`

Body: any subset of
```
declaration_type, gen_procedure_code, customs_office_code, border_office_code,
extended_customs_procedure, national_customs_procedure, border_mode,
inland_mode_of_transport, border_nationality, place_of_loading_code,
location_of_goods, container_flag
```
* An unknown key → 422 `REGIME_FIELD_UNKNOWN`.
* `null` for a key **reverts that field to the deployment default**.
* Each code is validated against its reference table at write time → 422 `REGIME_VALUE_INVALID`.
* Posting either half of Box 1 re-validates the **pair** against the 17 model lines.
* `container_flag` must be a real boolean.
* If nothing actually changed: returns `{status:"ok", changed:{}}` and does **not** bump the
  revision (no audit noise, no stale fingerprint).
* If something changed: bumps `review_selections.revision`, invalidates declaration + XML +
  brand/model/size artifacts, writes audit `REGIME_SELECTED` with `{field:{from,to}}`, recomputes
  the review, returns it.

Stored as `job.review_selections = {revision, values}`. Every future review build seeds from it
before falling back to deployment settings.

### 8.2 `POST /api/jobs/{id}/shipment-totals`

Body: `{gross_weight, weight_unit, total_packages}`.

Validation (all 422 on failure):
* `weight_unit` must be in `WEIGHT_UNIT_TO_KG` → `WEIGHT_UNIT_INVALID`.
* Both numbers finite and `> 0` → `SHIPMENT_TOTALS_INVALID`.
* `total_packages % 0.01 == 0` → else `TOTAL_PACKAGES_OFF_LATTICE`.
  *Rejecting beats quantizing:* item cartons are whole multiples of 0.01, so a total off the lattice
  could never be matched by their sum, and the reviewer would be reconciling against a number they
  never typed.

Effect: stores `shipment_override` in the overlay, bumps the revision, invalidates derived
artifacts. On every later recompute `apply_shipment_override` rewrites the resolved authority:
`selected_authority_type = "REVIEWER_OVERRIDE"`, both value authorities get confidence 100 and the
reason "reviewer-corrected totals are the final shipment authority", and a
`SHIPMENT_TOTALS_REVIEWED` warning is surfaced so the change stays visible.

---

## 9. The Detailed Review item table (`item_details`)

Ships **inside** the Critical Review payload. It is an item-level *preview* of what finalize will
produce: allocation and supplementary units are computed on deep **copies** of the resolved items
using the review-default totals, so the context is never mutated.

> **A blank cell must always come with the reason it is blank.** Every message the preview produces
> — shipment-scope and item-scope — is appended to `review.warnings`. Item messages are grouped: one
> message per distinct code naming up to 20 affected SNs, so a 200-row invoice does not emit 200
> identical lines.

### 9.1 `ItemDetailRow`

| Field | Meaning |
| --- | --- |
| `sn` | invoice order = XML sequence. **Item order is invoice order — never resorted by packing, HS, value or weight.** |
| `item_id` | immutable, server-controlled. `src:<sha1 of extraction lineage>` or `man:<uuid4>` |
| `origin` | `"source"` \| `"manual"` |
| `edited` | reviewer edited this row's invoice fields |
| `hs_source` | how the final HS was resolved |
| `hs_confidence` | `"1.00"` = exact/reviewed; lower = auto guess |
| `hs_low_confidence` | resolved but `< 1.0` → render a **LOW** chip |
| `hs_explicit` | the reviewer chose or confirmed it |
| `description`, `coo`, `invoice_hs`, `final_hs` | `final_hs` empty = manual review needed |
| `quantity`, `uom`, `total_price` | |
| `brand`, `model`, `size` | export-only, default `"NA"` |
| `gross`, `net`, `ctn` | allocation preview; blank when no gross authority exists |
| `gross_pinned`, `net_pinned`, `ctn_pinned` | reviewer pinned that value → render 📌 |
| `sup_unit`, `sup_name`, `sup_qty`, `sup_qty_pinned` | supplementary unit follows the HS's tariff unit |
| `src_file`, `src_page` | evidence deep-link; blank for manual rows |

Row markers to implement: orange row = no official HS; blue left edge = manually added; italic
description = reviewer-edited; 📌 = pinned; **LOW** chip = low-confidence HS; a bold TOTAL line
under the table.

### 9.2 The mutation overlay

Extraction evidence is **immutable**. All reviewer item changes live in a job-level JSON overlay
re-applied deterministically on every recompute:

```
{ schema, revision, ordered_item_ids[], manual_items[], tombstones[],
  hs_selections{}, shipment_override, field_edits{}, coo_all, bms_edits{}, reset_notice }
```

Apply order (pure, network-free): **filter tombstoned → materialize active manual rows →
apply `coo_all` → apply per-item `field_edits` → order by `ordered_item_ids` → re-sequence 1..N**,
then recompute `goods_total` and each invoice ref's `item_count`.

Invariants enforced at mutation time (409 on failure):
* active ids unique → `ITEM_ID_DUPLICATE`
* `ordered_item_ids` is an **exact, duplicate-free permutation** of the active ids →
  `ITEM_ORDER_INVALID`

The read path is lenient (a review must always stay computable); the strict checks run at write time.

`field_edits` are applied **before** HS/COO resolution and allocation, so every derived value
recomputes from the edited content.

### 9.3 Add item — `POST /items`

Body: `{insertion_sn, invoice_id, manual_review_addition, reason, item:{description, quantity, uom, total_price, country_of_origin, final_hs_code}}` with `extra="forbid"`.

* `1 <= insertion_sn <= count + 1` → else 422 `INSERTION_SN_OUT_OF_RANGE`.
* `invoice_id` must be an authoritative invoice number → 422 `INVOICE_UNKNOWN`.
* No invoice **and** `manual_review_addition != true` → 422 `MANUAL_CONFIRMATION_REQUIRED`.
* Client-supplied `item_id` and any derived field are **rejected** — identity, allocation,
  supplementary units and XML values are server-owned.
* Seed caps: description 400 chars, uom 20 (default `PCS`), COO 80, HS 20.
* Incomplete manual rows (no description, quantity ≤ 0) are **saved** with warning
  `MANUAL_ITEM_INCOMPLETE`, but the same condition is a **blocker** at finalize.

### 9.4 Delete item — `DELETE /items/{item_id}`

Body: `{confirmation_sn, reason}`.
* Reviewer must retype the row's **current** SN. Mismatch → 409 `CONFIRMATION_SN_MISMATCH`
  ("the row order changed; re-confirm against the refreshed review").
* Unknown/already-deleted id → 404 `ITEM_NOT_FOUND`.
* The last remaining goods row cannot be deleted → 409 `LAST_ITEM_UNDELETABLE`.
* Source rows get a **tombstone** (evidence kept); manual rows are marked `active:false`.
  **A deleted identity is never reactivated** — a replacement gets a new UUID.
* The removed id is purged from `hs_selections`, `field_edits` and `bms_edits`.

### 9.5 Edit item fields — `PATCH /items/{item_id}`

Editable: `description, quantity, uom, total_price, country_of_origin, gross_weight, net_weight,
carton_count, supplementary_quantity`. Anything else → 422 `ITEM_FIELD_NOT_EDITABLE`.
(HS goes through the HS channel; the supplementary unit **code** is server-derived from the HS's
tariff unit and is never typed.)

| Field | Rule | Error code |
| --- | --- | --- |
| `description` | non-empty after trim, ≤ 400 | `DESCRIPTION_REQUIRED` |
| `quantity` | finite, `> 0` | `QUANTITY_INVALID` |
| `total_price` | finite, `>= 0`, stored at 2dp | `TOTAL_PRICE_INVALID` |
| `uom` | upper-cased, ≤ 20, `""` → `PCS` | — |
| `country_of_origin` | ≤ 80 | — |
| `gross_weight`, `net_weight` | finite, `> 0`, kg, stored at 4dp | `GROSS_WEIGHT_INVALID` / `NET_WEIGHT_INVALID` |
| net vs gross | **net must be strictly below gross across the MERGED edit** (an earlier pinned value counts) | `NET_NOT_BELOW_GROSS` |
| `carton_count` | finite, `>= 0.01`, **and an exact multiple of 0.01** | `CARTON_COUNT_INVALID` / `CARTON_COUNT_OFF_LATTICE` |
| `supplementary_quantity` | finite, `> 0`, stored at 4dp | `SUPPLEMENTARY_QUANTITY_INVALID` |

**Empty string clears.** `gross_weight`, `net_weight`, `carton_count` and
`supplementary_quantity` all accept `""` to drop the override and let the computed value return.
Without this, "pin" is a one-way door.

**Pin semantics differ between the two groups — this distinction is load-bearing:**

* **Pins** (`gross_weight`, `net_weight`, `carton_count`): the value is kept **exact** and the
  allocation redistributes the remaining authorised total across the unpinned items. A carton pin
  takes the difference from the **estimated** rows first (up to ten of them, so the rest of the table
  stays where the reviewer last read it); rows whose cartons the packing list actually prints are
  only touched if the estimates cannot cover it, and then they are rescaled **together** so their
  ratios to one another survive — and the reviewer is told.
* **Override** (`supplementary_quantity`): a plain replacement. There is no shipment authority above
  it, so nothing is redistributed and no derivation rule changes — the entered number goes straight
  to the XML. It is deliberately **not** a member of `PIN_ITEM_FIELDS`, because that tuple also
  decides what a packing/AWB re-extraction discards, and this override is not reconciled against the
  authority those documents set.

Guidance to surface in the UI: pin only what a document actually states. Pinning everything leaves
the allocation nothing to balance with — and if the pinned values simply do not fit, the **totals**
are what gives way: the screen offers to make them the shipment authority (§8.2).

### 9.6 HS selection — `POST /items/hs-review` and `/items/hs-review-range`

Shared write-time gate:
1. `hs_review_source` must be in the allowlist → 422 `HS_REVIEW_SOURCE_INVALID`.
2. The code must normalize to **exactly 11 digits** — leading zeros preserved, **no zfill, no prefix
   completion** → 422 `HS_NOT_11_DIGITS`.
3. It must exist **verbatim** in the official HS database → 422 `HS_NOT_IN_DATABASE`.

Range form: printer-style SN spec — `"1-15, 19, 80"` or `"all"`.
* empty → 422 `SN_RANGE_EMPTY`
* malformed token → 422 `SN_RANGE_INVALID`
* reversed range → 422 `SN_RANGE_INVALID` ("write it low-high")
* out of `1..max_sn` → 422 `SN_RANGE_OUT_OF_BOUNDS`
* an SN that vanished since the client read the table → 409 `SN_RANGE_STALE`

Each selection stores `{final_hs_code, hs_review_source, explicit:true, selected_at,
description_key}`. The `description_key` (normalized item name) is what lets the choice be folded
into content-keyed HS history when the evidence is later superseded (§9.9).

A later per-row pick still overrides one row of a range apply.

**LLM-supplied HS is rejected outright** upstream (8-digit hints only, then completed from the
official DB and split into `Commodity_code`(8) + `Precision_1`(3)). An HS the machine guessed from
the description (`hs_source == "SEMANTIC_DESCRIPTION"`, confidence 0.3) is **blocking** at finalize
until a human confirms it (`HS_GUESS_UNCONFIRMED`) — HS sets the duty rate, so it can never look
identical to an invoice-printed exact match by the time it reaches XML.

### 9.7 Apply COO to all — `POST /items/coo-all`

Body `{country_of_origin}`. Must resolve to a canonical Alpha-2 via the country reference → else
422 `COO_INVALID`. Stamps `coo_all` on every row and **clears any prior per-item COO edit** so the
value truly appears everywhere; a later per-item edit still overrides one row. Stored evidence is
untouched.

### 9.8 Brand / Model / Size — `POST /items/brand-model-size`

Export-only columns of the sibling `.xls`. **The one channel that does not invalidate anything.**

* Accepts one cell, a pasted Excel block, or a filled range in a single call.
* Max 5000 rows per request → 422 `BMS_EDIT_TOO_LARGE`; each value ≤ 120 chars.
* Empty value clears that cell's override so the deterministic value returns.
* **`revision` is deliberately NOT bumped** — a correction here must not change the review
  fingerprint, stale the declaration, or invalidate a generated XML.
* Only the `.xls` is rebuilt; the stored declaration's export columns are kept in step by sequence.

Grid UX implemented: Tab/Enter/arrows to move, F2 to edit in place, Ctrl+D to copy the cell above,
Delete to clear (falls back to the automatic value), paste-from-Excel with out-of-grid cells ignored
**and counted in the report**, column+range Fill, staged-until-Save with a Revert, Copy-all-as-TSV.

### 9.9 Evidence-change reset (a rule you must not skip)

After **any** document (re)extraction, role decision or removal, reviewer overlay state derived from
the changed evidence is discarded. Prior reviewer values — COO especially — are never blindly
re-applied to evidence they were not made against. The overlay's item channels are keyed by
positional `src:` ids, which can silently re-bind to different physical rows once the item list
changes.

Role-scoped:

| Changed role | Discarded |
| --- | --- |
| `INVOICE` (feeds the item list) | all per-item `field_edits`, `coo_all`, `tombstones`, `ordered_item_ids`, `bms_edits`; active manual rows deactivated (kept for audit) |
| `AIR_WAYBILL` / `PACKING_LIST` (feed the shipment authority) | `shipment_override` **and** the per-item weight/carton **pins only**. Description/COO/quantity/price edits survive — the invoice did not change. |
| `BANKING` | nothing (the reviewed freight travels in the finalize body, never the overlay) |

Two sanctioned survivors:
1. **Regime/office selections** live outside this overlay entirely.
2. **Explicit HS selections** are *folded* into content-keyed history (normalized item name → code)
   and re-proposed at HISTORY confidence for re-confirmation — never re-applied by position.

A `reset_notice` records the revision it was written at, so the reviewer's next mutation retires it
automatically without anyone clearing it.

---

## 10. `packing_view` — the derived packing-authority panel

`null` when no packing list was uploaded. Otherwise a JSON view assembled from the stored raw
packing extraction + the resolved shipment authority + the invoice-item match.

Two rules this object exists to keep:

1. **It reports the match the ALLOCATOR made — it does not compute its own.** An independent
   re-implementation of matching means the two can disagree, and the one on screen is not the one in
   the XML.
2. **A weight is shown in kilograms only after being converted to kilograms.** Printing `value_raw`
   into a `*_kg` field displays a gram row as if it were kilograms while the allocator (which does
   convert) uses a value 1000× smaller.

Contents: `document_confidence` (`0.50` over-budget / `0.80` partial / `0.95` normal),
`source_files`, `packing_header`, `shipment_totals`, `transport_cross_reference`, per-row `items[]`
(with match confidence/method, normalized UOM and package codes, dimensions, batch/lot/serial,
carton groups, source trace), `allocation_rules_applied` (a human-readable statement of the rules in
force), and `validation` (row counts, sum-vs-printed-total checks with a 2% / 0.05 tolerance,
`net < gross`, `ready_for_invoice_merge`, `blocking_errors[]`, `warnings[]`).

Key rules stated there: **packing match is by product identity, never row number**
(`exact_name > product_code > scored_description_similarity`); duplicate packing rows are summed
before matching; shared cartons are divided without duplication; minimum package per item is `0.01`;
invoice order is preserved.

---

## 11. Message catalogue

### 11.1 Warnings raised while building the review

| Code | Trigger |
| --- | --- |
| `TRANSPORT_MODE_UNSELECTED` | Box 25 and/or Box 26 not yet chosen |
| `GROSS_UNKNOWN` / `PACKAGES_UNKNOWN` | no authority for that value |
| `TRANSPORT_DOC_MISSING` | no AWB/BL uploaded — enter gross, cartons and the transport number manually |
| `PACKING_DOC_MISSING` | no packing list — item weights split by quantity share, cartons by weight share |
| `PACKING_TIMEOUT_FALLBACK` | packing extraction exceeded its budget — full quantity-share fallback |
| `PACKING_PARTIAL_FALLBACK` | budget hit part-way — extracted rows ARE used, the rest take quantity share and are named |
| `BANKING_DOC_MISSING` | no banking document — bank/payment fields are manual |
| `WEIGHT_UNIT_MISSING` | authority carries no recognizable weight unit |
| `IMPORTER_EXIM_INVALID` | EXIM not 13–15 alphanumeric |
| `EXPORTER_COUNTRY_UNRESOLVED` | exporter country not in the country reference |
| `INVOICE_CURRENCY_MISSING` | no invoice currency |
| `INVOICE_REF_DUPLICATE` | same invoice number, different dates |
| `BL_FROM_INVOICE` | no B/L uploaded but the invoice states one — verify against the carrier's B/L |
| `BL_NUMBER_MISSING` | B/L shipment with no readable B/L number |
| `FIELD_40_EMPTY` | Field 40 not derivable from the air waybills |
| `BANK_REFERENCE_INCOMPLETE` | reference present, amount or date missing |
| `FREIGHT_MISSING` | no freight evidence (`0.00` must be explicit) |
| `FREIGHT_CURRENCY_MISMATCH` | a candidate is not in the invoice currency |
| `PAYMENT_TERMS_DEFAULTED` | unknown terms defaulted to L/C by config flag |
| `SHIPMENT_TOTALS_REVIEWED` | reviewer totals are now the final authority |
| `MANUAL_ITEM_INCOMPLETE` | manually added row missing description or quantity |
| `MANIFEST_MISSING`, `FIELD_18_EMPTY`, `FIELD_21_EMPTY`, `PAYMENT_TERMS_UNRESOLVED`, `BANK_UNRESOLVED`, `DOUBLE_COUNT_RISK` | raised by the validator |

### 11.2 Blocking errors attributable to Critical Review fields

| Code | Trigger |
| --- | --- |
| `IMPORTER_EXIM_INVALID` | EXIM not 13–15 alphanumeric |
| `PACKAGE_TYPE_UNSUPPORTED` | package code not one of the 7 |
| `DECLARATION_MODEL_INVALID` | Box 1 type+code not one of the 17 lines |
| `CUSTOMS_OFFICE_INVALID` | clearance or border office not in the reference |
| `PROCEDURE_INVALID` | Box 37 extended not in ANNEX 1, or national not in ANNEX 3 |
| `PROCEDURE_TYPE_MISMATCH` | extended procedure's first digit ≠ Box-1 general-procedure digit |
| `TRANSPORT_MODE_REQUIRED` | Box 25 and/or 26 unselected |
| `TRANSPORT_MODE_INVALID` | mode not in 01–09 |
| `FIELD_40_MISSING` | empty on an air shipment (`HAWB`/`TRUE_DO`/`TRACKING`/`SINGLE_AWB`) |
| `FIELD_40_UNCONFIRMED` | a transport reference exists but the reviewer did not tick confirm |
| `IMPORTER_COUNTRY_INVALID` / `EXPORTER_COUNTRY_INVALID` | not a valid alpha-2 |
| `BANK_PAIR_INCOMPLETE` / `BANK_PAIR_MISMATCH` | bank code+name not both blank / not an exact reference pair |
| `PAYMENT_TERM_INVALID` | code not in the terms table |
| `WEIGHT_UNIT_UNCONFIRMED` | unit unresolved and the confirmation checkbox unticked |
| `SHIPMENT_GROSS_REQUIRED` / `SHIPMENT_PACKAGES_REQUIRED` | reviewed total is zero or absent |
| `TRANSPORT_REFERENCE_REQUIRED` | no MAWB, HAWB or B/L number at all |
| `HS_GUESS_UNCONFIRMED` | HS auto-selected from the description and never confirmed |
| `MANUAL_ITEM_INCOMPLETE` | manual row still missing description/quantity |
| `MIXED_INVOICE_CURRENCIES`, `INVOICE_DUPLICATE_DOCUMENT` | escalated from warnings |
| item gates | `HS_MANUAL_REVIEW`, `COO_UNRESOLVED`, `SUPPLEMENTARY_QTY_INVALID`, `WEIGHT_RECONCILIATION_IMPOSSIBLE`, `CARTON_RECONCILIATION_FAILED` |

### 11.3 Warn mode

Default is **warn mode** (`xml_strict_blocking = false`): blocking cases warn and the XML is **still
produced**, so the reviewer can test it in real ASYCUDA. The response carries
`xml_built_with_blockers: true` and the full `blocking_errors[]` for a pop-up. Job status becomes
`VALIDATION_BLOCKED` (honest), and the audit records `XML_BUILT_WITH_BLOCKERS` with the code list.

**`WARN_MODE_HARD_CODES` can never be bypassed** — the XML is not built at all:

```
GROSS_ALLOCATION_IMPOSSIBLE        REVIEWED_GROSS_EXCEEDS_AUTHORITY
REVIEWED_GROSS_TOTAL_MISMATCH      WEIGHT_RECONCILIATION_IMPOSSIBLE
INVOICE_DUPLICATE_DOCUMENT         REVIEWED_CTN_EXCEEDS_AUTHORITY
REVIEWED_CTN_TOTAL_MISMATCH        CARTON_LATTICE_VIOLATION
CARTON_RECONCILIATION_FAILED
```

Rationale: warn mode exists so a reviewer can test an *otherwise-complete* XML. With no item weights
there is nothing to test and the file asserts a zero gross weight per line. A duplicated invoice is
not incomplete but **wrong** — the value and duty are overstated 2× and the condition is invisible in
the item grid the reviewer checks.

---

## 12. Reviewer → XML field map (quick reference)

| Review field | XML element |
| --- | --- |
| `customs_office_code` / `_name` | `Identification/Office_segment/Customs_clearance_office_code`, `_name` |
| `declaration_type`, `gen_procedure_code` | `Identification/Type/Type_of_declaration`, `Declaration_gen_procedure_code` |
| (derived from `declaration_type`) | `Property/Sad_flow` — `E` for EX/PEX, else `I` |
| `manifest_no` | `Identification/Manifest_reference_number` |
| exporter name + address | `Traders/Exporter/Exporter_name` |
| `exporter_exim_code` | `Traders/Exporter/Exporter_code` |
| importer name + address | `Traders/Consignee/Consignee_name` |
| `importer_exim_code` | `Traders/Consignee/Consignee_code` |
| **Field 9** | `Traders/Financial/Financial_name` |
| `exporter_country_code` | `Country_first_destination`, `Trading_country`, `Export/Export_country_code` |
| `field_18_transport_identity` | `Transport/…/Departure_arrival_information/Identity` |
| `field_21_transport_identity` | `Transport/…/Border_information/Identity` |
| `border_nationality`, `border_mode` | `Border_information/Nationality`, `/Mode` |
| `inland_mode_of_transport` | `Transport/Inland_mode_of_transport` |
| `container_flag` | `Transport/Container_flag` |
| `incoterm`, `delivery_place` | `Delivery_terms/Code` (`C&F` fallback), `/Place` |
| `border_office_code` / `_name` | `Transport/Border_office/Code`, `/Name` |
| `place_of_loading_code` | `Transport/Place_of_loading/Code` (Name emitted **empty** on purpose) |
| `location_of_goods` | `Transport/Location_of_goods` |
| `bank_code`, `bank_name` | `Financial/Bank/Code`, `/Name` |
| `payment_term_code` | `Financial/Terms/Code` + `/Description` |
| `mode_of_payment` | `Financial/Mode_of_payment` |
| gross weight (kg) | `Valuation/Weight/Gross_weight`, `Total/Total_weight` |
| freight | `Gs_external_freight` (national + foreign) |
| insurance | `Gs_insurance` (national, un-converted) |
| `total_packages` | `Property/Nbers/Total_number_of_packages` |
| `package_type_code` / `_name` | per item `Packages/Kind_of_packages_code`, `_name` |
| Box 37 pair | per item `Tarification/Extended_customs_procedure`, `National_customs_procedure` |
| **Field 40** | per item `Previous_doc/Summary_declaration` |
| first-item texts | item 1 only: `Previous_doc/Previous_document_reference`, `Free_text_1` |

Also fixed in the composer: the **Declarant block is always emitted empty**; three
`Supplementary_unit` blocks with only the first populated; eight empty `Taxation_line` stubs; HS split
into `Commodity_code`(8) + `Precision_1`(3) with `Precision_2..4` as `<null/>`.

**XML safety:** strip the full illegal set before serializing — C0 controls except tab/LF/CR, lone
surrogates `U+D800–U+DFFF`, and `U+FFFE`/`U+FFFF`. Reviewer-pasted text from a PDF carries these, and
a warn-mode declaration must always serialize rather than 500.

---

## 13. Acceptance checklist

Behaviour your re-implementation must reproduce:

1. Recomputing the review never re-reads documents and never costs an API call.
2. `null` vs `""` in the confirmation payload behave differently (§5.1).
3. The fingerprint stales on evidence/item/regime change and **not** on form typing; a mismatch
   refuses finalize at 409 with the refreshed review attached.
4. Every reference-coded field is validated at write time *and* re-validated at finalize.
5. An unrecognised printed weight unit disqualifies its source; it is never assumed to be kg.
6. Explicit freight `0.00` is honoured; a freight candidate in another currency is never auto-picked.
7. Insurance is national currency and is **not** multiplied by the exchange rate.
8. Field 9's client preview is byte-identical to the server's recomposition; an override survives a
   recompute; a blank-after-cleaning override falls back to recomposition.
9. Field 40 confirmation resets on every recompute and blocks finalize when unticked.
10. Box 25/26 have **no default** and block when unselected.
11. Item order is invoice order; item gross/package sums reconcile **exactly** to the authorised
    totals (Decimal, largest-remainder); every item's net is strictly below its gross.
12. Cartons live on a 0.01 lattice — off-lattice values are rejected, never quantized.
13. Clearing a pin (empty string) restores the computed value.
14. Brand/Model/Size edits never bump the revision, never stale the XML.
15. Any re-extraction resets the role-scoped overlay state; HS selections fold into name-keyed
    history instead of re-binding by position.
16. Warn mode builds a labelled test XML except for the hard-code list.
17. Every reviewer entry that differs from a computed default lands in the audit trail as
    `{field: {from, to}}`.
