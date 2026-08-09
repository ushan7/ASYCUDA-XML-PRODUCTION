"""Upload-time validation + document removal.

Everything the upload gate refuses used to be accepted silently and surface
only as a FAILED extraction: after a paid OCR round, behind an upstream error
message, on a document no retry could ever fix.  And on Windows, a filename
containing ":" was worse than an error — NTFS turned the tail into an
alternate-data-stream name, stored a 0-byte file, and OCR read an empty PDF.

Removal closes the loop the duplicate-upload refusal opens: its message says
"remove the existing document first if you meant to replace it", which needs
an endpoint (and a button) that actually does that.
"""
import json
import os

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR, get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DeclaredRole, DocumentStatus
from app.domain.errors import BlockingValidationError
from app.main import app
from app.models import AuditEvent, Document
from app.storage import safe_filename
from app import services

from sqlalchemy import select


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def invoice_bytes():
    pdf = SAMPLE_DIR / "sample_invoice.pdf"
    return pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\n"


@pytest.fixture(scope="module")
def invoice_fixture():
    return json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())


def _new_job(client):
    return client.post("/api/jobs").json()["job_id"]


# --------------------------------------------------------------------------- #
# The gate — refused at upload, with the reason, before anything is spent
# --------------------------------------------------------------------------- #
def test_empty_file_is_refused(client):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("scan.pdf", b"", "application/pdf")})
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "EMPTY_DOCUMENT_UPLOAD"
    assert "scan.pdf" in err["message"]
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_non_pdf_is_refused_and_named(client):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("invoice.docx", b"PK\x03\x04word-ish", "application/pdf")})
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    # says what the file actually is, not just what it is not
    assert "Office document" in err["message"]
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_unknown_bytes_and_unsupported_image_sniffing():
    # JPEG/PNG are ACCEPTED now (converted to PDF) — see test_photo_uploads.py.
    # Unsupported image formats and unknown bytes stay refused, with the reason.
    with pytest.raises(BlockingValidationError) as e:
        services.validate_upload("anim.gif", b"GIF89a gif bytes")
    assert "unsupported format" in e.value.message.message
    with pytest.raises(BlockingValidationError) as e:
        services.validate_upload("notes.pdf", b"just some text")
    assert e.value.message.code == "UNSUPPORTED_DOCUMENT_TYPE"


def test_pdf_header_tolerated_within_first_1024_bytes():
    # the spec allows preamble junk before %PDF-; some generators emit it
    services.validate_upload("ok.pdf", b"\x00" * 512 + b"%PDF-1.7\n")


def test_oversize_is_refused():
    cap = get_settings().max_upload_mb
    with pytest.raises(BlockingValidationError) as e:
        services.validate_upload("huge.pdf", b"%PDF-" + b"0" * (cap * 1024 * 1024))
    assert e.value.message.code == "DOCUMENT_TOO_LARGE"
    assert f"{cap} MB" in e.value.message.message


# --------------------------------------------------------------------------- #
# Filenames every filesystem accepts (the ":" ADS corruption, path smuggling)
# --------------------------------------------------------------------------- #
def test_safe_filename_neutralises_windows_unsafe_characters():
    assert ":" not in safe_filename("INV: 2026?<final>.pdf")
    assert safe_filename("INV: 2026?<final>.pdf").endswith(".pdf")
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\..\\boot.ini") == "boot.ini"
    assert safe_filename("") == "upload.bin"
    assert safe_filename("...") == "upload.bin"


def test_weird_filename_upload_stores_the_actual_bytes(client, invoice_bytes):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("INV: 2026.pdf", invoice_bytes, "application/pdf")})
    assert r.status_code == 200
    db = SessionLocal()
    doc = db.scalar(select(Document).where(Document.id == r.json()["document_id"]))
    with open(doc.storage_key, "rb") as f:
        assert f.read() == invoice_bytes          # not a 0-byte ADS husk
    db.close()


# --------------------------------------------------------------------------- #
# Removal — the replace path the duplicate refusal instructs
# --------------------------------------------------------------------------- #
def test_remove_staged_document_frees_the_box(client, invoice_bytes):
    job_id = _new_job(client)
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", invoice_bytes, "application/pdf")}).json()
    r = client.delete(f"/api/jobs/{job_id}/documents/{up['document_id']}")
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []
    # the same bytes are attachable again — the sha256 twin is gone with the row
    r2 = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", invoice_bytes, "application/pdf")})
    assert r2.status_code == 200


def test_remove_deletes_stored_file_and_audits(client, invoice_bytes):
    job_id = _new_job(client)
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", invoice_bytes, "application/pdf")}).json()
    db = SessionLocal()
    key = db.scalar(select(Document.storage_key).where(Document.id == up["document_id"]))
    assert os.path.exists(key)
    db.close()
    client.delete(f"/api/jobs/{job_id}/documents/{up['document_id']}")
    assert not os.path.exists(key)
    db = SessionLocal()
    events = db.scalars(select(AuditEvent).where(AuditEvent.job_id == job_id)).all()
    assert any(e.event_code == "DOCUMENT_REMOVED" and "inv.pdf" in e.detail for e in events)
    db.close()


def test_remove_extracted_document_invalidates_derived_state(client, invoice_bytes):
    """Removing a document the review was computed from stales the review."""
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    client.get(f"/api/jobs/{job_id}/critical-review")
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["critical_review"] is not None
    packing = next(d for d in job["documents"] if d["role"] == "PACKING_LIST")
    r = client.delete(f"/api/jobs/{job_id}/documents/{packing['document_id']}")
    assert r.status_code == 200
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["critical_review"] is None
    assert job["has_declaration"] is False
    assert all(d["role"] != "PACKING_LIST" for d in job["documents"])


def test_remove_refuses_a_running_extraction(client, invoice_bytes):
    job_id = _new_job(client)
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", invoice_bytes, "application/pdf")}).json()
    db = SessionLocal()
    doc = db.scalar(select(Document).where(Document.id == up["document_id"]))
    doc.status = DocumentStatus.EXTRACTING.value
    db.commit()
    db.close()
    r = client.delete(f"/api/jobs/{job_id}/documents/{up['document_id']}")
    assert r.status_code == 409
    assert r.json()["blocking_errors"][0]["code"] == "DOCUMENT_EXTRACTION_RUNNING"


def test_remove_unknown_document_is_404(client):
    job_id = _new_job(client)
    assert client.delete(f"/api/jobs/{job_id}/documents/no-such-doc").status_code == 404
