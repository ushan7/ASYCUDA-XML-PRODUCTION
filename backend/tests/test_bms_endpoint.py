"""HTTP integration for the brand/model/size .xls export: it is built alongside
the XML at finalize, served in XML-item order, and invalidated (like the XML)
whenever a downstream mutation stales the declaration."""
import pytest
import xlrd

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app

FINALIZE_BODY = {
    "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
    "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _reviewed_job(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    return job_id, review


def _finalize(client, job_id, review):
    return client.post(f"/api/jobs/{job_id}/finalize",
                       json=dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"]))


def test_bms_xls_built_and_served_in_declaration_order(client):
    job_id, review = _reviewed_job(client)
    fin = _finalize(client, job_id, review)
    assert fin.status_code == 200
    decl = fin.json()

    r = client.get(f"/api/jobs/{job_id}/brand-model-size.xls")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.ms-excel"
    assert "brand-model-size" in r.headers["content-disposition"]

    sh = xlrd.open_workbook(file_contents=r.content).sheet_by_index(0)
    assert [sh.cell_value(0, c) for c in range(4)] == ["", "BRAND", "MODEL", "SIZE"]
    assert sh.nrows == 1 + len(decl["items"])
    # every row matches the frozen declaration item, in the same order
    for i, it in enumerate(decl["items"], start=1):
        assert sh.cell_value(i, 0) == it["xml_item_sequence"]
        assert sh.cell_value(i, 1) == it["brand"]
        assert sh.cell_value(i, 2) == it["model"]
        assert sh.cell_value(i, 3) == it["size"]
    # every cell is populated (NA rather than blank when unresolved)
    assert all(str(sh.cell_value(i, c)).strip() for i in range(1, sh.nrows) for c in range(1, 4))


def _rows(client, job_id):
    return client.get(f"/api/jobs/{job_id}/critical-review").json()["item_details"]


def _sheet(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/brand-model-size.xls")
    assert r.status_code == 200
    return xlrd.open_workbook(file_contents=r.content).sheet_by_index(0)


def test_bms_edit_updates_grid_and_xls_without_touching_the_xml(client):
    job_id, review = _reviewed_job(client)
    assert _finalize(client, job_id, review).status_code == 200
    before = client.get(f"/api/jobs/{job_id}/xml")
    assert before.status_code == 200
    rows = _rows(client, job_id)

    # edit three cells across two rows (one per column)
    r = client.post(f"/api/jobs/{job_id}/items/brand-model-size", json={"edits": [
        {"item_id": rows[0]["item_id"], "brand": "Roche", "model": "01R6070"},
        {"item_id": rows[1]["item_id"], "size": "10 x 113 mL"},
    ]})
    assert r.status_code == 200
    payload = r.json()
    assert payload["edited_items"] == 2 and payload["xls_rebuilt"] is True

    # the review grid reflects the edit
    fresh = payload["critical_review"]["item_details"]
    assert fresh[0]["brand"] == "Roche" and fresh[0]["model"] == "01R6070"
    assert fresh[1]["size"] == "10 x 113 mL"

    # the .xls is rebuilt with the edited values, order preserved
    sh = _sheet(client, job_id)
    assert sh.cell_value(1, 1) == "Roche" and sh.cell_value(1, 2) == "01R6070"
    assert sh.cell_value(2, 3) == "10 x 113 mL"

    # ...and the XML is untouched: still downloadable, byte-identical
    after = client.get(f"/api/jobs/{job_id}/xml")
    assert after.status_code == 200 and after.content == before.content
    assert client.get(f"/api/jobs/{job_id}/declaration").status_code == 200


def test_bms_edit_survives_recompute_and_clears_back_to_deterministic(client):
    job_id, review = _reviewed_job(client)
    assert _finalize(client, job_id, review).status_code == 200
    rows = _rows(client, job_id)
    item_id, original_brand = rows[0]["item_id"], rows[0]["brand"]

    client.post(f"/api/jobs/{job_id}/items/brand-model-size",
                json={"edits": [{"item_id": item_id, "brand": "Roche"}]})
    # durability: an independent recompute still shows the override
    assert _rows(client, job_id)[0]["brand"] == "Roche"

    # empty value clears the override -> the deterministic value returns
    r = client.post(f"/api/jobs/{job_id}/items/brand-model-size",
                    json={"edits": [{"item_id": item_id, "brand": ""}]})
    assert r.status_code == 200
    assert _rows(client, job_id)[0]["brand"] == original_brand


def test_bms_edit_rejects_unknown_item(client):
    job_id, _ = _reviewed_job(client)
    r = client.post(f"/api/jobs/{job_id}/items/brand-model-size",
                    json={"edits": [{"item_id": "src:nope", "brand": "X"}]})
    assert r.status_code == 404


def test_bms_edit_before_xml_is_allowed_and_builds_nothing(client):
    job_id, _ = _reviewed_job(client)
    rows = _rows(client, job_id)
    r = client.post(f"/api/jobs/{job_id}/items/brand-model-size",
                    json={"edits": [{"item_id": rows[0]["item_id"], "brand": "PreXml"}]})
    assert r.status_code == 200
    assert r.json()["xls_rebuilt"] is False          # nothing built yet
    assert r.json()["critical_review"]["item_details"][0]["brand"] == "PreXml"
    assert client.get(f"/api/jobs/{job_id}/brand-model-size.xls").status_code == 404


def test_bms_xls_absent_before_build_and_after_mutation(client):
    job_id, review = _reviewed_job(client)
    # nothing built yet
    assert client.get(f"/api/jobs/{job_id}/brand-model-size.xls").status_code == 404
    assert _finalize(client, job_id, review).status_code == 200
    assert client.get(f"/api/jobs/{job_id}/brand-model-size.xls").status_code == 200
    # a shipment-totals correction stales the declaration -> derived .xls is gone
    r = client.post(f"/api/jobs/{job_id}/shipment-totals",
                    json={"gross_weight": "160", "weight_unit": "KGM", "total_packages": "9"})
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/brand-model-size.xls").status_code == 404
