"""Freight (and insurance/other-cost) selection + item allocation.

An air waybill's own contribution is its bottom-line ``Total Prepaid`` /
``Total Collect`` box (weight charge PLUS other charges), scaled to this
consignment by gross weight — see :func:`awb_charge_total` and
:func:`prorate_to_authority`.

Effective freight is selected deterministically from the available sources
(manual override wins, including an explicit zero; otherwise the configured rule
— currently the maximum of invoice and AWB/MAWB freight).  It is then allocated
across items by the configured basis: invoice-value share (matches the sample
XML) or gross-weight share (freight_rules.txt preferred).  Item amounts are
rounded to 2dp with the residual applied to the largest share so the sum is
exact.  All amounts are in foreign currency; conversion happens in valuation.
"""
from __future__ import annotations

from decimal import Decimal

from ..config import get_settings
from ..domain.errors import ValidationMessage
from ..numbers import q2
from .models import FreightResult, WorkItem


def awb_charge_total(
    total_prepaid: Decimal | None,
    total_collect: Decimal | None,
    weight_charge: Decimal | None,
    valuation_charge: Decimal | None,
    tax_charge: Decimal | None,
    other_charges: Decimal | None,
    printed_freight: Decimal | None,
) -> tuple[Decimal | None, str, list[ValidationMessage]]:
    """The air waybill's own TOTAL freight charge, before any weight proration.

    User rule 2026-07-21: the consignment's freight is the waybill's bottom-line
    ``Total Prepaid`` (or ``Total Collect``) box — the weight charge PLUS every
    other charge.  The rate line's ``Total`` column / ``Weight Charge`` box is
    only one component and using it silently undervalues the declaration (real
    failure: EUR 4653.00 weight charge taken instead of EUR 4708.00 total
    prepaid = 4653.00 + 55.00 AWC).

    Ladder — a printed grand total always wins over anything reconstructed:

        1. Total Prepaid            (the printed grand total)
        2. Total Collect            (freight-collect shipments)
        3. weight + valuation + tax + other charges   (grand-total box missing)
        4. whatever the extractor put in ``freight_amount``

    Returns ``(amount, detail, warnings)``; ``amount`` is None only when the
    waybill carries no readable charge at all.
    """
    warnings: list[ValidationMessage] = []

    def _positive(v: Decimal | None) -> Decimal | None:
        return v if v is not None and v > 0 else None

    parts = [p for p in (weight_charge, valuation_charge, tax_charge, other_charges) if p is not None]
    # A sum without the weight charge is not a freight total, only a surcharge.
    components = sum(parts, Decimal("0")) if _positive(weight_charge) else None

    grand = _positive(total_prepaid) or _positive(total_collect)
    if grand is not None:
        which = "Total Prepaid" if _positive(total_prepaid) else "Total Collect"
        detail = f"{which} {q2(grand)}"
        if components is not None and components > grand:
            # The printed grand total must cover its own components; if it does
            # not, one of the boxes was misread — say so instead of silently
            # declaring the lower number.
            warnings.append(ValidationMessage.warning(
                "AWB_CHARGE_TOTAL_MISMATCH",
                f"The air waybill's {which} ({q2(grand)}) is LESS than its own charge boxes "
                f"({q2(components)} = weight/valuation/tax/other charges). One of the boxes was "
                f"misread — verify the freight against the waybill before finalizing."))
        elif _positive(weight_charge) and grand > weight_charge:
            detail += f" (weight charge {q2(weight_charge)} + other charges {q2(grand - weight_charge)})"
        return q2(grand), detail, warnings

    if components is not None:
        detail = (f"no Total Prepaid/Collect box; weight charge {q2(weight_charge)} + other charges "
                  f"{q2(components - weight_charge)} = {q2(components)}")
        return q2(components), detail, warnings

    if printed_freight is not None:
        return q2(printed_freight), f"printed freight {q2(printed_freight)}", warnings
    return None, "", warnings


def prorate_to_authority(
    total: Decimal, form_gross: Decimal | None, authority_gross: Decimal | None,
) -> tuple[Decimal, str | None]:
    """Scale a waybill's total charge to this consignment's share of it.

    User rule: ``freight = total prepaid / MAWB weight x authority weight``.
    The divisor is the waybill's own GROSS weight — never its chargeable or
    volumetric weight, which is routinely far larger (550 vs 236 kg on the
    reference document) and would slash the freight.

    Only a waybill covering MORE weight than this consignment is prorated: an
    equal weight is this consignment's own waybill (ratio 1), and a smaller one
    would mean the authority weight is wrong, so scaling UP there would inflate
    the declared value off a bad number.  Returns ``(amount, note)``.
    """
    if not form_gross or not authority_gross or form_gross <= authority_gross:
        return q2(total), None
    share = q2(total * authority_gross / form_gross)
    return share, (f"covers {form_gross} kg; prorated to authority {authority_gross} kg "
                   f"({total} x {authority_gross}/{form_gross}) = {share}")


