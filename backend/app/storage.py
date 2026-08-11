"""Where uploaded documents live — a local directory, or an S3 bucket.

A local directory is the right default (a broker's laptop needs no cloud
account) and the wrong thing for more than one API instance: the instance that
did not receive the upload has no such file, so the reviewer's evidence panel
answers 410 or not depending on which one the load balancer picked.  Queue mode
makes it sharper still — the worker that OCRs a document is a different machine
from the API that stored it, so with local storage there is nothing for it to
read.

TWO RULES SHAPE THIS MODULE.

**A storage key says where it lives.**  Dispatch is on the KEY, never on the
current setting: an ``s3://`` key is fetched from S3 and anything else is a
local path, whatever ``storage_backend`` says today.  That is what lets a
deployment turn S3 on without orphaning every document it already holds — the
alternative reads the config, looks in the wrong place, and reports a reviewer's
existing evidence as missing.

**Bytes, not paths, cross this boundary.**  Callers get ``load_document`` /
``open_document``; nothing outside asks where the file is.  The OCR providers
used to take a ``file_path`` and open it themselves, which quietly made "the
document is on this machine's disk" a requirement of the extraction layer.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import IO

from .config import get_settings

log = logging.getLogger("easycustoms.storage")

S3_SCHEME = "s3://"

# Characters NTFS refuses in a file name, plus control characters.  A raw
# client filename containing ":" is worse than an error on Windows: the part
# after the colon becomes an NTFS alternate-data-stream name, the visible file
# is created 0 bytes long, and the document's actual bytes are silently
# unreachable — OCR then reads an empty PDF.
_UNSAFE_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DocumentUnavailable(Exception):
    """The stored bytes for a document could not be read.

    Raised rather than returning empty: an empty PDF extracts as a document
    with no content, which is indistinguishable from a real one that OCR could
    not read — and that difference decides whether a reviewer is told to
    re-upload or shown a declaration built from nothing.
    """


def safe_filename(filename: str) -> str:
    """The client's filename reduced to something every filesystem accepts."""
    # basename first (either separator): a path smuggled into the filename
    # must never navigate out of the job directory
    base = re.split(r"[/\\]", filename or "")[-1]
    base = _UNSAFE_NAME_CHARS.sub("_", base).strip(" .")
    return base or "upload.bin"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_remote(storage_key: str) -> bool:
    """Whether this key names an object the local filesystem cannot serve."""
    return bool(storage_key) and storage_key.startswith(S3_SCHEME)


# --------------------------------------------------------------------------- #
# S3 plumbing
# --------------------------------------------------------------------------- #
def _s3_client():
    """Same construction rule as queueing.make_sqs_client.

    Keys from backend/.env are read by pydantic into Settings only — they never
    reach os.environ, so boto3's own chain cannot see them; hand them over
    explicitly when both are present.  When either is blank, fall back to the
    chain, which on EC2/ECS is the task role (the right production source — no
    long-lived keys on a server).
    """
    import boto3  # lazy: only S3 deployments need it installed

    settings = get_settings()
    kwargs: dict[str, str] = {}
    if settings.s3_region:
        kwargs["region_name"] = settings.s3_region
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs)


def _split_s3_key(storage_key: str) -> tuple[str, str]:
    """``s3://bucket/some/key`` -> ``("bucket", "some/key")``."""
    bucket, _, key = storage_key[len(S3_SCHEME):].partition("/")
    if not bucket or not key:
        raise DocumentUnavailable(f"{storage_key!r} is not a usable S3 location")
    return bucket, key


# --------------------------------------------------------------------------- #
# The interface the rest of the app uses
# --------------------------------------------------------------------------- #
def store_document(job_id: str, document_id: str, filename: str, data: bytes) -> str:
    """Persist an upload and return the key that finds it again.

    The key is stored on the document row, so it has to stay valid for as long
    as the row does — including across a later change of backend, which is why
    the S3 form carries its own bucket rather than resolving one at read time.
    """
    name = f"{document_id}__{safe_filename(filename)}"
    settings = get_settings()
    if settings.storage_backend == "s3":
        key = f"{settings.s3_prefix}jobs/{job_id}/{name}"
        _s3_client().put_object(
            Bucket=settings.s3_bucket, Key=key, Body=data,
            ContentType="application/pdf",
            # Server-side encryption is the bucket's job (SSE-KMS by policy),
            # not something to weaken from here by naming a lesser algorithm.
        )
        return f"{S3_SCHEME}{settings.s3_bucket}/{key}"
    job_dir = settings.storage_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    path = job_dir / name
    path.write_bytes(data)
    return str(path)


def load_document(storage_key: str) -> bytes:
    """The stored bytes, or DocumentUnavailable."""
    if not storage_key:
        raise DocumentUnavailable("this document has no stored file")
    if is_remote(storage_key):
        bucket, key = _split_s3_key(storage_key)
        try:
            return _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
        except Exception as e:
            raise DocumentUnavailable(f"could not read {storage_key}: {e}") from e
    path = Path(storage_key)
    if not path.is_file():
        raise DocumentUnavailable(f"{storage_key} is no longer on this server")
    return path.read_bytes()


def open_document(storage_key: str) -> IO[bytes]:
    """A readable stream of the stored bytes, for serving without buffering the
    whole document in memory.  The caller closes it."""
    if not storage_key:
        raise DocumentUnavailable("this document has no stored file")
    if is_remote(storage_key):
        bucket, key = _split_s3_key(storage_key)
        try:
            return _s3_client().get_object(Bucket=bucket, Key=key)["Body"]
        except Exception as e:
            raise DocumentUnavailable(f"could not read {storage_key}: {e}") from e
    path = Path(storage_key)
    if not path.is_file():
        raise DocumentUnavailable(f"{storage_key} is no longer on this server")
    return path.open("rb")


def document_exists(storage_key: str) -> bool:
    """Whether the bytes are still there — the evidence panel's HEAD probe.

    A HEAD on S3 rather than a GET: the panel asks before rendering, and paying
    for the whole object to answer "yes" would double the cost of opening every
    document.
    """
    if not storage_key:
        return False
    if is_remote(storage_key):
        try:
            bucket, key = _split_s3_key(storage_key)
            _s3_client().head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False
    return Path(storage_key).is_file()


def delete_stored_document(storage_key: str) -> None:
    """Best-effort removal of a stored upload (the DB row is authoritative)."""
    if not storage_key:
        return
    if is_remote(storage_key):
        try:
            bucket, key = _split_s3_key(storage_key)
            _s3_client().delete_object(Bucket=bucket, Key=key)
        except Exception as e:
            # A leftover object costs storage; a raised exception here would
            # fail the reviewer's document removal, which is the actual request.
            log.warning("could not delete %s: %s", storage_key, e)
        return
    try:
        Path(storage_key).unlink(missing_ok=True)
    except OSError:
        pass
