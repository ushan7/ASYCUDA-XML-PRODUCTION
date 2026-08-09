"""Weight and carton allocation + reconciliation.

``docs/allocation-spec.md`` is the authoritative specification; this module is
its implementation.  Change both in the same commit.

Invoice order is never touched: values are computed per item in place.

Initial basis — the two conditions are independent, and both apply when the
packing list prints both:
* CONDITION 1 — packing gives item-wise gross weights: they are the gross
  basis.
* CONDITION 2 — packing gives item-wise cartons: cartons come from the
  (grouped, shared-aware) packing cartons.
* Whichever the packing list does NOT state is derived from the other: cartons
  proportional to gross, or gross proportional to carton share.
* No usable packing rows: value share when a packing list exists (historical
  reference behavior), QUANTITY share when none was uploaded (fallback rule);
  cartons follow weight share.

Net-weight ladder (spec section 5), highest authority first:

    1. reviewer pin (Detailed Review edit)
    2. invoice-printed item weight
    3. invoice QUANTITY in a mass unit — the line is sold by weight
    4. invoice DESCRIPTION conversion (HIGH, then LOW confidence)
    5. packing-list item net
    6. ratio x gross  (ADR-003 = 0.7; see config.ADR_003_NET_TO_GROSS_RATIO)

The invoice-printed weight is the highest non-reviewer authority: it is a
figure a person put on the commercial document, while a description conversion
is a parser reading free text.  A HIGH-confidence conversion used to outrank
it; it is now a CROSS-CHECK instead, and a disagreement beyond 10x raises
NET_WEIGHT_SOURCES_DISAGREE rather than silently replacing the stated value.
Ranks 1-4 set a provisional gross = net x 1.2; rank 5 leaves the packing gross
as the basis.  Note the 1.2 is a WEIGHTING, not a promise: section 6's exact-sum
rule rescales every gross to the authority, so the factor cancels entirely when
every line has a fixed net and only decides how much of the authority the
weighed lines claim against the unweighed ones in a mixed shipment.

Rank 3 (user rule 2026-08-04): a line invoiced BY WEIGHT states its own net —
``RESIN 500 KG`` is 500 kg of goods.  See ``net_from_uom_quantity``.

A description net (rank 4) that does not fit the authorised gross is REJECTED
and falls to the ratio, because rank 4 is a parser reading free text while
every other rank is a human or a document.  Otherwise one misread line makes
the apportionment infeasible, which blanks the gross, net and supplementary
columns of every item in the shipment.

Reviewer-pinned gross weights (Detailed Review edit, user rule 2026-07-19):
a pinned item keeps its entered gross EXACTLY; the remaining authorised
gross is reconciled across the UNPINNED items only.  Pins that exceed the
authority (or an all-pinned sum that misses it) raise blocking errors —
never silently rescaled.

Reviewer-pinned CARTONS (user rule 2026-08-03) follow the same contract with
a different redistribution rule, because the two columns are not alike.  A
carton count is a countable object on a coarse 0.01 lattice that a reviewer
reads and remembers; a gross weight is a continuous derived figure.  So the
carton column optimises for STABILITY — see ``_allocate_cartons``:

* every final CTN is >= 0.01 AND an exact multiple of 0.01 (MUST rule);
* the delta a pin creates is measured against a deterministic NO-PIN baseline
  and is therefore a whole number of 0.01 units — the redistribution is exact
  integer arithmetic with no rounding anywhere;
* ESTIMATED rows (no printed carton) absorb it first, concentrated into the
  ``CTN_DONOR_MAX`` highest of them, so one edit moves ~10 numbers instead of
  every number in the table;
* PACKING-STATED rows are rescaled proportionally as a whole, and only when
  the estimates cannot cover the delta: their mutual ratios are a printed
  fact, so the damage is spread there rather than concentrated.

Final reconciliation (mandatory): fixed nets stay fixed; each gets
min gross = net + epsilon while a rank-6 item needs only one precision unit;
the authorised gross is apportioned proportional to the provisional basis by
largest remainder; sum(item gross) == authorised gross EXACTLY and
sum(item carton) == authorised cartons EXACTLY (each carton >= 0.01).  Rank-6
nets are derived from the FINAL gross afterwards, so ``net < gross`` holds by
construction.  Mathematically impossible inputs assign nothing and produce
blocking errors with the spec's wording.
"""
from __future__ import annotations

import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from ..config import get_settings
from ..domain.errors import ValidationMessage
from ..numbers import q2, q3, q3_down, q4
from ..units import normalize_weight_unit, to_kg
from .description_weight import net_from_description
from .models import WorkItem
from .packing_match import PackingEvidence

_ZERO = Decimal("0")
_PLACES3 = Decimal("0.001")         # item weights are emitted at 3dp
_EPS = Decimal("0.001")             # one 3dp step: net < gross survives rounding
_MIN_CTN = Decimal("0.01")

# How many ESTIMATED rows absorb a reviewer carton pin before the set is grown.
# A code default, not an env override — same reasoning as the net-to-gross
# ratio (spec D6): a number that changes what the declaration says must not
# live in a `.env` line nobody reads at review time.
CTN_DONOR_MAX = 10

CARTON_TOO_SMALL_MSG = ("Cannot allocate cartons: authorized total carton is too small to "
                        "give every invoice item at least 0.01 CTN.")
REVIEWED_CTN_EXCEEDS_MSG = ("Reviewer-entered item carton counts leave too little of the "
                            "authorised total for the remaining items — every item must keep "
                            "at least 0.01 CTN. Lower the item cartons or apply corrected "
                            "shipment totals.")
REVIEWED_CTN_MISMATCH_MSG = ("Every item carton count is reviewer-entered but their sum does "
                             "not equal the authorised total packages — adjust the item "
                             "cartons or apply corrected shipment totals.")
GROSS_NOT_ABOVE_NET_MSG = ("Cannot allocate gross weight: authorized total gross weight is "
                           "not greater than total net weight.")
REVIEWED_GROSS_EXCEEDS_MSG = ("Reviewer-entered item gross weights exceed the authorised "
                              "shipment gross weight — lower the item weights or apply "
                              "corrected shipment totals.")
REVIEWED_GROSS_MISMATCH_MSG = ("Every item gross weight is reviewer-entered but their sum "
                               "does not equal the authorised shipment gross weight — adjust "
                               "the item weights or apply corrected shipment totals.")