def select_freight(
    invoice_freight: Decimal | None,
    awb_freight: Decimal | None,
    banking_freight: Decimal | None,
    manual_override: Decimal | None,
    currency: str,
    *,
    awb_currency: str = "",
    banking_currency: str = "",
) -> FreightResult:
    """The consignment's freight, in the INVOICE currency.

    The selection rule is "the highest candidate, to avoid undervaluation", and
    the amounts are not all denominated in the same money.  An air waybill
    prints its charges in whatever currency the carrier billed; a SWIFT message
    in the currency it settled.  Comparing those with `max()` and then handing
    the winner to valuation — which multiplies by the invoice currency's NRB
    rate — turns EUR 4,708 into USD 4,708 without a word.  It is also biased:
    the numerically larger number wins, so the stronger unit wins the
    comparison it should never have been in.

    A candidate whose printed currency differs from the invoice currency is
    therefore never auto-selected.  It is reported instead, with both
    currencies named, for the reviewer to convert and enter.  A candidate with
    no printed currency is assumed to be in the invoice currency (unchanged
    behaviour — most invoices and waybills for a shipment agree).
    """
    warnings: list[ValidationMessage] = []
    if manual_override is not None:
        # the reviewer types into a box labelled with the invoice currency
        return FreightResult(q2(manual_override), currency, "MANUAL_OVERRIDE", warnings)

    declared = (currency or "").strip().upper()

    def _comparable(raw: str) -> bool:
        other = (raw or "").strip().upper()
        return not other or not declared or other == declared

    comparable: dict[str, Decimal] = {}
    foreign: list[tuple[str, Decimal, str]] = []
    for name, amount, cur in (("INVOICE", invoice_freight, declared),
                              ("AWB", awb_freight, awb_currency),
                              ("BANKING", banking_freight, banking_currency)):
        if amount is None:
            continue
        if _comparable(cur):
            comparable[name] = amount
        else:
            foreign.append((name, amount, (cur or "").strip().upper()))

    for name, amount, cur in foreign:
        warnings.append(ValidationMessage.warning(
            "FREIGHT_CURRENCY_MISMATCH",
            f"The {name} freight of {cur} {q2(amount)} is not in the invoice currency "
            f"({declared}), so it was NOT selected automatically — comparing or declaring it "
            f"as {declared} would misstate the customs value. Convert it and enter the amount "
            f"in Critical Review.",
            scope="XML_FIELD", field="external_freight"))

    if not comparable:
        if not foreign:
            warnings.append(ValidationMessage.warning(
                "FREIGHT_MISSING", "No freight found; used 0.00 (review required)."))
        return FreightResult(Decimal("0.00"), currency, "MISSING_ZERO", warnings)

    # configured rule: highest comparable candidate, to avoid undervaluation
    source = max(comparable, key=lambda k: comparable[k])
    return FreightResult(q2(comparable[source]), currency, source, warnings)


def _shares(items: list[WorkItem], basis: str) -> list[Decimal]:
    if basis == "gross_weight" and all(i.gross_weight_kg for i in items):
        return [i.gross_weight_kg or Decimal("0") for i in items]
    return [i.line_total if i.line_total > 0 else Decimal("0") for i in items]  # value share


def allocate_cost(items: list[WorkItem], total: Decimal, field: str, basis: str | None = None) -> None:
    """Allocate ``total`` across items into ``field`` (item_external_freight/item_insurance)."""
    settings = get_settings()
    basis = basis or settings.cost_allocation_basis
    if not items or total == 0:
        for it in items:
            setattr(it, field, Decimal("0.00"))
        return
    shares = _shares(items, basis)
    denom = sum(shares) or Decimal("1")
    alloc = [q2(total * s / denom) for s in shares]
    residual = q2(total - sum(alloc))
    if alloc:
        k = max(range(len(items)), key=lambda i: shares[i])
        alloc[k] = q2(alloc[k] + residual)
    for it, a in zip(items, alloc):
        setattr(it, field, a)
