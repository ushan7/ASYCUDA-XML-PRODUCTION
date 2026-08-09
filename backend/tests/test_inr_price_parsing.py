"""Indian/INR amount parsing + the zero-price safety net.

Root cause of the reported bug: invoice amounts wrapped as 'Rs. 1,234.00' or
'75,000/-' failed to parse, so line_total became None and the item price
collapsed to qty x unit_price = 0.  parse_decimal now strips currency/notation
wrappers; invoice_authority warns instead of silently shipping a 0 price."""
import json
from decimal import Decimal

import pytest

from app.numbers import parse_decimal as p


# --------------------------------------------------------------------------- #
# parse_decimal robustness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("Rs. 1,234.00", "1234.00"),
    ("Rs.1,234/-", "1234"),
    ("1,234/-", "1234"),
    ("75,000/-", "75000"),
    ("1,234.00/-", "1234.00"),
    ("1,234.50 /-", "1234.50"),
    ("1,23,456/-", "123456"),          # Indian lakh grouping
    ("12,34,567.89", "1234567.89"),
    ("1,234.00-", "1234.00"),          # trailing minus = rupees-only, not negative
    ("INR 1,234.00", "1234.00"),
    ("INR 1,234.00 Dr", "1234.00"),
    ("Rs 500", "500"),
])
def test_inr_formats_now_parse(raw, expected):
    assert p(raw) == Decimal(expected)


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", "1234.56"),
    ("USD 1 234,56", "1234.56"),
    ("#7023,17#", "7023.17"),          # SWIFT
    ("(1,200)", "-1200"),              # accounting negative (parens preserved)
    ("1.234,56", "1234.56"),           # european
    ("1.600.00", "1600.00"),           # OCR-corrupted european
    ("0.00", "0.00"),                  # genuine free-of-charge
    ("2108.89", "2108.89"),
    ("-5.00", "-5.00"),                # leading minus still negative
])
def test_existing_formats_unchanged(raw, expected):
    assert p(raw) == Decimal(expected)


def test_jammed_double_number_still_rejected():
    assert p("35,00 700,00") is None   # two numbers in one cell -> unparseable


# --------------------------------------------------------------------------- #
# End-to-end: an INR invoice no longer yields 0-priced items
# --------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

from app.config import SAMPLE_DIR  # noqa: E402
from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _inr_invoice_job(client, rows):
    job_id = client.post("/api/jobs").json()["job_id"]
    fx = json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())
    fx["rows"] = rows
    fx["totals"] = None
    fx["header"]["currency_raw"] = "INR"
    pdf = (SAMPLE_DIR / "sample_invoice.pdf").read_bytes()
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", pdf, "application/pdf")},
                     data={"fixture": json.dumps(fx)})
    assert up.status_code == 200
    return job_id, client.get(f"/api/jobs/{job_id}/critical-review").json()


def _row(idx, desc, total, unit=None, qty="4"):
    return {"source_page_no": 1, "source_row_index": idx, "line_no_raw": str(idx),
            "description_raw": desc, "quantity_raw": qty, "uom_raw": "PCS",
            "unit_price_raw": unit, "line_total_raw": total, "currency_raw": "INR",
            "hs_code_raw": "85044090900", "country_of_origin_raw": "CN",
            "item_weight_scope": "UNKNOWN", "row_classification": "REAL_GOODS_ITEM"}


def test_inr_wrapped_totals_no_longer_zero(client):
    _, review = _inr_invoice_job(client, [
        _row(1, "Item A", "Rs. 12,340.00"),
        _row(2, "Item B", "75,000/-"),
        _row(3, "Item C", "1,23,456/-"),
    ])
    prices = [r["total_price"] for r in review["item_details"]]
    assert prices == ["12340.00", "75000.00", "123456.00"]     # none are 0
    assert review["calculated_goods_total"] == "210796.00"
    assert not any(w["code"] == "ITEM_PRICE_ZERO_SUSPECT" for w in review["warnings"])


def test_unparseable_amount_flags_not_silent_zero(client):
    # a truly garbled amount cell (two jammed numbers) -> can't parse; with no
    # usable unit price the row resolves to 0 but is FLAGGED, never silent
    _, review = _inr_invoice_job(client, [
        _row(1, "Garbled", "35,00 700,00", unit=None),
    ])
    codes = {w["code"] for w in review["warnings"]}
    assert "ITEM_PRICE_ZERO_SUSPECT" in codes


def test_genuine_free_of_charge_not_flagged(client):
    _, review = _inr_invoice_job(client, [_row(1, "FOC sample", "0.00", unit="0.00")])
    assert review["item_details"][0]["total_price"] == "0.00"
    codes = {w["code"] for w in review["warnings"]}
    assert "ITEM_PRICE_ZERO_SUSPECT" not in codes     # explicit 0.00 is legitimate