def allocate_weights_and_cartons(
    items: list[WorkItem],
    packing: dict[int, PackingEvidence],
    authorized_total_gross: Decimal,
    authorized_total_packages: Decimal,
    packing_present: bool = True,
    packing_partial: bool = False,
) -> list[ValidationMessage]:
    settings = get_settings()
    ratio = settings.default_net_to_gross_ratio
    warnings: list[ValidationMessage] = []
    if not items:
        return warnings
    n = len(items)
    evs = [packing.get(it.xml_item_sequence, PackingEvidence()) for it in items]

    # INDEPENDENT, not mutually exclusive.  `have_carton` used to be
    # `(not have_gross) and …`, so a packing list that printed BOTH — the
    # ordinary case — had its stated cartons thrown away and re-derived from
    # weight.  Condition 1 governs the weight, Condition 2 governs the cartons,
    # and each falls back to the other only where the document is silent.
    have_gross = any(ev.gross_weight and ev.gross_weight > 0 for ev in evs)
    have_carton = any(ev.carton_count and ev.carton_count > 0 for ev in evs)

    # ---- 1. provisional gross basis (Condition 1 / 2 / fallbacks) --------
    # The fallback shape, per item: what the whole shipment would be split by
    # if there were no packing evidence at all.  Under Condition 1/2 it is ALSO
    # what an item that the packing list does not mention is split by.
    # A PARTIAL packing extraction takes the QUANTITY shape, not the value one.
    # The value share is the historical reference behaviour for a shipment
    # whose packing list simply states no weights; here the packing list DOES
    # state them and the extraction stopped early, so the items still missing
    # are missing at random — and quantity predicts weight far better than
    # price does.  Value share systematically under-weights a cheap bulky line.
    use_value = packing_present and not packing_partial
    fallback_label = "invoice value" if use_value else "item quantity"
    fallback = [
        (it.line_total if it.line_total > 0 else Decimal("1")) if use_value
        else (it.quantity if it.quantity > 0 else Decimal("1"))
        for it in items]

    prov: list[Decimal] = []
    gross_src: list[str] = []
    unmatched: list[int] = []
    # Does THIS item have a packing row of its own?  Step 3b needs it: a
    # rejected description weight yields to the quantity share, except where
    # the packing list states a weight for that very item — evidence outranks
    # any fallback.  False for every item when there is no usable packing row
    # anywhere, which is what the two `else` branches below mean.
    has_packing_basis = [False] * n
    if have_gross or have_carton:
        # CONDITION 1 (packing gross) / CONDITION 2 (packing cartons).
        #
        # These used to be all-or-nothing: one item with a packing gross put
        # EVERY item on that basis, and an item the packing list never
        # mentioned got a basis of zero.  Zero does not mean "no information",
        # it means "no weight" — the item then sat on the apportionment floor
        # at 0.001 kg gross / 0.000 kg net, with `net < gross` satisfied, the
        # totals reconciling exactly, `validation_status: OK`, and not one
        # warning anywhere.  A 1-gram line is not a plausible customs
        # declaration; it is a missing match wearing a valid-looking number.
        #
        # An unmatched item now keeps its share of the shipment, scaled to what
        # the MATCHED items actually weigh per unit of value/quantity — the
        # best evidence available about how heavy this shipment's goods are.
        ev_value = [((ev.gross_weight or _ZERO) if have_gross else (ev.carton_count or _ZERO))
                    for ev in evs]
        matched_src = ("packing-list gross weight" if have_gross
                       else "carton-proportional distribution")
        prov, gross_src, unmatched = _basis_with_estimates(
            ev_value, fallback, matched_src,
            f"estimated from {fallback_label} share (no packing-list match)",
            groups=[(it.invoice_uom_raw or "").strip().upper() or "?" for it in items])
        for i in range(n):
            has_packing_basis[i] = ev_value[i] > 0
    elif use_value:
        prov = list(fallback)
        gross_src = ["invoice value share"] * n
        warnings.append(ValidationMessage.warning(
            "WEIGHT_BASIS_VALUE", "No packing gross weights; distributed authorised gross by invoice value share."))
    elif packing_present:
        # Partial extraction that yielded no usable weight OR carton at all.
        # `fallback` is already the quantity shape here — labelling it "invoice
        # value share" would have put a source string in the audit trail that
        # names a basis the allocation did not use.
        prov = list(fallback)
        gross_src = ["quantity share (packing extraction incomplete)"] * n
        warnings.append(ValidationMessage.warning(
            "WEIGHT_BASIS_QUANTITY",
            "The packing extraction stopped before any item weight or carton could be read; "
            "authorised gross weight distributed by item quantity share and cartons by weight "
            "share (exact-sum reconciled)."))
    else:
        prov = list(fallback)
        gross_src = ["quantity share (no packing list)"] * n
        warnings.append(ValidationMessage.warning(
            "WEIGHT_BASIS_QUANTITY",
            "No packing list uploaded; authorised gross weight distributed by item quantity "
            "share and cartons by weight share (exact-sum reconciled)."))

    if unmatched:
        sns = [items[i].xml_item_sequence for i in unmatched]
        shown = ", ".join(str(s) for s in sns[:20]) + (" …" if len(sns) > 20 else "")
        # "not found on the packing list" is not true when the extraction simply
        # did not reach them — the reviewer would go looking for a wording
        # mismatch that is not there.
        cause = ("were not reached before the packing extraction hit its time budget"
                 if packing_partial else "were not found on the packing list")
        warnings.append(ValidationMessage.warning(
            "PACKING_ITEMS_UNMATCHED",
            f"{len(unmatched)} of {n} invoice item(s) {cause} "
            f"(SN {shown}). Their weight is ESTIMATED from their {fallback_label} share of what "
            f"the matched items weigh — it was not read from any document. Verify these item "
            f"weights, or pin them in Detailed Review, before finalizing."))

    # ---- 2. authority, quantized to the OUTPUT precision -----------------
    # A sum of 3dp item weights can never equal an authority carrying more
    # decimal places, so the quantized value IS the authority from here on.
    auth_gross = (q3(authorized_total_gross)
                  if authorized_total_gross and authorized_total_gross > 0 else None)
    if auth_gross is None:
        auth_gross = q3(sum(prov, _ZERO))
        warnings.append(ValidationMessage.warning(
            "GROSS_AUTHORITY_MISSING",
            "No shipment gross authority; used packing/derived weights as-is."))
    auth_ctn = (q2(authorized_total_packages)
                if authorized_total_packages and authorized_total_packages > 0 else None)

    # Reviewer-pinned gross: kept EXACTLY, excluded from the apportionment.
    pinned: list[Decimal | None] = [
        (q3(it.manual_gross_weight_kg)
         if it.manual_gross_weight_kg and it.manual_gross_weight_kg > 0 else None)
        for it in items]
    pin_sum = sum((p for p in pinned if p is not None), _ZERO)

    # ---- 3. net ladder -> a FIXED net, or ratio mode ---------------------
    # Final override ladder, highest authority first:
    #   reviewer pin > invoice-printed weight > invoice quantity in a mass unit
    #   > description conversion (HIGH, then LOW) > packing-list net
    #   > ratio x gross
    # This comment described the PRE-2026-07-30 order — HIGH-confidence
    # description conversion above the invoice weight — for four commits after
    # decision D8 inverted it.  The code below is the authority; docs/
    # allocation-spec.md section 5 is the specification.
    #
    # A ratio item's net is deliberately NOT decided here.  It is derived in
    # step 5 from the FINAL reconciled gross, so net = r x gross holds exactly
    # and net < gross is guaranteed for r < 1 whatever the reconciliation does.
    # Deriving it from a pre-reconciliation share (as before) left only a
    # 1 - r margin, which at r = 0.7 is thin enough to invert.
    # The pre-override basis, kept so a rejected description net (step 3c) can
    # put its item back on the shipment-wide share it would have had.
    prov_base, gross_src_base = list(prov), list(gross_src)

    fixed: list[Decimal | None] = []
    net_src: list[str] = []
    estimated: list[bool] = []
    from_desc: list[bool] = []          # net came from the description parser
    for i, (it, ev) in enumerate(zip(items, evs)):
        est = False
        inv_net = None
        if it.item_weight_kg and it.item_weight_kg > 0:
            inv_net = (it.item_weight_kg * it.quantity
                       if it.item_weight_scope == "PER_UNIT" else it.item_weight_kg)
        dw = net_from_description(it.description_raw, it.quantity, it.invoice_uom_raw)
        if dw is not None and dw.net_kg <= 0:
            dw = None
        if dw is not None:
            for reason in dw.warnings:
                it.warnings.append(ValidationMessage.warning(
                    "DESCRIPTION_WEIGHT_UNCERTAIN",
                    f"Item {it.xml_item_sequence}: net weight taken from the description "
                    f"({q3(dw.net_kg)} kg) - {reason}. Verify or enter the weight.",
                    scope="ITEM", item_sequence=it.xml_item_sequence, field="net_weight"))

        # A line invoiced BY WEIGHT states its own net (user rule 2026-08-04).
        # The ambiguity warning is deliberately NOT raised here: a higher rung
        # may take the row, and telling the reviewer that 2000 kg "is taken as
        # this item's net weight" when the printed weight column actually won
        # sends them to check a number that was never used.
        uom_net, uom_src, uom_ambiguous = net_from_uom_quantity(it)

        net = None
        src = ""
        desc = False
        if it.manual_net_weight_kg and it.manual_net_weight_kg > 0:
            net, src = it.manual_net_weight_kg, "reviewer-entered net weight"
        elif inv_net and inv_net > 0:
            # Highest non-reviewer authority: a person put this figure on the
            # commercial document.  A description conversion — at ANY confidence
            # — is a parser reading free text, and now cross-checks it rather
            # than replacing it (user rule 2026-07-30).
            net, src = inv_net, "invoice weight override"
        elif uom_net is not None:
            # Below the printed weight COLUMN, above the packing list.  A
            # column headed NET WEIGHT is a direct statement of net; a mass unit
            # of sale is a very strong inference that is *usually* the net and
            # occasionally a billing or chargeable weight.  Both are INVOICE
            # sources — the document the declaration is built from — so both
            # outrank the packing list.
            net, src = uom_net, uom_src
        elif ev.net_weight and ev.net_weight > 0:
            # ABOVE the description parser (user rule 2026-08-04).  The packing
            # list is a shipping document stating this item's weight; a
            # description conversion is the parser reading a product NAME and
            # inferring a mass from it.  With the old order, "VACUUM FLASK
            # 500ML" x 100 read as 500 ml of water and replaced a printed
            # 720.00 kg with 50 kg — at LOW confidence — and the exact-sum
            # reconciliation then pushed the freed gross onto the other lines,
            # so items nobody had misread lost their printed figures too.
            net, src = ev.net_weight, "packing-list net weight"
        elif dw is not None:
            net, est, desc = dw.net_kg, dw.estimated, True
            src = dw.source if dw.confidence == "HIGH" else f"{dw.source} [LOW confidence]"
        from_desc.append(desc)

        # ...raised only once the row actually lands on the mass unit of sale.
        if uom_ambiguous and net is not None and src == uom_src:
            it.warnings.append(ValidationMessage.warning(
                "INVOICE_UOM_WEIGHT_AMBIGUOUS",
                f"Item {it.xml_item_sequence}: the quantity is priced in "
                f"{(it.invoice_uom_raw or '').strip().upper()!r}, read as a mass unit — "
                f"{uom_net} kg is taken as this item's net weight. That token also names a "
                f"length on some invoices; if this line is not sold by weight, correct the UOM "
                f"or pin the weight in Detailed Review.",
                scope="ITEM", item_sequence=it.xml_item_sequence, field="net_weight"))

        # Two independent readings of the same item disagreeing by an order of
        # magnitude is a unit or pack-multiplier error, not a judgement call —
        # and whichever the ladder picks, the other one was wrong by 10x.  Say
        # so instead of silently preferring one (spec section 11 gap).  The
        # comparison is CHOSEN-against-every-other rather than one fixed pair:
        # with three invoice-side readings (printed column, mass unit of sale,
        # description) a fixed pair would stay silent on exactly the
        # combinations the newest source introduced.
        if net is not None and net > 0 and src != "reviewer-entered net weight":
            others = [(inv_net, "the invoice prints a weight of"),
                      (uom_net, "its quantity column reads as"),
                      (dw.net_kg if dw is not None else None,
                       f"its description reads as" + (f" ({dw.source})" if dw else ""))]
            for value, phrase in others:
                if value is None or value <= 0 or value == net:
                    continue
                hi, lo = max(net, value), min(net, value)
                if hi > lo * 10:
                    it.warnings.append(ValidationMessage.warning(
                        "NET_WEIGHT_SOURCES_DISAGREE",
                        f"Item {it.xml_item_sequence}: {phrase} {q3(value)} kg against the "
                        f"{q3(net)} kg used ({src}) — a {hi / lo:.0f}x disagreement, so one of "
                        f"them has the wrong unit or pack size. Verify this weight.",
                        scope="ITEM", item_sequence=it.xml_item_sequence, field="net_weight"))

        if net is None:
            fixed.append(None)
            net_src.append(f"{ratio} x gross")
        else:
            fixed.append(q3(net))
            net_src.append(src)
            # `net x 1.2` replaces the gross BASIS even when the packing list
            # states a gross for this item.  That is the written rule ("after
            # replacing net weight from invoice, set provisional gross weight =
            # net_weight * 1.2"), and it is deliberate: an override that
            # contradicts the packing net usually contradicts the packing gross
            # too.  Keeping the measured gross instead would be more accurate
            # when the two sources agree and much worse when they do not —
            # net 8 kg against a measured gross of 50 kg would declare 42 kg of
            # packaging.  Changing this is a rule decision, not a code one.
            if pinned[i] is None and not src.startswith("packing-list"):
                prov[i] = q3(q3(net) * Decimal("1.2"))
                gross_src[i] = f"{src} x 1.2 provisional"
        estimated.append(est)

    # A non-ADR-003 ratio silently rewrites every ratio item's net weight, and
    # the override lives in a `.env` line nobody reads at review time.  Say so,
    # once, and only when items actually depend on it.
    ratio_note = settings.net_to_gross_ratio_note()
    if ratio_note and any(f is None for f in fixed):
        warnings.append(ValidationMessage.warning(
            "NET_TO_GROSS_RATIO_OVERRIDDEN",
            f"{sum(1 for f in fixed if f is None)} item(s) take their net weight from a "
            f"configured {ratio_note}. Verify the ratio before finalizing."))

    # ---- 3b. a description guess may never blank the whole table ----------
    # Ranks 2 and 4 are a parser reading free text; ranks 1, 3 and 5 are a
    # human or a document.  When the derived nets cannot fit the authorised
    # gross the apportionment is infeasible and EVERY item loses its gross and
    # net — including the rows the parser never touched — and each of those
    # then loses its supplementary quantity too, because the KGM tariff unit
    # is derived from the net.  One misread line silently emptying four
    # columns of a whole table is not a defensible failure mode: a description
    # net that cannot be true yields to the ratio and says so.
    #
    # "Stainless Steel 316 L" (a steel grade) parsed as 316 litres is the case
    # that motivated this: four suture lines claimed 1580 kg each against a
    # 62 kg authorised gross.  A reviewer pin, an invoice-printed weight or a
    # packing-list net still blocks — that is someone's stated value, and only
    # they may correct it.
    idx = [i for i in range(n) if pinned[i] is None]
    alloc_auth = q3(auth_gross - pin_sum)

    def _floor_sum() -> Decimal:
        return sum(((fixed[i] + _EPS) if fixed[i] is not None else _EPS for i in idx), _ZERO)

    rejected: list[tuple[int, Decimal, str]] = []
    # A negative budget means the PINS already overrun the authority; that is
    # the reviewer's own conflict to resolve, and stripping parser values would
    # only hide it behind a second, misleading message.
    if alloc_auth >= 0:
        for i in sorted((j for j in idx if from_desc[j]),
                        key=lambda j: -(fixed[j] or _ZERO)):   # worst offender first
            if _floor_sum() <= alloc_auth:
                break
            rejected.append((i, fixed[i], net_src[i]))
            fixed[i] = None
            net_src[i] = f"{ratio} x gross"
            estimated[i] = False
            # The "net weight taken from the description (1580 kg)" note this
            # item already carries is now untrue — the value was not used.
            items[i].warnings = [w for w in items[i].warnings
                                 if w.code != "DESCRIPTION_WEIGHT_UNCERTAIN"]

    if rejected:
        # QUANTITY SHARE, RESCALED (user rule 2026-07-22).  A rejected item has
        # no weight evidence left, and quantity predicts weight far better than
        # price does — invoice-value share systematically under-weights a cheap
        # bulky line and over-weights an expensive light one.
        #
        # But the surviving items' bases may be packing kilograms or invoice
        # value, and 5 pieces is not 80 dollars: mixing raw quantity into that
        # would hand the whole shipment to whichever item happens to carry the
        # larger magnitude.  Scaling the item's quantity by the surviving
        # basis-per-quantity makes the two commensurable, and makes the outcome
        # exact rather than approximate — the algebra cancels, so the item takes
        # PRECISELY its quantity share of the authorised gross whatever unit the
        # rest of the shipment is measured in, and the others keep their own
        # evidence and split the remainder by it.
        #
        # An item the packing list does state a weight for is the exception: a
        # document beats a fallback, so it goes back on its packing basis.
        for i, _, _ in rejected:
            if has_packing_basis[i]:
                prov[i], gross_src[i] = prov_base[i], gross_src_base[i]
        on_qty = [i for i, _, _ in rejected if not has_packing_basis[i]]
        if on_qty:
            qty = [(it.quantity if it.quantity > 0 else Decimal("1")) for it in items]
            ref = [i for i in idx if i not in set(on_qty)]
            ref_basis = sum((prov[i] for i in ref), _ZERO)
            ref_qty = sum((qty[i] for i in ref), _ZERO)
            # No reference items left: every unpinned basis is a quantity
            # already, so they are commensurable as they stand.
            scale = (ref_basis / ref_qty) if ref_basis > 0 and ref_qty > 0 else Decimal("1")
            for i in on_qty:
                prov[i] = qty[i] * scale
                gross_src[i] = "quantity share (description weight rejected)"
        shown = "; ".join(f"SN {items[i].xml_item_sequence} ({net} kg via {src})"
                          for i, net, src in rejected[:10])
        warnings.append(ValidationMessage.warning(
            "DESCRIPTION_NET_REJECTED",
            f"{len(rejected)} item net weight(s) read from the invoice DESCRIPTION could not be "
            f"true: they do not fit the authorised shipment gross of {auth_gross} kg, so the "
            f"description was misread (a grade, gauge or model code parsed as a unit). Those "
            f"items are re-allocated on their QUANTITY share of the authorised gross (or their "
            f"packing-list weight where the packing list states one) and take the {ratio} x gross "
            f"ratio for net; every other item keeps its own source. Verify these weights, or pin "
            f"them in Detailed Review: {shown}" + (" …" if len(rejected) > 10 else "") + "."))

    # ---- 3c. feasibility diagnostic - pure, nothing is written -----------
    warnings.extend(_feasibility_notes(
        items, [f if f is not None else _ZERO for f in fixed], net_src, auth_gross))

    # ---- 4. FINAL CONDITION 2 - apportion gross to the authority ---------
    blocked = False
    unallocated: set[int] = set()
    gross: list[Decimal | None] = [None] * n
    for i in range(n):
        if pinned[i] is not None:
            gross[i] = pinned[i]
            gross_src[i] = "reviewer-entered gross weight"
    if pin_sum > 0:
        detail = (f"the remaining {alloc_auth if alloc_auth > 0 else _ZERO} kg of the authorised "
                  f"gross is distributed across the other {len(idx)} item(s)"
                  if idx else "every item weight is reviewer-controlled")
        warnings.append(ValidationMessage.warning(
            "ITEM_WEIGHTS_REVIEWED",
            f"{n - len(idx)} item gross weight(s) reviewer-entered (sum {pin_sum} kg); {detail}."))
    if not idx:
        if pin_sum != auth_gross:
            warnings.append(ValidationMessage.blocking(
                "REVIEWED_GROSS_TOTAL_MISMATCH",
                REVIEWED_GROSS_MISMATCH_MSG
                + f" (items sum {pin_sum} kg, authority {auth_gross} kg)",
                remediation=f"Set the shipment gross weight to {pin_sum} kg."))
            blocked = True
    elif alloc_auth < 0:
        # The reviewer's own figures stand; say what the authority would have to
        # become so the correction is one decision instead of a search.
        #
        # A DESCRIPTION-derived net is excluded from that figure.  Step 3b skips
        # its rejection pass entirely on this branch (`if alloc_auth >= 0`), so
        # `fixed` still holds parser guesses that the engine would drop the
        # moment the authority rose — counting them proposes an authority the
        # shipment does not need.  A 100 kg pin beside a misread "316 L" would
        # have asked for 1000 kg and, once applied, resurrected the very value
        # the rejection exists to discard.
        need = q3(pin_sum + sum(
            ((fixed[i] + _EPS) if (fixed[i] is not None and not from_desc[i]) else _EPS
             for i in idx), _ZERO))
        warnings.append(ValidationMessage.blocking(
            "REVIEWED_GROSS_EXCEEDS_AUTHORITY",
            REVIEWED_GROSS_EXCEEDS_MSG
            + f" (pinned sum {pin_sum} kg, authority {auth_gross} kg)",
            remediation=f"Set the shipment gross weight to at least {need} kg."))
        blocked = True
        unallocated.update(idx)
    else:
        # A fixed net needs room above it; a ratio item needs only one precision
        # unit, because net = r x gross sits below gross by construction.
        floors = [(fixed[i] + _EPS) if fixed[i] is not None else _EPS for i in idx]
        # A gross may not exceed 1.2 x a net the INVOICE states (user rule
        # 2026-08-04).  `net x 1.2` was only ever a proportional WEIGHTING, and
        # section 6's exact-sum rule rescales every share against the authority
        # — so a line invoiced as "EPOXY RESIN 500 KGM" was taking 1636 kg gross
        # against a stated 500 kg net, because two heavier unweighed lines pulled
        # the whole basis upward.  A stated weight is evidence, not a hint, and
        # the cap holds it.  Only INVOICE-stated nets are capped: a packing-list
        # net keeps the packing gross beside it, a ratio net is 0.7 x gross by
        # construction, and a reviewer pin is exact and supreme.
        ceilings = [_invoice_gross_cap(fixed[i], net_src[i]) for i in idx]
        shares = apportion(alloc_auth, [prov[i] for i in idx], floors, _PLACES3, ceilings)
        if shares is None and any(c is not None for c in ceilings):
            # The authority cannot fit under the caps.  Report it and allocate
            # WITHOUT them rather than assigning nothing: the shipment gross and
            # the invoice's own weights genuinely disagree, and that is the
            # reviewer's decision, not a silent choice either way.
            capped = [(items[idx[k]].xml_item_sequence, ceilings[k])
                      for k in range(len(idx)) if ceilings[k] is not None]
            warnings.append(ValidationMessage.warning(
                "GROSS_EXCEEDS_INVOICE_WEIGHT_CAP",
                f"The authorised shipment gross ({auth_gross} kg) cannot be distributed without "
                f"some item exceeding 1.2 x the net weight its invoice states "
                f"({'; '.join(f'SN {sn} max {q3(c)} kg' for sn, c in capped)}). The cap was "
                f"released so the declaration still adds up — verify the shipment gross authority "
                f"and the invoice weights, because the two disagree.",
                scope="JOB", field="gross_weight"))
            shares = apportion(alloc_auth, [prov[i] for i in idx], floors, _PLACES3)
        if shares is None:
            fixed_total = sum((f for f in fixed if f is not None), _ZERO)
            # The nets that overran are STATED values — an invoice weight, a
            # mass unit of sale, a packing net or a reviewer pin (a parsed
            # description was already dropped in 3b).  Nobody's stated figure
            # should be the thing that yields, so name the authority that would
            # make them fit: the Detailed Review offers it as one click.
            need = q3(pin_sum + sum(floors, _ZERO))
            warnings.append(ValidationMessage.blocking(
                "GROSS_ALLOCATION_IMPOSSIBLE",
                GROSS_NOT_ABOVE_NET_MSG
                + f" (item net weights total {q3(fixed_total)} kg against an authorised "
                  f"gross of {auth_gross} kg). No item weights were assigned - correct the "
                  "item weights or the shipment gross authority.",
                remediation=f"Set the shipment gross weight to at least {need} kg."))
            blocked = True
            unallocated.update(idx)
        else:
            for pos, i in enumerate(idx):
                gross[i] = shares[pos]

    # ---- 5. nets: fixed values stand; ratio nets follow the FINAL gross --
    nets: list[Decimal | None] = [None] * n
    for i in range(n):
        if i in unallocated:
            continue
        if fixed[i] is not None:
            nets[i] = fixed[i]
        elif gross[i] is not None:
            nets[i] = q3_down(ratio * gross[i])

    # ---- 5b. cartons -----------------------------------------------------
    # Cartons are NOT independent of the gross allocation.  With no packing
    # carton evidence their basis IS the item gross, so an item whose gross
    # could not be allocated had a basis of zero — and zero is not "no
    # information" to an apportionment, it is "no weight".  One such item is
    # dropped onto the 0.01 floor; when EVERY gross is missing the basis is
    # uniformly zero and _largest_remainder collapses to an equal split. On
    # quantities 1/9/90 that gave 4.00/4.00/4.00 cartons, and the audit trail
    # still said "proportional by gross weight" — a plausible-looking column
    # that had nothing to do with weight, printed beside blank Gross and Net.
    #
    # An item with no allocated gross now takes its QUANTITY share instead,
    # rescaled into kilogram-equivalents by what the allocated items weigh per
    # unit of quantity — the same rule and the same rescale the weight ladder
    # uses (user rule 2026-07-22) — and the audit says which basis was used.
    # Packing-stated cartons are untouched: those really ARE independent of
    # the gross, which is what the old comment here assumed of all of them.
    carton_src = [("packing-list carton" if have_carton
                   else "proportional by gross weight")] * n
    if have_carton:
        # An item the packing list gives no carton for takes its share of the
        # cartons scaled by what the stated rows carry per kg — the same
        # estimate the gross basis uses, and for the same reason.  This path
        # only became reachable when Condition 1 and Condition 2 stopped being
        # mutually exclusive: before, a shipment with any packing gross never
        # used packing cartons at all.
        ctn_ev = [ev.carton_count or _ZERO for ev in evs]
        ctn_fb = [(gross[i] if gross[i] is not None else prov[i]) or _ZERO for i in range(n)]
        ctn_basis, carton_src, _ = _basis_with_estimates(
            ctn_ev, ctn_fb, "packing-list carton",
            "estimated from weight share (no packing-list carton)")
    else:
        ctn_basis = [(gross[i] or _ZERO) for i in range(n)]
        no_gross = [i for i in range(n) if gross[i] is None]
        if no_gross:
            qty = [(it.quantity if it.quantity > 0 else Decimal("1")) for it in items]
            ref = [i for i in range(n) if gross[i] is not None]
            ref_basis = sum((ctn_basis[i] for i in ref), _ZERO)
            ref_qty = sum((qty[i] for i in ref), _ZERO)
            scale = (ref_basis / ref_qty) if ref_basis > 0 and ref_qty > 0 else Decimal("1")
            for i in no_gross:
                ctn_basis[i] = qty[i] * scale
                carton_src[i] = "quantity share (gross weight not allocated)"
    # Reviewer-pinned cartons, on the 2dp lattice (the write path already
    # rejects anything else; q2 here only normalizes an exponent).
    pinned_ctn: list[Decimal | None] = [
        (q2(it.manual_package_count)
         if it.manual_package_count and it.manual_package_count > 0 else None)
        for it in items]
    # A row is carton EVIDENCE only when the packing list PRINTED a carton for
    # it.  A packing-list *weight* is not carton evidence: with no printed
    # cartons anywhere the whole column is derived from gross weight, so there
    # is no figure of anyone's to protect and every row is an estimate.
    ctn_evidence = [bool(have_carton and (ev.carton_count or _ZERO) > 0) for ev in evs]

    if auth_ctn is None:
        # No package authority: nothing to reconcile against, so the MUST rule
        # ("every CTN >= 0.01") does not apply — 0 is the explicit "no packages
        # declared" state.  A pin is still the reviewer's own figure and is
        # kept; it simply cannot be reconciled to anything.
        note = ("No authorised package count; item package counts left at 0.")
        if any(p is not None for p in pinned_ctn):
            note = ("No authorised package count: reviewer-entered item cartons are kept as "
                    "entered, every other item is left at 0, and nothing can be reconciled "
                    "until the shipment total packages are known.")
        warnings.append(ValidationMessage.warning("PACKAGES_MISSING", note))
        cartons = [(p if p is not None else _ZERO) for p in pinned_ctn]
        for i, p in enumerate(pinned_ctn):
            if p is not None:
                carton_src[i] = "reviewer-entered carton count"
    else:
        cartons, carton_src, ctn_msgs, ctn_blocked = _allocate_cartons(
            items, ctn_basis, carton_src, ctn_evidence, pinned_ctn,
            [(ev.carton_count or _ZERO) for ev in evs], auth_ctn)
        warnings.extend(ctn_msgs)
        blocked = blocked or ctn_blocked

    # ---- 6. write back + audit trail -------------------------------------
    for i, (it, ev) in enumerate(zip(items, evs)):
        if i in unallocated:
            # The authorised gross cannot cover the item net weights.  Assign
            # NOTHING rather than a proportional figure the reviewer would read
            # as a real allocation: the weight columns stay blank and the
            # shipment-level blocking message explains why.  The carton count
            # survives because step 5b put this item on its quantity share when
            # its gross went missing — it is a real basis, and it is named.
            it.gross_weight_kg = None
            it.net_weight_kg = None
            it.package_count = cartons[i]
            it.allocation_audit = {
                "invoice_line": it.xml_item_sequence,
                "invoice_item_description": it.description_raw,
                "matched_packing_item": ev.matched_name,
                "carton_source": carton_src[i],
                "gross_weight_source": "not allocated (authorised gross below total net)",
                "net_weight_source": (f"{net_src[i]} - rejected, {fixed[i]} kg"
                                      if fixed[i] is not None else net_src[i]),
                "estimated": estimated[i],
                "final_ctn": f"{cartons[i]:.2f}",
                "final_gross_weight_kg": "",
                "final_net_weight_kg": "",
                "validation_status": "BLOCKED",
            }
            continue
        it.gross_weight_kg = gross[i]
        it.net_weight_kg = nets[i]
        it.package_count = cartons[i]
        ok = nets[i] < gross[i] or blocked
        # gross == 0 was previously an escape hatch here, so an item with a
        # positive net and no gross passed silently.  A zero gross is invalid.
        if not nets[i] < gross[i]:
            it.warnings.append(ValidationMessage.blocking(
                "WEIGHT_RECONCILIATION_IMPOSSIBLE",
                f"Item {it.xml_item_sequence}: net {nets[i]} not < gross {gross[i]}",
                scope="ITEM", item_sequence=it.xml_item_sequence))
            ok = False
        elif q2(nets[i]) >= q2(gross[i]):
            # `net < gross` can hold at 3dp and vanish at the 2dp a customs
            # officer reads off the form.  Almost always an item that landed on
            # its apportionment floor, which is the signature of a bad basis.
            it.warnings.append(ValidationMessage.warning(
                "WEIGHT_GAP_INVISIBLE",
                f"Item {it.xml_item_sequence}: net {nets[i]} is below gross {gross[i]} only at "
                f"3 decimal places — both read as {q2(gross[i])} kg at the precision shown on "
                f"the declaration. Check this item's weight.",
                scope="ITEM", item_sequence=it.xml_item_sequence, field="net_weight"))
        carton_source = carton_src[i]
        # The shared-group note must not overwrite what actually decided this
        # row: a reviewer pin, or the rescale a pin forced on the printed
        # figures.  Losing that hides the pin from the audit trail entirely.
        if (have_carton and ev.carton_shared and pinned_ctn[i] is None
                and "reviewer carton pin" not in carton_source):
            carton_source = "shared carton allocation"
        it.allocation_audit = {
            "invoice_line": it.xml_item_sequence,
            "invoice_item_description": it.description_raw,
            "matched_packing_item": ev.matched_name,
            "carton_source": carton_source,
            "gross_weight_source": gross_src[i],
            "net_weight_source": net_src[i],
            "estimated": estimated[i],
            "final_ctn": f"{cartons[i]:.2f}",
            "final_gross_weight_kg": f"{gross[i]:.3f}",
            "final_net_weight_kg": f"{nets[i]:.3f}",
            "validation_status": "OK" if ok else "BLOCKED",
        }

    # ---- 7. the carton MUST rule, asserted rather than assumed ------------
    warnings.extend(_carton_lattice_check(items, auth_ctn, blocked))
    warnings.extend(_implied_unit_weight_check(items))
    return warnings


