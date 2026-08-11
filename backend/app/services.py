"""Application services that glue persistence + OCR + extraction + pipeline."""
from __future__ import annotations

import io
import json
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, text
from sqlalchemy import update as sql_update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import images
from .config import get_settings
from .declaration.validator import WARN_MODE_HARD_CODES
from .domain.enums import DeclaredRole, DocumentStatus, ExtractionProvenance, JobStatus
from .domain.errors import BlockingValidationError, ValidationMessage
from .extraction import field_profiles
from .extraction.service import extract_document
from .numbers import parse_decimal


# Attribution for events no human triggered: startup recovery, a cascade
# invalidation, a background rebuild.  Anything reached from a REQUEST records
# the signed-in operator instead — see _audit.
SYSTEM_ACTOR = "system"


def packing_timing_note(elapsed: float, budget: float, warnings: list) -> str | None:
    """What to say about a packing extraction that took longer than its budget.

    COMPLETE but late is NOT over budget.  This branch used to stamp the
    over-budget marker, which made the pipeline drop a fully extracted, fully
    validated packing list — every item weight the document stated — because it
    finished a few seconds after the clock, and then split the shipment by
    invoice value instead.  Evidence that exists is evidence.

    Returns None when the extractor already reported its own outcome (a hard
    abort, or a partial result): those messages are more specific than this one.
    """
    if elapsed <= budget:
        return None
    if any(("PACKING_EXTRACTION_OVER_BUDGET" in str(w)
            or "PACKING_EXTRACTION_PARTIAL" in str(w)) for w in warnings):
        return None
    return (f"PACKING_EXTRACTION_SLOW: extraction + reasoning took {elapsed:.0f}s "
            f"(budget {budget:.0f}s) but COMPLETED — the extracted item weights and cartons "
            f"are used as normal.")
from .models import AuditEvent, BmsArtifact, Document, Job, XmlArtifact
from .ocr.base import OcrDocument
from .ocr.offline import OfflineOcrProvider
from .ocr.service import get_ocr_provider
from .pipeline import finalize as pipeline_finalize
from .pipeline import resolve_context, to_critical_review
from .review import item_mutations as itemmut
from .review.critical_review import CriticalReviewConfirmation, merge_confirmation
from .storage import delete_stored_document, sha256_bytes, store_document
from .xml.bms_export import BMS_TEMPLATE_VERSION, build_bms_xls
from .xml.bms_export import checksum as bms_checksum
from .xml.composer import build_xml, checksum

# --------------------------------------------------------------------------- #
# Per-job mutual exclusion for review / finalize / item mutations.
#
# Two implementations, chosen by the database, because the guarantee has to
# survive the deployment shape rather than assume one:
#
#   POSTGRES — a TRANSACTION-scoped advisory lock.  Cross-process by
#     construction, which is the whole reason more than one API process may
#     exist: a threading.Lock only ever serialised the threads inside ONE
#     uvicorn worker, so with two workers (let alone two containers) a finalize
#     and an hs-review could interleave on the same declaration and neither side
#     would see the other.  That is not a slower app, it is a wrong one — the
#     comment this replaces said "the deployment is a single uvicorn process",
#     and it was load-bearing.
#
#     Transaction-scoped (pg_advisory_xact_lock) rather than session-scoped
#     (pg_advisory_lock), for two independent reasons:
#       * an exception that escapes the block releases the lock via the
#         rollback, so a crashed or killed request cannot wedge a job forever;
#       * it is the only kind that is safe behind a connection pooler in
#         transaction mode (PgBouncer, RDS Proxy), where the connection under a
#         session is not stable from one statement to the next.
#     The cost is that the lock ends at the COMMIT rather than at the end of the
#     `with` block.  Every call site already commits as its last statement and
#     then only returns values it has in hand — which is precisely the ordering
#     the "persist before the lock is released" comments describe.
#
#   SQLITE — the in-process threading.Lock.  SQLite has no advisory locks and
#     is single-writer anyway; this is the dev/test/one-laptop path.
# --------------------------------------------------------------------------- #

# Namespace for this application's advisory locks, so a key can never collide
# with another tool taking advisory locks on the same database.  Must fit int4.
_ADVISORY_LOCK_NAMESPACE = 0x45435F4A          # "EC_J"

# Postgres SQLSTATE 55P03 lock_not_available — what a lock_timeout raises.
_LOCK_NOT_AVAILABLE = "55P03"

_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCK_WAITERS: dict[str, int] = {}
_JOB_LOCKS_GUARD = threading.Lock()


@contextmanager
def _in_process_job_lock(job_id: str):
    """Serialise threads within ONE process, and forget the job afterwards.

    The dict used to keep an entry per job id for the life of the process.  On a
    laptop that is nothing; on a server it is a slow leak keyed by every job
    anyone has ever opened, and nothing ever removed an entry.  The refcount is
    what makes removal safe: an entry may only go when nobody holds the lock and
    nobody is queued for it.
    """
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.setdefault(job_id, threading.Lock())
        _JOB_LOCK_WAITERS[job_id] = _JOB_LOCK_WAITERS.get(job_id, 0) + 1
    try:
        with lock:
            yield
    finally:
        with _JOB_LOCKS_GUARD:
            remaining = _JOB_LOCK_WAITERS.get(job_id, 1) - 1
            if remaining > 0:
                _JOB_LOCK_WAITERS[job_id] = remaining
            else:
                _JOB_LOCK_WAITERS.pop(job_id, None)
                _JOB_LOCKS.pop(job_id, None)


def _is_postgres(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        # An unbound or exotic session is not a Postgres one for our purposes;
        # falling back to the in-process lock is the conservative answer.
        return False


def _take_advisory_lock(db: Session, job_id: str) -> None:
    timeout = get_settings().job_lock_timeout_seconds
    if timeout > 0:
        # set_config(..., is_local=true) is the parameterised form of
        # `SET LOCAL` — plain SET takes no bind parameters.  Being LOCAL is what
        # keeps the timeout from leaking onto the next request that borrows this
        # pooled connection.
        db.execute(text("SELECT set_config('lock_timeout', :ms, true)"),
                   {"ms": f"{int(timeout * 1000)}"})
    try:
        # hashtext() is 32-bit, so two different job ids can theoretically share
        # a key.  The consequence is that those two jobs serialise against each
        # other — slower, never wrong — which is why a hash is acceptable here
        # and would not be for a uniqueness check.
        db.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:job))"),
                   {"ns": _ADVISORY_LOCK_NAMESPACE, "job": job_id})
    except DBAPIError as e:
        if _sqlstate(e) != _LOCK_NOT_AVAILABLE:
            raise                            # a real database fault, not contention
        raise BlockingValidationError(
            "JOB_BUSY",
            f"Another change to this job is still running and did not finish within "
            f"{timeout:.0f}s. Wait for it to complete, then try again.") from e


