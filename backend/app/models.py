"""ORM models for the operational store.

Deliberately boring: server-owned IDs, immutable upload metadata, versioned
OCR / extraction / declaration payloads stored as JSON.  Nothing here trusts
model output — the LLM never writes a job_id, storage_key or role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, LargeBinary, String, Text
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
