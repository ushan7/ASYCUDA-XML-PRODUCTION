"""SQS producer — the enqueue half of queue-mode extraction.

The consumer half lives in backend/worker.py.  Contract between the two:

  * The producer CLAIMS the document first (status -> EXTRACTING, committed),
    then sends one message naming it.  The claim is the deduplication — at
    most one message per claim can exist — and it is what the SPA's existing
    EXTRACTING poll watches, so queue mode needs no frontend change.
  * The message carries REFERENCES only ({job_id, document_id}), never file
    bytes: documents live in shared storage, jobs in the shared database, and
    an SQS body is capped at 256 KB anyway.
  * If the send fails, the claim is released on the spot (the document goes
    back to its prior status with a visible warning) — otherwise it would be
    claimed forever with no worker ever coming.

boto3 is imported lazily so a queue-off deployment neither installs nor loads
it, matching how the OCR/LLM SDKs are treated.  Credentials are resolved by
boto3's own chain (env, shared config, instance role) — this module never
handles keys.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from . import services
from .config import get_settings
from .models import Document

log = logging.getLogger("easycustoms.queue")

# Message schema, versioned so a worker can refuse what it does not
# understand instead of guessing.  Bump `v` on any incompatible change.
MESSAGE_V = 1
KIND_EXTRACT_DOCUMENT = "extract_document"

_client: Any = None


class QueueSendError(RuntimeError):
    """send_message failed; the claim has already been released."""


def make_sqs_client(settings: Any):
    """One construction rule for producer AND worker.

    Keys from backend/.env are read by pydantic into Settings only — they
    never reach os.environ, so boto3's own chain cannot see them; hand them
    over explicitly when both are present.  When either is blank, fall back
    to the chain, which on EC2/ECS is the instance role (the right production
    source — no long-lived keys on a server).
    """
    import boto3  # lazy: only queue-mode deployments need it installed
    kwargs: dict[str, str] = {}
    if settings.sqs_region:
        kwargs["region_name"] = settings.sqs_region
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("sqs", **kwargs)


def _sqs():
    global _client
    if _client is None:
        _client = make_sqs_client(get_settings())
    return _client


def reset_client_cache() -> None:
    """Tests swap the client; nothing else should call this."""
    global _client
    _client = None


def enqueue_extraction(db: Session, doc: Document, *, actor: str) -> None:
    """Claim the document and put its extraction on the queue.

    Raises BlockingValidationError (409 at the API) when the document is
    already claimed, or QueueSendError (502) when SQS refused the message —
    in which case the claim has been rolled back and the document is exactly
    as retriable as before the call.
    """
    settings = get_settings()
    prior = services.claim_for_queued_extraction(db, doc, actor=actor)
    body = json.dumps({
        "v": MESSAGE_V,
        "kind": KIND_EXTRACT_DOCUMENT,
        "job_id": doc.job_id,
        "document_id": doc.id,
        # Audit attribution for the worker's writes: the human who pressed
        # Continue, not the worker process.
        "actor": actor,
    })
    try:
        _sqs().send_message(QueueUrl=settings.sqs_queue_url, MessageBody=body)
    except Exception as e:  # boto3 raises ClientError/EndpointConnectionError/…
        log.error("send_message failed for document %s (%s): %s",
                  doc.id, doc.original_file_name, e)
        services.release_queued_claim(db, doc, prior, error=str(e), actor=actor)
        raise QueueSendError(f"could not queue the extraction: {e}") from e
    log.info("queued extraction: job %s document %s (%s #%s) by %s",
             doc.job_id, doc.id, doc.declared_role,
             doc.upload_index_within_role, actor)
