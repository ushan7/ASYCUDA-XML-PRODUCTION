"""One-click demo — loads the bundled sample documents + fixtures into a job.

Uses the real sample PDFs (so OCR/upload metadata is genuine) paired with the
evidence-backed raw fixtures reconstructed from the reference declaration.  This
exercises the entire deterministic pipeline offline, with no API keys.
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .config import SAMPLE_DIR
from .domain.enums import DeclaredRole, ExtractionProvenance
from .models import Job
from .services import add_document, create_job

_DEMO = [
    (DeclaredRole.INVOICE, "sample_invoice.pdf", "invoice.json"),
    (DeclaredRole.PACKING_LIST, "sample_packing_list.pdf", "packing_list.json"),
    (DeclaredRole.AIR_WAYBILL, "sample_airwaybill.pdf", "air_waybill.json"),
    (DeclaredRole.BANKING, "sample_banking_doc.pdf", "banking.json"),
]


def seed_demo_job(db: Session, created_by: str = "demo", *, actor: str = "demo",
                  owner_key: str = "") -> Job:
    # The demo job belongs to whoever asked for it, like any other job — it is
    # a real row in the real dashboard, not a special case.
    job = create_job(db, created_by=created_by, actor=actor, owner_key=owner_key)
    for role, pdf_name, fixture_name in _DEMO:
        pdf_path = SAMPLE_DIR / pdf_name
        data = pdf_path.read_bytes() if pdf_path.exists() else b"%PDF-1.4\n"
        fixture = json.loads((SAMPLE_DIR / "fixtures" / fixture_name).read_text())
        # BUNDLED_DEMO, not the client-fixture hook: these files ship with the
        # server and no request can influence them, so the demo runs on any
        # deployment — while every document it seeds is stamped, audited and
        # shown as a demo, so the values can never be mistaken for OCR of the
        # attached PDF.  Sharing one ungated hook with client-supplied facts is
        # what made this button 409 wherever the fixture flag was off.
        add_document(db, job, role, pdf_name, data, fixture, actor=actor,
                     provenance=ExtractionProvenance.BUNDLED_DEMO)
    return job
