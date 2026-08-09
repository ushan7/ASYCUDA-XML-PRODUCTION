# Weight and carton allocation — authoritative specification

**Implemented by:** `backend/app/rules/weight_carton.py` (allocation, reconciliation),
`backend/app/rules/packing_match.py` (matching), `backend/app/rules/description_weight.py`
(description conversion), `backend/app/extraction/table_parser.py` +
`backend/app/extraction/validator.py` (where packing evidence comes from, §5b) and
`backend/app/review/packing_view.py` (the reviewer's view of it). Those modules and this file are
one unit — change them in the same commit.
**Supersedes:** all earlier allocation rule documents. Where this file and an older
document disagree, this file wins.

> A commit hash used to stand here as the status line. It named `04069d9` while five later
> commits had already changed the behaviour, which is the same failure mode this file exists
> to prevent — so the pointer is now to the code, and §11 below is the only place that
> records divergence.

This spec lives in the repository on purpose. The net-to-gross ratio drifted from 0.3 to 0.7
and back for months because the authoritative rules lived in a document with no connection to
the code they govern — nothing forced them to agree and nothing showed when they diverged.
**Change this file in the same commit that changes the behaviour.**

---

## 1. Definitions

| Term | Meaning |
|---|---|
| Invoice items | the item rows from the commercial invoice |
| Packing list rows | the item rows from the packing list |
| Authorized total gross | the final gross weight for the consignment — source of truth |
| Authorized total carton | the final carton count for the consignment — source of truth |
| CTN | carton. Fractional values allowed; **every final item CTN is at or above 0.01 AND an exact whole multiple of 0.01** |

Net weight must always be strictly less than gross weight, for every item.

### The carton lattice is a MUST rule (2026-08-03)

Every declared carton count — allocated, estimated or reviewer-entered — is a whole multiple of
`0.01` and never below `0.01`. So is the authorised total: a sum of lattice values can only ever
equal a lattice value, which is why `set_shipment_totals` rejects a package total off it instead of
quantizing quietly. `apportion` has always produced lattice values by construction, but a rule
nothing checks is a rule that silently stops being true, so `_carton_lattice_check` asserts it after
every allocation (`CARTON_LATTICE_VIOLATION`) rather than trusting it.

The rule is what makes reviewer carton pins exact: on a fixed lattice the difference a pin creates
is a whole number of `0.01` units, and redistributing whole units is integer arithmetic with no
rounding anywhere. The one place it does not apply is a shipment with **no package authority at
all** — there every CTN is `0`, which is the explicit "no packages declared" state rather than a
value on the scale.

### Authority ladder for the shipment totals

```
1. reviewer-corrected shipment totals   (set_shipment_totals overlay)
2. HAWB
3. remaining airwaybill_authority ladder (TRUE_DO -> TRACKING -> SINGLE_AWB -> PACKING_LIST)
4. none -> BLOCKING: the reviewer must enter the totals
```

Reviewer-corrected totals outrank the HAWB: the system suggests, the user decides, and their
choice is final.

---

## 2. General order rule

The XML goods items follow the invoice item order **exactly**. Never sort by packing-list order,
alphabetically, by HS code, by carton, or by weight. The original invoice line index is preserved
and the XML rows are emitted in that order.

Implementation: `invoice_authority` assigns `xml_item_sequence` in invoice order; `weight_carton`
computes in place; `packing_match` returns a dict keyed by sequence, never a list.

---

## 3. Item matching

Packing-list rows are matched to invoice items **by normalized product identity, never by row
number**.

Normalization lowercases and strips all non-alphanumerics, so spacing, punctuation and separators
(`-`, `/`, `_`) stop mattering while meaningful identifiers (size, model, colour, `500ml`, pack
size, brand, variant) survive inline. Items whose size, variant, model, colour or pack quantity
differ therefore never merge.

### The matching ladder

| Rank | Rule | Confidence | Reported |
|---|---|---|---|
| 1 | exact normalized description equality | 1.00 | — |
| 2 | product / part / item-code equality (packing `item_code_raw` / `model_raw` against the invoice's model or description) | 0.95 | `PACKING_MATCH_BY_CODE`, naming every pairing |
| 3 | scored description similarity | ≤ 0.90 | `PACKING_MATCH_LOW_CONFIDENCE`, naming every pairing |

A **measurement is not an identifier**: `500ML`, `3000W`, `80GSM`, `1200X600` pass a naive
letters-and-digits test, and a pack size printed in both documents' text paired a dishwash line
with a floor-cleaner line at 0.95 with no warning. Digit-led unit-tailed tokens and dimension
chains are rejected as codes, and rung 2 reports itself exactly like rung 3 — it reassigns an
item's whole weight on one token and was the only rung with no message.

Exact equality is the right *first* rule and the wrong *only* rule: two people type the same goods
onto two documents, and `Shampoo Bottle 500 ml` against `500ML SHAMPOO BOTTLE` cost the entire
match. A missed match is invisible — the item silently falls to an **estimated** weight instead of
the one the packing list printed.

Rank 3 is gated twice, because a wrong match is worse than none:

- **measurements must agree.** Word overlap is scored only after checking that no measurement of the
  same kind differs (`500 ml` vs `250 ml`, `2.25x15` vs `2.25x22`). This is what enforces the spec's
  "never merge items whose size, variant, model or pack quantity differs".
- **the winner must be clear.** A best score within 0.10 of the runner-up is *ambiguous*: the row is
  left unmatched and reported as `PACKING_MATCH_AMBIGUOUS`. Guessing between two candidates is worse
  than saying which two.

Rank-3 pairs are scored once and assigned **best-first across the whole document**, never in packing
row order. Assigning in row order lets a 0.62 match earlier in the file claim the invoice item that
a 0.95 match later in the file describes — the pairing would then depend on the order the supplier
typed their packing list, which is precisely what "match by identity, never by row" exists to
prevent.

An invoice line claimed by an exact or code match is never re-claimed by a similarity match.
Similarity confidence is capped below 1.00 so that a perfect word-overlap score still reads as a
proposal in the audit trail, not as certainty.

### Grouping and shared cartons

- Repeated packing rows of the same normalized item are **grouped and summed before** any assignment.
- Rows sharing a carton group contribute the group total **once**, divided among the group's items
  by row quantity (else equally), without changing the group total. The divided shares add back to
  the group total **exactly** — the last member takes the remainder, because a three-way split of 5
  cartons is 1.666… and three of those is 4.999…8.
- **Rows printing different values already carry their own split** — their sum IS the group total,
  and nothing may override a printed figure.
- **A repeated value is ambiguous; a carton range settles it.** Rows in one group printing the same
  number are either each repeating the group total (`2, 2, 2` = 2 cartons) or each stating their own
  single carton (`1, 1, 1` = 3 cartons), and nothing in the numbers distinguishes them. When the
  group id names a range — `1-5` — that range **is** the count, whatever the rows repeat. The id
  must be **essentially nothing but the range** (an optional C/NO-style prefix, then digits-dash-
  digits): `PO-1001-2024` is a purchase order whose hyphens span 1024, and reading marks, lot
  numbers or dates as ranges replaced printed carton counts with numerology.
- **Groups are per document.** Carton numbering restarts in each uploaded packing list, so two
  suppliers' groups both labelled `1-5` are two physically distinct sets of cartons — merging them
  counted five cartons where the consignment held ten.
- When several invoice lines share one identity, grouped values split proportionally by invoice
  quantity, else by invoice line value, else equally.

---

## 4. Initial basis

**Conditions 1 and 2 are independent.** They are not a choice: a packing list that prints both
item weights *and* item cartons — the ordinary case — governs both. `have_carton` used to read
`(not have_gross) and …`, so any printed gross weight anywhere threw away every printed carton and
re-derived the carton column from weight.

### Condition 1 — packing list gives item-wise gross weight

Packing gross is the provisional gross basis. Repeated rows are summed first, then mapped.

### Condition 2 — packing list gives item-wise cartons

Cartons come from the grouped, shared-aware packing cartons.

### Whichever the document does not state

Derived from the other: cartons proportional to gross, or gross proportional to carton share.

### Neither

Value share when a packing list was uploaded; **quantity share** when none was. Cartons follow
weight share.

### Items the packing list does not mention — per item, never zero

Conditions 1 and 2 apply **per item, not per shipment**. An item with no packing match takes the
fallback shape of "Neither" above — invoice value share, or quantity share when no packing list was
uploaded — **scaled by the matched items' density**:

```
density        = sum(matched packing basis) / sum(matched fallback basis)
unmatched[i]   = density x fallback[i]
```

With no usable fallback among the matched rows, the matched **mean per item** is used instead.

A basis of `0` is forbidden here. Zero does not mean "unknown" to an apportionment, it means "no
weight": the item lands on its floor and is declared at `0.001` kg gross / `0.000` kg net, with
`net < gross` satisfied, the totals reconciling exactly and `validation_status: OK`. Three equal
invoice lines with one matched produced `299.998 / 0.001 / 0.001` against a 300 kg authority, and
no warning was emitted at any level.

Every unmatched item is named in `PACKING_ITEMS_UNMATCHED`, its `allocation_audit`
`gross_weight_source` reads *estimated from … (no packing-list match)*, and the estimate is never
presented as document evidence.

**The same rule applies to the carton column.** An item with a packing gross but no printed carton
takes its carton share scaled by what the stated rows carry per kilogram (`estimated from weight
share (no packing-list carton)`). This path only became reachable when the two conditions stopped
being mutually exclusive — before, a shipment with any packing gross never used packing cartons at
all, so the zero-basis trap could not fire on that side.

Packing rows that match no invoice item are reported as `PACKING_ROWS_UNMATCHED` with their
product names — matching is by product name, so the usual cause is a wording difference.

---

## 5. Final override ladder

Applied after the initial basis. **Later application = higher authority.** Highest first:

| Rank | Source | Sets |
|---|---|---|
| 1 | reviewer pin (Detailed Review edit) | fixed net and/or exact gross, and/or exact **CTN** |
| 2 | invoice-printed item weight | fixed net; gross basis = net x 1.2 |
| 3 | invoice **quantity in a mass unit** — the line is sold by weight | fixed net; gross basis = net x 1.2 |
| 4 | packing-list item net | fixed net (gross basis stays the packing gross) |
| 5 | invoice **description** conversion (HIGH, then LOW confidence) | fixed net; gross basis = net x 1.2 |
| 6 | ratio | net = **0.7 x final gross** — see §6 |

### The 1.2 is a CEILING on invoice-stated weights, not only a weighting (2026-08-04)

For ranks 2 and 3 — the invoice's printed weight column, and a quantity sold in a mass unit —
`net x 1.2` is a **hard cap on the final gross**, not merely the provisional basis.

`net x 1.2` was previously only a proportional weighting, and §6's exact-sum rule rescales every
share against the authorised gross. On a three-line consignment that meant:

    EPOXY RESIN 500 KGM   net 500 kg (stated)   ->  gross 1636.364 kg     (3.3x the stated net)
    PLASTIC CRATE 200 PCS                       ->  gross  545.454 kg
    STEEL CLAMP   300 PCS                       ->  gross  818.182 kg

Two heavier unweighed lines pulled the whole basis upward and the weighed line went with it. A
stated weight is evidence, not a hint: 500 kg of resin does not become 1.6 tonnes because the
shipment also carries crates. With the cap the same input gives `600.000 / 960.000 / 1440.000` —
the weighed line pinned at exactly `1.2 x 500`, the released weight absorbed by the lines the
invoice says nothing about, and the total still exactly 3000.

**Only invoice-stated nets are capped.** A packing-list net has its own printed gross beside it
(that pair is the shipper's measurement, not a ratio we impose); a ratio net is `0.7 x gross` by
construction; a reviewer pin is exact and supreme.

**When the cap cannot hold**, the authorised gross and the invoice's own weights genuinely
disagree. The declaration must still add up, so the cap is released and
`GROSS_EXCEEDS_INVOICE_WEIGHT_CAP` names every capped item and its maximum. Silently breaking the
cap, and silently refusing to allocate, are both worse than saying the two sources conflict.

Implementation: `apportion` takes an optional per-item `ceiling` and cap-and-redistributes to a
fixed point (releasing weight onto the uncapped items can push one of THEM over its own cap, so
one pass is not enough).

### Ranks 4 and 5 swapped — a printed weight beats a parsed name (2026-08-04)

The description conversion used to sit ABOVE the packing-list net. It does not any more, on the
user's rule: **a weight the packing list prints outranks a weight inferred from a product name.**

What the old order did, on a four-line consignment whose packing list stated every weight:

    VACUUM FLASK 500ML  x 100   packing list prints  720.00 kg net
                                description reads "500ML" -> 500 ml x density 1.00
                                -> 50 kg net, at LOW confidence, and it WON

A product name is not a weight statement. `500ML` is the article's capacity, not the mass of what
is in it, and at LOW confidence the parser is saying so itself. The packing list, meanwhile, is a
shipping document whose whole purpose is to state what each line weighs.

The damage was not confined to the misread lines either. Because the exact-sum absolute (§6) must
hold, the gross freed from those two lines was pushed onto the remaining two — so items nobody had
misread also stopped showing their printed packing figures. One bad reading moved every weight on
the declaration.

**Invoice-stated weight still overrides everything below the reviewer pin.** Ranks 2 and 3 are
untouched and stay above the packing list: when the invoice itself states an item's weight — as a
weight column, or by selling the line in a mass unit — that figure governs, for the items that have
one, and items without one fall through to the packing list as before. The reviewer pin stays rank
1: an extracted number never overrides a human's explicit entry.

### Rank 3 — a line invoiced by weight states its own net (2026-08-04)

`RESIN 500 KG` sells five hundred kilograms of goods: the quantity column **is** that line's net
weight, at LINE_TOTAL scope — not a per-unit figure to multiply by anything. Before this it took a
ratio net (0.7 × an allocated gross) while the invoice stated the number outright, and on a KGM
tariff code that meant the *supplementary quantity* was derived from a figure nobody had written
down.

- **Every recognized mass unit counts**, through the one ingest boundary (`units.to_kg`). Matching
  only the literal `KG` would miss `5 MT` and under-declare it a thousandfold.
- **A blank unit is never accepted.** `to_kg` reads a blank as already-kilograms, which is correct
  for a weight column and completely wrong for a *quantity* column, where blank means pieces far
  more often than kilograms. The extraction's own default for a missing UOM is `PCS`.
- **`MT`, `T` and `G` are used but flagged** (`INVOICE_UOM_WEIGHT_AMBIGUOUS`). `MT` is metric ton on
  most invoices and METRE on some, and 500 metres of webbing read as 500 000 kg inverts a
  consignment. The description parser already treats these tokens as ambiguous; a unit-of-sale
  column is a stronger signal than free text, so they convert here rather than being refused — but
  never silently. Same doctrine either way: ambiguity is reported, not resolved out of sight.
- **It sits below the printed weight column and above the description parser.** A column headed
  `NET WEIGHT` is a direct statement of net; a mass unit of sale is a very strong inference that is
  usually the net and occasionally a billing or chargeable weight. Both are invoice sources, so
  both outrank the packing list regardless.

The 10× cross-check now compares the **chosen** net against every other reading rather than one
fixed pair: with three invoice-side sources a fixed pair stays silent on exactly the combinations
the newest source introduces.

**The invoice-printed weight is the highest non-reviewer authority** (user rule 2026-07-30). It is
a figure a person put on the commercial document; a description conversion is a parser reading free
text. A HIGH-confidence conversion used to outrank it, which put the least reliable source in the
top slot and rested the whole design on one confidence gate.

The conversion is now a **cross-check** instead. When both exist and disagree by more than 10x,
`NET_WEIGHT_SOURCES_DISAGREE` names both figures and their sources on that item: an order-of-
magnitude disagreement is a unit or pack-multiplier error, not a judgement call, and whichever the
ladder picks the other one was wrong by 10x. The stated value is still the one used.

### A description net that cannot be true is rejected, not blocked

Rank 4 is a parser reading free text; every other rank is a human or a document. So when
the derived nets cannot fit the authorized gross, the description nets are dropped — worst offender
first, until the apportionment is feasible — and those items fall to the ratio, with a
`DESCRIPTION_NET_REJECTED` warning naming each rejected line, its value and its source.

**A rejected item is re-based on its QUANTITY share of the authorized gross** (user rule
2026-07-22), not on the shipment's ordinary fallback. It has no weight evidence left, and quantity
predicts weight far better than price does: invoice-value share systematically under-weights a
cheap bulky line and over-weights an expensive light one.

The share is *rescaled*, because the surviving items may be on packing kilograms or invoice value
and 5 pieces is not 80 dollars — raw quantity dropped into that basis would hand the shipment to
whichever item carries the larger magnitude. Scaling by the surviving basis-per-quantity,
`k = sum(prov[ref]) / sum(qty[ref])`, makes them commensurable, and the algebra cancels:

```
share_i = k·q_i / (k·Σq_rejected + Σprov_ref) = q_i / Σq_all
```

so the item takes **precisely** its quantity share whatever unit the rest of the shipment is
measured in, and the others keep their own evidence and split the remainder by it. With no
reference items left, every basis is already a quantity and `k = 1`.

**Exception: a packing-list weight for that same item wins.** A document beats a fallback, so an
item with its own packing row goes back on its packing basis, not the quantity share. The rescue is
for items with no evidence left at all.

Without this, one misread line makes the apportionment infeasible, and infeasibility assigns
nothing to **every** item: the whole table loses its gross and net, and each row then loses its
supplementary quantity too, because the KGM tariff unit is derived from the net. Four columns of a
24-row table emptied by one line is not a defensible failure mode.

A reviewer pin, an invoice-printed weight, a **mass-unit invoice quantity** and a packing-list net
still block. Those are stated values, and only the person who stated them may correct them. Nothing
is rejected while the pins alone already overrun the authority — that conflict is the reviewer's to
resolve and must not be hidden behind a second message.

Because every surviving net at that point is somebody's stated figure, `GROSS_ALLOCATION_IMPOSSIBLE`
carries a `remediation` naming the authority that *would* fit (`Σ pins + Σ floors`), and the
Detailed Review offers it as a one-click shipment-totals change — the same treatment a reviewer pin
conflict gets, one step further out. A blank table with no way forward is not a resolution.

### Confidence gate (`rules/description_weight.py`) — the approved boundary (2026-07-30)

The boundary below was reviewed and approved as stated; it is pinned case by case in
`tests/test_description_confidence_boundary.py`, and a case moving between buckets is a rule
change that belongs here in the same commit.

| Bucket | When |
|---|---|
| **HIGH** | unambiguous mass or volume token; **known** density (a matched band, not the unknown-liquid fallback); pack multiplier consistent with the invoice UOM; GSM / denier / tex / kg-per-m / g-per-m with every variable present |
| **LOW** — used, flagged, never top authority | assumed density; ambiguous token (`g`, `l`, `cc`, `dl`, `dal`, `pt`, `qt`, `cup`, `tsp`, `tbsp`, `mt` collide with gauge, size and model codes); unverifiable pack multiplier; package-only weight; a US/Imperial-ambiguous unit (`gal`, `qt`, `pt`, bare `fl oz`, `cup`, `tsp`, `tbsp` — the US value is used and said so); a space-thousands reading (`1 250 KG`); a context-suppressed multiplier |
| **REFUSED** | ambiguous token **and** assumed density; dosage forms; concentrations and **rates** (`250 MG/5 ML`, `100 LTR/MIN`); **container/appliance capacities** (`WATER TANK 1000 LTR` — the goods are the tank); **numeric ranges** (`25-50 KG` — the value next to the unit is the range's upper bound); `CBM`/`m³`; length or area alone; `ST`; bare `oz` on a liquid or cosmetic with no net marker |

On the compound refusal: two guesses do not make a weight — nothing in the text says this is a
volume, and nothing says what liquid it would be. `Stainless Steel 316 L` is a steel grade; read as
316 litres x 1.00 kg/L it declares 316 kg of surgical suture per piece. Same rule as an
unrecognized printed unit (Units, below): the source is disqualified, never assumed. An unambiguous
token (`ml`, `litre`) still converts on an assumed density, and a known density still converts an
ambiguous token. Every LOW result names its demotion reason; a HIGH result carries no warnings.

Every demotion writes a `DESCRIPTION_WEIGHT_UNCERTAIN` warning naming the item and the reason.
Ambiguity is never resolved silently.

**Pack multipliers are gated on what the invoice quantity counts.** `24 x 500 ML` multiplies the
line only when the UOM counts packs (CTN/CASE/PACK/BOX/BAG/DRUM/DZN). When it counts pieces
(PCS/EA/NOS/UNIT/SET), the quantity already *is* the bottle count and the multiplier is not
applied — otherwise a 72 EA line is declared 24x too heavy.

`ST` is deliberately absent from the mass table: on a commercial invoice it means set / sterile /
Stück far more often than stone, and 6.35 kg per unit is enough on its own to invert a consignment.
The spelled-out `stone` remains. Dosage strengths (`500 mg tablets`) and `CBM`/`m3` never convert.

### Reading the description: what the parser must not do

Rules born from demonstrated failures, each of which produced a wrong number **at HIGH
confidence** before it existed (the 2026-07-30 adversarial audit added the second group; every
case is pinned in `tests/test_adversarial_regressions.py`):

- **A dozen multiplier has no stated per-piece value, so it never lands on a package weight.**
  `1 DOZEN PER POLYBAG, CARTON NET 6 KG` × 100 cartons is 600 kg; ×12 made it 7 200.
- **A dimension chain is not a pack.** `MASTER CARTON 40 X 30 X 20 CM` printed before
  `12 X 1 KG POUCHES` stole the binding and dropped the real multiplier. Three-part chains with a
  length unit (or none) are masked; with a mass/volume unit they are a NESTED pack (`4 x 6 x
  1.5 LTR` = 24 × 1.5 L).
- **Reversed and slash pack notations carry the multiplier.** `1 LTR X 12`, `500 ML X 24`,
  `24/250ML` are the same goods as `12 X 1 LTR` and must convert identically.
- **A rate is a ratio, not a content.** `100 LTR/MIN` is masked with the concentrations.
- **A capacity is not a cargo.** A volume on goods that ARE a container or appliance (tank, pump,
  dispenser, heater…) is refused outright.
- **A range refuses.** `25-50 KG` states a size band; declaring its upper bound is a 2× over-
  declaration.
- **A zero is never a weight**, and a space-separated thousands group is read whole at LOW
  (`1 000 ML` once converted as 0 kg at HIGH; `1 250 KG` as 250 kg).
- **Package context reaches back through the clause.** `TOTAL GROSS WEIGHT OF THE CONSIGNMENT
  INCLUDING PACKING 1250 KG` has its marker 48 characters from the number; a bare trailing clause
  (`…MASTER CARTON WITH INNER POLYBAG, 15 KG`) borrows the clause before the comma.

And the original four:

- **A thousands separator is not a decimal point.** `2,500 G` is 2.5 kg. Parsed by replacing `,`
  with `.` it was 2.5 g — a thousandfold under-declaration. Numeric tokens go through
  `numbers.parse_decimal`, the pipeline's one numeric boundary.
- **A pack multiplier binds to the value the pack names.** In `24 X 250 ML … CARTON NET 6 KG` the
  24 multiplies the bottles, not the carton's stated net weight; applied to the 6 kg it declared a
  600 kg line at 14 400 kg.
- **A weight next to CARTON / PALLET / MASTER / GROSS is the packaging**, unless a content word
  (NET, CONTENTS, CAPACITY) sits there too. `500 ML SHAMPOO IN 5 KG CARTON` is 0.5 kg of shampoo;
  `CARTON NET 6 KG` is six kilograms of goods. When the only figure printed *is* a package weight it
  is still used, at LOW confidence, saying so.
- **A concentration is not a content.** `250 MG / 5 ML` is a strength; it is masked out before any
  unit search, so the bottle's own volume can still be read from the rest of the line.

Per-length and per-area formulas (`kg/m`, `g/m`, GSM, denier, tex) are evaluated **before** the bare
mass search — `0.5 KG/M` contains `kg`, so the mass search claimed it first and those formulas were
unreachable. All of them now pass through the same confidence gate as everything else.

`gal`, `qt`, `pt`, `fl oz`, `cup`, `tsp`, `tbsp` differ between the US and Imperial systems: the US
value is used and the choice is stated in a warning. `SHORT/US TON` and `LONG/IMPERIAL TON` are
matched as multi-word units before the single-token table, so neither is read as a metric tonne.

### Units

Every weight is converted to kilograms at its **ingest boundary** (`app/units.py`) — invoice item
weight in `invoice_authority`, packing row gross/net in `packing_match`, and the reviewer-facing
figures in `packing_view` (which displayed `value_raw` under a `*_kg` label, so a row printed in
grams was shown as kilograms while the allocator used a value a thousand times smaller).

**A unit that is printed but unrecognized disqualifies its source.** The value is discarded, a
warning is emitted, and the next priority applies. It is never assumed to be kilograms. A silent
`factor = 1` default is what let `500 G` per unit become 500 kg and put an entire consignment's
net weight above its gross.

---

## 5b. Where packing evidence comes from

### Hard-won parser rules (2026-07-30 adversarial audit)

- **A row carrying data is never a header.** `| 6-10 | LEATHER GOODS | 50 | CTNS | … |` matched
  the `desc` and `ctn` header words, replaced the working column map mid-page, and silently
  deleted itself from a page the parser still owned. Any pure-numeric cell, or a qty|UOM data
  cell, disqualifies a header candidate — on both the packing and invoice paths.
- **A per-unit rate column maps to nothing.** `UNITS PER CARTON`, `PCS/CTN`, `N.W./CTN`, `KG/PC`
  are rates; mapping one to qty/ctn/weight declares a per-carton figure as the row's total. The
  qty key uses a narrower guard (package-word denominators only): `Qty./ Unit` is a combined
  quantity+UOM header, not a rate.
- **`TOTAL N.W.` / `G/W` map; `NET WT PER PIECE` does not.** The anchored abbreviations accept a
  TOTAL/TTL prefix and the slash form; the per-piece phrasing is caught by the rate guard.
- **Counts derived from carton ranges are never serials.** `1-2 / 3-5 / 6-9` derives 2, 3, 4 —
  consecutive by construction — and the serial heuristic destroyed it. Any row carrying a carton
  NUMBER exempts the document from that heuristic.
- **One canonical id per shared group.** Grouping is decided on a normalized key; publishing each
  row's own spelling (`1-5` vs `1 - 5`) let downstream split the group and count the range twice.
- **The printed total is parsed under the rows' locale.** An EU `12,000` parsed locale-blind as
  twelve thousand stood a correct parse down on a phantom mismatch.

### The deterministic parse must carry the weight columns

`table_parser._confirm_packing_row` emits a row's gross weight, net weight, carton count, carton
number, batch, expiry and origin — not just its description and quantity. It previously emitted only
the latter, **and a parser-owned page never reaches the LLM**, so every per-item weight and carton
printed on such a page was discarded. `have_gross` and `have_carton` were then both false and
allocation split the authorised gross by invoice **value** on a shipment whose packing list stated
every weight. That is the single largest cause of the "fell back to a proportional split" symptom.

Two rules make the added columns safe:

- **header-mapped only, never positional.** Reading a net column as gross is a silent declaration
  error no downstream check can see. A column is used only when a header names it. The header's own
  unit hint (`GROSS WT (KGS)`, `G.W. KGS`) applies to the whole column — accepted from two letters
  up, because the `G` in `G.W.` is the word "gross" and reading it as grams divides the column by a
  thousand.
- **a carton NUMBER is not a carton COUNT.** `C/NO 1-5` is five cartons with the identifier `1-5`;
  `CTN 7` is one carton numbered 7. Rows printed against the same carton number are marked as one
  shared group, so §3's division applies. A column reading 1, 2, 3, 4 … with no printed carton total
  to check it against is treated as numbers, not counts.

### The unlabelled weight column (live-job root cause, 2026-07-30)

Real packing lists routinely print ONE weight column headed just `Weight (KG)`. It is extracted as
`declared_weight` with `weight_type_raw = UNKNOWN` — and the allocator originally read only the
labelled fields, so a 115-row packing list stating every item weight was allocated by invoice
**value** share. The extraction was perfect; the evidence reached nobody.

Classification (`packing_match._classify_declared`), strongest evidence first:

1. a row-stated type (`weight_type_raw` GROSS/NET) wins outright;
2. the column's **sum against the document's own printed totals** — net checked first (net < gross
   always, so a column cannot match both): summing to the printed total net ⇒ the net breakdown,
   to the printed total gross ⇒ the gross breakdown, each within the 2% tolerance;
3. otherwise the column is still the best available weight **shape** (Condition 1 rescales to the
   authority anyway) — used, flagged as `SHAPE`.

Every inferred classification is reported as `PACKING_WEIGHT_TYPE_INFERRED`, naming the arithmetic
that decided it. A row with a labelled gross/net is never touched by the classification.

The invoice-side twin: the deterministic parser now emits `item_weight_raw` from a NET or bare
weight column (the spec's rank-2 net authority) — a GROSS column deliberately never, because a
gross is not a net.

### The document's own totals are the gate

A deterministic parse cannot invent a row, but it can read the wrong column. The packing list states
what its rows add up to, so `sum(rows)` is checked against the printed `TOTAL` row (read through the
same column map) or a labelled inline total, within 2%. On a mismatch the parse is abandoned
**wholesale** and the LLM path runs: a mismatch means the column map is wrong, and a wrong map is
wrong for every row on every page. The check runs only when the parser owns every page that holds
content — otherwise the sums are partial by construction.

`validate_packing` applies the same arithmetic to the finished extraction, whatever produced it
(`PACKING_SUM_MISMATCH`), plus per-row `net < gross` (`PACKING_ROW_NET_ABOVE_GROSS`) and a totals
line extracted as a goods row (`PACKING_TOTALS_ROW_EXTRACTED`). Whole-document only: a page window
holds part of the rows and sometimes all of the totals, so a per-window mismatch is arithmetic about
nothing — and would cost a full repair round to "fix".

### A budget that expires keeps what it has

Packing extraction runs against a time budget (`packing_extraction_budget_seconds`). Three outcomes,
and they are three states, not two:

| State | Marker | Allocation |
|---|---|---|
| complete | — | packing evidence, normally |
| complete but late | `PACKING_EXTRACTION_SLOW` | packing evidence, normally |
| partial | `PACKING_EXTRACTION_PARTIAL` | extracted rows used; **unreached items take the quantity share** |
| nothing usable | `PACKING_EXTRACTION_OVER_BUDGET` | quantity share throughout |

A late window used to raise, `[f.result() …]` re-raised at the first failing index, and every
sibling window's rows were discarded with it. Complete-but-late was stamped over-budget and thrown
away too. Both threw away evidence that existed and had been paid for.

**Partial is deliberately a third state.** Treating it as "packing list present" flips the
unreached items from the quantity share to the invoice-**value** share, which systematically
under-weights a cheap bulky line, and derives the density estimate from whichever rows happened to
finish first. `PACKING_ITEMS_UNMATCHED` also says *not reached before the time budget* rather than
*not found on the packing list* — the reviewer must not go looking for a wording mismatch that does
not exist. When the shipment authority itself came from a partial packing list, the reviewer is told
the total may be an interior subtotal (`PACKING_AUTHORITY_PARTIAL`).

A page that returns **no** rows at all is repaired by re-requesting that page alone
(`GAP_FILLED`), never by resending the whole window — nothing on an empty page can be duplicated. A
page that did return rows is never gap-filled: appending there risks duplicating a real customs
line, which is why automatic dedup was withdrawn in the first place.

---

## 6. Reconciliation

### The two absolutes

```
sum(item gross) == authorized total gross     exactly
sum(item CTN)   == authorized total carton    exactly
```

Therefore `net = gross x 1.2` and `net = 0.7 x gross` are **provisional bases only** — neither
factor survives to the XML literally. The override ladder decides the *shape* of the distribution;
the authority decides its *scale*.

### Ratio nets are derived from the FINAL gross

Not from a pre-reconciliation share. `net = r x gross` with `r < 1` is always below gross whatever
the reconciliation does, so:

1. Partition items into **fixed-net** (ladder ranks 1–5) and **ratio** (rank 6).
2. Floors: fixed-net items get `net + 0.001`; ratio items get one precision unit. A ratio item's
   net is not yet known and does not constrain the split.
3. Apportion the gross.
4. **Then** derive `net = ROUND_DOWN(0.7 x gross, 3)` for the ratio items.

This removes the apparent circularity and makes `net < gross` true by construction. Deriving the
ratio net from a provisional share leaves only a `1 - r` margin — 70% at r = 0.3, but only 30% at
r = 0.7, thin enough for reconciliation to invert.

It also means **infeasibility can only ever be caused by a stated value**: if every item is
ratio-mode the budget needs just `n x 0.001`, and a parser-derived net is rejected before the gate
(§5). What remains is a reviewer pin, an invoice-printed weight or a packing-list net — someone's
own number, which is exactly who the gate should be pointing at.

### `apportion` — one primitive, used for gross and cartons

```
apportion(total, basis[], floor[], places) -> values[] | None

  sum(values) == total     exactly at `places`
  values[i]   >= floor[i]  for every i
  None (infeasible)        when sum(floor) > total — never an approximation
```

Largest-remainder (Hamilton): floor every ideal share, then hand the remaining whole units to the
largest fractional remainders. The shortfall is spread across several items rather than dumped on
one — dumping it is how the previous carton allocator could drive an item negative, then silently
clamp it to 0.01 and break the exact-sum invariant it advertised (200 items in 3 cartons summed to
3.99).

**A floor is a constraint, not an additive base.** The pure proportional split is used whenever it
already clears every floor, so one item's large fixed net does not distort the others' shares.

`total` and every floor must be quantized to `places` before the call.

### Infeasible input assigns nothing

When the authorized gross cannot cover the fixed nets, the spec's Final Condition 2 says **stop**,
not reconcile. No gross is written for the affected items; the weight columns stay blank and a
blocking message names the totals. Reviewer pins are preserved — that is the reviewer's own input,
echoed back so they can correct it.

**Cartons are not independent of this.** With no packing carton evidence their basis *is* the item
gross, so an item whose gross was withheld has a carton basis of zero — and zero is "no weight", not
"no information". One such item lands on the 0.01 floor; when every gross is missing the basis is
uniformly zero and largest-remainder collapses to an **equal split**. Quantities 1/9/90 produced
4.00/4.00/4.00 cartons under an audit trail that still read `proportional by gross weight`.

An item with no allocated gross takes its **quantity share**, rescaled by what the allocated items
weigh per unit of quantity — the same rule and the same rescale as §5 — and `carton_source` names
the basis actually used. 1/9/90 now gives 0.12/1.08/10.80, and a pin overrunning the authority no
longer takes 9.99 of 10 cartons while a 9-piece line sits on the floor. Packing-stated cartons are
untouched: those genuinely are independent of the gross.

`GROSS_ALLOCATION_IMPOSSIBLE`, `REVIEWED_GROSS_EXCEEDS_AUTHORITY`,
`REVIEWED_GROSS_TOTAL_MISMATCH` and `WEIGHT_RECONCILIATION_IMPOSSIBLE` are in
`WARN_MODE_HARD_CODES`: warn mode may never bypass them. There is nothing to test in ASYCUDA when
every line would assert a zero gross weight.

**A blank cell always carries its reason.** `pipeline.item_details_preview` forwards *every*
message the preview produces to the review screen — shipment-scope verbatim, item-scope grouped by
code with the affected SNs named. It used to forward an allow-list of three pin codes, so an
infeasible allocation emptied Gross / Net / Sup_U / Sup_qty on every row while the blocking message
that explained it was filtered out. Blank columns with no stated cause read as "not extracted yet",
which is a different problem with a different fix. Never re-introduce the allow-list.

### Reviewer pins: the human value is always accepted (2026-08-03)

A value a reviewer types is never clamped, rescaled or rounded toward a derived figure. The system
adjusts the **other** items to hold the authority. This applies to gross weight, net weight and CTN
alike, and a pin that contradicts a document is accepted with a warning naming both figures
(`REVIEWED_CTN_OVERRIDES_PACKING`) — the reviewer read the document; rank 1 outranks it.

The two columns then diverge, deliberately:

| | Gross weight | CTN |
|---|---|---|
| Redistribution | all unpinned items re-apportioned on their provisional basis | delta absorbed by a **few** items |
| Optimises for | proportionality | **stability** — the numbers a reviewer already checked stay put |

A carton count is a countable object on a coarse lattice that a reviewer reads and remembers; a
gross weight is a continuous derived figure at 3 dp. Re-apportioning 200 carton rows because one was
edited churns every number in the table for no gain, so the carton path instead absorbs the
difference:

1. **A deterministic no-pin baseline.** `base = apportion(auth_ctn, ctn_basis, 0.01, 0.01)` with the
   pins ignored. The delta is measured against *this*, never against "what was on screen last time":
   the overlay is replayed from scratch on every recompute, so the outcome must depend only on the
   **set** of pins. Summing the delta across all pins at once makes pin A→B and pin B→A produce the
   same declaration; absorbing them one at a time would not.
2. **Estimated rows absorb first**, concentrated into the `CTN_DONOR_MAX` (10) highest of them.
   Those rows carry no information — nobody printed a carton for them — so concentrating the damage
   costs nothing real and leaves the rest of the table untouched.
3. **Packing-stated rows are rescaled as a set**, and only when the estimates cannot cover it. Their
   mutual ratios are a printed fact, so a uniform proportional haircut is applied — which preserves
   every pairwise ratio, and scales shared carton groups by a common factor so their internal
   division survives. Raiding the ten largest would destroy exactly what makes them worth keeping.
   This is loud: `CTN_PIN_RESCALED_PACKING_EVIDENCE` names the factor and the row count.

The donor set is bounded from two directions, and they pull opposite ways:

- **granularity caps it** — a delta of 3 units cannot be shared by 10 rows, because a third of a unit
  is not on the lattice. At most `|delta|` rows are used, so every donor moves at least one unit
  (the 10 → 9 → … → 1 fallback).
- **capacity grows it** — a donor may not fall below 0.01, so when the top rows have no headroom the
  set extends *down* the ordering. Shrinking it would make a capacity shortfall worse, not better.

Ties are broken on `(−baseline, SN)`; without a fixed tie-break the donor set is nondeterministic.

**When the pins cannot fit, the authority is what gives way.** `Σ pins ≤ auth_ctn − 0.01 × unpinned`
is the exact bound (and all-pinned must equal the authority exactly). Past it the pins still stand,
the unpinned rows sit on the lattice minimum, and `REVIEWED_CTN_EXCEEDS_AUTHORITY` /
`REVIEWED_CTN_TOTAL_MISMATCH` carry a `remediation` naming the total the shipment would need — which
the Detailed Review surfaces as a prefilled, one-click `POST /shipment-totals`. Nothing is applied
automatically: a declaration's total gross weight and package count must never move as a side effect
of editing one row. The same `remediation` is now carried by the two gross codes.

An outcome can satisfy every invariant and still be degenerate — one large pin can push a long tail
onto the 0.01 minimum. `CTN_DONORS_ON_FLOOR` names how many, because a column of 0.01s is
arithmetically valid and practically meaningless.

### Feasibility diagnostic

Runs before reconciliation and writes nothing. `sum(net) / auth_gross` names the cause:

| Ratio | Reading |
|---|---|
| ~1000 | a gram value read as kilograms |
| ~24 | a pack multiplier applied to a piece count |
| ~2.2 | pounds read as kilograms |
| ~0.7 | healthy |
| > 1.0 | infeasible |

A single item whose net exceeds the entire authorized gross is provably wrong and is named
individually (`ITEM_NET_EXCEEDS_SHIPMENT`).

---

## 7. Precision

| Value | Precision | Floor / epsilon |
|---|---|---|
| item gross | **3 dp** | eps = 0.001 |
| item net | **3 dp** | — |
| item CTN | 2 dp — a whole multiple of 0.01 (MUST, §1) | 0.01 |
| authority gross | quantized to 3 dp **before use** | — |
| authority CTN | quantized to 2 dp before use | — |

Decimal arithmetic only, never binary float.

3 dp matches the reference declaration, which carries net at 3 dp and gross at 2 dp. `trim_min1`
strips trailing zeros so `1.520` still emits as `1.52`.

**The authority must be representable at 3 dp.** A sum of 3 dp values is always a multiple of
0.001, so an authority of `199.0005` could never be matched. Quantize it first; the quantized value
*is* the authority from then on, for reconciliation, for display and for the reviewer.

**Ratio nets round DOWN.** At `gross = 0.001`, `0.7 x 0.001 = 0.0007` would round half-up to
`0.001` and equal its own gross. Rounding down also never over-declares a net weight. When gross
lands on 2 dp — as in the reference — `0.7 x gross` is exactly 3 dp anyway, so no rounding occurs
in the common case.

---

## 8. Audit trail

Every item carries `allocation_audit`: invoice line, description, matched packing item, carton
source, gross weight source, net weight source, `estimated`, final CTN / gross / net, and
validation status.

Never silently ignore a mismatch. Never silently drop an invoice item. Never create an XML item
that is not on the invoice. Never change invoice item order.

---

## 9. Expected final result

1. XML goods items in exact invoice order.
2. Packing rows matched by name, not row order.
3. Repeated packing names summed before allocation.
4. Shared cartons allocated fractionally without duplicating the group total.
5. `sum(item CTN) == authorized total carton`.
6. Every `item CTN >= 0.01`.
7. `sum(item gross) == authorized total gross`.
8. `net < gross` for every item.
9. The override ladder of §5 applied in order.
10. Values reconciled after rounding.
11. Mathematically impossible input raises a validation error and assigns nothing.

**Acceptance test:** the bundled 119-item demo reproduces `sample_data/sample_xml_format.xml`'s
item weights with zero mismatches — `net == 0.7 x gross` on 119 of 119, gross summing to 199.00.

---

## 10. Decisions on record

| | Decision | Rationale |
|---|---|---|
| D1 | Reviewer pin outranks all four override tiers | A reviewer who types a weight has seen the document; no parser outranks that |
| D2 | Reviewer-corrected shipment totals outrank the HAWB | The system suggests, the user decides |
| D3 | Packing gross is *shape*, rescaled to the authority — not verbatim | §6's exact-sum rule requires it |
| D4 | LOW-confidence description conversion demotes below invoice weight, rather than being discarded | Still the best available evidence when nothing else exists |
| D5 | 3 dp gross and net, eps = 0.001, CTN 2 dp | Matches the reference; 6 dp was false precision on ratio-derived values |
| D6 | 0.7 is the code default, not an env override | Reference and README always agreed; only the config default said 0.3 |
| D7 | `sum(net)` vs a declared total net is a **reported figure with a tolerance**, not a hard invariant | Pinning `sum(net)` *and* `sum(gross)* and fixed override nets is over-constrained — nets must stay free for the system to be solvable |
| D8 | The invoice-printed weight outranks a description conversion at ANY confidence; disagreement beyond 10x warns | A figure a person put on the commercial document beats a parser reading free text. The old order rested the design on one confidence gate, and four separate parser defects produced wrong values at HIGH |
| D9 | Conditions 1 and 2 are independent; a packing list stating both governs both | Re-deriving a printed carton column from weight discards evidence for no reason |
| D10 | A partial packing extraction is a THIRD state, not "present" or "absent" | "Present" silently switches unreached items to the value share; "absent" throws away rows already extracted |
| D11 | Matching below exact equality is a scored **proposal**, reported and capped below 1.00 | A missed match is invisible — it becomes an estimated weight with no warning. A wrong match is worse, so similarity is gated on measurements agreeing and on a clear winner |
| D12 | Ratio 0.7, weights at 3 dp, warn-mode (no new hard blocks) | Confirmed by the user 2026-07-30 against the alternative rule text proposing 0.3 / 6 dp / hard blocks |
| D13 | CTN is reviewer-editable; a pin is kept exact and the delta is absorbed by a few ESTIMATED rows, while packing-stated rows are rescaled proportionally as a set and only when the estimates cannot cover it | The two donor classes are not alike. An estimated carton carries no information, so concentrating the change there costs nothing and keeps the rest of the table still — which is what a reviewer working row by row needs. A printed carton carries structure, and a uniform haircut is the only redistribution that preserves every ratio between printed figures. Gross weight keeps full re-apportionment: it is continuous at 3 dp, not a countable object a reviewer remembers |
| D14 | Every CTN is ≥ 0.01 and a whole multiple of 0.01, asserted after every allocation; values off the lattice are rejected at write time, never quantized | It is what makes pin redistribution exact integer arithmetic. Quantizing 3.333 to 3.33 and reconciling the shipment around the difference produces a number the reviewer never typed and cannot account for |
| D15 | A human-entered value is always accepted; where it cannot fit, the AUTHORITY is what gives way, offered as a prefilled one-click shipment-totals change and never applied automatically | Confirmed by the user 2026-08-03. D2 already says the reviewer decides the shipment totals; this makes the same decision reachable from the row where the conflict appears, without letting one item edit silently redefine the consignment |

---

## 11. Known gaps

Specified above but **not yet implemented**. Do not read this file as a description of finished work.

- **`allocation_audit` is not surfaced in the Detailed Review grid.** The reviewer approves numbers
  without being able to see which rule produced them. It is only readable post-finalize.
- **A partial extraction cannot be retried.** The document's status is `EXTRACTED`, which
  `POST /extract` refuses, re-upload raises `DUPLICATE_DOCUMENT_UPLOAD`, and there is no delete
  route. The reviewer is told the extraction was partial and cannot act on it except by pinning
  weights by hand.
- **Cross-source plausibility is checked only between the invoice weight and the description
  conversion.** A packing-list net that disagrees with either by 10x is not compared.

### Closed

- ~~Matching has no fallback and no confidence.~~ §3's ladder: exact name → product code → scored
  similarity, gated on measurements agreeing and on a clear winner, every proposal reported.
  `tests/test_packing_match_scored.py`.
- ~~The deterministic parser drops packing weights and cartons.~~ §5b, with the printed-totals
  cross-check as the gate. `tests/test_packing_table_parser.py`.
- ~~A late window discards the whole packing extraction.~~ §5b's three states.
  `tests/test_packing_partial_extraction.py`.
- ~~Condition 1 and Condition 2 are mutually exclusive.~~ §4; `tests/test_allocation_dual_basis.py`.
- ~~No committed golden or property tests.~~ `tests/test_allocation_spec.py` and
  `tests/test_apportion_and_golden_weights.py` cover the `apportion` invariants, both
  impossibility errors and the reference weights.
- ~~Condition 1 is all-or-nothing.~~ §4 now specifies the per-item density estimate, and
  `PACKING_ITEMS_UNMATCHED` names every affected SN. See
  `tests/test_unmatched_packing_items.py`.
- ~~No visible-gap assertion.~~ `WEIGHT_GAP_INVISIBLE` warns when `round(net, 2)` is not below
  `round(gross, 2)` — the item is valid at 3 dp but reads as its own gross on the form.

---

## 12. Field allocation — description / COO / model / size (2026-07-30)

Live-job root cause (Medtronic invoice 4050032873, job `88491b56`): the vendor prints batch,
a quantity echo and the per-row COO **inside the description cell**, the model column pairs the
catalogue code with its GTIN barcode, and one OCR-misread quantity disowned eleven pages to an
LLM whose schema never defined any of these fields. Declared description carried the batch/COO
tail, COO fell back to the exporter (Singapore, for Irish goods), MODEL took the barcode, SIZE
missed `2.50X12RX`.

The permanent design — the pipeline **never trusts either extraction source raw**:

1. **Parser fixes** (`extraction/table_parser.py`): full-name COO capture (`COO: Ireland`),
   GTIN-skipping model tokens, the size header never claims the merged qty column, and an
   EXACT-arithmetic quantity repair (`total / price` a clean integer) with a reviewer-visible
   note instead of page disowning. Page furniture above a page's own reprinted header is never
   a suspicious leftover.
2. **The LLM contract is specified** (`extraction/common_models.py`, `openai_extractor.SYSTEM`
   rules 11/11c/14, `prompt_version = extract-v2`): every soft field carries a schema
   description; invoice rows gained `batch_no_raw`/`lot_no_raw`/`serial_no_raw`/
   `expiry_date_raw` so annotations have a home other than `description_raw`.
3. **Deterministic allocation at ingest** (`rules/field_allocation.py`, invoked from
   `rules/invoice_authority.py` for BOTH sources): leading previous-row overflow stripped,
   the description split at strong annotation labels (cut only when everything removed is
   provably annotation material), the row's own COO/batch mined from the removed tail,
   normalize-gated (`COC` accepted as the routine OCR misread of `COO`). Every removal is
   surfaced (`DESCRIPTION_ANNOTATION_TRIMMED`); the original wording stays in
   `evidence_description_raw` so packing-list matching is never perturbed.
4. **Vendor field profiles** (`extraction/field_profiles.py`, learned on finalize): a vendor's
   reviewer-confirmed/uniform COO becomes the default proposed BEFORE the exporter fallback
   (`VENDOR_PROFILE`, always warned) — the fix for trading exporters whose exporter-country
   guess is systematically wrong. The store never learns from fallback- or profile-sourced
   values. `vendor_field_profiles_enabled` gates both directions.
5. **Field gates**: the validator repairs the provable model/line-number swap (GTIN in
   `model_raw` + part code in `line_no_raw`); `field_allocation.audit_items` warns on a
   barcode-only MODEL and on annotation text left in a final description, whatever its source.

COO resolution ladder (`rules/coo.py`): ITEM_LEVEL (progressive normalization included) →
VENDOR_PROFILE (warned) → EXPORTER_FALLBACK (warned) → blocking `COO_UNRESOLVED`.

Pinned by `tests/test_field_allocation.py` and `tests/test_vendor_field_profiles.py`.