def _sqlstate(e: DBAPIError) -> str | None:
    """The SQLSTATE of a driver error, across psycopg 3 and psycopg 2 spellings."""
    orig = getattr(e, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


@contextmanager
def job_lock(db: Session, job_id: str):
    """Hold the job for a read-modify-write.  See the note above for why this
    takes the session: on Postgres the lock lives in the transaction."""
    if _is_postgres(db):
        _take_advisory_lock(db, job_id)
        yield                                # released by the caller's commit
        return
    with _in_process_job_lock(job_id):
        yield


def _fresh_under_lock(db: Session, job: Job) -> None:
    """MUST be the first statement inside job_lock: the endpoint loaded the Job
    BEFORE the lock, so its overlay/review/status may be a stale snapshot of a
    mutation that just committed.  Re-reading here (and committing before the
    lock is released — see the lock sites) makes read-modify-write atomic;
    without it a concurrent hs-review can silently resurrect a deleted item."""
    # The flush is not optional.  ``Job.documents`` cascades "all", which
    # INCLUDES refresh-expire, and the session runs with autoflush=False — so
    # a refresh here expires every loaded Document and DISCARDS any pending
    # change on it that has not been written yet.  That is exactly what killed
    # extraction: run_extraction set status/raw_extraction on the document,
    # then took this lock, and the refresh threw the results away.  The commit
    # that followed wrote only the job status and a DOCUMENT_EXTRACTED audit
    # event, so the trail claimed success while the row still read UPLOADED —
    # the document reverted to "uploaded — pending" and the reviewer paid for
    # the same extraction again on the next Continue.
    #
    # Flushing first makes the caller's own writes part of the transaction the
    # refresh reads back, so re-reading the job can no longer erase them.
    db.flush()
    db.refresh(job)


def create_job(db: Session, created_by: str = "", *, actor: str = SYSTEM_ACTOR,
               owner_key: str = "") -> Job:
    # `owner_key` comes from the caller naming a principal, and from nowhere
    # else.  It must NOT fall back to `actor`: actor is the audit label for
    # whoever triggered the action ("demo", "system", a background rebuild),
    # and inferring access from it is the exact audit/authorization conflation
    # that keeping the two columns apart is meant to prevent — it made
    # seed_demo_job mint jobs owned by the literal string "demo", which the
    # operator who asked for them could then not read.  A caller with no
    # principal to name creates an unowned job, which is what a direct service
    # or test call is.
    settings = get_settings()
    job = Job(status=JobStatus.UPLOADING.value, rule_set_version=settings.rule_set_version,
              exchange_rate=str(settings.default_exchange_rate),
              created_by=created_by, owner_key=owner_key)
    db.add(job)
    db.flush()
    _audit(db, job.id, "JOB_CREATED", "job created", actor=actor)
    return job


# Internal callers with no signed-in principal: startup recovery, a cascade
# invalidation, a background rebuild.  Spelled out rather than defaulted,
# because "owner is optional" is how every route ends up unscoped.
SYSTEM_PRINCIPAL = "\x00system"


def principal_of(session) -> str | None:
    """The access key for a verified session, or None if there is no session.

    Deliberately NOT defaulting to SYSTEM_PRINCIPAL. The auth middleware means
    a job route always has a session, so the fallback would never fire — right
    up until someone adds a route the gate does not cover, at which point
    "no session" would have quietly meant "see everything". Callers that
    genuinely have no user (startup recovery, cascade invalidation) name
    SYSTEM_PRINCIPAL themselves.
    """
    return getattr(session, "username", None) or None


def job_visible_to(job: Job, principal: str) -> bool:
    """The ONE place that decides whether a principal may touch a job.

    Ownership is a single predicate in a single function on purpose.  Twenty
    route handlers each remembering to add `.where(owner == me)` is twenty
    chances to forget, and the one that forgets is not discovered until someone
    reads another operator's declaration.

    An empty owner_key means the row predates ownership.  The SQLite migration
    backfills those to the configured operator, so on a migrated deployment
    this branch matches nothing; it stays for a Postgres deployment that has
    not run a backfill yet, where hiding a broker's entire history would be a
    worse failure than showing it to the single account that can log in.
    WHEN A SECOND ACCOUNT CAN EXIST, THIS BRANCH MUST GO — it is the difference
    between "not scoped yet" and "scoped wrongly".
    """
    if principal == SYSTEM_PRINCIPAL:
        return True
    if not job.owner_key:
        return True
    return job.owner_key == principal


def get_job(db: Session, job_id: str, *, principal: str) -> Job | None:
    """Fetch a job the principal is allowed to see, else None.

    None (not a 403) so the caller answers 404: an id that exists but belongs
    to someone else must be indistinguishable from one that does not exist,
    or the 403 itself confirms the job is real.
    """
    job = db.get(Job, job_id)
    if job is None or not job_visible_to(job, principal):
        return None
    return job


def _sniff_upload_kind(data: bytes) -> str | None:
    """Best-effort label for a refused upload, for a message that tells the
    reviewer what they actually picked instead of just 'not supported'.
    JPEG/PNG never reach this — they are accepted (converted to PDF)."""
    if (data.startswith(b"GIF8") or data.startswith(b"BM")
            or data.startswith(b"II*\x00") or data.startswith(b"MM\x00*")
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        return "an image in an unsupported format (re-save it as JPEG or PNG)"
    if data.startswith(b"PK\x03\x04"):
        return "an Office document (docx/xlsx/pptx) or zip archive"
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        return "a legacy Office document (doc/xls)"
    return None


def pdf_page_count(source) -> int | None:
    """How many pages a PDF has, or None when that cannot be read cheaply.

    Walks the page tree only — no content streams, no rendering — so it costs a
    rounding error against the OCR call it exists to gate.  An unreadable file
    returns None rather than raising: this is a ceiling, not a validity check,
    and a PDF pypdf cannot open earns a better message further in.
    """
    try:
        from pypdf import PdfReader
        return len(PdfReader(source).pages)
    except Exception:
        return None


def enforce_page_ceiling(page_count: int | None, *, filename: str = "") -> None:
    """Refuse a document with more pages than extraction would ever send.

    The same ceiling already exists in the OpenAI extractor, but it runs after
    the OCR bill has been paid and only on that one provider path — so an
    oversized PDF was fully OCR'd, charged for, and only then refused, and on
    the offline provider it was never refused at all.  Reading the page tree
    first makes the ceiling free and makes it apply everywhere.
    """
    max_pages = get_settings().extraction_max_pages
    if not max_pages or page_count is None or page_count <= max_pages:
        return
    subject = f"{filename!r} has" if filename else "This document has"
    raise BlockingValidationError(
        "DOCUMENT_TOO_MANY_PAGES",
        f"{subject} {page_count} pages — the extraction limit is {max_pages}. "
        f"Split it and attach the parts separately (each part extracts on its own and "
        f"the declaration combines them).", scope="DOCUMENT")


def validate_upload(filename: str, data: bytes) -> str:
    """Refuse an upload that can only fail later, while the reviewer is still
    looking at the box they dropped it in.  Returns what was accepted:
    ``"pdf"`` (stored as-is) or ``"image"`` (JPEG/PNG, converted to PDF).

    Every rejection here used to be accepted silently and surface as a FAILED
    extraction — after a paid OCR round, behind an upstream error message, on
    a document that no number of retries could ever fix.
    """
    if not data:
        raise BlockingValidationError(
            "EMPTY_DOCUMENT_UPLOAD",
            f"{filename!r} is empty (0 bytes). Re-export or re-scan the document and "
            f"attach it again.", scope="DOCUMENT")
    limit_mb = get_settings().max_upload_mb
    if len(data) > limit_mb * 1024 * 1024:
        raise BlockingValidationError(
            "DOCUMENT_TOO_LARGE",
            f"{filename!r} is {len(data) / (1024 * 1024):.0f} MB — the limit is {limit_mb} MB. "
            f"Customs paperwork this large is usually a mis-pick; if it is real, split the "
            f"PDF or re-export it at a lower scan resolution.", scope="DOCUMENT")
    # The PDF header must sit in the first 1024 bytes (the spec's own
    # tolerance for preamble junk that some generators emit).  That tolerance
    # must not stretch to a file that is MARKUP first and PDF only further in:
    # the stored upload is served back to the reviewer INLINE, so a file that
    # is a document to this gate and a script to their browser is the whole
    # attack.  A real PDF never opens with "<", so refusing that costs nothing.
    if b"%PDF-" in data[:1024]:
        if data.lstrip(b"\xef\xbb\xbf \t\r\n\x0c")[:1] == b"<":
            raise BlockingValidationError(
                "UNSUPPORTED_DOCUMENT_TYPE",
                f"{filename!r} begins with markup (HTML, SVG or XML) and only looks like a "
                f"PDF further in. Attach the PDF as the supplier's system exported it.",
                scope="DOCUMENT")
        # Page ceiling here, not at extraction: the reviewer is still looking at
        # the box they dropped it in, and nothing has been spent yet.
        enforce_page_ceiling(pdf_page_count(io.BytesIO(data)), filename=filename)
        return "pdf"
    if images.image_kind(data):
        # A photo only extracts through a live OCR provider: the offline
        # fallback is pypdf text-layer extraction, and an image-derived PDF
        # has no text layer — accepting it here would burn an extraction run
        # that can only come back empty.  Same philosophy as every other gate:
        # fail at upload, not three steps later.
        if not get_settings().ocr_live_ready:
            raise BlockingValidationError(
                "PHOTO_NEEDS_LIVE_OCR",
                f"{filename!r} is a photo, but this server is running the offline text-layer "
                f"OCR, which cannot read images. Configure the Mistral OCR provider (set "
                f"MISTRAL_API_KEY) to enable photo uploads, or attach a text PDF instead.",
                scope="DOCUMENT")
        return "image"
    if images.looks_like_heic(data):
        raise BlockingValidationError(
            "UNSUPPORTED_DOCUMENT_TYPE",
            f"{filename!r} is an iPhone HEIC photo, which this system cannot read directly. "
            f"Share or export it as JPEG and attach that (or set iPhone Camera → Formats → "
            f"Most Compatible, so the camera shoots JPEG).", scope="DOCUMENT")
    kind = _sniff_upload_kind(data)
    raise BlockingValidationError(
        "UNSUPPORTED_DOCUMENT_TYPE",
        f"{filename!r} is not a supported document" + (f" — it looks like {kind}" if kind else "")
        + ". Attach a PDF, or a JPEG/PNG photo of the document.",
        scope="DOCUMENT")


def add_document(db: Session, job: Job, role: DeclaredRole, filename: str, data: bytes,
                 fixture: dict | None, *, actor: str = SYSTEM_ACTOR,
                 provenance: ExtractionProvenance = ExtractionProvenance.OCR) -> Document:
    """Store the upload. Extraction is a separate user-triggered step, except
    for fixture uploads (demo/tests) which extract immediately — deterministic
    and instant, no external calls.

    ``provenance`` says where ``fixture`` came from, and every caller passing a
    fixture must state it (see :func:`_attach_document`).  It is what separates
    the bundled demo from unverified facts arriving in an HTTP request; the two
    used to be the same hook, so the gate on one closed the other.

    The client's declared MIME type is deliberately NOT a parameter.  What gets
    stored is always PDF bytes — validate_upload proved it, or images.py just
    converted it — so the content type is a server-side fact, not something the
    upload gets a say in.  It used to be taken from the multipart part header
    and persisted: the browser's guess (from the file extension) then came back
    as the media type of the inline evidence viewer, so an "invoice" that
    declared text/html ran as script on this origin.
    """
    kind = validate_upload(filename, data)
    # Dedup on the bytes the USER picked, before any conversion: that is the
    # file they can pick twice, and it keeps the digest stable regardless of
    # converter version.
    digest = sha256_bytes(data)
    converted = kind == "image"
    if converted:
        # JPEG/PNG → single-page PDF at the boundary (lossless; see images.py).
        # Downstream never learns that image uploads exist.
        data = images.photos_to_pdf([(filename, data)])
    return _attach_document(db, job, role, filename, data, digest,
                            fixture, converted_pages=1 if converted else 0, actor=actor,
                            provenance=provenance)


def add_photo_document(db: Session, job: Job, role: DeclaredRole,
                       photos: list[tuple[str, bytes]], *,
                       actor: str = SYSTEM_ACTOR) -> Document:
    """ONE document from several phone photos — each photo is one page of the
    SAME document (a 3-page invoice photographed page by page), merged in the
    order given.  Attaching them as separate documents instead would declare
    three invoices: the rows of each would be emitted again, tripling items
    and totals — the document-boundary failure this pipeline has been bitten
    by before."""
    if not photos:
        raise BlockingValidationError(
            "EMPTY_DOCUMENT_UPLOAD", "No photos were received — add at least one photo.",
            scope="DOCUMENT")
    for filename, data in photos:
        kind = validate_upload(filename, data)
        if kind != "image":
            raise BlockingValidationError(
                "UNSUPPORTED_DOCUMENT_TYPE",
                f"{filename!r} is a PDF — a photo set combines images into one document. "
                f"Attach the PDF on its own instead; it is already a complete document.",
                scope="DOCUMENT")
    # Digest over the original photo bytes in page order: deterministic (a
    # converter upgrade can't change it), and identical to the single-file
    # digest when the set holds one photo — so the same photo attached via
    # either path trips the same duplicate gate.
    digest = sha256_bytes(b"".join(data for _, data in photos))
    pdf = images.photos_to_pdf(photos)
    limit_mb = get_settings().max_upload_mb
    if len(pdf) > limit_mb * 1024 * 1024:
        raise BlockingValidationError(
            "DOCUMENT_TOO_LARGE",
            f"The {len(photos)} photos merge to {len(pdf) / (1024 * 1024):.0f} MB — the limit "
            f"is {limit_mb} MB. Remove some photos or retake them at a lower resolution.",
            scope="DOCUMENT")
    first = photos[0][0]
    display = first if len(photos) == 1 else f"{Path(first).stem} +{len(photos) - 1} photos.pdf"
    return _attach_document(db, job, role, display, pdf, digest,
                            fixture=None, converted_pages=len(photos), actor=actor)


def _stamp_provenance(db: Session, job: Job, doc: Document,
                      provenance: ExtractionProvenance, *, actor: str) -> None:
    """Record on the row where this document's facts came from, and say so in
    the audit trail when they did not come from the document.

    Silence is the failure mode worth avoiding here: a seeded extraction and a
    real one are the same JSON in the same column, so without this the only
    trace that a declaration was built from values nobody read off the paper is
    an operator's memory of which button they pressed.
    """
    doc.extraction_provenance = provenance.value
    if provenance is ExtractionProvenance.OCR:
        return
    # Flush before auditing: a brand-new row's id is assigned by the column
    # default AT FLUSH (models._uuid), so reading doc.id before one records the
    # event against document None — a trail that names no document is not a
    # trail.  Harmless no-op on the retry path, where the row is already
    # persistent.
    db.flush()
    _audit(db, job.id, "EXTRACTION_SEEDED",
           f"{doc.declared_role} extraction supplied, not read from the document "
           f"(provenance {provenance.value})", actor=actor,
           payload={"document_id": doc.id, "provenance": provenance.value})


def is_demo_job(job: Job) -> bool:
    """True when any of this job's documents was seeded from a bundled sample.

    DERIVED, never stored: a flag on the job and a provenance on the row are
    two things that can disagree, and the one that would be wrong is the one
    the UI reads.  The documents are the evidence, so they are the answer.
    """
    return any(d.extraction_provenance == ExtractionProvenance.BUNDLED_DEMO.value
               for d in job.documents)


def _attach_document(db: Session, job: Job, role: DeclaredRole, filename: str, data: bytes,
                     digest: str, fixture: dict | None,
                     converted_pages: int = 0, *, actor: str = SYSTEM_ACTOR,
                     provenance: ExtractionProvenance = ExtractionProvenance.OCR) -> Document:
    """Shared tail of every upload path: duplicate gate, retry-reset, row +
    stored file.  ``data`` is what is STORED (always PDF bytes by now);
    ``digest`` identifies what the user picked (pre-conversion)."""
    # The replay hook is gated HERE as well as on the upload route, because the
    # route is not the only way in — seed_demo_job calls add_document directly.
    #
    # But the gate is on PROVENANCE, not on "a fixture was passed", and the
    # difference is the whole bug.  What must never reach a declaration
    # unannounced is facts supplied by a CLIENT: chosen outside the server,
    # bounded by nothing, indistinguishable afterwards from something read off
    # the paper.  A fixture the server loads from its own backend/sample_data
    # is none of those things — it ships with the code, no request can alter
    # it, and it is the entire content of the demo.  Gating both as one turned
    # "reject unverified values from the network" into "the sample shipment
    # button 409s on every deployment that has not opted in", which is how it
    # was found.  Bundled fixtures are allowed and MARKED (below); client
    # fixtures still need the flag.
    if fixture is not None and provenance is ExtractionProvenance.OCR:
        # A caller that supplies an extraction without saying where it came
        # from does NOT get the benefit of the doubt: OCR is the default value,
        # so honouring it here would let a forgotten argument label supplied
        # facts as read-from-the-document — the one thing this column exists to
        # prevent.  Fall back to the gated, marked provenance instead, so the
        # mistake surfaces as a refusal on a default deployment rather than as
        # a quietly laundered value.
        provenance = ExtractionProvenance.CLIENT_FIXTURE
    if (fixture is not None and provenance is ExtractionProvenance.CLIENT_FIXTURE
            and not get_settings().allow_fixture_uploads):
        raise BlockingValidationError(
            "FIXTURE_UPLOADS_DISABLED",
            "This server does not accept supplied extraction values: a document's facts must "
            "come from its own OCR. Set EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS=true only on a test "
            "or demo deployment.", scope="DOCUMENT")
    # Nothing was supplied, so the row says what is true regardless of what the
    # caller asked for.
    if fixture is None:
        provenance = ExtractionProvenance.OCR
    # A byte-identical file in the SAME role box is never a legitimate second
    # document.  Several documents per role IS supported (a shipment may carry
    # several invoices), but every extra copy of the *same* file contributes
    # its rows again: 2N items and a doubled goods total, with nothing
    # downstream able to tell the copies apart.  Scoped to the role because a
    # combined "Invoice cum Packing List" PDF legitimately fills two boxes.
    twins = db.scalars(select(Document).where(
        Document.job_id == job.id, Document.declared_role == role.value,
        Document.sha256 == digest).order_by(Document.upload_index_within_role)).all()
    # A live twin wins over a failed one: a job stored before this gate existed
    # may hold both, and resetting the failed row there would add a SECOND
    # extraction of a file already extracted.
    live = next((d for d in twins if d.status != DocumentStatus.FAILED.value), None)
    if live is not None:
        raise BlockingValidationError(
            "DUPLICATE_DOCUMENT_UPLOAD",
            f"{filename!r} is byte-identical to {live.original_file_name!r}, already attached "
            f"to {role.value} (status {live.status}). Attaching it twice would declare its "
            f"rows, quantities and values twice. Remove the existing document first if you "
            f"meant to replace it.",
            scope="DOCUMENT", document_id=live.id)
    if twins:
        twin = twins[0]
        # Re-picking the same file after a failed extraction IS the retry the
        # UI offers (the box re-shows its file input on FAILED).  Reset the
        # existing row rather than adding a second one — the stored OCR
        # envelope survives, so the retry costs no re-OCR.
        twin.status = DocumentStatus.UPLOADED.value
        twin.warnings = []
        _stamp_provenance(db, job, twin, provenance, actor=actor)
        db.flush()
        _audit(db, job.id, "DOCUMENT_UPLOAD_RETRIED",
               f"{role.value} #{twin.upload_index_within_role} reset for re-extraction "
               f"(same file re-attached)", actor=actor)
        if fixture is not None:
            return run_extraction(db, twin, fixture, actor=actor)
        return twin

    idx = db.scalar(select(Document).where(Document.job_id == job.id, Document.declared_role == role.value)
                    .order_by(Document.upload_index_within_role.desc()))
    next_idx = (idx.upload_index_within_role + 1) if idx else 0

    # content_type is left to the column default ("application/pdf"): by here
    # `data` is always PDF bytes, whatever the upload called itself.
    doc = Document(job_id=job.id, declared_role=role.value, upload_index_within_role=next_idx,
                   original_file_name=filename, byte_size=len(data),
                   sha256=digest, status=DocumentStatus.UPLOADED.value)
    db.add(doc)
    _stamp_provenance(db, job, doc, provenance, actor=actor)
    db.flush()
    # A converted photo keeps its camera filename for display, but the stored
    # file IS a PDF — name it as one so the storage directory tells the truth.
    store_name = filename if not converted_pages or filename.lower().endswith(".pdf") \
        else filename + ".pdf"
    doc.storage_key = store_document(job.id, doc.id, store_name, data)
    note = (f" ({converted_pages} photo{'s' if converted_pages > 1 else ''} → "
            f"{converted_pages}-page PDF)") if converted_pages else ""
    _audit(db, job.id, "DOCUMENT_UPLOADED", f"{role.value} #{next_idx}{note}", actor=actor)
    if fixture is not None:
        return run_extraction(db, doc, fixture, actor=actor)
    return doc


def run_extraction(db: Session, doc: Document, fixture: dict | None = None, *,
                   actor: str = SYSTEM_ACTOR,
                   claim_from: tuple[str, ...] = (DocumentStatus.UPLOADED.value,
                                                  DocumentStatus.FAILED.value)) -> Document:
    """OCR + role-specific extraction for a stored document (also retries FAILED).

    ``claim_from`` is the set of statuses the claim below may take the document
    from.  The default is the interactive rule: an extraction starts from
    UPLOADED or FAILED, never over another run's claim.  The queue worker
    passes ``(EXTRACTING,)`` instead, because in queue mode the PRODUCER takes
    the claim (claim_for_queued_extraction) before the message is sent —
    EXTRACTING is the expected state at pickup, not a rival — and a redelivered
    message (its previous worker died mid-run without releasing the claim) must
    be able to take the document over: SQS re-delivers only when nothing is
    heartbeating the message, which is the liveness signal a status column
    alone cannot carry.

    FAILED is deliberately NOT in the worker's set.  A failed attempt deletes
    its own message, so the retry is the reviewer's Continue — a fresh claim
    and a fresh message — never an SQS redelivery racing the human who is
    being invited to press it.  See backend/worker.py's module docstring.
    """
    role = DeclaredRole(doc.declared_role)
    # Claim the document before the slow work.  The UPDATE is guarded on the
    # statuses an extraction may start from, which is what makes the claim
    # exclusive: the endpoint's own status check reads outside any
    # transaction, so a double-submit, a stale tab or a second browser each
    # passed it and started their own paid OCR + LLM round on the same file —
    # the slower one then overwrote the faster one's rows.  Exactly one of the
    # racing UPDATEs can match; the losers are refused here.
    claimed = db.execute(
        sql_update(Document)
        .where(Document.id == doc.id,
               Document.status.in_(claim_from))
        .values(status=DocumentStatus.EXTRACTING.value)).rowcount
    if not claimed:
        # Nothing was written, so there is nothing to undo.  Report the status
        # the DB actually holds: the in-memory one is the stale snapshot that
        # lost the race, which is the least useful value to print.
        current = db.scalar(select(Document.status).where(Document.id == doc.id))
        raise BlockingValidationError(
            "EXTRACTION_ALREADY_RUNNING",
            f"{doc.original_file_name!r} is already being extracted (status {current}) — "
            f"wait for the running extraction to finish rather than starting a second one. "
            f"Two runs on one document duplicate the OCR/LLM cost and the later result "
            f"silently overwrites the earlier one.",
            scope="DOCUMENT", document_id=doc.id)
    # Audit the start (in-flight work is otherwise invisible in the DB), then
    # commit before the slow OCR/LLM calls: extractions run concurrently and
    # SQLite allows a single writer, so the write lock must not span them.
    _audit(db, doc.job_id, "DOCUMENT_EXTRACTION_STARTED",
           f"{role.value} #{doc.upload_index_within_role}", actor=actor)
    db.commit()
    db.refresh(doc)                  # the guarded UPDATE deliberately bypassed the ORM
    started = time.monotonic()
    # Packing-list time budget (user rule 2026-07-17): extraction is aborted
    # once it elapses (not run to completion and then discarded). A deadline is
    # passed into the extractor, which stops launching new LLM calls / repair
    # rounds past it; the pipeline then uses the quantity-share allocation
    # fallback. The budget spans OCR + reasoning, so OCR eats into it.
    budget = get_settings().packing_extraction_budget_seconds
    packing_live = role == DeclaredRole.PACKING_LIST and fixture is None
    deadline = (started + budget) if packing_live else None
    try:
        if doc.ocr:
            # Retry after a failed extraction: the file is immutable (sha256),
            # so the stored OCR envelope is reused — no paid re-OCR.
            ocr = OcrDocument.model_validate(doc.ocr)
        else:
            # Fixture uploads (demo/tests) use offline OCR: deterministic, free,
            # and the fixtures' evidence quotes were built against pypdf text.
            provider = OfflineOcrProvider() if fixture is not None else get_ocr_provider()
            if fixture is None:
                # BEFORE the paid call, not after it.  Documents stored before
                # the upload gate existed still reach here, and this is the last
                # point at which refusing one costs nothing.
                enforce_page_ceiling(pdf_page_count(doc.storage_key),
                                     filename=doc.original_file_name or "")
            ocr = provider.run(document_id=doc.id, declared_role=role,
                               file_path=doc.storage_key, sha256=doc.sha256)
            doc.ocr = ocr.model_dump(mode="json")
            # Persist the paid OCR envelope immediately: if the LLM phase dies
            # or the process restarts, a retry reuses it instead of re-paying,
            # and a doc with OCR but no raw_extraction is visibly in flight.
            db.commit()
        result = extract_document(role, ocr, fixture, deadline=deadline)
        # Belt-and-suspenders: if extraction drained just past the budget
        # without the deadline firing (e.g. a single sub-budget call finishing
        # late), still flag it so allocation uses the fallback. extract_document
        # already stamps the marker on a hard abort — never duplicate it.
        if packing_live:
            note = packing_timing_note(time.monotonic() - started, budget, result.warnings)
            if note:
                result.warnings.append(note)
    except Exception as e:
        doc.status = DocumentStatus.FAILED.value
        reason = f"EXTRACTION_FAILED ({type(e).__name__}): {str(e)[:300]}"
        doc.warnings = [reason]
        _audit(db, doc.job_id, "DOCUMENT_EXTRACTION_FAILED",
               f"{role.value} #{doc.upload_index_within_role}", {"error": reason}, actor=actor)
        db.commit()
        raise
    doc.raw_extraction = result.payload_dict()
    doc.role_match = result.role_match
    doc.warnings = result.warnings + result.errors
    doc.status = (DocumentStatus.ROLE_REVIEW_REQUIRED.value if not result.role_match
                  else DocumentStatus.EXTRACTED.value)
    job = db.get(Job, doc.job_id)
    # New evidence supersedes everything derived from the OLD evidence.  Every
    # reviewer mutation already invalidates; extraction did not, so a document
    # extracted after finalize left the stored review, declaration and XML
    # artifact untouched and GET /jobs/{id}/xml kept serving the superseded
    # file at 200 — to a reviewer who very likely uploaded that document
    # BECAUSE the first result was wrong.
    #
    # The lock is taken here, briefly, at the END.  It deliberately does not
    # span the OCR/LLM calls: those commit mid-flight so SQLite's single writer
    # is not held across them, and the SPA extracts documents in parallel.  It
    # covers the one window that matters — this delete racing a concurrent
    # finalize writing the very artifact being deleted.
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        superseded = job.critical_review is not None or job.declaration is not None
        _invalidate_derived(db, job)
        _reset_extraction_derived_state(
            db, job, role.value,
            f"{role.value} #{doc.upload_index_within_role} "
            f"({doc.original_file_name!r}) was extracted")
        job.status = JobStatus.EXTRACTION_COMPLETE.value
        _audit(db, job.id, "DOCUMENT_EXTRACTED", f"{role.value} #{doc.upload_index_within_role}",
               {"warnings": result.warnings, "provider": result.provider}, actor=actor)
        if superseded:
            _audit(db, job.id, "DERIVED_STATE_INVALIDATED",
                   f"{role.value} #{doc.upload_index_within_role} extracted after the review / "
                   f"declaration was built; stored review, declaration and any XML discarded",
                   actor=actor)
        db.commit()                      # persist before the lock is released
    return doc


def recover_interrupted_extractions(db: Session) -> list[Document]:
    """Return documents left mid-extraction by a dead process to the queue.

    An ``EXTRACTING`` claim is released by the call that took it — unless that
    call never gets there: ``uvicorn --reload`` restarts the process on a file
    edit, Ctrl-C, a crash, a machine reboot.  The claim would then be
    permanent and the document unextractable ("already running" forever, with
    no running extraction).  Clearing it at startup is safe because the
    deployment is a single process (see ``job_lock``): if this process is
    starting, none of our extractions are in flight.

    Back to UPLOADED rather than FAILED, because nothing failed and nothing is
    lost — the OCR envelope was committed as soon as it was paid for, so
    Continue resumes with the LLM step only.  The note says so, since the
    reviewer is otherwise looking at a document that quietly went backwards.
    """
    stale = list(db.scalars(select(Document).where(
        Document.status == DocumentStatus.EXTRACTING.value)))
    for doc in stale:
        doc.status = DocumentStatus.UPLOADED.value
        doc.warnings = [
            "EXTRACTION_INTERRUPTED: the server restarted while this document was being "
            "extracted, so the extraction did not finish. Nothing was lost — press Continue "
            "to run it again" + (" (the stored OCR is reused, no re-scan)" if doc.ocr else "") + "."]
        _audit(db, doc.job_id, "DOCUMENT_EXTRACTION_INTERRUPTED",
               f"{doc.declared_role} #{doc.upload_index_within_role} returned to the queue "
               f"after a server restart")
    if stale:
        db.commit()
    return stale


def claim_for_queued_extraction(db: Session, doc: Document, *, actor: str) -> str:
    """Producer half of queue mode: take the extraction claim WITHOUT running.

    Same guarded UPDATE as run_extraction — the claim is the deduplication.
    Claiming before the send means a double-click, a stale tab or a retried
    request cannot put a second message on the queue for the same document:
    the second claim finds no UPLOADED/FAILED row to match and is refused
    here, so at most one message per claim ever exists.  It also means the
    SPA's very next poll shows EXTRACTING, which its existing claim-watcher
    already knows how to wait on — enqueueing needs no frontend change.

    Returns the status the claim was taken from, so a failed send can put the
    document back exactly where it was (release_queued_claim).
    """
    prior = db.scalar(select(Document.status).where(Document.id == doc.id))
    claimed = db.execute(
        sql_update(Document)
        .where(Document.id == doc.id,
               Document.status.in_((DocumentStatus.UPLOADED.value,
                                    DocumentStatus.FAILED.value)))
        .values(status=DocumentStatus.EXTRACTING.value)).rowcount
    if not claimed:
        current = db.scalar(select(Document.status).where(Document.id == doc.id))
        raise BlockingValidationError(
            "EXTRACTION_ALREADY_RUNNING",
            f"{doc.original_file_name!r} is already queued or being extracted "
            f"(status {current}) — wait for that run to finish rather than starting a "
            f"second one. Two runs on one document duplicate the OCR/LLM cost and the "
            f"later result silently overwrites the earlier one.",
            scope="DOCUMENT", document_id=doc.id)
    _audit(db, doc.job_id, "DOCUMENT_EXTRACTION_QUEUED",
           f"{doc.declared_role} #{doc.upload_index_within_role}", actor=actor)
    # Commit before the network send: the claim must be visible to every other
    # producer (and to the worker, which may receive the message milliseconds
    # after send_message returns) — an uncommitted claim deduplicates nothing.
    db.commit()
    db.refresh(doc)
    # The pre-read races the claim only in the harmless direction: if the
    # status changed between the two statements the claim still only matched
    # UPLOADED/FAILED, and reverting to either leaves a re-claimable document.
    return prior if prior in (DocumentStatus.UPLOADED.value,
                              DocumentStatus.FAILED.value) else DocumentStatus.UPLOADED.value


def release_queued_claim(db: Session, doc: Document, prior_status: str, *,
                         error: str, actor: str) -> None:
    """The send failed after the claim — put the document back, visibly.

    Without this a refused send_message leaves the document EXTRACTING
    forever: no message exists, so no worker will ever release it, and queue
    mode disables the startup sweep that used to clear orphaned claims.
    """
    doc.status = prior_status
    reason = f"QUEUE_SEND_FAILED: {error[:300]} — press Continue to retry."
    doc.warnings = [reason]
    _audit(db, doc.job_id, "DOCUMENT_EXTRACTION_QUEUE_FAILED",
           f"{doc.declared_role} #{doc.upload_index_within_role}",
           {"error": reason}, actor=actor)
    db.commit()


def declarable_documents(job: Job) -> list[Document]:
    """The documents the declaration is allowed to read.

    A reviewer-REJECTED document keeps its evidence (uploads and OCR are
    immutable) but contributes nothing: its rows, totals and party fields must
    be invisible to every engine, not merely unused by one of them.  Filtered
    here, once, so no caller can forget.
    """
    return [d for d in job.documents if d.status != DocumentStatus.ROLE_REJECTED.value]


def _require_extracted(job: Job) -> None:
    """Actionable error when documents are not ready to be declared."""
    # An extraction still in flight has to block too.  It carries no
    # raw_extraction yet, so letting the review through would declare the
    # shipment as if that document did not exist — and the reviewer would have
    # no reason to doubt a review that computed without complaint.
    running = sorted({d.declared_role for d in job.documents
                      if d.status == DocumentStatus.EXTRACTING.value})
    if running:
        raise BlockingValidationError(
            "EXTRACTION_IN_PROGRESS",
            f"{', '.join(running)} still extracting — the review would be computed without "
            f"its rows, weights and parties. Wait for it to finish.")
    pending = sorted({d.declared_role for d in job.documents
                      if d.status in (DocumentStatus.UPLOADED.value, DocumentStatus.FAILED.value)})
    if pending:
        raise BlockingValidationError(
            "DOCUMENTS_NOT_EXTRACTED",
            f"{', '.join(pending)} uploaded but not extracted — run extraction "
            f"(Continue button / POST .../documents/{{doc_id}}/extract) first.")
    # A role mismatch used to be a coloured pill and nothing else: the status
    # gated no endpoint, and `_group_raw` reads any document that carries a
    # raw_extraction.  So a packing list dropped into the INVOICE box became
    # the goods roster — its rows priced, its totals declared, its parties on
    # the SAD — and the only sign was a badge on the upload card.  The
    # extractor's own verdict now has to be answered before it can be believed.
    unconfirmed = sorted(
        f"{d.declared_role} #{d.upload_index_within_role} ({d.original_file_name!r})"
        for d in job.documents if d.status == DocumentStatus.ROLE_REVIEW_REQUIRED.value)
    if unconfirmed:
        raise BlockingValidationError(
            "DOCUMENT_ROLE_UNCONFIRMED",
            f"{len(unconfirmed)} document(s) do not look like the role they were uploaded "
            f"as: {'; '.join(unconfirmed)}. Confirm the role is right (the document is then "
            f"used as declared) or reject it (it is excluded from the declaration) before "
            f"continuing.")


def resolve_document_role(db: Session, job: Job, doc: Document, *, accept: bool,
                          reason: str = "", actor: str = SYSTEM_ACTOR) -> dict:
    """Answer the extractor's role-mismatch verdict for one document.

    ``accept`` keeps the document in the role its upload box declared — the
    reviewer has looked at it and the extractor was wrong.  Rejecting excludes
    it from the declaration without deleting anything.  Either way the answer
    changes what the declaration is built from, so everything derived is
    invalidated and the reviewer goes back through Critical Review.
    """
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        if doc.status != DocumentStatus.ROLE_REVIEW_REQUIRED.value:
            raise BlockingValidationError(
                "DOCUMENT_ROLE_NOT_IN_REVIEW",
                f"{doc.original_file_name!r} is not awaiting a role decision "
                f"(status {doc.status}).",
                scope="DOCUMENT", document_id=doc.id)
        doc.status = (DocumentStatus.EXTRACTED.value if accept
                      else DocumentStatus.ROLE_REJECTED.value)
        _invalidate_derived(db, job)
        _reset_extraction_derived_state(
            db, job, doc.declared_role,
            f"{doc.original_file_name!r} was {'confirmed in' if accept else 'rejected from'} "
            f"role {doc.declared_role}")
        job.status = JobStatus.EXTRACTION_COMPLETE.value
        verb = "confirmed in" if accept else "rejected from"
        db.add(AuditEvent(
            job_id=job.id, actor=actor,
            event_code="DOCUMENT_ROLE_CONFIRMED" if accept else "DOCUMENT_ROLE_REJECTED",
            detail=f"{doc.original_file_name!r} {verb} role {doc.declared_role} "
                   f"despite the extractor reporting a role mismatch"
                   + (f": {reason}" if reason else ""),
            payload={"document_id": doc.id, "role": doc.declared_role,
                     "file": doc.original_file_name, "accepted": accept,
                     "reason": reason or ""}))
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "document_id": doc.id, "role": doc.declared_role,
                "document_status": doc.status, "accepted": accept}


def remove_document(db: Session, job: Job, doc: Document, *, actor: str = SYSTEM_ACTOR) -> dict:
    """Detach an uploaded document from the job — the "remove the existing
    document first if you meant to replace it" that the duplicate-upload
    refusal instructs, which until now had no endpoint to point at.

    Removal is structural: rows, totals and parties the document contributed
    disappear from the declaration, so everything derived is invalidated the
    same way a role decision does it.  The stored file is deleted best-effort;
    the audit trail records the removal.
    """
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        if doc.status == DocumentStatus.EXTRACTING.value:
            raise BlockingValidationError(
                "DOCUMENT_EXTRACTION_RUNNING",
                f"{doc.original_file_name!r} is being extracted right now — wait for the "
                f"run to finish before removing it.",
                scope="DOCUMENT", document_id=doc.id)
        storage_key = doc.storage_key
        detail = (f"{doc.original_file_name!r} removed from {doc.declared_role} "
                  f"#{doc.upload_index_within_role} (was {doc.status})")
        payload = {"document_id": doc.id, "role": doc.declared_role,
                   "file": doc.original_file_name, "status_at_removal": doc.status}
        db.delete(doc)
        db.flush()
        _invalidate_derived(db, job)
        _reset_extraction_derived_state(
            db, job, payload["role"],
            f"{payload['file']!r} was removed from {payload['role']}")
        remaining = [d for d in job.documents if d.id != payload["document_id"]]
        job.status = (JobStatus.EXTRACTION_COMPLETE.value
                      if any(d.status == DocumentStatus.EXTRACTED.value for d in remaining)
                      else JobStatus.UPLOADING.value)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="DOCUMENT_REMOVED",
                          detail=detail, payload=payload))
        db.commit()                      # persist before the lock is released
        delete_stored_document(storage_key)
        return {"status": "ok", "document_id": payload["document_id"],
                "role": payload["role"], "job_status": job.status}