# How far an item's implied per-unit weight may sit from what the rest of the
# shipment carries in the SAME unit before it is worth a reviewer's attention.
# Deliberately wide: goods sold by the same unit still vary (a child's sandal
# and a work boot are both PRS), so this is an absurdity detector, not a
# tolerance.  A factor of ten either way is the boundary between "heavier than
# its neighbours" and "this figure cannot be a pair of shoes".
_UNIT_WEIGHT_BAND = Decimal("10")


def _implied_unit_weight_check(items: list[WorkItem]) -> list[ValidationMessage]:
    """Report an allocated weight whose implied PER-UNIT mass is not credible.

    Every check in this module so far asks whether the numbers add up.  None of
    them asks whether they are physically possible: a declaration can reconcile
    exactly to its authority and still claim 0.02 kg for a pair of shoes or
    400 kg for one keyboard, and nothing on screen would say so.

    The band is the shipment's own evidence — the median implied weight among
    items sold in the SAME unit, taken from rows the packing list actually
    stated — so no external reference table is needed and no assumption is made
    about what the goods are.  A unit the shipment cannot teach (fewer than two
    stated rows in that unit) is not judged at all.

    Reports only.  Nothing here changes an allocated figure: the exact-sum
    absolutes hold, and a weight that looks wrong is a question for the
    reviewer, not a number for this function to invent.
    """
    from statistics import median

    per_unit: dict[str, list[tuple[int, Decimal]]] = {}
    for i, it in enumerate(items):
        uom = (it.invoice_uom_raw or "").strip().upper()
        gross, qty = it.gross_weight_kg, it.quantity
        if not uom or not gross or not qty or qty <= 0 or gross <= 0:
            continue
        per_unit.setdefault(uom, []).append((i, gross / qty))
    out: list[ValidationMessage] = []
    for uom, rows in per_unit.items():
        stated = [v for i, v in rows
                  if "packing-list" in (items[i].allocation_audit or {}).get(
                      "gross_weight_source", "")]
        if len(stated) < 2:
            continue                          # the shipment cannot teach this unit
        typical = Decimal(str(median(sorted(float(v) for v in stated))))
        if typical <= 0:
            continue
        for i, value in rows:
            ratio = value / typical if typical else Decimal("1")
            if _UNIT_WEIGHT_BAND > ratio > (Decimal("1") / _UNIT_WEIGHT_BAND):
                continue
            it = items[i]
            out.append(ValidationMessage.warning(
                "ITEM_UNIT_WEIGHT_IMPLAUSIBLE",
                f"Item {it.xml_item_sequence} is allocated {q3(it.gross_weight_kg)} kg gross "
                f"across {it.quantity} {uom}, i.e. {q3(value)} kg per {uom} — the other "
                f"{uom} lines on this shipment average about {q3(typical)} kg per {uom}. "
                f"The totals still reconcile, so this is the weight EVIDENCE disagreeing, "
                f"not the arithmetic. Verify the packing weight or the quantity.",
                scope="ITEM", item_sequence=it.xml_item_sequence, field="gross_weight"))
    return out


