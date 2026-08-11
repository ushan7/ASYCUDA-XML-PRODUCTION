"""Documents have to live somewhere both API instances can reach.

A local directory is not shared. The instance that did not receive an upload has
no such file, so the reviewer's evidence panel answers 410 or not depending on
which instance the load balancer picked — and in queue mode the worker that OCRs
a document is a different machine from the API that stored it, so it has nothing
to read at all. That makes local storage the last thing that stops this app
running as more than one instance.

The property that carries the most risk here is not "S3 works". It is that
turning S3 ON does not orphan the documents a deployment already holds: storage
dispatches on the KEY, never on the current setting, so a local path written
last week is still read from disk by a process configured for S3 today.
"""
from __future__ import annotations

import io

import pytest

from app import storage
from app.config import Settings, get_settings
from app.domain.enums import DeclaredRole
from app.ocr.offline import OfflineOcrProvider

PDF = b"%PDF-1.4\n%fake pdf bytes\n"


class FakeS3:
    """Records calls and serves objects from a dict."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[str] = []

    def put_object(self, *, Bucket, Key, Body, **kw):
        self.calls.append("put_object")
        self.put_kwargs = kw
        self.objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket, Key):
        self.calls.append("get_object")
        try:
            return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        except KeyError:
            raise RuntimeError("NoSuchKey")

    def head_object(self, *, Bucket, Key):
        self.calls.append("head_object")
        if (Bucket, Key) not in self.objects:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.calls.append("delete_object")
        self.objects.pop((Bucket, Key), None)
        return {}


@pytest.fixture()
def s3(monkeypatch, tmp_path):
    """storage_backend=s3, with a fake client and an isolated local dir."""
    client = FakeS3()
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "customs-docs")
    monkeypatch.setattr(settings, "s3_prefix", "prod/")
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(storage, "_s3_client", lambda: client)
    return client


@pytest.fixture()
def local(monkeypatch, tmp_path):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_s3_without_a_bucket_is_refused_at_boot():
    """Not at the first upload: a bucket-less s3 backend would accept a
    document, fail to store it, and lose it."""
    with pytest.raises(Exception) as caught:
        Settings(_env_file=None, storage_backend="s3", s3_bucket="")
    assert "S3_BUCKET" in str(caught.value)


def test_an_unknown_backend_is_refused():
    with pytest.raises(Exception):
        Settings(_env_file=None, storage_backend="gcs")


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #
def test_local_round_trip(local):
    key = storage.store_document("job-1", "doc-1", "invoice.pdf", PDF)
    assert not storage.is_remote(key)
    assert storage.load_document(key) == PDF
    assert storage.document_exists(key)
    storage.delete_stored_document(key)
    assert not storage.document_exists(key)


def test_s3_round_trip(s3):
    key = storage.store_document("job-1", "doc-1", "invoice.pdf", PDF)
    assert key == "s3://customs-docs/prod/jobs/job-1/doc-1__invoice.pdf"
    assert storage.is_remote(key)
    assert storage.load_document(key) == PDF
    assert storage.document_exists(key)
    storage.delete_stored_document(key)
    assert not storage.document_exists(key)


def test_the_key_carries_its_own_bucket(s3, monkeypatch):
    """The key is stored on the document row and has to stay valid for as long
    as the row does — including after the configured bucket changes."""
    key = storage.store_document("job-1", "doc-1", "invoice.pdf", PDF)
    monkeypatch.setattr(get_settings(), "s3_bucket", "some-other-bucket")
    assert storage.load_document(key) == PDF


def test_existence_is_a_head_not_a_get(s3):
    """The evidence panel probes before rendering; paying for the whole object
    to answer "yes" would double the cost of opening every document."""
    key = storage.store_document("job-1", "doc-1", "invoice.pdf", PDF)
    s3.calls.clear()
    assert storage.document_exists(key)
    assert s3.calls == ["head_object"]


def test_a_filename_cannot_escape_its_job_prefix(s3):
    """The same sanitising as the local path, for the same reason: the filename
    is client-supplied."""
    key = storage.store_document("job-1", "doc-1", "../../etc/passwd", PDF)
    assert key == "s3://customs-docs/prod/jobs/job-1/doc-1__passwd"


# --------------------------------------------------------------------------- #
# The property that protects existing deployments
# --------------------------------------------------------------------------- #
def test_local_documents_stay_readable_after_switching_to_s3(monkeypatch, tmp_path):
    """Dispatch is on the KEY, not on the setting.

    Reading the config instead would look in the bucket for a document that is
    on disk, and report a reviewer's existing evidence as missing on the day S3
    was turned on.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    legacy_key = storage.store_document("job-old", "doc-old", "invoice.pdf", PDF)

    # ...the deployment moves to S3
    client = FakeS3()
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "customs-docs")
    monkeypatch.setattr(storage, "_s3_client", lambda: client)

    assert storage.load_document(legacy_key) == PDF
    assert storage.document_exists(legacy_key)
    assert client.calls == [], "a local key must never be looked for in the bucket"

    # ...and new documents go to the bucket
    assert storage.is_remote(storage.store_document("job-new", "doc-new", "i.pdf", PDF))


