"""A document attached twice must never be declared twice (W1).

The failure this pins was silent end-to-end: `add_document` created a second
Document row per upload, `_group_raw` fed every invoice document into
`finalize_invoices`, and the row loop ran for each copy — 2N items and a
doubled goods total.  Nothing caught it: `finalize_invoices` deduped only the
InvoiceRef *display* entry, `printed_grand_total` doubled alongside the rows so
TOTAL_MISMATCH / ROWS_INCOMPLETE_SUSPECT stayed quiet, each copy's rows
reconciled against its own printed total, and the review's duplicate check
compared one merged ref against itself.  The demo shipment finalized at 238
items / 4217.78 with `ready_for_xml: True` and zero warnings.

Two gates, because neither covers the other's case:
  * byte-identical re-upload  -> rejected at upload (sha256, before any spend);
  * a re-scan of the same invoice (different bytes) -> INVOICE_DUPLICATE_DOCUMENT,
    a blocker that warn mode may not bypass.

Rows are never dropped by either gate — a wrong drop is worse than a blocked
job (see the withdrawn auto-dedup attempt: 11 catastrophic false-drops).
"""
import json

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.declaration.validator import WARN_MODE_HARD_CODES
from app.demo import seed_demo_job
from app.domain.enums import DeclaredRole, DocumentStatus
from app.domain.errors import BlockingValidationError
from app.extraction.common_models import InvoiceChunkRaw
from app.main import app
from app.rules.invoice_authority import finalize_invoices
from app import services

