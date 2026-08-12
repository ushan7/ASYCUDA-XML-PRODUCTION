# Detailed Review — Complete Functional & Technical Specification

**Audience:** a developer re-implementing this screen in another language/stack.
**Scope:** the item-row table — every column, how each value is derived, how the reviewer changes it,
what validates the change, what it produces in the XML, and every message it can raise.

**Companion document:** `docs/critical-review-spec.md` (the declaration-level gate that precedes
this screen). **Authoritative rule source for weights and cartons:** `docs/allocation-spec.md` —
where this file summarises it, that file wins, and the two must change in the same commit.

Source of truth in the reference implementation:

| Concern | File |
| --- | --- |
| Row preview assembly (`item_details`) | `backend/app/pipeline.py` → `item_details_preview` |
| Overlay: add / delete / edit / pin / HS / COO / BMS | `backend/app/review/item_mutations.py` |
| Row model + response shape | `backend/app/review/critical_review.py` → `ItemDetailRow` |
| Resolution chain order | `backend/app/pipeline.py` → `resolve_context` |
| HS cascade + DB gate | `backend/app/rules/hs_resolver.py` |
| HS search | `backend/app/reference/store.py` → `search_hs` |
| Country of origin | `backend/app/rules/coo.py` |
| Packing-row → invoice-item matching | `backend/app/rules/packing_match.py` |
| Weight / carton allocation + reconciliation | `backend/app/rules/weight_carton.py` |
| Net weight from the description | `backend/app/rules/description_weight.py` |
| Description cleaning / vendor-mixed cells | `backend/app/rules/description_clean.py`, `field_allocation.py` |
| Supplementary units | `backend/app/rules/supplementary_unit.py` |
| Brand / Model / Size (export-only) | `backend/app/rules/brand_model_size.py`, `backend/app/xml/bms_export.py` |
| Item mutation routes | `backend/app/main.py` |
| Persistence, locking, invalidation, audit | `backend/app/services.py` |
| Item-level blocking gates | `backend/app/declaration/validator.py`, `backend/app/pipeline.py` → `finalize` |

---

## 0. The governing rules of this screen

1. **The extraction evidence is immutable.** Nothing the reviewer does on this screen edits an
   uploaded document, its OCR, or its stored raw extraction. Every change is a record in a job-level
   **overlay** that is re-applied deterministically on every recompute.
2. **The server is authoritative for every derived value.** The client sends *identity + intent*
   (`item_id`, a code, a number) and re-renders from the server's response. It never computes an HS
   unit, a supplementary quantity, an allocation share or a readiness verdict locally.
3. **Item order is invoice order.** Never sorted by packing list, alphabetically, by HS, by carton
   or by weight. `sn` is both the display order and the XML item sequence.
4. **A reviewer's typed value is never clamped, rescaled or rounded toward a derived figure.** The
   system adjusts the *other* items to hold the shipment authority.
5. **A blank cell always carries the reason it is blank.** Every message the allocation preview
   produces is forwarded to the review screen — shipment-scope verbatim, item-scope grouped by code
   with the affected SNs named. Never re-introduce an allow-list here: an infeasible allocation once
   emptied four columns on every row while the message explaining it was filtered out, which reads
   as "not extracted yet" — a different problem with a different fix.

---

## 1. Where a row comes from

Detailed Review does not have its own endpoint. `item_details` ships **inside** the Critical Review
payload, and every mutation endpoint returns a freshly recomputed one.

```
stored raw extraction (immutable)
        │
        ▼  resolve_context — the fixed order below is load-bearing
 1. invoice_authority.finalize_invoices      → items in invoice order, xml_item_sequence assigned
 2. banking / airwaybill authority           → shipment gross + packages
 3. apply_shipment_override(overlay)         → reviewer totals outrank the documents
 4. apply_item_mutations(overlay)            → tombstones, manual rows, coo_all, field_edits,
                                               ordering, re-sequence 1..N, totals recomputed
 5. packing_match.match_packing              → per-item packing evidence (by identity, not row no.)
 6. hs_resolver.resolve_hs_all(history)      → 4-priority cascade
 7. hs_resolver.apply_hs_reviews(overlay)    → reviewer HS selections, DB-gated again
 8. coo_rules.resolve_coo_all                → 3-rung COO ladder
 9. brand_model_size: apply_edits → resolve  → export-only columns (overrides first)
10. field_allocation.audit_items             → post-resolution gates
        │
        ▼  item_details_preview  (on DEEP COPIES — ctx is never mutated)
11. weight_carton.allocate_weights_and_cartons(review-default totals)
12. supplementary_unit.resolve_supplementary_all
        │
        ▼
   ItemDetailRow[]  +  every message either step produced
```

Two properties of step 4 matter: mutations are applied **before** matching/HS/COO so every
downstream engine sees the mutated list; and `field_edits` are applied before resolution so an
edited description re-derives its own HS, COO, size and packing match.

**The preview is a preview.** Finalize re-runs the real allocation against the reviewer-confirmed
totals from Critical Review. With no gross authority the weight columns stay blank rather than
showing a meaningless basis-as-kg split.

---

## 2. The overlay — the whole persistence model

```json
{
  "schema": 1,
  "revision": 0,
  "ordered_item_ids": [],
  "manual_items":     [],
  "tombstones":       [],
  "hs_selections":    {},
  "field_edits":      {},
  "bms_edits":        {},
  "coo_all":          null,
  "shipment_override": null,
  "reset_notice":     null
}
```

### 2.1 Item identity

| Origin | `item_id` | Stability |
| --- | --- | --- |
| source row | `src:` + first 16 hex of `sha1("{invoice_no}|{invoice_date}|{line_index}|{occurrence}")` | stable across recomputes because the evidence is immutable |
| manual row | `man:` + uuid4, stored in the overlay | stable for the life of the row |

`occurrence` is a per-basis counter that disambiguates byte-identical lineage keys. Enumeration
order is stable because the stored extraction never changes.

**Ids are server-owned.** A client-supplied `item_id` on an add request is rejected
(`extra="forbid"`), and so is every derived field.

### 2.2 Applying the overlay (pure, network-free)

```
1. filter out tombstoned ids
2. materialize active manual rows  (unit_price = total_price / quantity when quantity > 0)
3. if coo_all: stamp it on every row
4. apply field_edits by item_id     (a deleted row's edit is inert, not an error)
5. sort by ordered_item_ids position, then existing sequence
6. re-sequence 1..N
7. recompute inv.goods_total and each invoice ref's item_count
```

### 2.3 Invariants (strict at write time, lenient on read)

* active ids unique → 409 `ITEM_ID_DUPLICATE`
* `ordered_item_ids` is an **exact, duplicate-free permutation** of the active ids →
  409 `ITEM_ORDER_INVALID`

The read path stays lenient on purpose: a review must always remain computable, even from an overlay
written by an older version.

### 2.4 Revision & invalidation

`revision` increments on every structural mutation. It is part of the Critical Review
**fingerprint**, so any item change stales an in-flight review and forces re-verification before
finalize.

Every mutation except brand/model/size calls `_invalidate_derived`:

```
job.critical_review = null      job.declaration = null
delete every XmlArtifact        delete every BmsArtifact
```
…then recomputes the review and sets status back to `CRITICAL_REVIEW_REQUIRED`.

**Brand/Model/Size is the deliberate exception** — see §9.

### 2.5 When mutations are allowed

`_ITEM_MUTABLE_STATES = {CRITICAL_REVIEW_REQUIRED, VALIDATION_BLOCKED, XML_READY}`.
Anything else → 409 `JOB_STATE_NOT_REVIEWABLE` ("Compute the critical review first").

Every mutation takes the job lock, re-reads the job under it, and commits before releasing.

---

## 3. API surface

| Action | Route | Body | Success payload (all include `critical_review`) |
| --- | --- | --- | --- |
| Add row | `POST /api/jobs/{id}/items` | `{insertion_sn, invoice_id, manual_review_addition, reason, item{…}}` | `{status, added_item_id, inserted_sn, revision}` |
| Delete row | `DELETE /api/jobs/{id}/items/{item_id}` | `{confirmation_sn, reason}` | `{status, deleted_item_id, deleted_sn, revision}` |
| Edit fields / pins | `PATCH /api/jobs/{id}/items/{item_id}` | `{fields{…}}` | `{status, item_id, edited{…}, revision}` |
| HS — one row | `POST /api/jobs/{id}/items/hs-review` | `{item_id, final_hs_code, hs_review_source}` | `{status, item_id, final_hs_code, revision}` |
| HS — SN range | `POST /api/jobs/{id}/items/hs-review-range` | `{final_hs_code, hs_review_source, sn_range}` | `{status, final_hs_code, applied_sns[], applied_count, revision}` |
| COO — all rows | `POST /api/jobs/{id}/items/coo-all` | `{country_of_origin}` | `{status, country_of_origin, revision}` |
| Brand/Model/Size | `POST /api/jobs/{id}/items/brand-model-size` | `{edits:[{item_id, brand?, model?, size?}]}` | `{status, edited_items, xls_rebuilt}` |
| Shipment totals | `POST /api/jobs/{id}/shipment-totals` | `{gross_weight, weight_unit, total_packages}` | `{status, gross_weight, weight_unit, total_packages, revision}` |
| HS search | `GET /api/reference/hs?q=&limit=` | — | `[{code, description, unit, explanation, score}]` |

