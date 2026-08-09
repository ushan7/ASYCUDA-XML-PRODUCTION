"""A role-mismatched document must be answered before it can be declared (A2).

`run_extraction` records the extractor's verdict in two places — `role_match`
and a `ROLE_REVIEW_REQUIRED` document status — and before this gate existed
NEITHER was consulted again.  `_require_extracted` only refused UPLOADED and
FAILED documents, and `_group_raw`'s only test is "does it carry a
raw_extraction".  So a packing list dropped into the INVOICE upload box became
the goods roster: its rows priced, its totals declared, its parties written to
the SAD, `ready_for_xml` true.  The single sign was a red pill on the upload
card, which nothing forced anyone to act on.

The verdict is now a question with two answers, both explicit and both audited:

  accept -> the reviewer looked at the document and the extractor was wrong;
            it is used in the role its upload box declared;
  reject -> it is excluded from the declaration.  The upload, the OCR envelope
            and the extraction are all KEPT (evidence is immutable) — the
            document simply stops being readable by the pipeline.

Either answer invalidates everything derived, because either answer changes
what the declaration is built from.
"""
import json
from decimal import Decimal

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.demo import seed_demo_job
from app.domain.enums import DeclaredRole, DocumentStatus, JobStatus
from app.domain.errors import BlockingValidationError
from app.main import app
from app import services


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def packing_fixture():
    return json.loads((SAMPLE_DIR / "fixtures" / "packing_list.json").read_text())


def _mismatched_invoice_upload(db, job, packing_fixture):
    """A packing list uploaded into the INVOICE box — the extractor says so."""
    fixture = json.loads(json.dumps(packing_fixture))
    fixture["role_validation"] = {"expected_role": "INVOICE",
                                  "matches_expected_role": False,
                                  "reasons": ["document is a packing list"]}
    return services.add_document(db, job, DeclaredRole.INVOICE, "packing_in_invoice_box.pdf",
                                 b"%PDF-1.4\nrole-mismatch\n", fixture)


def _doubted_invoice_upload(db, job):
    """A REAL second invoice the extractor wrongly flags as the wrong role —
    the false-negative direction, where the right answer is to accept."""
    fixture = {
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": False,
                            "reasons": ["layout not recognised"]},
        "header": {"invoice_number_raw": "SUPP-77", "invoice_date_raw": "02.04.2026",
                   "currency_raw": "USD"},
        "sub_invoices": [], "totals": None,
        "rows": [{"source_page_no": 1, "source_row_index": 1,
                  "description_raw": "STAINLESS STEEL CLAMP 40MM", "quantity_raw": "4",
                  "uom_raw": "PCS", "unit_price_raw": "25.00", "line_total_raw": "100.00"}],
    }
    return services.add_document(db, job, DeclaredRole.INVOICE, "supplementary_invoice.pdf",
                                 b"%PDF-1.4\ndoubted-invoice\n", fixture)