ROLE_OK = {"expected_role": "INVOICE", "matches_expected_role": True}
FINALIZE_BODY = {
    "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
    "manifest_no": "2026/1436", "field_18_transport_identity": "BA16CHA8099",
    "field_21_transport_identity": "BA16CHA8099", "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


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


def _row(page, idx, desc, total="100.00"):
    return {"source_page_no": page, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": total,
            "line_total_raw": total}


def _chunk(number, rows, date="01.04.2026"):
    return InvoiceChunkRaw.model_validate({
        "role_validation": ROLE_OK, "rows": rows, "sub_invoices": [],
        "header": {"invoice_number_raw": number, "invoice_date_raw": date,
                   "currency_raw": "USD"},
        "totals": None,
    })


# --------------------------------------------------------------------------- #
# Gate 1 — byte-identical re-upload never creates a second row
# --------------------------------------------------------------------------- #
def test_identical_reupload_into_same_role_is_rejected(invoice_bytes, invoice_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()

    with pytest.raises(BlockingValidationError) as e:
        services.add_document(db, job, DeclaredRole.INVOICE, "sample_invoice.pdf",
                              invoice_bytes, invoice_fixture)
    db.rollback()

    assert e.value.message.code == "DUPLICATE_DOCUMENT_UPLOAD"
    # names the document already attached, so the reviewer knows what collided
    assert "sample_invoice.pdf" in e.value.message.message
    assert e.value.message.document_id
    # nothing was stored: the job still carries exactly one invoice
    assert len([d for d in job.documents if d.declared_role == "INVOICE"]) == 1
    db.close()


def test_identical_file_in_a_different_role_box_is_allowed(invoice_bytes, invoice_fixture):
    """A combined 'Invoice cum Packing List' PDF legitimately fills two boxes —
    the gate is scoped to the role, not to the job."""
    init_db()
    db = SessionLocal()
    job = services.create_job(db)
    services.add_document(db, job, DeclaredRole.INVOICE, "combined.pdf",
                          invoice_bytes, invoice_fixture)
    doc = services.add_document(db, job, DeclaredRole.PACKING_LIST, "combined.pdf",
                                invoice_bytes, None)
    db.commit()
    assert doc.declared_role == "PACKING_LIST"
    assert len(job.documents) == 2
    db.close()


def test_reupload_after_failed_extraction_reuses_the_row(invoice_bytes, invoice_fixture):
    """The UI re-shows the file input on FAILED, so re-picking the same file IS
    the retry.  It must reset that row — not add a second one that then gets
    extracted alongside the first when Continue retries both."""
    init_db()
    db = SessionLocal()
    job = services.create_job(db)
    first = services.add_document(db, job, DeclaredRole.INVOICE, "sample_invoice.pdf",
                                  invoice_bytes, None)
    first.status = DocumentStatus.FAILED.value
    first.warnings = ["EXTRACTION_FAILED (RateLimitError): 429"]
    db.commit()
    first_id = first.id

    again = services.add_document(db, job, DeclaredRole.INVOICE, "sample_invoice.pdf",
                                  invoice_bytes, invoice_fixture)
    db.commit()

    assert again.id == first_id                                    # same row, reset
    assert again.status == DocumentStatus.EXTRACTED.value
    # the stale failure note is gone (the extraction's own notes replace it)
    assert not any("EXTRACTION_FAILED" in str(w) for w in again.warnings or [])
    assert len([d for d in job.documents if d.declared_role == "INVOICE"]) == 1

    review = services.critical_review(db, job)
    db.commit()
    assert review["invoice_item_count"] == 119                     # not 238
    assert review["calculated_goods_total"] == "2108.89"
    db.close()


def test_a_live_twin_outranks_a_failed_one(invoice_bytes, invoice_fixture):
    """A job stored before this gate existed can hold both a failed and a live
    copy.  Resetting the failed row there would extract the same file twice."""
    init_db()
    db = SessionLocal()
    job = services.create_job(db)
    stale = services.add_document(db, job, DeclaredRole.INVOICE, "first.pdf",
                                  invoice_bytes, None)
    stale.status = DocumentStatus.FAILED.value
    db.flush()
    # a second row with the same bytes, as the old code would have created
    from app.models import Document
    live = Document(job_id=job.id, declared_role=DeclaredRole.INVOICE.value,
                    upload_index_within_role=1, original_file_name="second.pdf",
                    content_type="application/pdf", byte_size=len(invoice_bytes),
                    sha256=stale.sha256, status=DocumentStatus.EXTRACTED.value)
    db.add(live)
    db.commit()

    with pytest.raises(BlockingValidationError) as e:
        services.add_document(db, job, DeclaredRole.INVOICE, "third.pdf",
                              invoice_bytes, invoice_fixture)
    db.rollback()
    assert e.value.message.code == "DUPLICATE_DOCUMENT_UPLOAD"
    assert "second.pdf" in e.value.message.message          # names the LIVE one
    db.close()


def test_upload_endpoint_returns_409_for_a_duplicate(client, invoice_bytes):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    r = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                    files={"file": ("sample_invoice.pdf", invoice_bytes, "application/pdf")})
    assert r.status_code == 409
    body = r.json()
    assert body["blocking_errors"][0]["code"] == "DUPLICATE_DOCUMENT_UPLOAD"
    # and the job is unchanged — one invoice document, still extractable
    docs = client.get(f"/api/jobs/{job_id}").json()["documents"]
    assert len([d for d in docs if d["role"] == "INVOICE"]) == 1


# --------------------------------------------------------------------------- #
# Gate 2 — the same printed invoice from two documents is BLOCKING
# --------------------------------------------------------------------------- #
def test_same_invoice_from_two_documents_blocks_and_names_both():
    rows = [_row(1, 1, "A1"), _row(1, 2, "A2")]
    inv = finalize_invoices([_chunk("INV-1", rows), _chunk("INV-1", rows)],
                            ["invoice.pdf", "invoice_rescan.pdf"])
    dups = [w for w in inv.warnings if w.code == "INVOICE_DUPLICATE_DOCUMENT"]
    assert len(dups) == 1
    assert "INV-1" in dups[0].message
    assert "invoice.pdf" in dups[0].message and "invoice_rescan.pdf" in dups[0].message
    # NOT dropped: every row survives for the reviewer to see and decide on
    assert len(inv.items) == 4


def test_duplicate_detection_ignores_whitespace_and_case():
    rows = [_row(1, 1, "A1")]
    inv = finalize_invoices([_chunk("INV-1", rows), _chunk("inv- 1", rows)],
                            ["a.pdf", "b.pdf"])
    assert any(w.code == "INVOICE_DUPLICATE_DOCUMENT" for w in inv.warnings)


def test_duplicate_detection_survives_a_differing_date():
    """A re-scan may lose or reformat the printed date; identity is the number."""
    rows = [_row(1, 1, "A1")]
    inv = finalize_invoices([_chunk("INV-1", rows, date="01.04.2026"),
                             _chunk("INV-1", rows, date="")], ["a.pdf", "b.pdf"])
    assert any(w.code == "INVOICE_DUPLICATE_DOCUMENT" for w in inv.warnings)


def test_two_genuinely_different_invoices_are_not_flagged():
    """Multi-document shipments are a supported feature — the gate keys on
    invoice identity, never on the document count."""
    inv = finalize_invoices([_chunk("INV-1", [_row(1, 1, "A1")]),
                             _chunk("INV-2", [_row(1, 1, "B1")])], ["a.pdf", "b.pdf"])
    assert not any(w.code == "INVOICE_DUPLICATE_DOCUMENT" for w in inv.warnings)
    assert [r.number for r in inv.invoice_refs] == ["INV-1", "INV-2"]
    assert len(inv.items) == 2


def test_repeated_number_inside_one_document_is_not_a_cross_document_duplicate():
    """Sub-invoice merge already unifies repeats within a chunk; this gate is
    strictly about one invoice arriving from two uploads."""
    chunk = InvoiceChunkRaw.model_validate({
        "role_validation": ROLE_OK, "rows": [_row(1, 1, "A1"), _row(2, 1, "A2")],
        "sub_invoices": [{"invoice_number_raw": "INV-1", "first_page_no": 1},
                         {"invoice_number_raw": "INV-1", "first_page_no": 2}],
        "header": {"invoice_number_raw": "INV-1", "currency_raw": "USD"}, "totals": None,
    })
    inv = finalize_invoices([chunk], ["only.pdf"])
    assert not any(w.code == "INVOICE_DUPLICATE_DOCUMENT" for w in inv.warnings)


def test_finalize_invoices_still_accepts_a_bare_chunk_list():
    """`sources` is optional — callers that only have payloads still work."""
    inv = finalize_invoices([_chunk("INV-1", [_row(1, 1, "A1")])])
    assert len(inv.items) == 1


# --------------------------------------------------------------------------- #
# Gate 2 end-to-end: warn mode may NOT bypass it
# --------------------------------------------------------------------------- #
def test_duplicate_invoice_blocks_xml_even_in_warn_mode(invoice_bytes, invoice_fixture):
    assert "INVOICE_DUPLICATE_DOCUMENT" in WARN_MODE_HARD_CODES
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()
    # a re-scan: same printed invoice, different bytes, so the sha256 gate
    # cannot see it — this is exactly what gate 2 exists for
    services.add_document(db, job, DeclaredRole.INVOICE, "sample_invoice_rescan.pdf",
                          invoice_bytes + b"\n% rescanned\n",
                          invoice_fixture)
    db.commit()

    review = services.critical_review(db, job)
    db.commit()
    assert any(w["code"] == "INVOICE_DUPLICATE_DOCUMENT" for w in review["warnings"])

    decl = services.finalize_job(db, job, dict(FINALIZE_BODY,
                                               review_fingerprint=review["review_fingerprint"]))
    db.commit()
    assert decl["ready_for_xml"] is False
    assert [e["code"] for e in decl["blocking_errors"]] == ["INVOICE_DUPLICATE_DOCUMENT"]
    # warn mode ships blocked declarations for ASYCUDA testing — but never a
    # declaration whose customs value and duty are 2x
    assert decl["xml_built_with_blockers"] is False
    assert services.latest_xml(db, job.id) is None
    db.close()