All request models use **`extra="forbid"`**. Errors are `{status_code, code, message}` mapped to
HTTP 404 / 409 / 422 — see each section below for the code list.

---

## 4. Data contract — `ItemDetailRow`

| Field | Type | Meaning / format |
| --- | --- | --- |
| `sn` | int | invoice order = XML item sequence |
| `item_id` | str | immutable identity (§2.1) |
| `origin` | str | `"source"` \| `"manual"` |
| `edited` | bool | reviewer edited this row's invoice fields |
| `description` | str | the customs-declared commercial description (post-cleaning, post-edit) |
| `coo` | str | resolved alpha-2, else the raw printed value |
| `invoice_hs` | str | HS exactly as printed on the invoice (`""` when none) |
| `final_hs` | str | official 11-digit code; `""` = needs manual review |
| `hs_source` | str | resolution source (§7.1) |
| `hs_confidence` | str | `"1.00"` = exact/reviewed; lower = auto guess |
| `hs_low_confidence` | bool | resolved **and** confidence < 1.0 → render the **LOW** chip |
| `hs_explicit` | bool | the reviewer chose or confirmed this code |
| `quantity` | str | compact number, 4 dp, trailing zeros trimmed |
| `uom` | str | invoice unit of measure, upper-cased; `""` stays `""` |
| `total_price` | str | `%.2f` |
| `gross` | str | kg, 4 dp trimmed; `""` when not derivable |
| `net` | str | kg, 4 dp trimmed |
| `ctn` | str | cartons, 2 dp trimmed |
| `gross_pinned` / `net_pinned` / `ctn_pinned` | bool | reviewer-entered → render 📌 |
| `sup_unit` | str | supplementary unit code (from the HS tariff unit) |
| `sup_name` | str | its official name |
| `sup_qty` | str | supplementary quantity, 4 dp trimmed |
| `sup_qty_pinned` | bool | reviewer override active |
| `brand` / `model` / `size` | str | export-only; default `"NA"` |
| `src_file` | str | source document file name; `""` for manual rows |
| `src_page` | int\|null | the page this row was printed on |

**Number formatting rule (`_trim_num`):** fixed decimal places, then strip trailing zeros and a
trailing dot; empty input → `""`, never `"0"` by accident. `ctn` uses 2 places, everything else 4.

---

## 5. The table UI

### 5.1 Header bar

```
[+ Add item]  [COO for all items ______] [Apply COO to all]  [HS search → item range]  N rows · revision R
```

Then, in order: the mutation-result banner, the **pin-conflict banner** (§7.4.7), and the row-queue
bar.

### 5.2 Row queues — filters that are work lists, not decoration

A 200-row declaration is an ordinary day. The reviewer's question is never "show me everything", it
is "which rows still need me". Each chip is a bucket that can be worked to zero, and the count is
the honest size of the job still ahead.

| Chip | Predicate | Tone |
| --- | --- | --- |
| All rows | always | — |
| Needs HS | `!final_hs` | blocking |
| Low-confidence HS | `hs_low_confidence` | warning |
| No COO | `coo` blank | warning |
| Flagged | the row's SN has an item-scope review note | warning |
| Reviewer-set | `edited \|\| origin=="manual" \|\| any pin` | — |

Behaviour to reproduce:

* A chip with count 0 is **disabled**, with a tooltip saying there is nothing to work there.
* When the active bucket empties (the reviewer just fixed its last row) the view **falls back to
  All rows** rather than showing an empty table.
* Free-text search matches `sn, description, coo, invoice_hs, final_hs, uom, sup_unit`.
* Filters and search are **presentation only** — allocation, the totals row and the XML always read
  every row. The row counter says `"X of Y rows"` whenever anything is hidden.
* Filter/search state is cleared when the job changes; carrying it across jobs would silently hide
  rows the reviewer never chose to hide.

### 5.3 Columns

```
SN │ Description │ COO │ Invoice HS │ Final HS │ HS Search │ QTY │ UOM │ Total Price │
Gross │ Net │ CTN │ Sup_U │ Sup_qty │ Doc·Edit·Del
```

Widths are percentages so the table compresses to any window width — **no horizontal scrolling**;
Description gets the most room.

### 5.4 Row markers

| Marker | Meaning |
| --- | --- |
| orange row | no official HS yet — cannot go to XML in that state |
| blue left edge | reviewer-added row |
| *italic* description | reviewer-edited |
| 📌 next to a number | pinned/overridden by the reviewer |
| **LOW** chip | auto HS below full confidence |
| ⚠ / 🛑 next to the SN | that row carries item-scope review notes (tooltip lists code + message) |
| 📄 | open the source document at the page this row was printed on |

### 5.5 Totals row

A `<tfoot>` line summing **all** rows — never the filtered view. These are the numbers customs
reads, so a filter must not change them. Its label says so: `TOTAL (all 214)` when rows are hidden.
Sums: quantity (4 dp), total price (2 dp), gross (4 dp), net (4 dp), CTN (2 dp).

### 5.6 Evidence deep-link

📄 resolves `src_file` to a live document — preferring the row's own role, falling back to any
document with that name so even a rejected upload stays inspectable — and opens it in a **pinned
side panel** at `src_page`. The panel stays open; pressing 📄 on another row just turns the page.
Manual rows have no 📄 because they have no printed source.

---

## 6. Column-by-column specification

### 6.1 SN

Display order and XML sequence. Not editable directly — it changes only through add/delete/ordering.
Carries the ⚠/🛑 note marker. It is also the token used by the **delete confirmation** and the
**HS range** spec, which is why both re-validate it against the *current* list.

### 6.2 Description

**Feeds:** `Goods_description/Commercial_Description`. (`Description_of_goods` carries the official
HS description, falling back to this text when the HS has none.)

Derivation, in order:
1. Extracted cell text.
2. `field_allocation.allocate_description` — strip a leading previous-row OCR overflow run, then cut
   at the first *strong* annotation label (`Batch:`, `Lot#`, `Batch/Expiry Date`, `(Qty)`, `COO:`,
   `Country of Origin`, `Made in`) and mine the row's own COO/batch out of the removed tail.
   **A cut is taken only when everything removed is provably annotation material** — labels, codes,
   numbers, dates, UOM words, a normalizable country. `GENUINE MADE IN ITALY WALLET` keeps its
   description whole (while still yielding COO=IT) because `WALLET` is not annotation material.
3. `description_clean.clean_description` — anchor-based trailing-annotation trim:
   * the trim may only **start at an anchor**: a trigger label (Batch / Lot / Mfg / Exp / Expiry /
     Serial / MRP / Best / Use / Packed) or a real date. Everything before the anchor is kept
     verbatim, so a trailing model number or year is never swallowed
     (`Filter Model 4021 Batch 5567` → `Filter Model 4021`).
   * every token from the anchor to the end must be annotation material **and** the region must
     contain a real value or date — so `Job Lot`, `Parking Lot`, `Steel Rod 12 Batch` are left alone.
   * date recognition is strict (real month names or a 4-digit year), so `8/32` thread size,
     `40/60` grit, `10/20` yarn count and `OPC-53` grade codes are never read as dates.
4. Reviewer edit (`field_edits.description`) — replaces the declared text. The pre-edit text is
   preserved as `evidence_description_raw`, because **packing rows were printed against that text**
   and evidence-to-evidence matching must keep using it. The edit is declaration-level, not
   evidence-level.

Neither cleaner ever invents text; both only remove or classify.

Messages: `DESCRIPTION_ANNOTATION_TRIMMED`, `DESCRIPTION_EXTRA_INFO`, `DESCRIPTION_CODE_ONLY`
(the description is a bare part code with no product name — reconstruction is deliberately **not**
attempted, since a guessed customs description would be an invented fact).

### 6.3 COO

**Feeds:** `Goods_description/Country_of_origin_code`. **Blocking when unresolved**
(`COO_UNRESOLVED`), and the validator additionally blocks any value whose length ≠ 2.

Ladder (`rules/coo.py`):

| Rank | Source | Behaviour |
| --- | --- | --- |
| 1 | `ITEM_LEVEL` | the row's own printed origin, normalize-gated — including labelled/trailing forms (`COO: Ireland`, `Ireland Tariff Code` → `IE`) via progressive normalization |
| 2 | `VENDOR_PROFILE` | this exporter's remembered origin, learned from previously finalized/reviewer-corrected jobs. **Always warned, never silent** (`COO_VENDOR_PROFILE`) |
| 3 | `EXPORTER_FALLBACK` | the exporter's own country, with `COO_EXPORTER_FALLBACK` |
| 4 | — | blocking `COO_UNRESOLVED` |

