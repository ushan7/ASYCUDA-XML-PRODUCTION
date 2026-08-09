"""GET /api/jobs — the dashboard listing (job persistence work, 2026-08-01).

The SPA previously had no way to reopen a job: the API was get-by-id only and
the id lived in a React ref.  The listing is newest-first, summarises from the
STORED critical review (never recomputes), and flags which jobs have an XML.
Since 2026-08-08 only jobs holding at least one document are listed: an empty
job (created, nothing ever uploaded) is not history.
"""
import json

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
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


def _listed(client, **params):
    r = client.get("/api/jobs", params=params)
    assert r.status_code == 200
    return r.json()


def _uploaded_only_job(client):
    """A job with one document and NO stored review — real work, not yet reviewed."""
    job_id = client.post("/api/jobs").json()["job_id"]
    fx = json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())
    pdf = (SAMPLE_DIR / "sample_invoice.pdf").read_bytes()
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", pdf, "application/pdf")},
                     data={"fixture": json.dumps(fx)})
    assert up.status_code == 200
    return job_id


def test_listing_orders_and_summarises(client):
    empty = client.post("/api/jobs").json()["job_id"]          # nothing uploaded
    uploaded = _uploaded_only_job(client)                      # no review yet
    demo = client.post("/api/jobs/demo").json()["job_id"]
    review = client.get(f"/api/jobs/{demo}/critical-review").json()
    fin = client.post(f"/api/jobs/{demo}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200

    body = _listed(client)
    assert body["total"] >= 2
    by_id = {j["job_id"]: j for j in body["jobs"]}
    # an empty job is not history: never listed, never counted
    assert empty not in by_id
    assert demo in by_id and uploaded in by_id
    # newest activity first: the just-finalized demo job outranks the older one
    assert body["jobs"].index(by_id[demo]) < body["jobs"].index(by_id[uploaded])

    d = by_id[demo]
    assert d["status"] == "XML_READY" and d["has_xml"] is True
    assert d["invoice_numbers"] == ["DEMO-209-1"]
    assert d["item_count"] == 119
    assert d["goods_total"] == "2108.89" and d["currency"] == "USD"
    assert d["customs_office_code"] == "TIA00"
    assert d["declaration_type"] == "IM 4"
    assert d["documents"] >= 4 and "INVOICE" in d["roles"]
    assert d["created_at"] and d["updated_at"]

    e = by_id[uploaded]
    # nothing reviewed yet: summary honestly empty, never recomputed
    assert e["has_xml"] is False
    assert e["invoice_numbers"] == [] and e["item_count"] == 0
    assert e["goods_total"] == "" and e["customs_office_code"] == ""
    assert e["documents"] == 1 and e["roles"] == ["INVOICE"]


def test_empty_jobs_never_listed(client):
    before = _listed(client)["total"]
    empty = client.post("/api/jobs").json()["job_id"]
    body = _listed(client)
    assert body["total"] == before                       # count unchanged
    assert all(j["job_id"] != empty for j in body["jobs"])
    # the job itself still exists and is reachable by id (uploads land on it)
    assert client.get(f"/api/jobs/{empty}").status_code == 200


def test_doc_removed_job_stays_listed(client):
    """Removing a job's last document must NOT drop it from the history.

    The job had real work (upload, extraction, audit trail, possibly reviewer
    selections) — hiding it would strand that behind a remembered URL.  The
    DOCUMENT_UPLOADED audit event is the durable proof, surviving the
    hard-delete of the Document row."""
    job_id = _uploaded_only_job(client)
    doc_id = client.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]
    assert client.delete(f"/api/jobs/{job_id}/documents/{doc_id}").status_code == 200

    body = _listed(client)
    by_id = {j["job_id"]: j for j in body["jobs"]}
    assert job_id in by_id
    assert by_id[job_id]["documents"] == 0 and by_id[job_id]["roles"] == []


def test_listing_paginates(client):
    # self-sufficient: two listable jobs of its own, so a selective run
    # (pytest …::test_listing_paginates) on a fresh DB still has rows to page
    _uploaded_only_job(client)
    _uploaded_only_job(client)
    all_jobs = _listed(client)
    page = _listed(client, limit=1)
    assert len(page["jobs"]) == 1 and page["total"] == all_jobs["total"]
    assert page["jobs"][0]["job_id"] == all_jobs["jobs"][0]["job_id"]
    nxt = _listed(client, limit=1, offset=1)
    assert len(nxt["jobs"]) == 1
    assert nxt["jobs"][0]["job_id"] == all_jobs["jobs"][1]["job_id"]