def _carton_lattice_check(items: list[WorkItem], auth_ctn: Decimal | None,
                          already_blocked: bool) -> list[ValidationMessage]:
    """Every declared CTN is >= 0.01 and an exact multiple of 0.01, and they
    sum to the authority (user rule 2026-08-03, spec section 7).

    ``apportion`` guarantees all three by construction and the carton pin path
    is exact integer arithmetic on the same lattice — so this should never
    fire.  It is here because it is the one rule stated as a MUST, and a rule
    nothing checks is a rule that silently stops being true: a value off the
    lattice reaches ASYCUDA as a package count no one can reconcile, and the
    only visible symptom would be a customs rejection.
    """
    if auth_ctn is None or not items:
        return []
    bad = [it.xml_item_sequence for it in items
           if it.package_count is None or it.package_count < _MIN_CTN
           or it.package_count != q2(it.package_count)
           or it.package_count % _MIN_CTN != _ZERO]
    out: list[ValidationMessage] = []
    if bad:
        shown = ", ".join(str(s) for s in bad[:20]) + (" …" if len(bad) > 20 else "")
        out.append(ValidationMessage.blocking(
            "CARTON_LATTICE_VIOLATION",
            f"{len(bad)} item carton count(s) are not a whole multiple of 0.01 CTN at or above "
            f"the 0.01 minimum (SN {shown}). Every declared carton count must sit on the 0.01 "
            "lattice — this is an internal allocation fault, not a data problem."))
    total = sum((it.package_count or _ZERO for it in items), _ZERO)
    if not already_blocked and total != auth_ctn:
        out.append(ValidationMessage.blocking(
            "CARTON_RECONCILIATION_FAILED",
            f"Item carton counts sum to {total} against an authorised total of {auth_ctn} "
            "CTN — the allocation did not reconcile."))
    return out