def critical_review(db: Session, job: Job, *, actor: str = SYSTEM_ACTOR) -> dict:
    _require_extracted(job)
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        docs = declarable_documents(job)
        ctx = resolve_context(docs, _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        review = to_critical_review(ctx, docs, review_selections=job.review_selections)
        job.critical_review = review.model_dump(mode="json")
        job.status = JobStatus.CRITICAL_REVIEW_REQUIRED.value
        _audit(db, job.id, "CRITICAL_REVIEW_BUILT", "declaration control values computed",
               {"fingerprint": review.review_fingerprint}, actor=actor)
        db.commit()                      # persist before the lock is released
        return job.critical_review


# Legacy request keys still accepted from older clients.
_LEGACY_BODY_KEYS = {"insurance_national": "manual_insurance_amount",
                     "freight_override": "manual_freight_amount"}


def finalize_job(db: Session, job: Job, body: dict, *, actor: str = SYSTEM_ACTOR) -> dict:
    _require_extracted(job)
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        result = _finalize_job_locked(db, job, body, actor=actor)
        db.commit()                      # persist before the lock is released
        return result


def _finalize_job_locked(db: Session, job: Job, body: dict, *, actor: str = SYSTEM_ACTOR) -> dict:
    rate_before = job.exchange_rate or str(get_settings().default_exchange_rate)
    rate = _rate(job, _validated_rate_override(body.get("exchange_rate")))
    ctx = resolve_context(declarable_documents(job), rate, item_mutations=job.item_mutations,
                          hs_history=job.hs_history)
    review = to_critical_review(ctx, declarable_documents(job),
                                review_selections=job.review_selections)

    payload = dict(body or {})
    for old, new in _LEGACY_BODY_KEYS.items():
        if payload.get(old) not in (None, "") and payload.get(new) in (None, ""):
            payload[new] = payload[old]
    conf = CriticalReviewConfirmation.model_validate(payload)

    # Checked AFTER the legacy-alias fold above, so `insurance_national` and
    # `freight_override` go through the same gate as the current names.
    _validated_cost_override(conf.manual_freight_amount, code="FREIGHT_AMOUNT_INVALID",
                             field="manual_freight_amount", label="Freight amount")
    _validated_cost_override(conf.manual_insurance_amount, code="INSURANCE_AMOUNT_INVALID",
                             field="manual_insurance_amount", label="Insurance amount")

    # SN-keyed hs_overrides are only safe against the exact review the client
    # saw — sequences shift on add/delete, so without the fingerprint lock an
    # override could land on the WRONG item and reach XML unreviewed.
    if payload.get("hs_overrides") and not conf.review_fingerprint:
        raise BlockingValidationError(
            "HS_OVERRIDES_REQUIRE_FINGERPRINT",
            "hs_overrides are sequence-keyed and require the review_fingerprint of "
            "the review they were entered against — re-run the critical review and "
            "resubmit, or use the item_id-keyed /items/hs-review channel.")

    # Stale-review detection: the client locked a fingerprint; if the evidence
    # or rules changed since, force the declaration back through review.
    if conf.review_fingerprint and conf.review_fingerprint != review.review_fingerprint:
        job.critical_review = review.model_dump(mode="json")
        job.status = JobStatus.CRITICAL_REVIEW_REQUIRED.value
        _audit(db, job.id, "REVIEW_STALE", "review fingerprint changed; re-review required", actor=actor)
        return {"status": "REVIEW_STALE",
                "message": "The reviewed evidence changed since this review was computed — "
                           "re-run Critical Review before finalizing.",
                "critical_review": job.critical_review}

    reviewed, mismatch = merge_confirmation(review, conf)
    if mismatch:
        job.status = JobStatus.EXTRACTION_RUNNING.value
        _audit(db, job.id, "ITEM_COUNT_MISMATCH", "second invoice pass required", actor=actor)
        return {"status": "ITEM_COUNT_MISMATCH",
                "message": "Reported item count differs; re-run invoice extraction only.",
                "expected": review.invoice_item_count}

    decl = pipeline_finalize(ctx, reviewed, hs_overrides=body.get("hs_overrides") or {})
    decl.job_id = job.id
    decl.warnings.extend(_rate_plausibility(rate, review.goods_currency))
    # Warn-mode (user rule 2026-07-18): blocking cases never stop the XML —
    # the reviewer tests it in real ASYCUDA and gets a pop-up listing every
    # unresolved case instead.  ready_for_xml keeps the honest validation
    # verdict; strict mode (EASYCUSTOMS_XML_STRICT_BLOCKING=1) restores blocks.
    # Weight-allocation impossibilities are never warn-mode-bypassable: with no
    # item weights assigned there is nothing to test in ASYCUDA, and the file
    # would declare a zero gross weight on every line.
    hard_blockers = [m for m in decl.blocking_errors if m.code in WARN_MODE_HARD_CODES]
    build_xml_now = decl.ready_for_xml or (
        not get_settings().xml_strict_blocking and bool(decl.items) and not hard_blockers)
    decl.xml_built_with_blockers = build_xml_now and not decl.ready_for_xml
    job.declaration = decl.model_dump(mode="json")
    # The rate that produced these numbers now lives ON THE JOB, not only in a
    # request body that is never stored.  Every national value and the duty
    # derive from it, so a filed declaration has to be able to answer "at what
    # rate?" long after the request is gone.  Written here, next to the
    # declaration it belongs to, so the two can never disagree.
    job.exchange_rate = str(rate)
    _audit(db, job.id, "CRITICAL_REVIEW_LOCKED", "reviewed values applied to declaration", actor=actor, payload={
        "fingerprint": review.review_fingerprint,
        "exchange_rate": str(rate),
        "item_count": review.invoice_item_count,
        "invoice_roster": [e.model_dump(mode="json") for e in review.invoice_roster],
        "shipment_authority": review.shipment_authority_type,
        "gross_weight_source": review.gross_weight_source_doc,
        "package_count_source": review.package_count_source_doc,
        "package_type": reviewed.package_type_code,
        "mixed_source_reason": reviewed.mixed_source_reason,
        "overrides": _overrides(review, conf, reviewed, rate=rate, rate_before=rate_before),
    })

    # Vendor field profile: a finalized job is confirmed knowledge — remember
    # this exporter's COO pattern so the NEXT job from the same vendor proposes
    # it instead of the exporter-country guess.  Reviewer corrections (a bulk
    # COO stamp or per-item COO edits) count as confirmed; profile- and
    # fallback-sourced values are excluded inside the recorder so the store can
    # never learn from its own output.  Gated on LIVE OCR evidence: fixture /
    # demo uploads carry the offline OCR envelope, and letting them teach the
    # store filled it with the demo vendor on every test run — the same reason
    # layout memory only ever learns from real parser runs.  Never blocks
    # finalize.
    try:
        invoice_live = any(
            d.declared_role == DeclaredRole.INVOICE.value
            and ((d.ocr or {}).get("ocr_provider") or "offline") != "offline"
            for d in declarable_documents(job))
        if invoice_live:
            overlay = itemmut.overlay_of(job.item_mutations)
            reviewer_confirmed = bool(overlay.get("coo_all")) or any(
                (fe or {}).get("country_of_origin")
                for fe in (overlay.get("field_edits") or {}).values())
            field_profiles.record_coo_observation(
                ctx.inv.exporter_name,
                [(it.coo_alpha2, it.coo_source) for it in ctx.items],
                reviewer_confirmed=reviewer_confirmed)
    except Exception as exc:                           # pragma: no cover - defensive
        _audit(db, job.id, "FIELD_PROFILE_RECORD_FAILED", f"{type(exc).__name__}: {exc}")

    if build_xml_now:
        xml_bytes = build_xml(decl)
        art = XmlArtifact(job_id=job.id, declaration_version=decl.version, template_version="asycuda-np-sad-v1",
                          checksum=checksum(xml_bytes), xml_bytes=xml_bytes)
        db.add(art)
        # Sibling brand/model/size workbook — built from the SAME declaration,
        # right after the XML, so it can never drift from it (export-only; a
        # build failure must never sink the XML the reviewer needs).
        try:
            xls_bytes = build_bms_xls(decl.items)
            db.add(BmsArtifact(job_id=job.id, declaration_version=decl.version,
                               template_version=BMS_TEMPLATE_VERSION,
                               checksum=bms_checksum(xls_bytes), xls_bytes=xls_bytes))
        except Exception as exc:                       # pragma: no cover - defensive
            _audit(db, job.id, "BMS_BUILD_FAILED", f"{type(exc).__name__}: {exc}")
        if decl.ready_for_xml:
            job.status = JobStatus.XML_READY.value
            _audit(db, job.id, "XML_BUILT", f"{len(decl.items)} items", {"checksum": art.checksum}, actor=actor)
        else:
            # honest status + audit trail: the XML exists but carries known
            # unresolved cases — ASYCUDA-side testing is at the reviewer's risk
            job.status = JobStatus.VALIDATION_BLOCKED.value
            _audit(db, job.id, "XML_BUILT_WITH_BLOCKERS",
                   f"{len(decl.items)} items; {len(decl.blocking_errors)} blocking case(s) "
                   "unresolved — XML generated for ASYCUDA testing",
                   {"checksum": art.checksum,
                    "blocking_codes": sorted({m.code for m in decl.blocking_errors})},
                   actor=actor)
    else:
        job.status = JobStatus.VALIDATION_BLOCKED.value
        _audit(db, job.id, "VALIDATION_BLOCKED", f"{len(decl.blocking_errors)} blocking errors", actor=actor)
    return job.declaration


def _overrides(review, conf, reviewed=None, rate=None, rate_before=None) -> dict:
    """Reviewer entries that differ from the computed review defaults — the
    audit trail's record of every human decision."""
    defaults = {
        "confirmed_gross_weight": review.gross_weight,
        "confirmed_total_packages": review.total_packages,
        "reviewed_packing_weight_unit": review.reviewed_packing_weight_unit,
        "manifest_no": review.manifest_no,
        "package_type": review.package_type,
        "hawb_no": review.hawb_no, "mawb_no": review.mawb_no,
        "bill_of_lading_no": review.bill_of_lading_no,
        "bill_of_lading_date": review.bill_of_lading_date,
        "transport_doc_type": review.transport_doc_type,
        "field_18_transport_identity": review.field_18_transport_identity,
        "field_21_transport_identity": review.field_21_transport_identity,
        "field_40_previous_document": review.field_40_previous_document,
        "exporter_name": review.exporter.name, "exporter_address": review.exporter.address,
        "exporter_exim_code": review.exporter.exim_code,
        "exporter_country_code": review.exporter.country_code,
        "importer_name": review.importer.name, "importer_address": review.importer.address,
        "importer_exim_code": review.importer.exim_code,
        "importer_country_code": review.importer.country_code,
        "incoterm": review.incoterm, "delivery_place": review.delivery_place,
        "bank_code": review.bank_code, "bank_name": review.bank_name,
        "swift_code": review.swift_code,
        "payment_term_code": review.payment_term_code,
        "mode_of_payment": review.mode_of_payment,
        "bank_reference": review.bank_reference, "bank_amount": review.bank_amount,
        "bank_currency": review.bank_currency, "bank_date": review.bank_date,
        "manual_freight_amount": review.manual_freight_amount,
        "manual_insurance_amount": review.manual_insurance_amount,
        # regime & transport (per-job selections, 2026-08-01)
        "declaration_type": review.declaration_type,
        "gen_procedure_code": review.gen_procedure_code,
        "customs_office_code": review.customs_office_code,
        "customs_office_name": review.customs_office_name,
        "border_office_code": review.border_office_code,
        "border_office_name": review.border_office_name,
        "extended_customs_procedure": review.extended_customs_procedure,
        "national_customs_procedure": review.national_customs_procedure,
        "border_mode": review.border_mode,
        "inland_mode_of_transport": review.inland_mode_of_transport,
        "border_nationality": review.border_nationality,
        "place_of_loading_code": review.place_of_loading_code,
        "location_of_goods": review.location_of_goods,
        "container_flag": review.container_flag,
    }
    out = {}
    for key, default in defaults.items():
        supplied = getattr(conf, key, None)
        if supplied is not None and str(supplied).strip() != str(default).strip():
            out[key] = {"from": default, "to": str(supplied).strip()}
    # Field 9 is recorded from the ACTUAL emitted value: only when the override
    # really took effect (a blank-nullified override falls back to
    # recomposition and must not be logged as a reviewer change).
    if reviewed is not None and getattr(reviewed, "field_9_override", False):
        auto = review.field_9_invoice_transport_document
        if reviewed.field_9_text.strip() != (auto or "").strip():
            out["field_9_text"] = {"from": auto, "to": reviewed.field_9_text}
    # The exchange rate is NOT a CriticalReviewConfirmation field — it is read
    # straight from the untyped finalize body — so the getattr loop above can
    # never see it, and a reviewer changing the rate that sets every national
    # value left no trace at all.
    if rate is not None and rate_before is not None and str(rate) != str(rate_before):
        out["exchange_rate"] = {"from": str(rate_before), "to": str(rate)}
    return out


# --------------------------------------------------------------------------- #
# Reviewer item mutations (Detailed Review add / delete)
# --------------------------------------------------------------------------- #
# States in which the item list may be edited: in review, review-blocked
# ("ready" with blockers) or XML already generated (mutation invalidates it).
_ITEM_MUTABLE_STATES = {JobStatus.CRITICAL_REVIEW_REQUIRED.value,
                        JobStatus.VALIDATION_BLOCKED.value,
                        JobStatus.XML_READY.value}


def _require_item_mutable(job: Job) -> None:
    if job.status not in _ITEM_MUTABLE_STATES:
        raise itemmut.ItemMutationError(
            409, "JOB_STATE_NOT_REVIEWABLE",
            f"Items can only be added or deleted while the job is in review / ready / "
            f"XML state (current: {job.status}). Compute the critical review first.")
    _require_extracted(job)


def _invalidate_derived(db: Session, job: Job) -> None:
    """A structural mutation stales everything downstream: stored review,
    declaration and any generated XML artifact (evidence is untouched)."""
    job.critical_review = None
    job.declaration = None
    db.execute(sql_delete(XmlArtifact).where(XmlArtifact.job_id == job.id))
    db.execute(sql_delete(BmsArtifact).where(BmsArtifact.job_id == job.id))


def _reset_extraction_derived_state(db: Session, job: Job, role_value: str,
                                    cause: str) -> None:
    """Discard reviewer overlay state that the changed evidence supersedes
    (user rule 2026-08-02): Critical/Detailed Review recompute FRESH after any
    document (re)extraction, role decision or removal — prior reviewer values
    (COO especially) are never blindly re-applied to evidence they were not
    made against.  Two sanctioned survivors: regime/office selections
    (``Job.review_selections``, untouched here) and explicit HS selections,
    which fold into content-keyed ``Job.hs_history`` and re-propose through
    the resolver's HISTORY cascade for re-confirmation.  Caller holds the job
    lock and commits."""
    overlay, hs_fold, discarded = itemmut.reset_for_evidence_change(
        job.item_mutations, role_value, cause)
    if overlay is None:
        return
    if hs_fold:
        job.hs_history = {**(job.hs_history or {}), **hs_fold}
    job.item_mutations = overlay
    _audit(db, job.id, "REVIEW_STATE_RESET",
           f"{cause}: reviewer entries made against the previous evidence were set aside "
           f"({', '.join(sorted(discarded))}); the review recomputes fresh. "
           f"{len(hs_fold)} HS selection(s) folded into item-name history.",
           {"role": role_value, "cause": cause, "discarded": discarded,
            "hs_history_folded": sorted(hs_fold), "revision": overlay["revision"]})


def _rebuild_bms_artifact(db: Session, job: Job, items) -> bool:
    """Refresh the brand/model/size ``.xls`` (and the stored declaration's
    export-only columns) in place after a reviewer edit.

    No-op when nothing has been built yet — finalize will produce the workbook.
    The XML artifact and the declaration's customs values are never touched.
    """
    existing = db.scalar(select(BmsArtifact).where(BmsArtifact.job_id == job.id)
                         .order_by(BmsArtifact.created_at.desc()))
    if existing is None:
        return False
    # keep the stored declaration's export columns in step (matched by sequence)
    decl_json = job.declaration
    if decl_json and isinstance(decl_json.get("items"), list):
        by_seq = {it.xml_item_sequence: it for it in items}
        rows = []
        for row in decl_json["items"]:
            it = by_seq.get(row.get("xml_item_sequence"))
            rows.append({**row, "brand": it.brand, "model": it.model, "size": it.size}
                        if it is not None else row)
        job.declaration = {**decl_json, "items": rows}
    xls_bytes = build_bms_xls(items)
    db.execute(sql_delete(BmsArtifact).where(BmsArtifact.job_id == job.id))
    db.add(BmsArtifact(job_id=job.id, declaration_version=existing.declaration_version,
                       template_version=BMS_TEMPLATE_VERSION,
                       checksum=bms_checksum(xls_bytes), xls_bytes=xls_bytes))
    return True


def edit_item_bms(db: Session, job: Job, body: dict, actor: str = SYSTEM_ACTOR) -> dict:
    """Reviewer BRAND / MODEL / SIZE edits (export-only columns).

    Unlike every other item mutation this does NOT invalidate anything: the
    three values never reach the customs XML, so the declaration, the XML
    artifact and the job status all survive.  The override is stored, the review
    preview is refreshed, and only the .xls is rebuilt (user rule 2026-07-21).
    """
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_extracted(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.edit_bms_fields(
            job.item_mutations, ctx.items, edits=list(body.get("edits") or []))
        job.item_mutations = overlay
        # recompute with the overrides applied — status/declaration untouched
        ctx2 = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        review = to_critical_review(ctx2, declarable_documents(job),
                                    review_selections=job.review_selections)
        job.critical_review = review.model_dump(mode="json")
        rebuilt = _rebuild_bms_artifact(db, job, ctx2.items)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_BMS_EDITED",
                          detail=f"{event['edited_items']} item(s) brand/model/size edited"
                                 f"{' — .xls rebuilt' if rebuilt else ''}",
                          payload=event))
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "edited_items": event["edited_items"],
                "xls_rebuilt": rebuilt, "critical_review": job.critical_review}


