"""ORM models for the operational store.

Deliberately boring: server-owned IDs, immutable upload metadata, versioned
OCR / extraction / declaration payloads stored as JSON.  Nothing here trusts
model output — the LLM never writes a job_id, storage_key or role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, ForeignKey, Index, Integer, LargeBinary, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "customs_job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(40), default="UPLOADING")
    version: Mapped[int] = mapped_column(Integer, default=1)
    rule_set_version: Mapped[str] = mapped_column(String(40), default="")
    exchange_rate: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")
    # WHO MAY SEE THIS JOB.  Deliberately not `created_by`: that column is
    # audit ("who made it"), and overloading one field for both audit and
    # authorization is how a later ownership transfer silently rewrites
    # history, or a history fix silently grants access.
    #
    # Holds the signed-in principal.  Today that is the single configured
    # operator's username; when accounts move to Supabase it becomes that
    # user's id, which is also a string — so the seam does not have to be cut
    # twice.  Empty means a job created before ownership existed; see
    # services.job_visible_to for the one place that decides what that means.
    owner_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Latest computed artefacts (JSON) — see pipeline.
    critical_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    declaration: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Reviewer add/delete overlay (review.item_mutations) — evidence stays immutable.
    item_mutations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Durable per-job regime/office/transport selections
    # ({"revision": n, "values": {...}}) — seed every Critical Review recompute.
    review_selections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Content-keyed HS history (user rule 2026-08-02): normalized item name ->
    # previously reviewer-confirmed HS selection, folded out of the positional
    # overlay whenever invoice evidence changes.  Feeds the resolver's HISTORY
    # cascade as a low-confidence proposal — never re-applied blindly as final.
    hs_history: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "uploaded_document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declared_role: Mapped[str] = mapped_column(String(40))
    upload_index_within_role: Mapped[int] = mapped_column(Integer, default=0)
    original_file_name: Mapped[str] = mapped_column(String(400))
    content_type: Mapped[str] = mapped_column(String(120), default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(40), default="UPLOADED")
    # Where raw_extraction came from (domain.enums.ExtractionProvenance).  The
    # row is the only place that can answer it afterwards: a fixture-seeded
    # extraction and an OCR'd one are the same JSON in the same column, and
    # "these values were never read off the document" is exactly what an audit
    # of a declaration needs to be able to establish.  Defaults to OCR, which
    # is what every row written before this column existed was.
    extraction_provenance: Mapped[str] = mapped_column(String(24), default="OCR",
                                                       server_default="OCR")

    ocr: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_extraction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    role_match: Mapped[bool | None] = mapped_column(nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="documents")


class XmlArtifact(Base):
    __tablename__ = "xml_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declaration_version: Mapped[int] = mapped_column(Integer, default=1)
    template_version: Mapped[str] = mapped_column(String(40), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    xml_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BmsArtifact(Base):
    """The per-item brand/model/size ``.xls`` produced alongside each XML build
    (same declaration version).  Export-only sibling of :class:`XmlArtifact`."""

    __tablename__ = "bms_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declaration_version: Mapped[int] = mapped_column(Integer, default=1)
    template_version: Mapped[str] = mapped_column(String(40), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    xls_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LoginAttempt(Base):
    """One FAILED sign-in, kept only long enough to rate-limit the next one.

    The throttle was a process-local dict, which is two different bugs at once
    on a real deployment.  With N API processes it allowed N times the intended
    guess rate, because each kept its own count.  And behind a load balancer
    every request appears to come from the balancer, so all callers shared ONE
    bucket: ten failures from anywhere locked out every user of the system, and
    an unauthenticated attacker could do that deliberately.  A shared store
    fixes the first; ``auth.client_key`` (trusted_proxy_hops) fixes the second,
    and neither is any use without the other.

    Successful sign-ins delete the caller's rows, and anything older than the
    window is pruned on write — this table is a short sliding window, never a
    login history.  It is deliberately NOT an audit record: `AuditEvent` is
    where anything durable belongs.
    """

    __tablename__ = "login_attempt"
    __table_args__ = (Index("ix_login_attempt_client_created", "client_key", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # An IP address — 45 characters covers IPv6, including a v4-mapped form.
    client_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorLayout(Base):
    """A remembered table column map, keyed by role + header signature.

    Was ``storage/vendor_layouts.json``, and the file is why this table exists.
    Both stores were whole-file read-modify-write: two processes that recorded a
    layout at the same time each loaded the document, each edited its own copy,
    and the second `os.replace` discarded everything the first had learned.
    Atomic on ONE writer, lossy on two — so the file was a reason the app could
    not run more than one process, not merely a slower way to store this.

    A row per key makes concurrent writers independent, and the read-modify-write
    that remains (counters, `max` of confirmed_rows) is done under a row lock.
    """

    __tablename__ = "vendor_layout"
    __table_args__ = (UniqueConstraint("role", "header_signature",
                                       name="uq_vendor_layout_role_signature"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role: Mapped[str] = mapped_column(String(40), index=True)
    # Never "POSITIONAL" and never empty: layout_memory.record_layout refuses to
    # write a layout that did not come from the document's own header, because
    # such an entry matches every headerless document of the role and confirms
    # itself.  The column is not nullable so that rule cannot be bypassed here.
    header_signature: Mapped[str] = mapped_column(String(255))
    mapping: Mapped[dict] = mapped_column(JSON)
    vendor_hint: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_rows: Mapped[int] = mapped_column(Integer, default=0)
    docs: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorFieldProfile(Base):
    """Learned per-vendor field defaults — today only the default COO.

    Was ``storage/vendor_field_profiles.json``; see :class:`VendorLayout` for why
    a file could not survive a second process.  ``vendor`` is the normalised key
    (``field_profiles._norm_vendor``), so it is the primary key outright.
    """

    __tablename__ = "vendor_field_profile"

    vendor: Mapped[str] = mapped_column(String(160), primary_key=True)
    display: Mapped[str] = mapped_column(String(120), default="")
    coo_default: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # REVIEWER (a deliberate decision) outranks OBSERVED (agreeing finalized
    # jobs) — the state machine that depends on this lives in field_profiles.
    coo_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coo_docs: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    actor: Mapped[str] = mapped_column(String(120), default="system")
    event_code: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="events")