# UOM tokens that are a mass unit to ``units.normalize_weight_unit`` but are
# NOT unambiguously one in an invoice quantity column.  ``MT`` is the dangerous
# one: metric ton on most invoices, METRE on some, and reading 500 metres of
# webbing as 500 000 kg inverts an entire consignment.  ``T`` and ``G`` carry
# the same shape of risk (tonne, and gram-vs-gross).  The spec already treats
# these tokens as ambiguous when the DESCRIPTION parser meets them; a unit-of-
# sale column is a stronger signal than free text, so they are USED here rather
# than refused — but never silently.  Same doctrine either way: ambiguity is
# reported, not resolved behind the reviewer's back.
_AMBIGUOUS_UOM = {"MT", "T", "G"}


def net_from_uom_quantity(it: WorkItem) -> tuple[Decimal | None, str, bool]:
    """A line invoiced BY WEIGHT states its own net weight (user rule 2026-08-04).

    ``RESIN 500 KG`` sells five hundred kilograms of goods: the quantity column
    IS that line's net weight, at LINE_TOTAL scope — it is not a per-unit
    figure to multiply by anything.  Before this, such a line took a ratio net
    (0.7 x an allocated gross) while the invoice stated the number outright,
    and for a KGM-tariff HS code that meant the supplementary quantity was
    derived from a figure nobody had written down.

    Every recognized mass unit counts, converted through the one ingest
    boundary (``units.to_kg``): matching only the literal "KG" would miss
    ``5 MT`` and under-declare it a thousandfold.  A blank unit is NOT accepted
    — ``to_kg`` treats a blank as already-kilograms, which is right for a
    weight column and completely wrong for a quantity column, where blank means
    "pieces" far more often than "kilograms".

    Returns ``(net_kg, source label, ambiguous)``; ``(None, "", False)`` when
    the line is not sold by weight.
    """
    qty = it.quantity
    if not qty or qty <= 0:
        return None, "", False
    printed = (it.invoice_uom_raw or "").strip().upper()
    # A UOM cell carrying a PACK SIZE is not a bare unit: `25 KG` on a line of
    # 200 means 200 bags of 25 kg = 5 000 kg, not 200 kg.  `normalize_weight_unit`
    # strips every non-letter, so `25 KG`, `500 GM` and `12 LB` all reduce to a
    # clean mass unit and the multiplier vanishes silently — a 25x, 500x or 12x
    # under-declaration with nothing on screen to show for it.  Reading the
    # multiplier here would be the description parser's pack-multiplier problem
    # all over again, and its gate (does the quantity count packs or pieces?)
    # cannot be answered from a unit cell.  So the source is DISQUALIFIED, which
    # is the same rule the spec applies to any unit it cannot trust.
    if any(ch.isdigit() for ch in printed):
        return None, "", False
    if normalize_weight_unit(printed) is None:      # blank or not a mass unit
        return None, "", False
    kg, recognized = to_kg(qty, printed)
    if not recognized or kg is None:
        return None, "", False
    # Quantize BEFORE the positivity test.  `250 MG` is 0.00025 kg, which is
    # above zero but rounds to 0.000 at the 3dp the declaration carries — and a
    # 0.000 returned here is not None, so the ladder would accept it as a STATED
    # net, outranking the description parser, the packing list and the ratio,
    # and declare the item at zero net weight.
    net = q3(kg)
    if net <= 0:
        return None, "", False
    token = re.sub(r"[^A-Z]", "", printed)
    return net, f"invoice quantity in {printed}", token in _AMBIGUOUS_UOM


