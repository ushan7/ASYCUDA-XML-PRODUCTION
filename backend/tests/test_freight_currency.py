"""Freight is chosen across currencies, so the currency has to travel with it (A4).

`select_freight`'s rule is "the highest candidate, to avoid undervaluation".
The candidates are an invoice total (invoice currency), an air-waybill charge
box (whatever the carrier billed in) and a SWIFT mention (whatever it settled
in) — and `max()` compared the bare numbers.  The winner was then stamped with
the INVOICE currency and handed to valuation, which multiplies by that
currency's NRB rate.  An air waybill printing EUR 4,708 against a USD 4,000
invoice was declared as USD 4,708 and converted at the USD rate, with no
warning at any level.

The bias runs one way: a numerically larger figure wins, so the stronger unit
wins a comparison it should never have been entered into.  The waybill's
currency was even being read already — `_money_currency` — and used only to
decorate a display string.

Rules now:
  * a candidate whose printed currency differs from the invoice currency is
    never auto-selected, and is reported with both currencies named;
  * a candidate with no printed currency is still assumed to be in the invoice
    currency (unchanged — for most shipments the documents agree);
  * a manual reviewer amount always wins: the input is labelled with the
    invoice currency.

The insurance premium is the same class of bug from the other side: it is added
to the CIF as a NATIONAL-currency amount and never converted, while sitting in
an unlabelled box directly beneath a freight box labelled in the invoice
currency.  The review now states which currency it expects.
"""
from decimal import Decimal

from app.config import get_settings
from app.rules.freight import select_freight


def _codes(result):
    return {w.code for w in result.warnings}


# --------------------------------------------------------------------------- #
# The regression
# --------------------------------------------------------------------------- #
def test_a_stronger_currency_no_longer_wins_the_comparison():
    r = select_freight(Decimal("4000"), Decimal("4708"), None, None, "USD",
                       awb_currency="EUR")
    # the USD invoice figure is selected; the larger EUR number is not
    assert r.source == "INVOICE"
    assert r.effective_freight_foreign == Decimal("4000.00")
    assert r.currency == "USD"
    assert "FREIGHT_CURRENCY_MISMATCH" in _codes(r)


def test_the_mismatch_warning_names_both_currencies_and_the_amount():
    r = select_freight(Decimal("4000"), Decimal("4708"), None, None, "USD",
                       awb_currency="EUR")
    w = next(x for x in r.warnings if x.code == "FREIGHT_CURRENCY_MISMATCH")
    assert "EUR 4708.00" in w.message
    assert "USD" in w.message
    assert w.field == "external_freight"


def test_a_foreign_only_candidate_is_not_declared_as_the_invoice_currency():
    """Nothing comparable exists — the freight must be 0.00 pending a reviewer
    conversion, never the foreign number wearing the invoice's currency."""
    r = select_freight(None, Decimal("4708"), None, None, "USD", awb_currency="EUR")
    assert r.effective_freight_foreign == Decimal("0.00")
    assert r.source == "MISSING_ZERO"
    assert "FREIGHT_CURRENCY_MISMATCH" in _codes(r)
    # not the generic "no freight found" — the amount exists, it is unusable
    assert "FREIGHT_MISSING" not in _codes(r)


def test_banking_currency_is_checked_too():
    r = select_freight(Decimal("500"), None, Decimal("90000"), None, "USD",
                       banking_currency="NPR")
    assert r.source == "INVOICE" and r.effective_freight_foreign == Decimal("500.00")
    assert "FREIGHT_CURRENCY_MISMATCH" in _codes(r)


# --------------------------------------------------------------------------- #
# Unchanged behaviour
# --------------------------------------------------------------------------- #
def test_same_currency_candidates_still_take_the_highest():
    r = select_freight(Decimal("100"), Decimal("120"), None, None, "USD",
                       awb_currency="USD")
    assert r.source == "AWB" and r.effective_freight_foreign == Decimal("120.00")
    assert not r.warnings


def test_an_unprinted_currency_is_assumed_to_be_the_invoice_currency():
    r = select_freight(Decimal("100"), Decimal("120"), None, None, "USD")
    assert r.source == "AWB" and r.effective_freight_foreign == Decimal("120.00")
    assert "FREIGHT_CURRENCY_MISMATCH" not in _codes(r)


def test_manual_override_wins_regardless_of_document_currencies():
    r = select_freight(Decimal("100"), Decimal("4708"), None, Decimal("250"), "USD",
                       awb_currency="EUR")
    assert r.source == "MANUAL_OVERRIDE"
    assert r.effective_freight_foreign == Decimal("250.00")


def test_explicit_zero_override_still_wins():
    r = select_freight(Decimal("100"), None, None, Decimal("0"), "USD")
    assert r.source == "MANUAL_OVERRIDE" and r.effective_freight_foreign == Decimal("0.00")


def test_no_candidates_at_all_reports_missing_not_mismatch():
    r = select_freight(None, None, None, None, "USD")
    assert r.source == "MISSING_ZERO"
    assert "FREIGHT_MISSING" in _codes(r)


def test_case_and_whitespace_do_not_manufacture_a_mismatch():
    r = select_freight(Decimal("100"), Decimal("120"), None, None, "usd",
                       awb_currency=" USD ")
    assert r.source == "AWB"
    assert "FREIGHT_CURRENCY_MISMATCH" not in _codes(r)


# --------------------------------------------------------------------------- #
# The reviewer-facing surface
# --------------------------------------------------------------------------- #
def test_candidate_chips_carry_currency_and_a_comparability_flag():
    from types import SimpleNamespace
    from app.pipeline import ResolvedContext, freight_candidates

    ctx = ResolvedContext(
        inv=SimpleNamespace(currency="USD"), ship=SimpleNamespace(), banking=SimpleNamespace(),
        items=[], packing_evidence={},
        invoice_freight=Decimal("4000"), awb_freight=Decimal("4708"), banking_freight=None,
        exchange_rate=Decimal("145.76"), awb_freight_currency="EUR")

    chips = {c["source"]: c for c in freight_candidates(ctx)}
    assert chips["INVOICE"]["currency"] == "USD" and chips["INVOICE"]["comparable"] is True
    # the EUR chip is shown (it is real evidence) but flagged unclickable
    assert chips["AWB"]["currency"] == "EUR" and chips["AWB"]["comparable"] is False


def test_review_states_the_currency_the_insurance_box_expects():
    """It is added to the CIF unconverted, so it must not read as the invoice
    currency the freight box above it is labelled with."""
    from app.demo import seed_demo_job
    from app.database import SessionLocal, init_db
    from app import services

    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    review = services.critical_review(db, job)
    assert review["insurance_currency"] == get_settings().national_currency
    assert review["insurance_currency"] != review["goods_currency"]
    db.close()