Rank 2 outranks rank 3 because the exporter fallback is *systematically* wrong for a trader
exporting goods made elsewhere — a live job declared Singapore for Irish stents. The profile store
never learns from its own output (profile- and fallback-sourced values are excluded when recording),
and it only learns from jobs whose invoice went through **live** OCR, so demo and fixture runs
cannot poison it.

**Never emits `XX` / `ZZ` / `UNKNOWN` / `N/A` / empty. `NA` means Namibia, not null.**

Editing: per-row via ✎, or bulk via **Apply COO to all** (§8.6).

### 6.4 Invoice HS

Read-only. The HS exactly as printed on the invoice row, or `—`. It is the *input* to priority 1 of
the cascade, never a declared value in its own right.

### 6.5 Final HS, HS Search and the LOW chip

**Feeds:** `Tarification/HScode/Commodity_code` (first 8 digits) + `Precision_1` (digits 9–11).
`Precision_2..4` are emitted as `<null/>`. The HS also determines the **supplementary unit code**
and the official goods description.

Rules that never bend:

* **Only an exact official 11-digit database code may become final.** No zfill, no nearest-numeric
  correction, no invented digits, leading zeros preserved.
* **An LLM-proposed HS11 is rejected outright.** The model may supply an 8-digit *hint* only, which
  is then expanded through the database.
* **A resolution is not a confirmation** — see the blocking gate below.

The cascade, confidence table and the search contract are in §7.1.

**UI:**
* The cell shows `final_hs` with a tooltip naming source and confidence.
* `hs_low_confidence` renders a **LOW** chip plus a **✓ confirm** button that posts the *same* code
  through the review channel with source `detailed_review` — turning a proposal into an explicit
  human decision (confidence 1.00, `hs_explicit = true`).
* The **HS Search** cell is a per-row combobox keyed by `item_id`; the parent keeps at most one
  dropdown open. It must use an `AbortController` **and** a monotonically increasing sequence number
  checked before results are applied — otherwise a slow earlier query overwrites a fast later one.
* A header-level **HS → item range** search opens the bulk-apply dialog (§8.5).

**Blocking gate you must reproduce:** an HS the machine picked by matching the description
(`hs_source == "SEMANTIC_DESCRIPTION"`, confidence 0.30) and never confirmed is **blocking** at
finalize (`HS_GUESS_UNCONFIRMED`), even though it is a valid 11-digit code. The cascade always
finalizes *something* and the validator only checks digit count, so without this gate a guess is
indistinguishable from an invoice-printed exact match by the time it reaches the XML — and HS sets
the duty rate. It is deliberately **not** in `WARN_MODE_HARD_CODES`: warn mode may still build a test
XML.

### 6.6 QTY

**Feeds:** the declaration item's `quantity`; also the basis for several supplementary formulas, and
the fallback allocation basis when no packing list was uploaded.

Editable. Must be finite and `> 0` (422 `QUANTITY_INVALID`). Editing quantity re-derives
`unit_price = total_price / quantity`.

### 6.7 UOM

**Feeds:** the declaration item's `invoice_uom`; gates the pack-multiplier and pair/dozen rules.

Editable; upper-cased, capped at 20 chars, `""` → `PCS` on a **manual add** but **blank stays blank
on an edit** — an extracted row with no readable unit must keep raising `ITEM_UOM_MISSING` rather
than being silently coerced.

Count-type UOMs recognised by the supplementary engine:
`PCS, PC, NOS, NO, UNIT, UNITS, EA, EACH, SET, SETS, UNT`.

### 6.8 Total Price

**Feeds:** `Item_price`, `Item_Invoice` (foreign + national), and via the sum, the header
`Gs_Invoice` and `Total_CIF`. Also the default allocation basis for freight and insurance.

Editable. Finite, `>= 0` (422 `TOTAL_PRICE_INVALID`), stored at 2 dp. Zero is allowed but flagged
(`ITEM_PRICE_ZERO_SUSPECT`).

### 6.9 / 6.10 / 6.11 Gross · Net · CTN

**Feed:** `Valuation_item/Weight_itm/Gross_weight_itm`, `Net_weight_itm`, and
`Packages/Number_of_packages`.

These three are the output of the allocation engine (§7.4) and the most rule-dense part of the
screen. Summary of the contract:

* `Σ item gross == authorised total gross` **exactly**; `Σ item CTN == authorised total cartons`
  **exactly**.
* `net < gross` for **every** item, strictly.
* Every CTN is `>= 0.01` and a whole multiple of `0.01`.
* Precision: gross and net 3 dp (`eps = 0.001`), CTN 2 dp. Decimal arithmetic only.
* A typed value **pins** that item: kept exact, other items adjusted. An empty string **un-pins**.

### 6.12 Sup_U (supplementary unit code)

**Feeds:** `Supplementary_unit/Suppplementary_unit_code` + `_name` (note the source XML's tripled
`p` — reproduce the tag name exactly).

**Never typed.** It follows the official tariff UNIT of the final HS — not the invoice UOM. This is
why the edit dialog rejects any attempt to set it (422 `ITEM_FIELD_NOT_EDITABLE`).

Recognised codes and names: `UNT` Unit, `KGM` Kilogram, `PR` Pair, `NPR` Number of pairs,
`DZN` Dozen, `MTR` Metre, `SQM` Square metre, `LTR` Litre. Anything else falls back to `UNT` with a
warning naming the unknown unit.

### 6.13 Sup_qty (supplementary quantity)

**Feeds:** `Suppplementary_unit_quantity`. **Blocking if `<= 0`** (`SUPPLEMENTARY_QTY_INVALID`).

Derived per unit (§7.5). Editable as a **plain override**, not a pin: nothing is redistributed and no
formula changes — the typed number is what the XML carries. Clear the box to restore the computed
value.

### 6.14 Doc · Edit · Del

📄 evidence deep-link (§5.6) · ✎ edit dialog (§8.3) · 🗑 delete dialog (§8.4).
🗑 is disabled when only one row remains.

---

## 7. The derivation engines

### 7.1 HS cascade

| Priority | Source | Condition | `hs_source` | Confidence |
| --- | --- | --- | --- | --- |
| 1 | invoice HS, exact | printed code is an exact 11-digit DB record | `INVOICE_HS_EXACT` | **1.00** |
| 1 | invoice HS, completed | printed 8/6/4-digit prefix, best code chosen **within the band** by description match | `INVOICE_HS_COMPLETED_8` / `_6` / `_4` | 0.80 / 0.60 / 0.50 |
| 1 | invoice HS, band "Other" | nothing in the band cleared the semantic threshold | `..._8_OTHER` / `_6_OTHER` / `_4_OTHER` | 0.50 / 0.40 / 0.35 |
| 2 | same-item history | a previously **reviewer-confirmed** code for this exact normalized item name | `HISTORY` | 0.70 |
| 3 | semantic description | no invoice HS at all — match the description against the DB | `SEMANTIC_DESCRIPTION` | **0.30** |
| 4 | LLM HS8 hint | 8-digit hint only, expanded through the DB | `LLM_HS8` | 0.60 |
| — | none | | — | blocking `HS_MANUAL_REVIEW` |
| ★ | reviewer selection | `detailed_review` / `detailed_review_hs_search` | source upper-cased | **1.00**, `explicit = true` |

**Normalized item name** (the history key): `lowercase`, then strip everything that is not
`[a-z0-9]`.

**Within-band selection** (`_pick_within_band`), threshold `0.34`:

```
score(query_tokens, record) =
    Σ over query tokens:  1.0 if the token matches the OFFICIAL DESCRIPTION
                          0.5 if it matches only the AI EXPLANATION
    ────────────────────────────────────────────────────────────────────
                        number of query tokens

token match = equal, or one starts with the other when the prefix is ≥ 4 chars
tokens      = length ≥ 2 and not purely numeric
best        = highest score, preferring a non-"Other" code, stable by hs11
below 0.34  = the band's broadest "Other"-titled code (max hs11), else the residual last code
```

A distinctive description word therefore outweighs an explanation that merely mentions the term in
passing.

**Priority 2 exists but stays LOW on purpose.** History nearly always finds something, so an item is
rarely left visibly unresolved — which is exactly why it must not read as final. It is re-proposed
with `HS_HISTORY_APPLIED` for re-confirmation.

**Two reviewer channels must not diverge:**
* `apply_hs_reviews` — `item_id`-keyed, from this screen, applied *during* resolution.
* `apply_manual_hs` — sequence-keyed, posted with `/finalize`, applied *after* it, and therefore
  overwriting the first. It requires the review fingerprint (sequences shift on add/delete).

Both require an exact official 11-digit code. A stored selection that no longer validates is
**never applied and never serialized**: the item keeps its cascade proposal or stays blocked, and
`HS_REVIEW_REJECTED` is raised twice — item-scope BLOCKING (gates the XML) and review-scope WARNING
(makes it visible on screen).

