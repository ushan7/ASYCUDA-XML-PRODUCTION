"""Local object storage (tenant/job scoped keys) + hashing."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import get_settings

# Characters NTFS refuses in a file name, plus control characters.  A raw
# client filename containing ":" is worse than an error on Windows: the part
# after the colon becomes an NTFS alternate-data-stream name, the visible file
# is created 0 bytes long, and the document's actual bytes are silently
# unreachable — OCR then reads an empty PDF.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(filename: str) -> str:
    """The client's filename reduced to something every filesystem accepts."""
    # basename first (either separator): a path smuggled into the filename
    # must never navigate out of the job directory
    base = re.split(r"[/\\]", filename or "")[-1]
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip(" .")
    return base or "upload.bin"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_document(job_id: str, document_id: str, filename: str, data: bytes) -> str:
    settings = get_settings()
    job_dir = settings.storage_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    key = job_dir / f"{document_id}__{safe_filename(filename)}"
    key.write_bytes(data)
    return str(key)


def delete_stored_document(storage_key: str) -> None:
    """Best-effort removal of a stored upload (the DB row is authoritative)."""
    if not storage_key:
        return
    try:
        Path(storage_key).unlink(missing_ok=True)
    except OSError:
        pass