def _recompute_after_mutation(db: Session, job: Job) -> dict:
    ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
    review = to_critical_review(ctx, declarable_documents(job),
                                review_selections=job.review_selections)
    job.critical_review = review.model_dump(mode="json")
    job.status = JobStatus.CRITICAL_REVIEW_REQUIRED.value
    return job.critical_review


def add_job_item(db: Session, job: Job, body: dict, actor: str = SYSTEM_ACTOR) -> dict:
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.add_item(
            job.item_mutations, ctx.items, ctx.inv, declarable_documents(job),
            insertion_sn=int(body.get("insertion_sn") or 0),
            invoice_id=str(body.get("invoice_id") or ""),
            manual_review_addition=bool(body.get("manual_review_addition")),
            seed=dict(body.get("item") or {}),
            reason=str(body.get("reason") or ""))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_ADDED",
                          detail=f"item {event['item_id']} inserted at SN {event['new_sn']} "
                                 f"(revision {overlay['revision']})",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "added_item_id": event["item_id"],
                "inserted_sn": event["new_sn"], "revision": overlay["revision"],
                "critical_review": review}


def review_item_hs(db: Session, job: Job, body: dict, actor: str = SYSTEM_ACTOR) -> dict:
    """Detailed-Review HS selection (search pick or low-confidence confirm).
    Validated against the official DB at write time, stored in the overlay by
    immutable item_id, then everything derived (supplementary, review,
    declaration, XML) is invalidated and recomputed deterministically."""
    from .reference.store import get_reference

    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.select_hs(
            job.item_mutations, ctx.items, get_reference(),
            item_id=str(body.get("item_id") or ""),
            final_hs_code=str(body.get("final_hs_code") or ""),
            hs_review_source=str(body.get("hs_review_source") or ""))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_HS_REVIEWED",
                          detail=f"item {event['item_id']} (SN {event['sn']}) reviewed HS "
                                 f"{event['final_hs_code']} via {event['hs_review_source']}",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "item_id": event["item_id"],
                "final_hs_code": event["final_hs_code"],
                "revision": overlay["revision"], "critical_review": review}