def _basis_with_estimates(evidence: list[Decimal], fallback: list[Decimal],
                          matched_src: str, estimate_src: str,
                          groups: list[str] | None = None,
                          ) -> tuple[list[Decimal], list[str], list[int]]:
    """Per-item basis from packing evidence, with the silent items ESTIMATED.

    An item the packing list does not mention must never get a basis of zero.
    Zero does not mean "no information" to an apportionment, it means "no
    weight": the item lands on its floor and is declared at 0.001 kg with
    ``net < gross`` satisfied, the totals reconciling exactly and
    ``validation_status: OK`` — a missing match wearing a valid-looking number.

    So an unmentioned item keeps its share of the shipment, scaled to what the
    MATCHED items actually carry per unit of value/quantity/weight: the best
    evidence available about this consignment's goods.  Callers must have at
    least one matched item.
    """
    n = len(evidence)
    matched = [i for i in range(n) if evidence[i] > 0]
    unmatched = [i for i in range(n) if evidence[i] <= 0]
    matched_ev = sum((evidence[i] for i in matched), _ZERO)
    matched_fb = sum((fallback[i] for i in matched), _ZERO)
    # packing units per fallback unit; with no usable fallback among the
    # matched rows, the matched mean per item is the next best thing
    density = (matched_ev / matched_fb) if matched_fb > 0 else (matched_ev / len(matched))
    # Per UNIT OF SALE where the shipment can teach it.  One global density is
    # a single kilograms-per-dollar (or per-piece) for the whole consignment,
    # which is wrong in the way that matters: a metre of cloth, a pair of shoes
    # and a speaker do not weigh alike, and an unmatched line inherits whatever
    # mix happened to be matched.  Items sold in the same unit DO weigh alike
    # to a useful approximation, so kg/PRS is learned from the matched PRS
    # rows, kg/MTR from the matched MTR rows, and only a unit the shipment
    # cannot teach falls back to the global figure.
    per_group: dict[str, Decimal] = {}
    if groups:
        by_group: dict[str, list[int]] = {}
        for i in matched:
            by_group.setdefault(groups[i], []).append(i)
        for g, idx in by_group.items():
            fb = sum((fallback[i] for i in idx), _ZERO)
            if fb > 0:
                per_group[g] = sum((evidence[i] for i in idx), _ZERO) / fb
    basis: list[Decimal] = []
    source: list[str] = []
    for i in range(n):
        if evidence[i] > 0:
            basis.append(evidence[i])
            source.append(matched_src)
        else:
            rate = per_group.get(groups[i]) if groups else None
            if rate is not None and fallback[i] > 0:
                estimate = rate * fallback[i]
                src = f"{estimate_src}, per {groups[i]} learned from this shipment"
            else:
                estimate = density * fallback[i] if matched_fb > 0 else density
                src = estimate_src
            basis.append(estimate if estimate > 0 else Decimal("1"))
            source.append(src)
    return basis, source, unmatched


def _units(value: Decimal) -> int:
    """A CTN value as a whole number of 0.01 units — the carton lattice."""
    return int((value / _MIN_CTN).to_integral_value(rounding=ROUND_HALF_UP))