**HS search contract** (`GET /api/reference/hs`):

| Aspect | Rule |
| --- | --- |
| Query gate | ≥ 2 characters after trim; a purely numeric query needs ≥ 2 digits (enforced identically client- and server-side) |
| Numeric branch | digit-prefix match — exact 11-digit scores 100.0, any prefix 90.0 |
| Text branch | token similarity over the **official description only** — the explanation column is returned for display but is **never searched or ranked** |
| Token scoring | exact 1.0, description token starts with query token 0.8, query ≥ 4 chars contained in token 0.6; **every** query token must match |
| Sort | score desc, then hs11 asc |
| Bounds | `limit` clamped to 1..50; **query tokens capped at 12** |
| I/O | none — local, deterministic, network-free |

The query-token cap is a real DoS fix: the text branch is a cross-product over ~12k codes, so an
unbounded query multiplied the scan by its token count — one authenticated request able to spend
arbitrary CPU, repeatably.

### 7.2 Packing-row → invoice-item matching

**By normalized product identity, never by row number.** Normalization lowercases and strips all
non-alphanumerics, so spacing, punctuation and separators stop mattering while size, model, colour,
`500ml` and pack size survive inline. Items differing in size, variant, model, colour or pack
quantity therefore never merge.

| Rank | Rule | Confidence | Message |
| --- | --- | --- | --- |
| 1 | exact normalized description equality | 1.00 | — |
| 2 | product / part / item-code equality | 0.95 | `PACKING_MATCH_BY_CODE`, naming every pairing |
| 3 | scored description similarity | ≤ 0.90 | `PACKING_MATCH_LOW_CONFIDENCE`, naming every pairing |

* **A measurement is not an identifier.** `500ML`, `3000W`, `80GSM`, `1200X600` pass a naive
  letters-and-digits test; a pack size printed on both documents once paired a dishwash line with a
  floor-cleaner line at 0.95 with no warning. Digit-led unit-tailed tokens and dimension chains are
  rejected as codes, and rank 2 reports itself exactly like rank 3.
* Rank 3 is gated twice: **measurements of the same kind must agree** (`500 ml` vs `250 ml` fails),
  and **the winner must be clear** — a best score within 0.10 of the runner-up is ambiguous, the row
  is left unmatched, and `PACKING_MATCH_AMBIGUOUS` names both candidates.
* Rank-3 pairs are scored once and assigned **best-first across the whole document**, never in
  packing-row order. Row-order assignment lets a 0.62 match earlier in the file claim the item a 0.95
  match later describes — making the pairing depend on the order the supplier typed their list.
* An invoice line claimed by an exact or code match is never re-claimed by a similarity match.
* Repeated packing rows of the same identity are **grouped and summed before** any assignment.
* Shared carton groups contribute the group total **once**, divided by row quantity (else equally),
  and the shares add back to the group total exactly — the last member takes the remainder.
* **Groups are per document**: carton numbering restarts in each packing list, so two suppliers'
  groups both labelled `1-5` are physically distinct sets.
* Unmatched invoice items → `PACKING_ITEMS_UNMATCHED`; unmatched packing rows →
  `PACKING_ROWS_UNMATCHED`, both naming the products.

### 7.3 Where the item's net weight comes from — the override ladder

Applied after the initial basis. **Later application = higher authority.**

| Rank | Source | Sets |
| --- | --- | --- |
| 1 | **reviewer pin** | exact net and/or gross and/or CTN |
| 2 | invoice-printed item weight | fixed net; gross basis = `net × 1.2` (**and a hard cap**) |
| 3 | invoice **quantity in a mass unit** — the line is sold by weight | fixed net; gross basis = `net × 1.2` (**capped**) |
| 4 | packing-list item net | fixed net; gross basis stays the packing gross |
| 5 | invoice **description** conversion (HIGH, then LOW confidence) | fixed net; gross basis = `net × 1.2` |
| 6 | ratio | `net = 0.7 × final gross` |

Four rules inside this ladder are worth stating explicitly, because each was a real wrong number:

**(a) The 1.2 is a ceiling on invoice-stated weights, not just a weighting.** For ranks 2 and 3,
`net × 1.2` caps the *final* gross. Without the cap, exact-sum reconciliation rescaled every share
and a stated 500 kg line came out at 1636 kg because two unweighed lines pulled the basis upward.
A stated weight is evidence, not a hint. Only invoice-stated nets are capped — a packing net has its
own printed gross beside it, a ratio net is `0.7 × gross` by construction, and a reviewer pin is
supreme. When the cap cannot hold, it is released and `GROSS_EXCEEDS_INVOICE_WEIGHT_CAP` names every
capped item and its maximum. Implementation note: the cap-and-redistribute must iterate to a fixed
point — releasing weight onto uncapped items can push one of *them* over its own cap.

**(b) A printed weight beats a parsed name.** The description conversion sits **below** the packing
list. `VACUUM FLASK 500ML × 100` with a packing list printing 720 kg net once converted to 50 kg
because `500ML` was read as the article's capacity. A product name is not a weight statement; a
packing list is a document whose purpose is to state what each line weighs. And because exact-sum
must hold, the gross freed from the misread lines moved onto lines nobody had misread — one bad
reading moved every weight on the declaration.

**(c) A line invoiced by weight states its own net.** `RESIN 500 KG` sells five hundred kilograms —
at LINE_TOTAL scope, not a per-unit figure. Every recognised mass unit counts, through the single
`units.to_kg` boundary. **A blank unit is never accepted here** (blank means pieces far more often
than kilograms on a *quantity* column). `MT`, `T` and `G` convert but are flagged
`INVOICE_UOM_WEIGHT_AMBIGUOUS` — `MT` is metric ton on most invoices and METRE on some.

**(d) A description net that cannot be true is rejected, not blocked.** Rank 5 is a parser reading
free text; every other rank is a human or a document. When the derived nets cannot fit the
authorised gross, description nets are dropped worst-offender-first until feasible, with
`DESCRIPTION_NET_REJECTED` naming each. A rejected item is re-based on its **quantity share**
(quantity predicts weight far better than price does), rescaled so it is commensurable with items
still measured in kilograms or dollars — the algebra cancels to exactly `q_i / Σq_all`. **Exception:**
an item with its own packing row goes back on its packing basis. A reviewer pin, an invoice-printed
weight, a mass-unit invoice quantity and a packing net still **block** — those are stated values, and
only the person who stated them may correct them.

**Description-conversion confidence gate** (the approved boundary — a case moving between buckets is
a rule change):

| Bucket | When |
| --- | --- |
| **HIGH** | unambiguous mass/volume token; **known** density; pack multiplier consistent with the invoice UOM; GSM/denier/tex/kg-per-m/g-per-m with every variable present |
| **LOW** (used, flagged, never top authority) | assumed density; ambiguous token (`g`, `l`, `cc`, `dl`, `dal`, `pt`, `qt`, `cup`, `tsp`, `tbsp`, `mt`); unverifiable pack multiplier; package-only weight; US/Imperial-ambiguous unit (US value used, and said so); space-thousands reading (`1 250 KG`); context-suppressed multiplier |
| **REFUSED** | ambiguous token **and** assumed density; dosage forms; concentrations and rates (`250 MG/5 ML`, `100 LTR/MIN`); container/appliance capacities (`WATER TANK 1000 LTR` — the goods are the tank); numeric ranges (`25-50 KG`); `CBM`/`m³`; length or area alone; `ST`; bare `oz` on a liquid with no net marker |

Every demotion writes `DESCRIPTION_WEIGHT_UNCERTAIN` naming the item and the reason; a HIGH result
carries no warnings. Parser rules that exist because each produced a wrong number *at HIGH
confidence*: a thousands separator is not a decimal point (`2,500 G` = 2.5 kg); a pack multiplier
binds to the value the pack names; a weight beside CARTON/PALLET/MASTER/GROSS is packaging unless a
content word sits there too; a concentration is not a content; a dimension chain is not a pack; a
dozen multiplier never lands on a package weight; reversed and slash notations (`1 LTR X 12`,
`24/250ML`) carry the multiplier; a zero is never a weight. `ST` is deliberately absent from the mass
table — on an invoice it means set/sterile/Stück far more often than stone.

When both an invoice-stated net and a description conversion exist and disagree by more than **10×**,
`NET_WEIGHT_SOURCES_DISAGREE` names both figures and their sources. The stated value is still used —
an order-of-magnitude gap is a unit or pack-multiplier error, not a judgement call.

### 7.4 Allocation and reconciliation

#### 7.4.1 The two absolutes

```
Σ item gross == authorised total gross   exactly
Σ item CTN   == authorised total carton  exactly
```

So `gross = net × 1.2` and `net = 0.7 × gross` are **provisional bases only** — neither factor
survives to the XML literally. The ladder decides the *shape* of the distribution; the authority
decides its *scale*.

#### 7.4.2 Initial basis