def review_item_hs_range(db: Session, job: Job, body: dict, actor: str = SYSTEM_ACTOR) -> dict:
    """Bulk Detailed-Review HS apply: one DB-validated HS code stamped on every
    item in a printer-style SN range ("1-15, 19, 80" or "all").  Same validation
    and recompute path as the per-row selection — this is an additional facility,
    not a replacement; a later per-row pick still overrides any one row."""
    from .reference.store import get_reference

    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.select_hs_range(
            job.item_mutations, ctx.items, get_reference(),
            final_hs_code=str(body.get("final_hs_code") or ""),
            hs_review_source=str(body.get("hs_review_source") or ""),
            sn_range=str(body.get("sn_range") or ""))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_HS_REVIEWED_RANGE",
                          detail=f"HS {event['final_hs_code']} applied to SN range "
                                 f"{event['sn_range']!r} ({len(event['item_ids'])} items) "
                                 f"via {event['hs_review_source']}",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "final_hs_code": event["final_hs_code"],
                "applied_sns": event["sns"], "applied_count": len(event["item_ids"]),
                "revision": overlay["revision"], "critical_review": review}


def edit_job_item(db: Session, job: Job, item_id: str, body: dict,
                  actor: str = SYSTEM_ACTOR) -> dict:
    """Reviewer edits of an item's invoice fields (description / COO / qty /
    UOM / total price) — stored by immutable item_id, every derived value
    (HS/COO resolution, allocation, supplementary, totals, XML) recomputed."""
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.edit_item_fields(
            job.item_mutations, ctx.items, item_id=item_id,
            fields=dict(body.get("fields") or {}))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_FIELDS_EDITED",
                          detail=f"item {item_id} (SN {event['sn']}) fields edited: "
                                 f"{', '.join(sorted(event['changed']))} "
                                 f"(revision {overlay['revision']})",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "item_id": item_id, "edited": event["changed"],
                "revision": overlay["revision"], "critical_review": review}


