"""Deprecated shim — ORM models now live in ``app.models``.

Kept only as a re-export so any stray ``from app.db import ...`` keeps working
without redefining the SQLAlchemy tables.
"""
from .models import AuditEvent, Document, Job, XmlArtifact  # noqa: F401

__all__ = ["Job", "Document", "XmlArtifact", "AuditEvent"]
