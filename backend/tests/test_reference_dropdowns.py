"""Bank and payment-term reference endpoints backing the Critical Review
dropdowns (user request 2026-07-19): full official lists, deterministic order,
served from the warm ReferenceStore singleton."""
import pytest

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def test_banks_endpoint_serves_full_reference(client):
    r = client.get("/api/reference/banks")
    assert r.status_code == 200
    banks = r.json()
    assert len(banks) == 279                      # the full official bank list
    assert {"code", "name", "swift"} <= set(banks[0])
    assert all(b["code"] and b["name"] for b in banks)
    names = [b["name"].upper() for b in banks]
    assert names == sorted(names)                 # stable name order for the dropdown
    assert any(b["code"] == "11020000" for b in banks)   # demo reference bank


def test_payment_terms_endpoint_serves_full_reference(client):
    r = client.get("/api/reference/payment-terms")
    assert r.status_code == 200
    terms = r.json()
    assert len(terms) >= 20
    by_code = {t["code"]: t["description"] for t in terms}
    assert "200" in by_code and "400" in by_code  # LC / TT
    assert by_code["200"]                         # descriptions present
    numeric = [int(t["code"]) for t in terms if t["code"].isdigit()]
    assert numeric == sorted(numeric)             # numeric codes in numeric order