def set_all_item_coo(db: Session, job: Job, body: dict, actor: str = SYSTEM_ACTOR) -> dict:
    """Bulk-apply one reviewer COO to every item (DB-validated), then recompute."""
    from .reference.store import get_reference

    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        overlay, event = itemmut.set_all_coo(
            job.item_mutations, get_reference(),
            country_of_origin=str(body.get("country_of_origin") or ""))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_COO_APPLIED_ALL",
                          detail=f"COO {event['country_of_origin']} applied to all items "
                                 f"(revision {overlay['revision']})",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "country_of_origin": event["country_of_origin"],
                "revision": overlay["revision"], "critical_review": review}


def review_shipment_totals(db: Session, job: Job, body: dict,
                           actor: str = SYSTEM_ACTOR) -> dict:
    """Reviewer-corrected gross weight / cartons become the durable shipment
    authority: stored in the overlay, every recompute reconciles item sums
    exactly to them, and stale declaration/XML are invalidated."""
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        overlay, event = itemmut.set_shipment_totals(
            job.item_mutations,
            gross_weight=body.get("gross_weight"),
            weight_unit=str(body.get("weight_unit") or "KGM"),
            total_packages=body.get("total_packages"))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="SHIPMENT_TOTALS_REVIEWED",
                          detail=f"reviewer set shipment authority to {event['gross_weight']} "
                                 f"{event['weight_unit']} / {event['total_packages']} pkgs "
                                 f"(revision {overlay['revision']})",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "gross_weight": event["gross_weight"],
                "weight_unit": event["weight_unit"],
                "total_packages": event["total_packages"],
                "revision": overlay["revision"], "critical_review": review}