# --------------------------------------------------------------------------- #
# The verdict is recorded and it stops the pipeline
# --------------------------------------------------------------------------- #
def test_extractor_mismatch_marks_the_document_for_review(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()

    assert doc.role_match is False
    assert doc.status == DocumentStatus.ROLE_REVIEW_REQUIRED.value
    db.close()


def test_unanswered_mismatch_blocks_critical_review(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()

    with pytest.raises(BlockingValidationError) as e:
        services.critical_review(db, job)
    msg = e.value.message
    assert msg.code == "DOCUMENT_ROLE_UNCONFIRMED"
    # actionable: names the file and both available answers
    assert "packing_in_invoice_box.pdf" in msg.message
    assert "reject" in msg.message.lower()
    db.rollback()
    db.close()


def test_unanswered_mismatch_blocks_finalize(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()

    with pytest.raises(BlockingValidationError) as e:
        services.finalize_job(db, job, {})
    assert e.value.message.code == "DOCUMENT_ROLE_UNCONFIRMED"
    db.rollback()
    db.close()


# --------------------------------------------------------------------------- #
# Rejecting excludes the document without destroying it
# --------------------------------------------------------------------------- #
def test_rejected_document_is_excluded_but_its_evidence_is_kept(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    baseline = services.critical_review(db, job)

    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()
    services.resolve_document_role(db, job, doc, accept=False, reason="wrong box")

    assert doc.status == DocumentStatus.ROLE_REJECTED.value
    # evidence survives — uploads and OCR are immutable
    assert doc.raw_extraction is not None
    assert doc.storage_key
    # and the declaration is exactly what it was before the stray upload
    after = services.critical_review(db, job)
    assert after["invoice_item_count"] == baseline["invoice_item_count"]
    assert after["calculated_goods_total"] == baseline["calculated_goods_total"]
    assert doc not in services.declarable_documents(job)
    db.close()


def test_accepting_uses_the_document_in_its_declared_role():
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    baseline = services.critical_review(db, job)

    doc = _doubted_invoice_upload(db, job)
    db.commit()
    services.resolve_document_role(db, job, doc, accept=True, reason="checked, it is an invoice")

    assert doc.status == DocumentStatus.EXTRACTED.value
    # the mismatch verdict itself is NOT erased — the reviewer overrode it
    assert doc.role_match is False
    assert doc in services.declarable_documents(job)
    # accepted means used: its row is on the declaration and its value counted
    after = services.critical_review(db, job)
    assert after["invoice_item_count"] == baseline["invoice_item_count"] + 1
    assert Decimal(after["calculated_goods_total"]) == \
        Decimal(baseline["calculated_goods_total"]) + Decimal("100.00")
    assert "SUPP-77" in after["invoice_numbers"]
    db.close()


# --------------------------------------------------------------------------- #
# The decision is audited and it stales everything derived
# --------------------------------------------------------------------------- #
def test_decision_invalidates_the_stored_declaration_and_is_audited(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    services.critical_review(db, job)
    assert job.critical_review is not None

    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()
    services.resolve_document_role(db, job, doc, accept=False)

    assert job.critical_review is None
    assert job.declaration is None
    assert job.status == JobStatus.EXTRACTION_COMPLETE.value

    codes = [e.event_code for e in job.events]
    assert "DOCUMENT_ROLE_REJECTED" in codes
    payload = next(e.payload for e in job.events if e.event_code == "DOCUMENT_ROLE_REJECTED")
    assert payload["accepted"] is False
    assert payload["role"] == "INVOICE"
    db.close()


def test_deciding_twice_is_refused(packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()
    services.resolve_document_role(db, job, doc, accept=True)

    with pytest.raises(BlockingValidationError) as e:
        services.resolve_document_role(db, job, doc, accept=False)
    assert e.value.message.code == "DOCUMENT_ROLE_NOT_IN_REVIEW"
    db.close()


def test_a_matching_document_never_needs_a_decision():
    """The gate must not add a click to the normal path."""
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    db.commit()

    assert all(d.status != DocumentStatus.ROLE_REVIEW_REQUIRED.value for d in job.documents)
    services.critical_review(db, job)          # no exception
    db.close()


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #
def test_role_decision_endpoint(client, packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()
    job_id, doc_id = job.id, doc.id
    db.close()

    blocked = client.get(f"/api/jobs/{job_id}/critical-review")
    assert blocked.status_code == 409
    assert blocked.json()["blocking_errors"][0]["code"] == "DOCUMENT_ROLE_UNCONFIRMED"

    ok = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/role-decision",
                     json={"accept": False, "reason": "uploaded to the wrong box"})
    assert ok.status_code == 200
    assert ok.json()["document_status"] == DocumentStatus.ROLE_REJECTED.value

    assert client.get(f"/api/jobs/{job_id}/critical-review").status_code == 200


def test_role_decision_endpoint_rejects_unknown_fields(client, packing_fixture):
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db)
    doc = _mismatched_invoice_upload(db, job, packing_fixture)
    db.commit()
    job_id, doc_id = job.id, doc.id
    db.close()

    r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/role-decision",
                    json={"accept": True, "declared_role": "PACKING_LIST"})
    assert r.status_code == 422        # the role is server metadata, never client input
