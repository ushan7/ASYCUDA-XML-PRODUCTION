"""A stored upload is served as a PDF, never as what the upload called itself.

`POST /documents/{role}` used to forward the multipart part's Content-Type into
the Document row, and `GET /documents/{id}/file` handed that stored string back
as the response media type with `Content-Disposition: inline`.  Both halves are
client-influenced: the browser fills the part header in from the file
EXTENSION, and the accept gate only required "%PDF-" somewhere in the first
1024 bytes.  So a file named `invoice.svg` whose bytes were
`<svg onload=...><!--%PDF-1.4-->` was accepted as a document, stored as
image/svg+xml, and then rendered as SCRIPT in the reviewer's evidence iframe —
on this origin, where every same-origin fetch it makes carries the operator's
session cookie.  The HttpOnly flag is no defence: the payload never needs to
read the token, only to spend it.

Two independent gates now, either of which alone stops it:

  1. the upload gate refuses a file that OPENS with markup, and
  2. the response media type is a server-side constant, plus nosniff.

Both are tested here, because either one regressing silently restores the hole.
"""
import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.domain.errors import BlockingValidationError
from app.main import app
from app.models import Document
from app import services


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def invoice_bytes():
    pdf = SAMPLE_DIR / "sample_invoice.pdf"
    return pdf.read_bytes() if pdf.exists() else b"%PDF-1.4\nreal pdf\n"


def _new_job(client):
    return client.post("/api/jobs").json()["job_id"]


# --------------------------------------------------------------------------- #
# Gate 1 — a file that opens with markup is not a document
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'><!--%PDF-1.4--></svg>",
    b"<html><script>fetch('/api/jobs')</script><!-- %PDF-1.7 --></html>",
    b"<!DOCTYPE html>\n<body onload=alert(1)>%PDF-1.5",
    b"\xef\xbb\xbf<svg onload=alert(1)><!--%PDF-1.4-->",      # UTF-8 BOM first
    b"\n\r\t  <svg onload=alert(1)><!--%PDF-1.4-->",          # leading whitespace
])
def test_markup_prefixed_polyglot_is_refused(payload):
    """`%PDF-` inside a comment does not make a script a document."""
    with pytest.raises(BlockingValidationError) as e:
        services.validate_upload("commercial-invoice.svg", payload)
    assert e.value.message.code == "UNSUPPORTED_DOCUMENT_TYPE"
    assert "markup" in e.value.message.message


def test_polyglot_upload_is_refused_over_http_and_stores_nothing(client):
    job_id = _new_job(client)
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("commercial-invoice.svg",
                                    b"<svg onload=alert(1)><!--%PDF-1.4-->",
                                    "image/svg+xml")})
    assert r.status_code == 409
    assert r.json()["blocking_errors"][0]["code"] == "UNSUPPORTED_DOCUMENT_TYPE"
    assert client.get(f"/api/jobs/{job_id}").json()["documents"] == []


def test_binary_preamble_pdf_is_still_accepted():
    """The spec's preamble tolerance is deliberately kept — the guard rejects a
    markup PREFIX, not a generator's junk bytes.  Over-fixing here would refuse
    real customs paperwork."""
    assert services.validate_upload("ok.pdf", b"\x00" * 512 + b"%PDF-1.7\n") == "pdf"
    assert services.validate_upload("ok.pdf", b"\xef\xbb\xbf%PDF-1.7\n") == "pdf"


# --------------------------------------------------------------------------- #
# Gate 2 — the served media type is the server's fact, not the upload's claim
# --------------------------------------------------------------------------- #
def test_declared_content_type_is_never_persisted(client, invoice_bytes):
    """A genuine PDF announced as text/html must not be stored as text/html."""
    job_id = _new_job(client)
    doc_id = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                         files={"file": ("invoice.pdf", invoice_bytes, "text/html")}
                         ).json()["document_id"]
    db = SessionLocal()
    try:
        stored = db.scalar(select(Document).where(Document.id == doc_id))
        assert stored.content_type == "application/pdf"
    finally:
        db.close()


@pytest.mark.parametrize("declared", ["text/html", "image/svg+xml",
                                      "application/xhtml+xml", ""])
def test_served_file_is_always_pdf_and_nosniff(client, invoice_bytes, declared):
    job_id = _new_job(client)
    doc_id = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                         files={"file": ("invoice.pdf", invoice_bytes, declared)}
                         ).json()["document_id"]
    r = client.get(f"/api/jobs/{job_id}/documents/{doc_id}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers["x-content-type-options"] == "nosniff"
    # still inline: the evidence viewer renders it next to the extracted values
    assert r.headers["content-disposition"].startswith("inline")


# --------------------------------------------------------------------------- #
# Gate 3 — the evidence PDF must stay framable BY THIS ORIGIN
#
# The hardening that made the media type a server fact also put
# `X-Frame-Options: DENY` on every response, evidence included.  DENY refuses
# same-origin framing exactly as firmly as cross-origin, so the reviewer's
# document panel — an <iframe> on this very origin — rendered nothing at all:
# clicking a 📄 evidence link opened an empty panel, with the bytes served
# correctly at 200 the whole time.  Nothing tested the header, so a change that
# broke the app's central verification surface looked green.
#
# Both directions are asserted, because the fix is a split and either half can
# regress on its own: too strict blanks the viewer again, too loose puts the
# finalize button back inside somebody else's frame.
# --------------------------------------------------------------------------- #
def test_served_file_is_framable_by_this_origin(client, invoice_bytes):
    job_id = _new_job(client)
    doc_id = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                         files={"file": ("invoice.pdf", invoice_bytes, "application/pdf")}
                         ).json()["document_id"]
    r = client.get(f"/api/jobs/{job_id}/documents/{doc_id}/file")
    assert r.status_code == 200
    # NOT "DENY": that is what the browser refuses to display in the panel.
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    # ...and not framable from anywhere else — an importer's invoice is not for
    # embedding in a third-party page.
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_app_shell_is_never_framable(client):
    """The workspace HTML is the clickjacking target (finalize is one click on
    it), so its rule does NOT relax with the evidence rule."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