Conditions 1 and 2 are **independent**, not a choice — a packing list printing both item weights and
item cartons governs both.

| Case | Gross basis | Carton basis |
| --- | --- | --- |
| packing prints item gross | packing gross (grouped, shared-aware) | derived from gross if cartons absent |
| packing prints item cartons | derived from cartons if gross absent | packing cartons |
| neither, packing list uploaded | invoice **value** share | follows weight share |
| neither, no packing list | invoice **quantity** share | follows weight share |

**Per item, not per shipment.** An item with no packing match takes the fallback shape *scaled by
the matched items' density*: `density = Σ(matched packing basis) / Σ(matched fallback basis)`,
`unmatched[i] = density × fallback[i]`. With no usable fallback among matched rows, the matched mean
per item is used.

**A basis of `0` is forbidden.** Zero does not mean "unknown" to an apportionment, it means "no
weight": the item lands on its floor and is declared at `0.001` kg with everything reconciling and
`validation_status: OK`. Three equal invoice lines with one matched once produced
`299.998 / 0.001 / 0.001` against a 300 kg authority, with no warning at any level.

#### 7.4.3 `apportion` — one primitive, used for gross and cartons

```
apportion(total, basis[], floor[], places, ceiling[]?) -> values[] | None

  Σ values == total        exactly at `places`
  values[i] >= floor[i]    for every i
  None (infeasible)        when Σ floor > total — never an approximation
```

Largest-remainder (Hamilton): floor every ideal share, then hand the remaining whole units to the
largest fractional remainders, so the shortfall is spread rather than dumped on one item. Dumping it
is how a previous carton allocator drove an item negative, clamped it to 0.01, and broke the exact-sum
invariant it advertised (200 items in 3 cartons summing to 3.99).

**A floor is a constraint, not an additive base** — the pure proportional split is used whenever it
already clears every floor. `total` and every floor must be quantized to `places` before the call.

#### 7.4.4 Ratio nets are derived from the FINAL gross

```
1. partition items into fixed-net (ranks 1–5) and ratio (rank 6)
2. floors: fixed-net items get net + 0.001; ratio items get one precision unit
3. apportion the gross
4. THEN derive net = ROUND_DOWN(0.7 × gross, 3) for the ratio items
```

This removes the apparent circularity and makes `net < gross` true by construction. It also means
**infeasibility can only ever be caused by a stated value** — if every item is ratio-mode the budget
needs just `n × 0.001`. Ratio nets round **down**: at `gross = 0.001`, `0.7 × 0.001` would round
half-up to `0.001` and equal its own gross.

#### 7.4.5 Precision

| Value | Precision | Floor / epsilon |
| --- | --- | --- |
| item gross | 3 dp | 0.001 |
| item net | 3 dp | — |
| item CTN | 2 dp, whole multiple of 0.01 | 0.01 |
| authority gross | quantized to 3 dp **before use** | — |
| authority CTN | quantized to 2 dp before use | — |

The authority must be representable at 3 dp — a sum of 3 dp values is always a multiple of 0.001, so
an authority of `199.0005` could never be matched. Quantize first; the quantized value *is* the
authority from then on, for reconciliation, display and the reviewer. `trim_min1` strips trailing
zeros so `1.520` emits as `1.52`.

#### 7.4.6 The carton lattice is a MUST rule

Every declared carton count — allocated, estimated or reviewer-entered — is `>= 0.01` and a whole
multiple of `0.01`. So is the authorised total, which is why `POST /shipment-totals` **rejects** an
off-lattice package total instead of quantizing quietly. `apportion` produces lattice values by
construction, but a rule nothing checks is a rule that silently stops being true — so it is asserted
after every allocation (`CARTON_LATTICE_VIOLATION`).

The lattice is what makes carton pins exact: on a fixed lattice the difference a pin creates is a
whole number of 0.01 units, and redistributing whole units is integer arithmetic with no rounding.
The one exception is a shipment with **no package authority at all** — there every CTN is `0`, the
explicit "no packages declared" state rather than a value on the scale.

#### 7.4.7 Reviewer pins

**The human value is always accepted.** Never clamped, rescaled or rounded toward a derived figure.
A pin that contradicts a document is accepted with `REVIEWED_CTN_OVERRIDES_PACKING` naming both
figures — the reviewer read the document, and rank 1 outranks it.

The two columns then diverge **deliberately**:

| | Gross weight | CTN |
| --- | --- | --- |
| Redistribution | all unpinned items re-apportioned on their provisional basis | delta absorbed by a **few** items |
| Optimises for | proportionality | **stability** — numbers the reviewer already checked stay put |

A carton count is a countable object on a coarse lattice that a reviewer reads and remembers; a gross
weight is a continuous derived figure at 3 dp. Re-apportioning 200 carton rows because one was edited
churns every number for no gain. So the carton path:

1. computes a **deterministic no-pin baseline** — `apportion(auth_ctn, basis, 0.01, 0.01)` with pins
   ignored. The delta is measured against *this*, never "what was on screen last time": the overlay
   replays from scratch, so the outcome must depend only on the **set** of pins. Summing the delta
   across all pins at once makes pin A→B and pin B→A produce the same declaration.
2. **estimated rows absorb first**, concentrated into the `CTN_DONOR_MAX = 10` highest of them —
   those rows carry no printed information, so concentrating the damage costs nothing real.
3. **packing-stated rows are rescaled as a set**, and only when the estimates cannot cover it: a
   uniform proportional haircut that preserves every pairwise ratio and scales shared carton groups
   by a common factor. Loud: `CTN_PIN_RESCALED_PACKING_EVIDENCE` names the factor and row count.

The donor set is bounded from two directions that pull opposite ways: **granularity caps it** (a
delta of 3 units cannot be shared by 10 rows — at most `|delta|` donors, so every donor moves at
least one unit), while **capacity grows it** (a donor may not fall below 0.01, so a shortfall extends
the set *down* the ordering). Ties break on `(−baseline, SN)`; without a fixed tie-break the donor
set is nondeterministic.

**When the pins cannot fit, the authority is what gives way.** The exact bound is
`Σ pins ≤ auth_ctn − 0.01 × unpinned` (all-pinned must equal the authority exactly). Past it the pins
still stand, unpinned rows sit on the lattice minimum, and `REVIEWED_CTN_EXCEEDS_AUTHORITY` /
`REVIEWED_CTN_TOTAL_MISMATCH` carry a `remediation` naming the total the shipment would need — which
this screen surfaces as a **prefilled one-click `POST /shipment-totals`** (the pin-conflict banner).
Nothing is applied automatically: a declaration's total gross weight and package count must never
move as a side effect of editing one row. The two gross codes carry the same remediation.

`CTN_DONORS_ON_FLOOR` names how many rows a large pin pushed onto the 0.01 minimum — an outcome can
satisfy every invariant and still be degenerate.

#### 7.4.8 Infeasible input assigns nothing

When the authorised gross cannot cover the fixed nets, allocation **stops** rather than reconciling.
No gross is written for the affected items, the weight columns stay blank, and a blocking message
names the totals. Reviewer pins are preserved and echoed back so they can be corrected.

**Cartons are not independent of this.** With no packing carton evidence their basis *is* the item
gross, so an item whose gross was withheld has a carton basis of zero — and zero is "no weight", not
"no information". When every gross is missing, largest-remainder collapses to an equal split:
quantities 1/9/90 produced 4.00/4.00/4.00 under an audit trail still reading *proportional by gross
weight*. An item with no allocated gross therefore takes its **quantity share**, rescaled by what the
allocated items weigh per unit of quantity, and `carton_source` names the basis actually used
(1/9/90 → 0.12/1.08/10.80). Packing-stated cartons are untouched — those genuinely are independent.

`GROSS_ALLOCATION_IMPOSSIBLE` carries a `remediation` naming the authority that *would* fit
(`Σ pins + Σ floors`), offered as the same one-click shipment-totals change.

#### 7.4.9 Feasibility diagnostic

Runs before reconciliation and writes nothing. `Σ net / auth_gross` names the likely cause:

| Ratio | Reading |
| --- | --- |
| ~1000 | a gram value read as kilograms |
| ~24 | a pack multiplier applied to a piece count |
| ~2.2 | pounds read as kilograms |
| ~0.7 | healthy |
| > 1.0 | infeasible |

A single item whose net exceeds the entire authorised gross is provably wrong and is named
individually (`ITEM_NET_EXCEEDS_SHIPMENT`).

#### 7.4.10 Audit trail

Every item carries `allocation_audit`: invoice line, description, matched packing item, carton
source, gross weight source, net weight source, `estimated` flag, final CTN / gross / net, and
validation status. Never silently ignore a mismatch, drop an invoice item, create an XML item that is
not on the invoice, or change invoice item order.

### 7.5 Supplementary quantity formulas

Driven by the **official tariff unit of the final HS**, never the invoice UOM.