def _spread_units(total: int, weights: list[int]) -> list[int]:
    """Split ``total`` >= 0 whole units across the slots proportional to
    ``weights``, EXACTLY, by largest remainder.  All-zero weights split equally.

    Integer in, integer out: the carton column lives on the 0.01 lattice, so a
    redistribution expressed in units needs no rounding at all and cannot drift.
    """
    k = len(weights)
    if k == 0:
        return []
    w = list(weights)
    if sum(w) <= 0:
        w = [1] * k
    tw = sum(w)
    out = [total * wi // tw for wi in w]
    order = sorted(range(k), key=lambda j: (-((total * w[j]) % tw), j))
    for j in range(total - sum(out)):          # always fewer than k units left
        out[order[j]] += 1
    return out


def _absorb_into_estimates(cartons: list[Decimal], base: list[Decimal],
                           est: list[int], delta: int) -> tuple[int, list[int]]:
    """Concentrate ``delta`` whole 0.01 units into the highest ESTIMATED rows.

    ``est`` is ordered highest-baseline first.  Two limits shape the donor set,
    and they pull in OPPOSITE directions:

    * **granularity** caps it from above — a delta of 3 units cannot be shared
      by 10 rows, because a third of a unit is not on the lattice.  At most
      ``|delta|`` rows are used, so every donor moves by at least one unit.
    * **capacity** grows it — a donor may not fall below 0.01 CTN, so when the
      top rows have no headroom the set extends down the ordering.  Shrinking
      it (the granularity direction) would make a capacity shortfall worse.

    Returns ``(unabsorbed units, donor indices)``.  Anything unabsorbed spills
    to the packing-stated rows, which are rescaled as a whole rather than
    raided — see :func:`_allocate_cartons`.
    """
    if delta > 0:                          # REMOVE: bounded by headroom
        cap = {i: _units(base[i]) - 1 for i in est}
        pool = [i for i in est if cap[i] > 0]      # a row already on the floor gives nothing
        if not pool:
            return delta, []
        k = min(CTN_DONOR_MAX, len(pool), delta)
        while k < len(pool) and sum(cap[i] for i in pool[:k]) < delta:
            k += 1
        donors = pool[:k]
        take = min(delta, sum(cap[i] for i in donors))
        moves = _spread_units(take, [cap[i] for i in donors])
        for pos, i in enumerate(donors):
            cartons[i] = q2(base[i] - Decimal(moves[pos]) * _MIN_CTN)
        return delta - take, [i for pos, i in enumerate(donors) if moves[pos]]

    add = -delta                           # ADD: no ceiling, granularity only
    k = min(CTN_DONOR_MAX, len(est), add)
    donors = est[:k]
    moves = _spread_units(add, [_units(base[i]) for i in donors])
    for pos, i in enumerate(donors):
        cartons[i] = q2(base[i] + Decimal(moves[pos]) * _MIN_CTN)
    return 0, [i for pos, i in enumerate(donors) if moves[pos]]


def _allocate_cartons(items: list[WorkItem], basis: list[Decimal], src: list[str],
                      evidence: list[bool], pinned: list[Decimal | None],
                      printed: list[Decimal], auth_ctn: Decimal,
                      ) -> tuple[list[Decimal], list[str], list[ValidationMessage], bool]:
    """Allocate the authorised cartons, honouring reviewer pins EXACTLY.

    User rule 2026-08-03: a human-entered value is always accepted; the system
    adjusts the remaining items to keep the shipment authority.  Where that is
    arithmetically impossible — the pins alone leave the other items no room
    above the 0.01 minimum, or every item is pinned and the sum simply differs
    — the pins still stand and the message says what the shipment total would
    have to become, because the reviewer's own numbers are not the thing to
    give way.

    The redistribution runs in two tiers, for a reason worth keeping:
    ESTIMATED rows carry no information (nobody printed a carton for them), so
    the delta is CONCENTRATED into a few of them and the rest of the table does
    not move.  PACKING-STATED rows carry structure — their mutual ratios are a
    printed fact — so when the estimates cannot cover the delta those rows are
    SPREAD, rescaled proportionally as a whole, which preserves every pairwise
    ratio between them.

    Returns ``(cartons, carton sources, messages, blocked)``.
    """
    n = len(items)
    msgs: list[ValidationMessage] = []
    src = list(src)
    unpinned = [i for i in range(n) if pinned[i] is None]
    pin_ids = [i for i in range(n) if pinned[i] is not None]
    pin_sum = sum((pinned[i] for i in pin_ids), _ZERO)

    # A pin contradicting a printed carton is ACCEPTED — the reviewer saw the
    # document, and they are rank 1 of the override ladder — but never
    # silently: a typo here is invisible in a column of plausible numbers.
    for i in pin_ids:
        src[i] = "reviewer-entered carton count"
        if evidence[i] and printed[i] != pinned[i]:
            sn = items[i].xml_item_sequence
            msgs.append(ValidationMessage.warning(
                "REVIEWED_CTN_OVERRIDES_PACKING",
                f"Item {sn}: the packing list states {q2(printed[i])} CTN but you entered "
                f"{pinned[i]} CTN. Your value is used — verify it.",
                scope="ITEM", item_sequence=sn, field="package_count"))

    def reviewed_note(detail: str) -> ValidationMessage:
        return ValidationMessage.warning(
            "ITEM_CARTONS_REVIEWED",
            f"{len(pin_ids)} item carton count(s) reviewer-entered (sum {pin_sum} CTN); {detail}.")

    # ---- every item pinned: their sum IS the declaration ------------------
    if not unpinned:
        cartons = [pinned[i] for i in range(n)]
        if pin_sum != auth_ctn:
            msgs.append(ValidationMessage.blocking(
                "REVIEWED_CTN_TOTAL_MISMATCH",
                REVIEWED_CTN_MISMATCH_MSG
                + f" (items sum {pin_sum} CTN, authority {auth_ctn} CTN)",
                remediation=f"Set the shipment total packages to {pin_sum}."))
            return cartons, src, msgs, True
        msgs.append(reviewed_note("every item carton count is reviewer-controlled"))
        return cartons, src, msgs, False

    # ---- feasibility: every unpinned item must keep at least 0.01 --------
    # Only a PIN can make this the reviewer's conflict.  With no pins the same
    # shortfall means the authorised carton total is simply too small for the
    # item count, which is CARTON_ALLOCATION_IMPOSSIBLE below — blaming the
    # reviewer for it would name a cause that does not exist.
    floor_need = _MIN_CTN * len(unpinned)
    if pin_ids and auth_ctn - pin_sum < floor_need:
        msgs.append(ValidationMessage.blocking(
            "REVIEWED_CTN_EXCEEDS_AUTHORITY",
            REVIEWED_CTN_EXCEEDS_MSG
            + f" (pinned sum {pin_sum} CTN across {len(pin_ids)} item(s) against an authorised "
              f"{auth_ctn} CTN; the other {len(unpinned)} item(s) need {floor_need} CTN)",
            remediation=("Set the shipment total packages to at least "
                         f"{q2(pin_sum + floor_need)}.")))
        # pins stand; the rest sit on the minimum so no row leaves the lattice
        return ([(pinned[i] if pinned[i] is not None else _MIN_CTN) for i in range(n)],
                src, msgs, True)

    # ---- deterministic NO-PIN baseline -----------------------------------
    # The delta is measured against this, never against "what was on screen
    # last time": the overlay is replayed from scratch on every recompute, so
    # the result must depend only on the SET of pins and not on the order the
    # reviewer entered them.  Two reviewers making the same edits in different
    # orders must get the same declaration.
    base = apportion(auth_ctn, basis, [_MIN_CTN] * n, _MIN_CTN)
    if base is None:
        msgs.append(ValidationMessage.blocking(
            "CARTON_ALLOCATION_IMPOSSIBLE", CARTON_TOO_SMALL_MSG))
        return [_MIN_CTN] * n, src, msgs, True
    # When the packing list PRINTS a carton count, that count is the document's
    # own statement and `apportion` has just used it as a proportional shape —
    # rescaling every one of them to the authority.  Silently, until now: a
    # 385-vs-492 shortfall on the live 2026-08-04 job multiplied all fifteen
    # printed counts by 1.2779 and declared 2.56 and 6.39 physical cartons for
    # lines the document prints as whole boxes.  The blast radius is the whole
    # table, not the misread rows — ONE bad reading moves every count, exactly
    # the pathology section 5 documents for the weight column.
    #
    # The same haircut forced by a reviewer PIN is already loud
    # (CTN_PIN_RESCALED_PACKING_EVIDENCE, spec D13).  It must be no quieter
    # when the authority causes it.
    # Measured on the RESULT, never inferred from the sums: printed cartons
    # legitimately coexist with weight-derived estimates on the same shipment,
    # so "evidence sum != authority" does not by itself mean anything moved.
    # The only question that matters is whether an item now declares a count
    # its packing list does not print.
    moved = [i for i in range(n) if evidence[i] and printed[i] > 0
             and base[i] != printed[i]]
    if moved:
        stated = sum((printed[i] for i in moved), _ZERO)
        got = sum((base[i] for i in moved), _ZERO)
        est_floor = _MIN_CTN * sum(1 for i in range(n) if not evidence[i])
        msgs.append(ValidationMessage.warning(
            "PACKING_CTN_TOTAL_MISMATCH",
            f"The packing list prints a carton count for {len(moved)} item(s) totalling "
            f"{q2(stated)} CTN, but the authorised total of {q2(auth_ctn)} CTN forced them to "
            f"{q2(got)} — those items no longer show the count their packing list prints. "
            f"Re-read the carton column or correct the shipment total packages.",
            scope="JOB", field="package_count",
            remediation=f"Set the shipment total packages to "
                        f"{q2(stated + est_floor + sum((base[i] for i in range(n) if i not in moved and evidence[i]), _ZERO))}."))
        factor = (got / stated) if stated > 0 else Decimal("1")
        for i in moved:
            src[i] = (f"{src[i]}, scaled x{factor:.4f} to the authorised "
                      f"{q2(auth_ctn)} CTN (packing list states {q2(printed[i])})")
    if not pin_ids:
        return base, src, msgs, False

    cartons = list(base)
    for i in pin_ids:
        cartons[i] = pinned[i]
    # Whole 0.01 units to take OUT of the unpinned rows (negative = to add).
    delta = sum(_units(pinned[i]) - _units(base[i]) for i in pin_ids)

    est = sorted((i for i in unpinned if not evidence[i]), key=lambda i: (-base[i], i))
    evid = [i for i in unpinned if evidence[i]]
    donors: list[int] = []
    rescaled_evidence = False
    leftover = delta
    if delta and est:
        leftover, donors = _absorb_into_estimates(cartons, base, est, delta)
        for i in donors:
            moved = q2(Decimal(_units(cartons[i]) - _units(base[i])) * _MIN_CTN)
            src[i] = f"{src[i]}, {moved:+} CTN absorbed for reviewer carton pin"

    if leftover and evid:
        # The estimates could not cover it, so a printed figure has to move.
        # Rescale the packing-stated rows AS A SET: a uniform proportional
        # haircut preserves every ratio between them, which raiding the ten
        # largest would not.  Shared carton groups scale by the same factor, so
        # their internal division survives too.
        before = sum((base[i] for i in evid), _ZERO)
        target = q2(before - Decimal(leftover) * _MIN_CTN)
        shares = apportion(target, [base[i] for i in evid], [_MIN_CTN] * len(evid), _MIN_CTN)
        if shares is None:                    # unreachable: the feasibility gate above
            msgs.append(ValidationMessage.blocking(
                "REVIEWED_CTN_EXCEEDS_AUTHORITY", REVIEWED_CTN_EXCEEDS_MSG))
            return cartons, src, msgs, True
        for pos, i in enumerate(evid):
            cartons[i] = shares[pos]
        leftover, rescaled_evidence = 0, True
        factor = (target / before) if before > 0 else Decimal("1")
        for i in evid:
            src[i] = f"{src[i]}, scaled x{factor:.4f} to reconcile reviewer carton pin(s)"
        msgs.append(ValidationMessage.warning(
            "CTN_PIN_RESCALED_PACKING_EVIDENCE",
            f"Your carton pins are large enough that the estimated items could not absorb them: "
            f"{len(evid)} item(s) whose cartons the packing list PRINTS were rescaled "
            f"(x{factor:.4f}, {before} -> {target} CTN) to keep the authorised total. Their "
            f"ratios to one another are preserved, but these are document figures — check the "
            f"pins, or correct the shipment total packages."))
    elif leftover:                            # no estimates and no evidence left
        msgs.append(ValidationMessage.blocking(
            "REVIEWED_CTN_EXCEEDS_AUTHORITY", REVIEWED_CTN_EXCEEDS_MSG))
        return cartons, src, msgs, True

    # A distribution can satisfy every invariant and still be degenerate: one
    # large pin can push a long tail of items onto the 0.01 minimum.  That is
    # arithmetically valid and practically wrong, so say it.
    on_floor = [items[i].xml_item_sequence for i in unpinned
                if cartons[i] <= _MIN_CTN < base[i]]
    if on_floor:
        shown = ", ".join(str(s) for s in on_floor[:20]) + (" …" if len(on_floor) > 20 else "")
        msgs.append(ValidationMessage.warning(
            "CTN_DONORS_ON_FLOOR",
            f"{len(on_floor)} of {len(unpinned)} unpinned item(s) were reduced to the 0.01 CTN "
            f"minimum to reconcile your carton pins (SN {shown}). Their carton counts no longer "
            f"mean anything — lower the pins or raise the shipment total packages."))

    parts = []
    if donors:
        parts.append(f"{len(donors)} estimated item(s) absorbed the difference")
    if rescaled_evidence:
        parts.append(f"{len(evid)} packing-stated item(s) were rescaled proportionally")
    msgs.append(reviewed_note(" and ".join(parts) if parts
                              else "the other items were left unchanged"))
    return cartons, src, msgs, False


def _largest_remainder(total: Decimal, basis: list[Decimal],
                       places: Decimal) -> list[Decimal]:
    """Split ``total`` proportional to ``basis`` with an EXACT sum at ``places``.

    Hamilton apportionment: floor every ideal share, then hand the remaining
    whole units to the largest fractional remainders.  The shortfall is always a
    small integral number of units, so it is spread across several items rather
    than dumped on one (which is how the previous carton allocator could drive
    an item negative and then silently clamp it, breaking the exact-sum
    invariant it advertised).
    """
    n = len(basis)
    weight = list(basis)
    if sum(weight, _ZERO) <= 0:
        weight = [Decimal(1)] * n                  # no usable basis -> equal split
    tw = sum(weight, _ZERO)
    ideal = [total * w / tw for w in weight]
    out = [v.quantize(places, rounding=ROUND_DOWN) for v in ideal]
    units = int(((total - sum(out, _ZERO)) / places).to_integral_value(
        rounding=ROUND_HALF_UP))
    order = sorted(range(n), key=lambda i: (-(ideal[i] - out[i]), i))
    for k in range(units):
        out[order[k % n]] += places
    return out


def apportion(total: Decimal, basis: list[Decimal], floor: list[Decimal],
              places: Decimal, ceiling: list[Decimal | None] | None = None
              ) -> list[Decimal] | None:
    """Distribute ``total`` proportional to ``basis``, honouring per-item floors
    and — where given — per-item ceilings.

    Guarantees:

    * ``sum(result) == total`` EXACTLY at ``places``
    * ``result[i] >= floor[i]`` for every i
    * ``result[i] <= ceiling[i]`` for every i whose ceiling is not None
    * ``None`` (infeasible) when ``sum(floor) > total`` or when the ceilings
      cannot absorb ``total`` -- never an approximation

    A floor is a CONSTRAINT, not an additive base: the pure proportional split
    is used whenever it already clears every floor, so a large fixed net on one
    item does not distort the shares of the others.  Only when a floor actually
    binds does each item take its floor plus a proportional share of what is
    left.

    A ceiling binds the same way but from above, and is what stops the exact-sum
    rule from inflating a line whose weight the INVOICE states (user rule
    2026-08-04: a gross may not exceed 1.2 x an invoice-stated net).  Capped
    items are held at their cap and the remainder is re-apportioned across the
    rest, repeatedly, because releasing weight onto the uncapped items can push
    one of THEM over its own cap.

    ``total`` and every ``floor`` entry must already be quantized to ``places``.
    """
    n = len(basis)
    if n == 0:
        return []
    floor_sum = sum(floor, _ZERO)
    if floor_sum > total:
        return None

    def _split(tot: Decimal, idx: list[int]) -> list[Decimal]:
        sub = _largest_remainder(tot, [basis[i] for i in idx], places)
        if any(sub[k] < floor[idx[k]] for k in range(len(idx))):
            sub_floor = sum((floor[i] for i in idx), _ZERO)
            rest = _largest_remainder(tot - sub_floor, [basis[i] for i in idx], places)
            sub = [floor[idx[k]] + rest[k] for k in range(len(idx))]
        return sub

    if not ceiling or all(c is None for c in ceiling):
        return _split(total, list(range(n)))

    # Cap-and-redistribute until nothing more binds.  Bounded by n passes: each
    # pass pins at least one item, and a pass that pins none is the fixed point.
    pinned: dict[int, Decimal] = {}
    for _ in range(n + 1):
        free = [i for i in range(n) if i not in pinned]
        if not free:
            break
        remaining = total - sum(pinned.values(), _ZERO)
        if remaining < _ZERO:
            return None
        shares = _split(remaining, free)
        over = [(free[k], shares[k]) for k in range(len(free))
                if ceiling[free[k]] is not None and shares[k] > ceiling[free[k]]]
        if not over:
            out = [_ZERO] * n
            for k, i in enumerate(free):
                out[i] = shares[k]
            for i, v in pinned.items():
                out[i] = v
            return out
        for i, _v in over:
            if ceiling[i] < floor[i]:
                return None                  # the cap sits below the item's floor
            pinned[i] = ceiling[i]
    return None                              # ceilings cannot absorb the total


# The gross a line may carry above an INVOICE-stated net.  Same 1.2 the
# provisional basis uses — but here it is a CEILING, not a weighting.
_INVOICE_GROSS_FACTOR = Decimal("1.2")
# Net sources that are the invoice speaking about THIS item's weight: the
# printed weight column (rank 2) and a quantity sold in a mass unit (rank 3).
# Deliberately not the packing-list net (its own gross sits beside it), the
# ratio net (gross by construction), or a reviewer pin (exact and supreme).
_INVOICE_NET_SOURCES = ("invoice weight override", "invoice quantity in ")


def _invoice_gross_cap(fixed_net: Decimal | None, source: str) -> Decimal | None:
    """1.2 x the net when the INVOICE stated it, else no cap."""
    if fixed_net is None or fixed_net <= 0:
        return None
    if not any(source.startswith(p) for p in _INVOICE_NET_SOURCES):
        return None
    return q3(fixed_net * _INVOICE_GROSS_FACTOR)


def _feasibility_notes(items: list[WorkItem], nets: list[Decimal], net_src: list[str],
                       auth_gross: Decimal) -> list[ValidationMessage]:
    """Name the CAUSE when the derived nets cannot fit the authorised gross.

    Run before reconciliation, writes nothing.  ``sum(net) / auth_gross`` is the
    diagnostic: ~1000 is a gram value read as kilograms, ~24 a pack multiplier
    applied against a piece count, ~2.2 pounds read as kilograms, ~0.7 healthy.
    Without this the reviewer sees only the symptom — every item's net above its
    gross — with nothing pointing at which source produced it.
    """
    out: list[ValidationMessage] = []
    if auth_gross <= 0 or not items:
        return out
    # a single item heavier than the whole consignment is provably wrong
    for i in range(len(items)):
        if nets[i] > auth_gross:
            sn = items[i].xml_item_sequence
            out.append(ValidationMessage.warning(
                "ITEM_NET_EXCEEDS_SHIPMENT",
                f"Item {sn}: derived net weight {q4(nets[i])} kg exceeds the entire authorised "
                f"shipment gross of {q4(auth_gross)} kg (source: {net_src[i]}) — the weight or "
                "its unit is wrong.", scope="ITEM", item_sequence=sn, field="net_weight"))
    total_net = sum(nets, _ZERO)
    ratio = total_net / auth_gross
    if ratio > Decimal("0.95"):
        hint = ("~1000x, a gram value read as kilograms" if ratio > 100 else
                "~24x, a pack multiplier applied to a piece count" if ratio > 10 else
                "~2.2x, pounds read as kilograms" if ratio > Decimal("1.8") else
                "marginal — packaging would have to weigh almost nothing")
        worst = sorted(range(len(items)), key=lambda i: nets[i], reverse=True)[:3]
        out.append(ValidationMessage.warning(
            "NET_TO_GROSS_RATIO_IMPLAUSIBLE",
            f"Item net weights total {q4(total_net)} kg against an authorised gross of "
            f"{q4(auth_gross)} kg (ratio {ratio:.2f} — {hint}). Largest contributors: "
            + "; ".join(f"SN {items[i].xml_item_sequence} {q4(nets[i])} kg via {net_src[i]}"
                        for i in worst) + "."))
    return out
