"""Server-owned enumerations.

The declared role is chosen by the frontend upload box and is *immutable
server metadata*.  The LLM may report a role mismatch but may never redefine
the role.
"""
from __future__ import annotations

from enum import Enum


class DeclaredRole(str, Enum):
    INVOICE = "INVOICE"
    PACKING_LIST = "PACKING_LIST"
    AIR_WAYBILL = "AIR_WAYBILL"
    BANKING = "BANKING"
    INSURANCE = "INSURANCE"
    CERTIFICATE_OF_ORIGIN = "CERTIFICATE_OF_ORIGIN"


# Only the invoice is compulsory (user rule 2026-07-17). Absent documents
# degrade to deterministic fallbacks + manual reviewer entry:
#   PACKING_LIST absent -> authority gross split by item quantity share,
#                          cartons by weight share (exact-sum reconciled);
#   AIR_WAYBILL  absent -> reviewer enters total weight, total cartons and
#                          MAWB / HAWB / Bill-of-Lading number manually;
#   BANKING      absent -> bank/payment fields entered manually in review.
REQUIRED_ROLES = (DeclaredRole.INVOICE,)
OPTIONAL_ROLES = (
    DeclaredRole.PACKING_LIST,
    DeclaredRole.AIR_WAYBILL,
    DeclaredRole.BANKING,
    DeclaredRole.INSURANCE,
    DeclaredRole.CERTIFICATE_OF_ORIGIN,
)


class JobStatus(str, Enum):
    UPLOADING = "UPLOADING"
    UPLOAD_COMPLETE = "UPLOAD_COMPLETE"
    OCR_RUNNING = "OCR_RUNNING"
    OCR_COMPLETE = "OCR_COMPLETE"
    EXTRACTION_RUNNING = "EXTRACTION_RUNNING"
    ROLE_REVIEW_REQUIRED = "ROLE_REVIEW_REQUIRED"
    EXTRACTION_COMPLETE = "EXTRACTION_COMPLETE"
    RULE_RESOLUTION_RUNNING = "RULE_RESOLUTION_RUNNING"
    CRITICAL_REVIEW_REQUIRED = "CRITICAL_REVIEW_REQUIRED"
    DETAIL_REVIEW_READY = "DETAIL_REVIEW_READY"
    VALIDATION_BLOCKED = "VALIDATION_BLOCKED"
    XML_BUILDING = "XML_BUILDING"
    XML_READY = "XML_READY"
    FAILED = "FAILED"


class DocumentStatus(str, Enum):
    UPLOADED = "UPLOADED"
    OCR_COMPLETE = "OCR_COMPLETE"
    # Claimed by a running extraction (services.run_extraction).  The claim is
    # taken with a status-guarded UPDATE before the slow OCR/LLM work, so only
    # one run per document can be in flight: without it a double-submit or a
    # second tab started a second paid round on the same file and the slower
    # one overwrote the faster one's rows.  It also makes in-flight work
    # visible — a document being extracted used to be indistinguishable from
    # one merely waiting, so reloading the page mid-extraction showed
    # "uploaded — pending".  Released by the same call, or by
    # recover_interrupted_extractions at startup if the process died.
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    # The extractor reported the document is not the role its upload box
    # declares.  A terminal state for the pipeline: the reviewer must accept
    # the declared role or reject the document before anything downstream may
    # read it (services.resolve_document_role).
    ROLE_REVIEW_REQUIRED = "ROLE_REVIEW_REQUIRED"
    # Reviewer rejected the role match: the evidence is kept, but the document
    # contributes nothing to the declaration.
    ROLE_REJECTED = "ROLE_REJECTED"
    FIELD_REVIEW_REQUIRED = "FIELD_REVIEW_REQUIRED"
    FAILED = "FAILED"


class Severity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class RowClassification(str, Enum):
    REAL_GOODS_ITEM = "REAL_GOODS_ITEM"
    FREIGHT_CHARGE = "FREIGHT_CHARGE"
    INSURANCE_CHARGE = "INSURANCE_CHARGE"
    PICKUP_CHARGE = "PICKUP_CHARGE"
    OTHER_CHARGE = "OTHER_CHARGE"
    DISCOUNT_OR_ADJUSTMENT = "DISCOUNT_OR_ADJUSTMENT"
    SUBTOTAL_OR_TOTAL_ROW = "SUBTOTAL_OR_TOTAL_ROW"
    AMBIGUOUS_REVIEW = "AMBIGUOUS_REVIEW"