| Tariff unit | Formula | Rounding | Warning |
| --- | --- | --- | --- |
| `KGM` | net weight (kg) | 4 dp | — (blocking if `<= 0` and no override) |
| `UNT` | invoice quantity | 2 dp | — |
| `PR` / `NPR` | `qty / 2` when `pair_divide_by_two` **and** the UOM is a count unit; else `qty` | integer | odd count → "verify manually" |
| `DZN` | `qty / 12` when the UOM is a count unit; else `qty` | 3 dp | not a multiple of 12 → "fractional dozen" |
| `MTR` | quantity when the UOM is metre-like (`MTR/M/METER/METRE`); else **net weight as proxy** | 3 dp | proxy use is warned |
| `SQM` | `qty × 1.524` when the UOM is metre-like (60-inch width assumption); else net weight as proxy | 3 dp | always warned — verify the width |
| `LTR` | net weight as proxy (density ≈ 1) | 3 dp | always warned |
| anything else | falls back to `UNT` + invoice quantity | 2 dp | names the unknown tariff unit |

Every assumption raises `SUPPLEMENTARY_ASSUMPTION` (item-scope). A result `<= 0` raises blocking
`SUPPLEMENTARY_QTY_INVALID`.

**Reviewer override:** replaces the computed quantity and **clears the conversion warning it
replaced** — but the unknown-tariff-unit warning survives, because that message is about the *code*,
not the number. The unit code and name still come from the HS.

`pair_divide_by_two` is ADR-004, a versioned config flag published at `/api/config`, not a silent
constant.

### 7.6 Brand / Model / Size (export-only)

Never written into the customs XML. Resolved deterministically from already-extracted cells — the
resolver invents nothing — and reviewer overrides are applied **first** so they always win.

* **BRAND** — a per-row brand column when the invoice printed one, else the exporter/manufacturer
  name. Either way: the **first significant word, UPPERCASE**. Courtesy and legal-form prefixes and
  bare initials are skipped, so `M/S. Abbott Laboratories` → `ABBOTT` (not `M`) and
  `A. Menarini Diagnostics` → `MENARINI`. Neither available → `NA`.
* **MODEL** — the invoice's model/part/SKU/catalogue cell, parsed **label-aware** (vendors print the
  label inside the cell: `REF 01R6070`, `P/N: AB-12`, `CFN 07K5901` — the label is stripped and the
  code kept, never the other way round). Failing a label: the first letter-AND-digit token, then the
  first digit-bearing one. **A 13–14 digit GTIN/EAN barcode is never a model**, wherever it appears
  (`MODEL_BARCODE_ONLY` flags a row whose only candidate was one).
* **SIZE** — a real size, never a quantity. Cascade: a size/dimension/capacity column (trusted
  verbatim) → a measured specification mined from the description (`400ML`, `1L`, `20 X 30MM`,
  `10 x 113 mL`, `32GB`, `42mm`, `EU 42`) → a word size (`LARGE`, `EXTRA LARGE`) → `NA`.
  Count and packaging units (PCS, EA, NOS, SET, BOX…) are **deliberately absent from the size
  vocabulary**, so `Card Reader 5 PCS` can never report `5 PCS` as a size — fixed at the root rather
  than out-ranked. All candidates are collected and the most specific wins. Short forms are never
  emitted: `S/M/L/XL/XXL` expand to `SMALL`/`MEDIUM`/`LARGE`/`EXTRA LARGE`/`EXTRA EXTRA LARGE`.
  A letter bound to a number stays a unit (`1L` = one litre) while a standalone letter is a garment
  size (`T-Shirt L` → `LARGE`); designator labels are guarded, so `Model L` is not a size.

Every raw cell first passes a placeholder filter — `-`, `N/A`, `NIL`, `TBD`, `0`, `none`, `null`, `?`
are treated as **absent**, not as data.

---

## 8. Mutation channels in full

### 8.1 Add item — `POST /items`

Dialog fields: insertion SN, authoritative invoice (dropdown from the roster), "reviewed manual
addition" checkbox, then description, quantity, UOM, total price, country of origin, final HS.

| Check | Error |
| --- | --- |
| `1 <= insertion_sn <= count + 1` | 422 `INSERTION_SN_OUT_OF_RANGE` |
| `invoice_id` is an authoritative invoice number | 422 `INVOICE_UNKNOWN` (lists the known ones) |
| no invoice **and** `manual_review_addition != true` | 422 `MANUAL_CONFIRMATION_REQUIRED` |
| quantity / total price finite and non-negative | 422 at the route boundary |
| any client-supplied `item_id` or derived field | 422 (`extra="forbid"`) |

Seed caps: description 400, UOM 20 (default `PCS`), COO 80, HS 20 chars.
The stored record keeps invoice lineage: `{invoice_id, invoice_no, invoice_date, currency,
source_document_id, source_file, association}`.

The row may be saved **incomplete** — missing description or `quantity <= 0` raises the warning
`MANUAL_ITEM_INCOMPLETE` at review, and the *same condition* is a **blocker** at finalize.

### 8.2 Delete item — `DELETE /items/{item_id}`

Dialog requires the reviewer to type the row's **current** SN.

| Check | Error |
| --- | --- |
| item exists and is active | 404 `ITEM_NOT_FOUND` |
| typed SN equals the current SN | 409 `CONFIRMATION_SN_MISMATCH` — "the row order changed; re-confirm against the refreshed review" |
| more than one row remains | 409 `LAST_ITEM_UNDELETABLE` |

Effects: source rows get a **tombstone** carrying the full lineage (invoice no/date, line index, line
no, description, previous SN, timestamp) — the evidence itself is kept. Manual rows are marked
`active:false`; **a deleted identity is never reactivated**, and a replacement gets a new UUID. The
id is purged from `hs_selections`, `field_edits` and `bms_edits`.

### 8.3 Edit fields / pins — `PATCH /items/{item_id}`