# Reviewer-selectable regime & transport fields (2026-08-01) and their
# write-time validators.  Every code is gated against the reference at write
# time; the pair/cascade rules are re-checked as finalize blockers so an old
# stored selection can never bypass them.
_REGIME_FIELDS = frozenset({
    "declaration_type", "gen_procedure_code", "customs_office_code",
    "border_office_code", "extended_customs_procedure", "national_customs_procedure",
    "border_mode", "inland_mode_of_transport", "border_nationality",
    "place_of_loading_code", "location_of_goods", "container_flag",
})


def review_regime_selections(db: Session, job: Job, body: dict,
                             actor: str = SYSTEM_ACTOR) -> dict:
    """Durable per-job regime/office/transport selections (Boxes 1, 25/26, 27,
    30, 37, A).  Stored on the job — they survive reloads and recomputes and
    seed every future Critical Review; the finalize confirmation can still
    override any of them one-shot.  ``null`` reverts a field to the deployment
    default.  Bumps a revision that stales any in-flight review fingerprint."""
    from .reference.store import get_reference
    ref = get_reference()
    unknown = set(body or {}) - _REGIME_FIELDS
    if unknown:
        raise itemmut.ItemMutationError(
            422, "REGIME_FIELD_UNKNOWN",
            f"Unknown regime field(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(_REGIME_FIELDS))}.")

    def _clean(key, value):
        if key == "container_flag":
            if not isinstance(value, bool):
                raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                                "container_flag must be true or false.")
            return value
        v = str(value).strip()
        if key in ("declaration_type", "customs_office_code", "border_office_code",
                   "border_nationality", "place_of_loading_code"):
            v = v.upper()
        if key == "customs_office_code" and v not in ref.office_by_code:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"Customs office {v!r} is not in the NECAS office reference.")
        if key == "border_office_code" and v and v not in ref.office_by_code:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"Border office {v!r} is not in the NECAS office reference.")
        if key == "extended_customs_procedure" and v not in ref.extended_proc_by_code:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"Extended procedure {v!r} is not in ANNEX 1.")
        if key == "national_customs_procedure" and v not in ref.national_proc_by_code:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"National procedure {v!r} is not in ANNEX 3.")
        if key in ("border_mode", "inland_mode_of_transport") and v not in ref.transport_mode_by_code:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"Transport mode {v!r} is not in the 01–09 reference.")
        if key == "border_nationality" and v not in ref.valid_alpha2:
            raise itemmut.ItemMutationError(422, "REGIME_VALUE_INVALID",
                                            f"Nationality {v!r} is not a valid alpha-2 country code.")
        return v

    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_extracted(job)
        stored = dict(job.review_selections or {})
        values = dict(stored.get("values") or {})
        changed: dict[str, dict] = {}
        for key, value in (body or {}).items():
            before = values.get(key)
            if value is None:
                values.pop(key, None)
            else:
                values[key] = _clean(key, value)
            after = values.get(key)
            if before != after:
                changed[key] = {"from": before, "to": after}
        # Box 1 is picked as ONE line — when either half is posted, the stored
        # pair must be a valid declaration-model line.
        if ("declaration_type" in (body or {})) or ("gen_procedure_code" in (body or {})):
            typ = values.get("declaration_type") or get_settings().declaration_type
            gen = values.get("gen_procedure_code") or get_settings().declaration_gen_procedure_code
            if (typ, gen) not in ref.declaration_model_pairs:
                raise itemmut.ItemMutationError(
                    422, "REGIME_VALUE_INVALID",
                    f"{typ} {gen} is not one of the 17 Box-1 declaration-model lines.")
        if not changed:
            return {"status": "ok", "changed": {}, "revision": int(stored.get("revision") or 0),
                    "critical_review": job.critical_review}
        revision = int(stored.get("revision") or 0) + 1
        job.review_selections = {"revision": revision, "values": values}
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="REGIME_SELECTED",
                          detail=f"regime/transport selection changed: {', '.join(sorted(changed))} "
                                 f"(revision {revision})",
                          payload={"changed": changed, "revision": revision}))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "changed": changed, "revision": revision,
                "critical_review": review}


def delete_job_item(db: Session, job: Job, item_id: str, body: dict,
                    actor: str = SYSTEM_ACTOR) -> dict:
    with job_lock(db, job.id):
        _fresh_under_lock(db, job)
        _require_item_mutable(job)
        ctx = resolve_context(declarable_documents(job), _rate(job), item_mutations=job.item_mutations,
                              hs_history=job.hs_history)
        overlay, event = itemmut.delete_item(
            job.item_mutations, ctx.items,
            item_id=item_id,
            confirmation_sn=str(body.get("confirmation_sn") or ""),
            reason=str(body.get("reason") or ""))
        job.item_mutations = overlay
        _invalidate_derived(db, job)
        db.add(AuditEvent(job_id=job.id, actor=actor, event_code="ITEM_DELETED",
                          detail=f"item {item_id} deleted from SN {event['old_sn']} "
                                 f"(revision {overlay['revision']})",
                          payload=event))
        review = _recompute_after_mutation(db, job)
        db.commit()                      # persist before the lock is released
        return {"status": "ok", "deleted_item_id": item_id,
                "deleted_sn": event["old_sn"], "revision": overlay["revision"],
                "critical_review": review}


