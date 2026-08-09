"""Photo uploads: JPEG/PNG accepted and converted to PDF at the boundary.

Everything downstream of the upload gate assumes "a stored document is a PDF"
(OCR envelope reuse, the evidence viewer, its #page=N deep links) — so photos
convert to PDF at upload, losslessly, and downstream never learns images
exist.  Several photos merge into ONE multi-page document: a 3-page invoice
photographed page by page is one document, and uploading the pages as three
documents would declare three invoices (the document-boundary failure this
pipeline has been bitten by before).

Photos only extract through a live OCR provider — the offline fallback is
pypdf text-layer extraction and an image-derived PDF has no text layer — so
whether photos are accepted at all depends on ``Settings.ocr_live_ready``,
pinned per-test here (the real value depends on the developer's .env).
"""
import io

import pytest

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Document


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def live_ocr(monkeypatch):
    monkeypatch.setattr(Settings, "ocr_live_ready", property(lambda self: True))


@pytest.fixture
def offline_ocr(monkeypatch):
    monkeypatch.setattr(Settings, "ocr_live_ready", property(lambda self: False))


def _jpeg(w=1600, h=1200, shade=180):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (shade, shade, shade)).save(buf, "JPEG")
    return buf.getvalue()


def _png_with_alpha(w=1600, h=1200):
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


def _new_job(client):
    return client.post("/api/jobs").json()["job_id"]


def _stored(document_id):
    db = SessionLocal()
    doc = db.scalar(select(Document).where(Document.id == document_id))
    with open(doc.storage_key, "rb") as f:
        data = f.read()
    db.close()
    return doc, data


# --------------------------------------------------------------------------- #
# Single photo → single-page PDF document
# --------------------------------------------------------------------------- #
def test_jpeg_upload_is_stored_as_pdf(client, live_ocr):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("IMG_4231.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    doc, data = _stored(r.json()["document_id"])
    assert data.startswith(b"%PDF-")                      # converted, not raw JPEG
    assert len(PdfReader(io.BytesIO(data)).pages) == 1
    assert doc.content_type == "application/pdf"          # the viewer renders it as PDF
    assert doc.original_file_name == "IMG_4231.jpg"       # display keeps the camera name
    assert doc.storage_key.endswith(".pdf")               # storage tells the truth


def test_png_with_alpha_is_flattened_and_accepted(client, live_ocr):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("scan.png", _png_with_alpha(), "image/png")})
    assert r.status_code == 200, r.text
    _, data = _stored(r.json()["document_id"])
    assert data.startswith(b"%PDF-")


def test_photo_refused_when_ocr_is_offline(client, offline_ocr):
    # The offline provider reads PDF text layers; a photo-PDF has none, so the
    # extraction could only come back empty — refused at upload instead.
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("IMG.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "PHOTO_NEEDS_LIVE_OCR"
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_low_resolution_photo_is_refused_with_retake_message(client, live_ocr):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("blurry.jpg", _jpeg(640, 480), "image/jpeg")})
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "PHOTO_RESOLUTION_TOO_LOW"
    assert "640×480" in err["message"] and "Retake" in err["message"]


def test_heic_refused_with_jpeg_conversion_hint(client):
    # iPhone default format; refused regardless of OCR provider, with the fix.
    job_id = _new_job(client)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("IMG_0001.heic", heic, "image/heic")})
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    assert "HEIC" in err["message"] and "JPEG" in err["message"]


def test_unsupported_image_format_is_named(client):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("anim.gif", b"GIF89a" + b"\x00" * 32, "image/gif")})
    assert r.status_code == 409
    assert "unsupported format" in r.json()["blocking_errors"][0]["message"]


def test_duplicate_photo_same_role_is_refused(client, live_ocr):
    job_id = _new_job(client)
    photo = _jpeg(shade=77)
    first = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                        files={"file": ("inv.jpg", photo, "image/jpeg")})
    assert first.status_code == 200
    # Dedup keys on the bytes the USER picked (pre-conversion), so the same
    # photo trips the gate even though each conversion could differ.
    again = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                        files={"file": ("inv-copy.jpg", photo, "image/jpeg")})
    assert again.status_code == 409
    assert again.json()["blocking_errors"][0]["code"] == "DUPLICATE_DOCUMENT_UPLOAD"


# --------------------------------------------------------------------------- #
# Photo set → ONE multi-page document
# --------------------------------------------------------------------------- #
def test_photo_set_merges_into_one_multipage_document(client, live_ocr):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE/photos",
                    files=[("files", ("p1.jpg", _jpeg(shade=10), "image/jpeg")),
                           ("files", ("p2.jpg", _jpeg(shade=20), "image/jpeg")),
                           ("files", ("p3.png", _png_with_alpha(), "image/png"))])
    assert r.status_code == 200, r.text
    docs = client.get(f"/api/jobs/{job_id}").json()["documents"]
    assert len(docs) == 1                                 # ONE document, not three
    doc, data = _stored(r.json()["document_id"])
    assert len(PdfReader(io.BytesIO(data)).pages) == 3    # one page per photo, in order
    assert "+2 photos" in doc.original_file_name
    db = SessionLocal()
    events = db.scalars(select(AuditEvent).where(AuditEvent.job_id == job_id)).all()
    db.close()
    assert any("3 photos" in e.detail for e in events if e.event_code == "DOCUMENT_UPLOADED")


def test_photo_set_refuses_a_pdf_member(client, live_ocr):
    # A PDF is already a complete document — merging it with photos would blur
    # the one-set-one-document contract, so the set is images-only.
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE/photos",
                    files=[("files", ("p1.jpg", _jpeg(), "image/jpeg")),
                           ("files", ("inv.pdf", b"%PDF-1.7\n", "application/pdf"))])
    assert r.status_code == 409
    err = r.json()["blocking_errors"][0]
    assert err["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    assert "on its own" in err["message"]
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_single_photo_set_and_single_upload_share_the_duplicate_gate(client, live_ocr):
    # digest(set of one) == digest(single file): the same photo attached via
    # either path is the same pick, and the twin gate must see that.
    job_id = _new_job(client)
    photo = _jpeg(shade=99)
    assert client.post(f"/api/jobs/{job_id}/documents/INVOICE/photos",
                       files=[("files", ("inv.jpg", photo, "image/jpeg"))]).status_code == 200
    again = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                        files={"file": ("inv.jpg", photo, "image/jpeg")})
    assert again.status_code == 409
    assert again.json()["blocking_errors"][0]["code"] == "DUPLICATE_DOCUMENT_UPLOAD"