Editable set: `description, quantity, uom, total_price, country_of_origin, gross_weight, net_weight,
carton_count, supplementary_quantity`. Anything else → 422 `ITEM_FIELD_NOT_EDITABLE` (HS goes through
the HS channel; the supplementary unit **code** follows the HS's tariff unit).
No fields supplied → 422 `ITEM_EDIT_EMPTY`.

| Field | Rule | Error |
| --- | --- | --- |
| `description` | non-empty after trim, ≤ 400 | `DESCRIPTION_REQUIRED` |
| `quantity` | finite, `> 0` | `QUANTITY_INVALID` |
| `total_price` | finite, `>= 0`, stored 2 dp | `TOTAL_PRICE_INVALID` |
| `uom` | upper-cased, ≤ 20 | — |
| `country_of_origin` | ≤ 80 | — |
| `gross_weight` / `net_weight` | finite, `> 0`, kg, stored 4 dp | `GROSS_WEIGHT_INVALID` / `NET_WEIGHT_INVALID` |
| net vs gross | net strictly below gross **across the merged edit** (an earlier pinned value counts) | `NET_NOT_BELOW_GROSS` |
| `carton_count` | finite, `>= 0.01`, **exact multiple of 0.01**, stored 2 dp | `CARTON_COUNT_INVALID` / `CARTON_COUNT_OFF_LATTICE` |
| `supplementary_quantity` | finite, `> 0`, stored 4 dp | `SUPPLEMENTARY_QUANTITY_INVALID` |

**Off-lattice cartons are rejected, not quantized.** Rounding 3.333 to 3.33 and reconciling the whole
shipment around the difference produces a number the reviewer never typed and cannot account for.

**Three states per pin field — all three must survive the round trip:**

```
untouched  → omit the key entirely   (an unchanged computed value must never become a pin)
changed    → send the number
cleared    → send ""                 (server drops the pin; the computed value returns)
```

The client computes this by comparing the current input against the value the dialog opened with;
equal → `undefined`, otherwise the string. Without the third state a pin could be changed but never
taken back — a one-way door.

`CLEARABLE_ITEM_FIELDS = PIN_ITEM_FIELDS + ("supplementary_quantity",)`. The supplementary override
joins the pins **only** for clearing; it is deliberately not a member of `PIN_ITEM_FIELDS`, because
that tuple also decides what a packing/AWB re-extraction discards, and this override is not reconciled
against the authority those documents set.

Partial edits **merge** with earlier ones; the event payload records `changed`, `previous_edits`,
`cleared[]` and a full `before` snapshot.

### 8.4 HS — one row

`POST /items/hs-review` with `{item_id, final_hs_code, hs_review_source}`.

Write-time gate (shared with the range form):
1. source in `{detailed_review, detailed_review_hs_search}` → else 422 `HS_REVIEW_SOURCE_INVALID`
2. code normalizes to exactly 11 digits — **no zfill, no prefix completion**, leading zeros preserved
   → else 422 `HS_NOT_11_DIGITS`
3. the code exists **verbatim** in the official database → else 422 `HS_NOT_IN_DATABASE`
4. the item exists → else 404 `ITEM_NOT_FOUND`

Stored: `{final_hs_code, hs_review_source, explicit:true, selected_at, description_key}`.
The `description_key` (normalized item name) is what lets the decision be folded into content-keyed
HS history when the evidence is later superseded (§8.8).

The client sends **only** identity + code + source. Unit, official description, supplementary
quantity and readiness are all re-derived server-side from the database.

### 8.5 HS — SN range

`POST /items/hs-review-range` with `{final_hs_code, hs_review_source, sn_range}`.

Printer-style spec: `"1-15, 19, 80"`, or `"all"`. En-dash tolerated.

| Check | Error |
| --- | --- |
| spec empty / resolves to nothing | 422 `SN_RANGE_EMPTY` |
| token is not an SN or a low-high range | 422 `SN_RANGE_INVALID` |
| range reversed | 422 `SN_RANGE_INVALID` — "write it low-high" |
| SN outside `1..max_sn` | 422 `SN_RANGE_OUT_OF_BOUNDS` |
| an SN vanished since the client read the table | 409 `SN_RANGE_STALE` |

Each targeted row gets the same explicit selection record as a per-row pick, so a later per-row pick
still overrides one row. Response includes `applied_sns[]` and `applied_count`.

### 8.6 COO — all rows

`POST /items/coo-all` with `{country_of_origin}`. Must resolve to a canonical alpha-2 via the country
reference → else 422 `COO_INVALID`.

Stamps `coo_all` and **clears every per-item COO edit** so the value truly shows on all rows; a later
per-item edit still overrides one row. Stored evidence is untouched.

*(The Decision Queue offers a related, narrower operation — set a COO only on the rows that have
none — implemented as a sequence of per-row PATCHes, reporting how many succeeded if one fails.)*

### 8.7 Shipment totals from this screen

The pin-conflict banner posts `POST /shipment-totals` prefilled from the engine's `remediation`.
**It always posts `weight_unit: "KGM"`** — the banner's figure is kilograms because every engine
remediation and the Gross column are kilograms, while the Critical Review form's figure is in the
shipment's own unit. Posting a kg number under a shipment unit of `LB` stores a *smaller* authority
than intended and the block never clears; under `T` it stores 1000× and reaches Field 35.

### 8.8 Evidence-change reset (scoped to this screen's state)

After any document (re)extraction, role decision or removal:

| Changed role | Discarded from the overlay |
| --- | --- |
| `INVOICE` | all `field_edits`, `coo_all`, `tombstones`, `ordered_item_ids`, `bms_edits`; active manual rows deactivated (kept for audit, marked `superseded_by_extraction`) |
| `AIR_WAYBILL` / `PACKING_LIST` | `shipment_override` **and** the weight/carton **pins only** — description/COO/quantity/price edits survive, because the invoice did not change |
| `BANKING` | nothing |

**Explicit HS selections are folded, not discarded**: `description_key → {code, source, at}` moves
into `job.hs_history` and is re-proposed through the cascade at `HISTORY` confidence for
re-confirmation. Regime/office selections live outside this overlay entirely.

Why: the overlay's item channels are keyed by positional `src:` ids, which can silently re-bind to
different physical rows once the item list changes. A `reset_notice` records the revision it was
written at, so the reviewer's next mutation retires it automatically.

---

## 9. Brand · Model · Size grid

A collapsible bar under the table opening a spreadsheet-style editable grid over three columns.

**The one channel that invalidates nothing.** The values never reach the customs XML, so the
declaration, the XML artifact and the job status all survive; `revision` is **not** bumped, so the
review fingerprint stays valid and a generated XML is not staled. Only the `.xls` is rebuilt, and the
stored declaration's export columns are kept in step by sequence.

| Interaction | Behaviour |
| --- | --- |
| Tab / Enter / arrows | move the selection |
| F2 or Enter | edit in place; typing a character replaces the cell |
| Ctrl/Cmd + D | copy the cell above (Excel fill-down) |
| Delete / Backspace | clear → falls back to the **automatic** value, not to nothing |
| Paste | a whole Excel block; cells outside the grid are ignored **and counted in the report**, so a paste one row too low tells you instead of silently dropping data |
| Fill | choose column + value + row range (`1-40, 55` or `all`) |
| Save / Revert | edits are staged locally and highlighted until one bulk write; Revert discards them |
| Copy all (TSV) | whole grid to the clipboard |
| Download .xls | appears once the workbook has been built at finalize |

Server limits: ≤ 5000 rows per request (422 `BMS_EDIT_TOO_LARGE`), ≤ 120 chars per value, at least one
of brand/model/size per entry (422 `BMS_EDIT_EMPTY`), each entry an object (422 `BMS_EDIT_INVALID`),
unknown item → 404 `ITEM_NOT_FOUND`. An empty value **removes** the override.

Workbook: one sheet, bold header row, columns `SN · BRAND · MODEL · SIZE`, rows sorted defensively by
`xml_item_sequence` so the workbook can never disagree with the XML.

**Paste counting caveat worth copying:** count clipped cells **synchronously**, outside the state
updater. React may run an updater later, or twice, so a counter incremented inside it reads back as 0.

---

## 10. The finalize block

Rendered at the bottom of the same card.

### 10.1 Pre-flight (client-side, never gates the button)

Everything in it is knowable before the round trip, so the reviewer should not spend a finalize to
find it out. The server stays the authority on what actually blocks.

| Row | Condition | Action |
| --- | --- | --- |
| ✔/✖ HS coverage | rows with no `final_hs` | "show the rows ↑" → sets the Needs-HS filter and scrolls to the queue bar |
| ⚠ low-confidence | rows with the LOW chip | "review the rows ↑" |
| ⚠ flagged | rows carrying an engine note | "review the rows ↑" |

### 10.2 What finalize runs

```
manual HS overrides (DB-gated) → weight/carton allocation → supplementary units
  → freight allocation → insurance allocation → valuation → blocking validation → XML + .xls
```

It uses the values confirmed in Critical Review, not the documents' originals.

### 10.3 Unresolved-HS entry table

When the declaration comes back not-ready with items missing an HS, a table lets the reviewer type an
11-digit code (or an 8-digit prefix the database can complete) per item and finalize again. These are
**sequence-keyed** `hs_overrides` and therefore require the review fingerprint — without it the
request is refused with `HS_OVERRIDES_REQUIRE_FINGERPRINT`, because sequences shift on add/delete and
an override could land on the wrong item.

### 10.4 Warn mode

Blocking cases normally still produce an XML so the reviewer can test it in real ASYCUDA. The
response carries `xml_built_with_blockers: true`; the download is labelled **(test — has warnings)**
and a pop-up lists every unresolved case. Job status becomes `VALIDATION_BLOCKED` and the audit
records `XML_BUILT_WITH_BLOCKERS` with the code list.

`WARN_MODE_HARD_CODES` can never be bypassed — no XML is built at all:

```
GROSS_ALLOCATION_IMPOSSIBLE        REVIEWED_GROSS_EXCEEDS_AUTHORITY
REVIEWED_GROSS_TOTAL_MISMATCH      WEIGHT_RECONCILIATION_IMPOSSIBLE
REVIEWED_CTN_EXCEEDS_AUTHORITY     REVIEWED_CTN_TOTAL_MISMATCH
CARTON_LATTICE_VIOLATION           CARTON_RECONCILIATION_FAILED
INVOICE_DUPLICATE_DOCUMENT
```

Rationale: warn mode exists so a reviewer can test an *otherwise-complete* XML. With no item weights
there is nothing to test and every line would assert a zero gross weight. A package count that does
not reconcile, or sits off the lattice, is rejected by ASYCUDA rather than tested by it. A duplicated
invoice is not incomplete but **wrong** — value and duty overstated 2×, and invisible in the item grid
the reviewer checks.

### 10.5 After a successful build

Download buttons for the XML and the `.xls`, a KPI strip (total invoice FOB, external freight, total
CIF in NPR, gross weight in kg), collapsible warnings, and a preview of the first 12 items in
preserved invoice order.

**Any later item mutation invalidates the XML** — the artifacts are deleted and the reviewer must
rebuild.

---

## 11. Item-scope message catalogue

### 11.1 Blocking (item-scope)

| Code | Meaning |
| --- | --- |
| `HS_MANUAL_REVIEW` | no official HS11 found for this item |
| `HS_REVIEW_REJECTED` | a stored reviewed HS is not an exact official code — selection ignored |
| `HS_GUESS_UNCONFIRMED` | HS auto-selected from the description and never confirmed by a human |
| `COO_UNRESOLVED` | neither item origin nor exporter country maps to an alpha-2 |
| `SUPPLEMENTARY_QTY_INVALID` | supplementary quantity ≤ 0 (or KGM with no net weight) |
| `WEIGHT_RECONCILIATION_IMPOSSIBLE` | item net not < gross, or Σ gross ≠ authorised total |
| `CARTON_RECONCILIATION_FAILED` | Σ packages ≠ authorised total |
| `CARTON_LATTICE_VIOLATION` | a carton value is off the 0.01 lattice |
| `GROSS_ALLOCATION_IMPOSSIBLE` / `CARTON_ALLOCATION_IMPOSSIBLE` | fixed values exceed the authority — nothing assigned |
| `REVIEWED_GROSS_EXCEEDS_AUTHORITY` / `REVIEWED_GROSS_TOTAL_MISMATCH` | pins overrun the gross authority |
| `REVIEWED_CTN_EXCEEDS_AUTHORITY` / `REVIEWED_CTN_TOTAL_MISMATCH` | pins overrun the carton authority |
| `MANUAL_ITEM_INCOMPLETE` | reviewer-added row missing description or quantity |
| `ITEM_NET_EXCEEDS_SHIPMENT` | one item's net exceeds the whole authorised gross |

### 11.2 Warnings (item-scope)

| Code | Meaning |
| --- | --- |
| `HS_HISTORY_APPLIED` | HS proposed from a previously confirmed selection for this item name |
| `HS_SEMANTIC_GUESS` | no invoice HS; code auto-selected by description match |
| `COO_VENDOR_PROFILE` | origin taken from this exporter's remembered default |
| `COO_EXPORTER_FALLBACK` | origin taken from the exporter's country |
| `SUPPLEMENTARY_ASSUMPTION` | a proxy or conversion assumption was used |
| `PACKING_MATCH_BY_CODE` / `PACKING_MATCH_LOW_CONFIDENCE` / `PACKING_MATCH_AMBIGUOUS` | how (or whether) a packing row was matched |
| `PACKING_ITEMS_UNMATCHED` / `PACKING_ROWS_UNMATCHED` | named items/rows with no counterpart |
| `PACKING_CTN_TOTAL_MISMATCH` | row cartons do not sum to the printed total |
| `PACKING_WEIGHT_TYPE_INFERRED` / `PACKING_WEIGHT_UNIT_UNKNOWN` | weight column read with an assumption, or an unusable unit |
| `DESCRIPTION_WEIGHT_UNCERTAIN` | a description conversion was demoted to LOW, with the reason |
| `DESCRIPTION_NET_REJECTED` | a parser-derived net was dropped to make the allocation feasible |
| `NET_WEIGHT_SOURCES_DISAGREE` | two sources differ by more than 10× |
| `GROSS_EXCEEDS_INVOICE_WEIGHT_CAP` | the `net × 1.2` ceiling had to be released |
| `NET_TO_GROSS_RATIO_IMPLAUSIBLE` / `NET_TO_GROSS_RATIO_OVERRIDDEN` | ratio sanity / ADR-003 override in force |
| `WEIGHT_BASIS_QUANTITY` / `WEIGHT_BASIS_VALUE` | which fallback basis was used |
| `WEIGHT_GAP_INVISIBLE` | a weight difference that would not show at display precision |
| `ITEM_WEIGHTS_REVIEWED` / `ITEM_CARTONS_REVIEWED` | pins are in force on this item |
| `REVIEWED_CTN_OVERRIDES_PACKING` | a pin contradicts a printed carton count |
| `CTN_PIN_RESCALED_PACKING_EVIDENCE` | packing-stated cartons were proportionally rescaled, with the factor |
| `CTN_DONORS_ON_FLOOR` | how many rows a pin pushed onto the 0.01 minimum |
| `ITEM_UOM_MISSING` | the invoice printed no readable unit for this row |
| `ITEM_WEIGHT_UNIT_UNKNOWN` | printed weight unit unrecognised → source disqualified |
| `INVOICE_UOM_WEIGHT_AMBIGUOUS` | `MT` / `T` / `G` used as a mass unit of sale |
| `ITEM_UNIT_WEIGHT_IMPLAUSIBLE` | per-unit weight outside plausible bounds |
| `ITEM_TOTAL_ESTIMATED` / `ITEM_TOTAL_UNPARSED` / `ITEM_PRICE_ZERO_SUSPECT` | line-total quality flags |
| `DESCRIPTION_ANNOTATION_TRIMMED` / `DESCRIPTION_EXTRA_INFO` / `DESCRIPTION_CODE_ONLY` | description cleaning outcomes |
| `MODEL_BARCODE_ONLY` | the only model candidate was a GTIN/EAN barcode |

### 11.3 Message grouping

Item messages are grouped for display: one message per distinct code, re-scoped to `JOB`, naming up
to 20 affected SNs (`"37 item(s) — SN 3, 7, 12 … "`). A single-item message keeps its item anchor so
the row's own ⚠ still resolves. Without grouping a 200-row invoice emits 200 identical lines.

---

## 12. Item → XML map

| Row value | XML element (inside `<Item>`) |
| --- | --- |
| `ctn` | `Packages/Number_of_packages` |
| (job-level package type) | `Packages/Kind_of_packages_code`, `_name` |
| (job-level incoterm) | `IncoTerms/Code`, `/Place` |
| `final_hs[0:8]` | `Tarification/HScode/Commodity_code` |
| `final_hs[8:11]` (else `000`) | `Tarification/HScode/Precision_1` |
| — | `Precision_2..4` = `<null/>` |
| (job-level Box 37) | `Tarification/Extended_customs_procedure`, `National_customs_procedure` |
| `sup_unit` / `sup_name` / `sup_qty` | first `Supplementary_unit` block; two more blocks emitted empty |
| `total_price` | `Tarification/Item_price` |
| valuation | `Tarification/Value_item` = `"extF+intF+ins+other-ded"` (national, thousands-formatted) |
| `coo` | `Goods_description/Country_of_origin_code` |
| official HS description (else `description`) | `Goods_description/Description_of_goods` |
| `description` | `Goods_description/Commercial_Description` |
| (job-level Field 40) | `Previous_doc/Summary_declaration` — identical on every item |
| first-item banking texts | item 1 only: `Previous_doc/Previous_document_reference`, `Free_text_1` |
| `gross` | `Valuation_item/Weight_itm/Gross_weight_itm` |
| `net` | `Valuation_item/Weight_itm/Net_weight_itm` |
| derived | `Total_cost_itm`, `Total_CIF_itm`, `Statistical_value` (6 dp), `Alpha_coeficient_of_apportionment` (15 dp), `Rate_of_adjustement` = `1` |
| derived | `Item_Invoice`, `item_external_freight`, `item_insurance` (each national + foreign); `item_internal_freight`, `item_other_cost`, `item_deduction` = `0.00` |
| — | eight empty `Taxation_line` stubs per item |
| `brand` / `model` / `size` | **never in the XML** — the sibling `.xls` only |

Per-item valuation formulas:

```
item_invoice_national     = line_total × rate
item_external_freight_nat = item_freight × rate
Total_cost_itm            = ext_freight_nat + insurance_nat
Total_CIF_itm             = item_invoice_national + Total_cost_itm
Statistical_value         = Total_CIF_itm
Alpha_coefficient         = Total_CIF_itm / Total_CIF
```

Freight and insurance are apportioned across items by the configured basis (`cost_allocation_basis`:
invoice-value share, matching the sample XML, or gross-weight share), rounded to 2 dp with the
residual applied to the largest share so the sum is exact.

---

## 13. Acceptance checklist

1. Item order is invoice order — never re-sorted by any other key.
2. The evidence is never mutated; every reviewer change is an overlay record replayed from scratch.
3. `item_id` is server-owned and stable; a deleted identity is never reactivated.
4. `ordered_item_ids` is always an exact duplicate-free permutation of the active ids.
5. Every mutation except brand/model/size invalidates the declaration and the XML, bumps the revision
   and stales the review fingerprint.
6. Only an exact official 11-digit HS can become final, through either reviewer channel; an
   unconfirmed description-guess HS blocks finalize.
7. HS search ranks the official description only, caps query tokens, and gates short/numeric queries.
8. COO never emits `XX`/`ZZ`/`UNKNOWN`/empty, and `NA` is Namibia.
9. Packing matching is by identity with the ambiguity and measurement gates, assigned best-first
   across the document.
10. `Σ gross == authority` and `Σ CTN == authority` exactly; `net < gross` strictly on every row.
11. Cartons are on the 0.01 lattice; off-lattice input is rejected, never quantized.
12. A pin is kept exact; gross redistributes proportionally, cartons absorb into ≤ 10 estimated
    donors first and rescale packing rows as a set only when forced — and say so.
13. Clearing a pin (empty string) restores the computed value; an unchanged value never becomes a pin.
14. Ratio nets derive from the **final** gross and round down.
15. Infeasible allocation assigns nothing, preserves pins, and carries a remediation the UI offers as
    a one-click shipment-totals change.
16. Every blank cell is accompanied by a message explaining it — no allow-list filtering.
17. The supplementary unit code always follows the HS tariff unit; the quantity override replaces
    only that number.
18. Brand/Model/Size edits never bump the revision or invalidate an XML; a cleared cell falls back to
    the deterministic value.
19. Filters and search are presentation-only; the totals row and the XML always read every row.
20. Re-extraction resets the role-scoped overlay channels and folds explicit HS selections into
    name-keyed history rather than re-binding them by position.