def list_jobs(db: Session, limit: int = 50, offset: int = 0, *, principal: str) -> dict:
    """Newest-first job listing for the dashboard, scoped to the principal.

    Scoping this endpoint matters more than scoping any single job route: a
    UUID primary key is unguessable on its own, and this is the call that hands
    out the whole set of them. Unscoped, "you need to know the id" is not a
    control, it is one request.

    Only jobs that ever received an upload are listed: a job that was created
    but never given a document is not history, so it never appears (the SPA
    does not create the row until the first upload, and any empty row that
    exists anyway stays invisible here).  A job whose documents were all
    REMOVED still shows — it holds real work (audit trail, reviewer
    selections, HS history) and hiding it would strand that behind a
    remembered URL; the DOCUMENT_UPLOADED audit event is the durable proof an
    upload happened.  Summary fields come from the STORED critical review
    (None until the first review computes — the dashboard shows what is
    known, never recomputes).  The heavy JSON columns the summary does not
    read (declaration, item_mutations) are deliberately not loaded.
    """
    from sqlalchemy import and_, func, or_
    from sqlalchemy.orm import load_only, selectinload

    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    worked = or_(Job.documents.any(),
                 Job.events.any(AuditEvent.event_code == "DOCUMENT_UPLOADED"))
    if principal != SYSTEM_PRINCIPAL:
        # Mirrors job_visible_to, in SQL. The blank-owner arm is the same
        # pre-ownership allowance and must be removed at the same time.
        worked = and_(worked, or_(Job.owner_key == principal,
                                  Job.owner_key == "", Job.owner_key.is_(None)))
    total = int(db.scalar(select(func.count()).select_from(Job).where(worked)) or 0)
    jobs = db.scalars(
        select(Job)
        .options(load_only(Job.id, Job.status, Job.created_at, Job.updated_at,
                           Job.exchange_rate, Job.critical_review),
                 selectinload(Job.documents))
        .where(worked)
        # id as tiebreaker: rows written in the same clock tick otherwise have
        # no stable order, and pagination across ties duplicates/skips rows
        .order_by(Job.updated_at.desc(), Job.id.desc())
        .limit(limit).offset(offset)).all()
    ids = [j.id for j in jobs]
    with_xml = set(db.scalars(select(XmlArtifact.job_id)
                              .where(XmlArtifact.job_id.in_(ids)))) if ids else set()
    out = []
    for j in jobs:
        cr = j.critical_review or {}
        docs = [d for d in j.documents]
        out.append({
            "job_id": j.id,
            "status": j.status,
            "created_at": j.created_at.isoformat() if j.created_at else "",
            "updated_at": j.updated_at.isoformat() if j.updated_at else "",
            "documents": len(docs),
            "roles": sorted({d.declared_role for d in docs}),
            "invoice_numbers": cr.get("invoice_numbers") or [],
            "exporter_name": (cr.get("exporter") or {}).get("name") or "",
            "importer_name": (cr.get("importer") or {}).get("name") or "",
            "item_count": cr.get("invoice_item_count") or 0,
            "goods_total": cr.get("calculated_goods_total") or "",
            "currency": cr.get("goods_currency") or "",
            "customs_office_code": cr.get("customs_office_code") or "",
            "declaration_type": " ".join(x for x in (cr.get("declaration_type"),
                                                     cr.get("gen_procedure_code")) if x),
            "has_xml": j.id in with_xml,
            # Demo jobs sit in the real dashboard beside real ones (that is the
            # point — the demo shows the actual workspace), so the listing has
            # to say which is which.
            "is_demo": is_demo_job(j),
        })
    return {"jobs": out, "total": total}


def latest_xml(db: Session, job_id: str) -> XmlArtifact | None:
    return db.scalar(select(XmlArtifact).where(XmlArtifact.job_id == job_id)
                     .order_by(XmlArtifact.created_at.desc()))


def latest_bms(db: Session, job_id: str) -> BmsArtifact | None:
    return db.scalar(select(BmsArtifact).where(BmsArtifact.job_id == job_id)
                     .order_by(BmsArtifact.created_at.desc()))


def _rate(job: Job, override: Decimal | None = None) -> Decimal:
    # `is not None`, never truthiness: Decimal("0") is falsy, and a silent
    # fallback to the default is exactly how a reviewer's stated rate went
    # missing without anyone being told.  Overrides arrive already validated
    # by _validated_rate_override.
    if override is not None:
        return override
    return Decimal(job.exchange_rate) if job.exchange_rate else get_settings().default_exchange_rate


# An order of magnitude either side of the configured NRB rate.  Wide on
# purpose: it catches a shifted decimal point without second-guessing a
# genuine rate move.
_RATE_TYPO_FACTOR = Decimal("10")


def _validated_rate_override(raw) -> Decimal | None:
    """The reviewer's exchange rate, validated where it enters the system.

    /finalize takes an untyped body, so this value arrived unchecked and then
    decided every national-currency amount — and therefore the duty.  Returns
    None when nothing usable was supplied (keep the job's rate); refuses rather
    than letting an unusable value through.  Measured before this gate existed:

      "0"       silently DISCARDED -- the truthy string got past `_rate`, then
                resolve_context's `or` swallowed the falsy Decimal, so the
                declaration quietly used the default instead of the entry.
      "-145.76" applied verbatim: customs value -471244.09, ready_for_xml true.
      "1457.6"  applied verbatim: a 10x duty, indistinguishable on screen.
      "145,76"  raised InvalidOperation out of the endpoint as a 500.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None                          # explicit blank = keep the job's rate
    try:
        rate = Decimal(text)
    except InvalidOperation:
        raise BlockingValidationError(
            "EXCHANGE_RATE_INVALID",
            f"Exchange rate {text!r} is not a number. Enter the NRB rate as a plain "
            f"decimal such as 145.76 — no thousands separator, comma decimal mark or "
            f"currency symbol.",
            scope="XML_FIELD", field="exchange_rate")
    if not rate.is_finite() or rate <= 0:
        raise BlockingValidationError(
            "EXCHANGE_RATE_INVALID",
            f"Exchange rate {text!r} must be greater than zero — it multiplies every "
            f"national-currency value on the declaration, so a zero or negative rate "
            f"declares a zero or negative customs value.",
            scope="XML_FIELD", field="exchange_rate")
    return rate


def _validated_cost_override(raw, *, code: str, field: str, label: str) -> None:
    """A reviewer's freight/insurance override, validated where it enters.

    /finalize takes an untyped body, so these two amounts arrived unchecked and
    then moved the duty base directly: select_freight returns a manual override
    verbatim (rules/freight.py), allocate_cost spreads it across every item and
    the builder folds it into total_cost_itm, total_cif_itm and the statistical
    value.  Nothing downstream treats a negative cost as blocking, so the
    declaration still reached XML_READY.  Measured before this gate existed:

      "(250000)" parse_decimal reads the accounting form as -250000 and it was
                 applied verbatim: every item's CIF fell BELOW its own invoice
                 value and the XML declared an understated duty base, with a
                 clean XML_READY status and no warning on screen.
      "-250000"  same, via the plain leading-minus form.
      "abc"      unparseable -> None, so the typed figure was silently
                 DISCARDED and the deterministic freight used instead —
                 the same silent-drop failure the exchange-rate gate documents.

    Returns None and raises on refusal; the caller keeps using ``conf``.
    """
    if raw is None:
        return
    text = str(raw).strip()
    if not text:
        return                               # explicit blank = deterministic value
    amount = parse_decimal(text)
    if amount is None:
        raise BlockingValidationError(
            code,
            f"{label} {text!r} is not a number. Enter it as a plain decimal such as "
            f"1250.00 — the value is added to the customs value of every item, so it "
            f"cannot be guessed at.",
            scope="XML_FIELD", field=field)
    if not amount.is_finite() or amount < 0:
        raise BlockingValidationError(
            code,
            f"{label} {text!r} is negative. It is ADDED to the goods value to reach the "
            f"customs value, so a negative amount declares a CIF below the invoice total "
            f"and understates the duty. Enter the amount as a positive figure, or leave "
            f"it blank to use the deterministic value.",
            scope="XML_FIELD", field=field)


def _rate_plausibility(rate: Decimal, goods_currency: str) -> list[ValidationMessage]:
    """Non-blocking check for a misplaced decimal point.

    Validation cannot catch this one: 1457.6 is a perfectly legal number, it is
    simply ten times the right answer, and on the finalize screen a 10x total
    CIF looks no different from a correct one.  Only compared when the invoice
    is priced in the currency the default is quoted for — a JPY or KRW invoice
    legitimately sits orders of magnitude from a USD rate, and warning on those
    would be noise that teaches reviewers to ignore the banner.
    """
    settings = get_settings()
    base = settings.default_exchange_rate
    if not base or (goods_currency or "").strip().upper() != \
            settings.default_exchange_rate_currency.strip().upper():
        return []
    # Strict bounds: a shifted decimal point lands EXACTLY on the factor
    # (145.76 -> 1457.6), so an inclusive band would let the very typo this
    # exists to catch sit on its boundary and pass.
    if base / _RATE_TYPO_FACTOR < rate < base * _RATE_TYPO_FACTOR:
        return []
    return [ValidationMessage.warning(
        "EXCHANGE_RATE_IMPLAUSIBLE",
        f"Exchange rate {rate} is more than {_RATE_TYPO_FACTOR}x away from the configured "
        f"NRB rate {base} for {goods_currency} — check for a misplaced decimal point. "
        f"Every national-currency value, and the duty, scales with this number.",
        scope="XML_FIELD", field="exchange_rate")]


def _audit(db: Session, job_id: str, code: str, detail: str, payload: dict | None = None,
           *, actor: str = SYSTEM_ACTOR) -> None:
    """Record one audit event.

    ``actor`` is WHO, and it is not decoration: this trail is the only record of
    who did what to a legally binding customs declaration.  The column has
    always existed, but nothing supplied it, so every event — every upload,
    every HS override, every finalize — was attributed to the literal string
    "system" or "reviewer".  A trail that cannot say which operator changed a
    tariff code cannot support non-repudiation, which is the one thing a
    post-clearance audit asks it for.

    SYSTEM_ACTOR remains correct for events no human triggered (restart
    recovery, a background invalidation); anything reached from a request
    should pass the signed-in username.
    """
    db.add(AuditEvent(job_id=job_id, actor=actor or SYSTEM_ACTOR,
                      event_code=code, detail=detail, payload=payload))
