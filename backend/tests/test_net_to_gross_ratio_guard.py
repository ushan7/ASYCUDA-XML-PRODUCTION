"""The net-to-gross ratio cannot be configured into an invalid declaration (A1).

ADR-003 was resolved to 0.7 on 2026-07-21, but `.env.example` kept shipping
`EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO=0.3` — in the very file whose first line
tells you to copy it to `backend/.env`.  An env line outranks the code default,
so following the documented setup silently rewrote the net weight of every item
with no packing net, no invoice weight and no convertible description (on the
reference shipment: all 119 of them) from 0.7 x gross to 0.3 x gross.  Nothing
on screen, in the audit trail or in the XML said which ratio produced the
numbers.  This is the drift docs/allocation-spec.md exists to prevent.

Two gates, at the two places the value can go wrong:

* a ratio outside (0, 1) is refused where it enters — at r >= 1 every ratio
  item asserts a net at or above its own gross, at r <= 0 it declares a zero or
  negative net weight;
* a ratio that is merely DIFFERENT from ADR-003 is legal (it is a documented
  override) but never silent: it is reported to the reviewer as a warning and
  published on /api/config.
"""
import os

import pytest

from decimal import Decimal

from app.config import ADR_003_NET_TO_GROSS_RATIO, Settings, get_settings
from app.rules.models import WorkItem
from app.rules.packing_match import PackingEvidence
from app.rules.weight_carton import allocate_weights_and_cartons


def _item(sn: int, desc: str, qty: str, total: str) -> WorkItem:
    return WorkItem(
        xml_item_sequence=sn, source_invoice_number="INV-1", source_invoice_date="",
        source_invoice_item_index=sn, source_invoice_item_no=str(sn),
        description_raw=desc, quantity=Decimal(qty), invoice_uom_raw="PCS",
        unit_price=Decimal(total) / Decimal(qty), line_total=Decimal(total),
        currency="USD")


# --------------------------------------------------------------------------- #
# The value that ships
# --------------------------------------------------------------------------- #
def test_env_example_publishes_the_resolved_adr_003_ratio():
    """The template must not re-introduce the pre-resolution 0.3."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, ".env.example"), encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh
                 if ln.strip().startswith("EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO=")]
    assert lines == [f"EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO={ADR_003_NET_TO_GROSS_RATIO}"]


def test_code_default_is_the_adr_003_value():
    assert get_settings().default_net_to_gross_ratio == ADR_003_NET_TO_GROSS_RATIO
    assert get_settings().net_to_gross_ratio_note() is None


# --------------------------------------------------------------------------- #
# Refused at the boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["0", "-0.7", "1", "1.2", "7"])
def test_ratio_outside_the_open_unit_interval_is_refused(bad):
    with pytest.raises(ValueError) as exc:
        Settings(default_net_to_gross_ratio=Decimal(bad))
    assert "0.7" in str(exc.value)          # names the resolved value to use


@pytest.mark.parametrize("ok", ["0.3", "0.5", "0.7", "0.95"])
def test_ratios_inside_the_interval_are_accepted(ok):
    assert Settings(default_net_to_gross_ratio=Decimal(ok)).default_net_to_gross_ratio \
        == Decimal(ok)


# --------------------------------------------------------------------------- #
# Legal but never silent
# --------------------------------------------------------------------------- #
def test_a_non_adr_ratio_is_reported():
    note = Settings(default_net_to_gross_ratio=Decimal("0.3")).net_to_gross_ratio_note()
    assert note is not None
    assert "0.3" in note and "0.7" in note


def test_overridden_ratio_warns_the_reviewer_when_items_depend_on_it(monkeypatch):
    """The warning names how many items the override actually moved."""
    import app.rules.weight_carton as wc

    monkeypatch.setattr(wc, "get_settings",
                        lambda: Settings(default_net_to_gross_ratio=Decimal("0.3")))
    items = [_item(1, "WIDGET A", "10", "1000"), _item(2, "WIDGET B", "10", "1000")]
    msgs = allocate_weights_and_cartons(items, {}, Decimal("100"), Decimal("4"))

    override = [m for m in msgs if m.code == "NET_TO_GROSS_RATIO_OVERRIDDEN"]
    assert len(override) == 1
    assert "2 item(s)" in override[0].message
    # and the ratio really is the one that was configured
    assert all(it.net_weight_kg == wc.q3_down(Decimal("0.3") * it.gross_weight_kg)
               for it in items)


def test_no_override_warning_on_the_resolved_default():
    items = [_item(1, "WIDGET A", "10", "1000"), _item(2, "WIDGET B", "10", "1000")]
    msgs = allocate_weights_and_cartons(items, {}, Decimal("100"), Decimal("4"))
    assert not [m for m in msgs if m.code == "NET_TO_GROSS_RATIO_OVERRIDDEN"]


def test_no_override_warning_when_no_item_uses_the_ratio(monkeypatch):
    """Every item has a fixed net from the packing list — the ratio is unused,
    so an override of it changes nothing and must not cry wolf."""
    import app.rules.weight_carton as wc

    monkeypatch.setattr(wc, "get_settings",
                        lambda: Settings(default_net_to_gross_ratio=Decimal("0.3")))
    items = [_item(1, "WIDGET A", "10", "1000"), _item(2, "WIDGET B", "10", "1000")]
    packing = {1: PackingEvidence(gross_weight=Decimal("60"), net_weight=Decimal("40"),
                                  matched=True, matched_name="WIDGET A"),
               2: PackingEvidence(gross_weight=Decimal("40"), net_weight=Decimal("25"),
                                  matched=True, matched_name="WIDGET B")}
    msgs = allocate_weights_and_cartons(items, packing, Decimal("100"), Decimal("4"))
    assert not [m for m in msgs if m.code == "NET_TO_GROSS_RATIO_OVERRIDDEN"]
