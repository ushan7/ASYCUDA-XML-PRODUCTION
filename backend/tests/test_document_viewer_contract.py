"""The contract the reviewer's evidence panel depends on.

The panel cannot learn anything from the <iframe> it renders the document in:
a frame's load event fires for a refusal exactly as it does for a PDF, and no
script runs inside a navigation.  So the panel asks HEAD first and shows either
the document or the server's reason — which only works while the two halves
asserted here hold:

  * HEAD on the file route answers like GET (status + headers, no body), and
  * a refusal carries a JSON ``detail`` written for a reviewer, not a stack.

Both are load-bearing UI, and neither is visible from any test that only ever
requests a file that exists.  A document whose stored file is gone is not
hypothetical here: this repo's own dev database holds hundreds of rows whose
``storage_key`` points into earlier checkouts.
"""
import pytest

from fastapi.testclient import TestClient
from pathlib import Path
from sqlalchemy import select

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Document


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def invoice_bytes():
    pdf = SAMPLE_DIR / "sample_invoice.pdf"
    return pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\nreal pdf\n"


def _job_with_document(client, invoice_bytes):
    job_id = client.post("/api/jobs").json()["job_id"]
    doc_id = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                         files={"file": ("invoice.pdf", invoice_bytes, "application/pdf")}
                         ).json()["document_id"]
    return job_id, doc_id


def test_head_answers_like_get_without_the_body(client, invoice_bytes):
    """The probe is a HEAD: if the route stopped answering it, every panel would
    report a document it can in fact serve as unavailable."""
    job_id, doc_id = _job_with_document(client, invoice_bytes)
    url = f"/api/jobs/{job_id}/documents/{doc_id}/file"
    head = client.head(url)
    assert head.status_code == 200
    assert head.headers["content-type"].startswith("application/pdf")
    assert head.content == b""
    assert head.headers["x-frame-options"] == client.get(url).headers["x-frame-options"]


def test_missing_stored_file_explains_itself(client, invoice_bytes):
    """A row whose bytes are gone (restored database, moved checkout, cleaned
    disk) answers 410 with a sentence the panel can put on screen — and says
    the extracted content survived, because it did."""
    job_id, doc_id = _job_with_document(client, invoice_bytes)
    db = SessionLocal()
    try:
        doc = db.scalar(select(Document).where(Document.id == doc_id))
        Path(doc.storage_key).unlink()
    finally:
        db.close()

    url = f"/api/jobs/{job_id}/documents/{doc_id}/file"
    assert client.head(url).status_code == 410
    r = client.get(url)
    assert r.status_code == 410
    assert r.headers["content-type"].startswith("application/json")
    detail = r.json()["detail"]
    assert "invoice.pdf" in detail                      # names the file, not the id
    assert "extracted content is still available" in detail

    # ...and the job itself is untouched: the document is still listed, so the
    # reviewer keeps working with values that never lived in the file.
    assert any(d["document_id"] == doc_id
               for d in client.get(f"/api/jobs/{job_id}").json()["documents"])