# --------------------------------------------------------------------------- #
# Missing bytes are an error, never silence
# --------------------------------------------------------------------------- #
def test_a_missing_local_file_raises(local):
    with pytest.raises(storage.DocumentUnavailable):
        storage.load_document(str(local / "nope.pdf"))


def test_a_missing_s3_object_raises(s3):
    with pytest.raises(storage.DocumentUnavailable):
        storage.load_document("s3://customs-docs/prod/jobs/j/absent.pdf")


def test_an_empty_key_raises(s3):
    """Empty bytes would extract as a document with no content, which is
    indistinguishable from a real one OCR could not read."""
    with pytest.raises(storage.DocumentUnavailable):
        storage.load_document("")
    assert storage.document_exists("") is False


def test_a_malformed_s3_key_raises(s3):
    with pytest.raises(storage.DocumentUnavailable):
        storage.load_document("s3://bucket-only")


def test_deleting_a_missing_object_is_not_an_error(s3, local):
    """Removal is best effort — the row is authoritative, and raising here would
    fail the reviewer's document removal, which is the actual request."""
    storage.delete_stored_document("s3://customs-docs/prod/absent")
    storage.delete_stored_document(str(local / "absent.pdf"))
    storage.delete_stored_document("")


# --------------------------------------------------------------------------- #
# The OCR layer no longer assumes a local disk
# --------------------------------------------------------------------------- #
def test_offline_ocr_reads_bytes_not_a_path():
    """A provider that opens a file makes "the document is on this machine's
    disk" a requirement of extraction — false the moment documents live in S3,
    and false in queue mode regardless."""
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)

    ocr = OfflineOcrProvider().run(document_id="d1", declared_role=DeclaredRole.INVOICE,
                                   data=buf.getvalue(), sha256="abc")
    assert len(ocr.pages) == 1


def test_the_ocr_protocol_takes_no_file_path():
    """Pinned as a signature: a provider re-adding file_path would compile and
    then fail only on a deployment that had moved its documents."""
    import inspect

    from app.ocr.base import OcrProvider

    params = inspect.signature(OcrProvider.run).parameters
    assert "data" in params
    assert "file_path" not in params


# --------------------------------------------------------------------------- #
# End to end: the evidence panel against a bucket-backed document
# --------------------------------------------------------------------------- #
def test_the_viewer_serves_a_document_that_lives_in_the_bucket(s3):
    """The reviewer's panel frames this route. With documents in S3 the bytes
    still have to come back through it — behind this app's own login, and on
    this app's own origin, which is what the page's CSP allows to be framed."""
    from fastapi.testclient import TestClient

    from app.database import SessionLocal, init_db
    from app.main import app
    from app.models import Document

    init_db()
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]

        # Re-home one of the demo's documents into the bucket, exactly as an
        # upload on an s3 deployment would have stored it.
        db = SessionLocal()
        try:
            doc = db.query(Document).filter(Document.job_id == job_id).first()
            key = storage.store_document(job_id, doc.id, "invoice.pdf", PDF)
            doc.storage_key = key
            db.commit()
            document_id = doc.id
        finally:
            db.close()

        head = client.head(f"/api/jobs/{job_id}/documents/{document_id}/file")
        assert head.status_code == 200, "the panel probes with HEAD before rendering"

        got = client.get(f"/api/jobs/{job_id}/documents/{document_id}/file")
        assert got.status_code == 200
        assert got.content == PDF
        assert got.headers["content-type"] == "application/pdf"
        # A stored document must never be re-interpreted as markup.
        assert got.headers["x-content-type-options"] == "nosniff"
        assert "inline" in got.headers["content-disposition"]


def test_a_document_missing_from_the_bucket_is_410_not_500(s3):
    from fastapi.testclient import TestClient

    from app.database import SessionLocal, init_db
    from app.main import app
    from app.models import Document

    init_db()
    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        db = SessionLocal()
        try:
            doc = db.query(Document).filter(Document.job_id == job_id).first()
            doc.storage_key = "s3://customs-docs/prod/jobs/gone/never-written.pdf"
            db.commit()
            document_id = doc.id
        finally:
            db.close()

        r = client.get(f"/api/jobs/{job_id}/documents/{document_id}/file")
        assert r.status_code == 410
        # The extracted content survives the bytes going missing, and the
        # message has to say so or the reviewer assumes the job is lost.
        assert "extracted content is still available" in r.text
