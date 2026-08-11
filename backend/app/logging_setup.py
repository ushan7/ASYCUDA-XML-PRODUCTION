"""One line of JSON per event, correlated by request, with secrets scrubbed.

Nothing configured the API's logging at all: uvicorn's defaults produced human
prose on stdout, which is fine on one machine and useless across a fleet.  With
several API tasks and a worker fleet behind a load balancer, "why did that
reviewer's finalize fail" means reading interleaved lines from processes that
never mention which request they belong to.

Three things this provides, in order of how much they matter:

1. **A request id on every line**, echoed to the client in ``X-Request-ID`` and
   carried into the SQS message so a queued extraction's logs join up with the
   request that enqueued it.  A reviewer can quote the id from a failure and it
   selects the whole story, across processes.
2. **JSON**, so CloudWatch Logs Insights (or anything else) can filter on
   ``principal``, ``job_id`` or ``status`` instead of matching substrings.
3. **Redaction that is enforced rather than documented.**  This app's logs are
   one careless line away from holding an importer's invoice contents or an
   operator's password.

ON DEBUG.  Raising this app's level to DEBUG is reasonable; raising SQLAlchEmy's
or httpx's is not, and the two used to be the same switch.  ``sqlalchemy.engine``
at DEBUG prints every statement WITH ITS PARAMETERS — party names, invoice
values, the declaration itself — and ``httpx`` at DEBUG prints request bodies,
which on the login route is the password. So the noisy third-party loggers are
pinned at WARNING and only a separate, deliberate setting lets them down. A
comment saying "do not enable DEBUG in production" is not a control; this is.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone

# Libraries whose DEBUG output contains the very data this app exists to
# protect.  Pinned unless log_third_party_debug is explicitly turned on.
NOISY_LOGGERS = (
    "sqlalchemy.engine",     # statements WITH bound parameters
    "sqlalchemy.pool",
    "httpx", "httpcore",     # request/response bodies — the login password
    "urllib3",
    "openai", "mistralai",   # prompts, i.e. the document contents
    "botocore", "boto3", "s3transfer",
)

# Fields the LogRecord always carries; anything else an emitter attached via
# `extra=` is application context and belongs in the JSON output.
_STANDARD = frozenset((
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "thread", "threadName", "taskName",
))

# key=value / "key": "value" in either quoting style, and bearer tokens.
#
# The key may carry a PREFIX — the thing that actually appears in logs is
# `EASYCUSTOMS_AUTH_SECRET=...`, not a bare `secret=...`, and a leading `\b`
# never matches between `_` and `S` because both are word characters. Allowing
# the prefix is the difference between this catching the real case and only the
# textbook one.
_SECRET_KEYS = r"password|passwd|secret|token|api[_-]?key|apikey|authorization|anon[_-]?key"
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)([A-Za-z0-9_.\-]*(?:{_SECRET_KEYS}))(\"?\'?\s*[:=]\s*\"?\'?)([^\s,\"'}}\]]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+")

REDACTED = "***"

# Per-request correlation.  ContextVars rather than thread locals because the
# app is ASGI and the slow routes hop to a worker thread — anyio copies the
# context across that hop, so a finalize logs the same request id from the
# threadpool as the handler that started it.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
principal_var: contextvars.ContextVar[str] = contextvars.ContextVar("principal", default="")


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_request_id() -> str:
    return request_id_var.get()


def redact(text: str) -> str:
    """Blank out anything that looks like a credential.

    Applied to the FORMATTED line, so it cannot be bypassed by choosing a
    format or by an f-string that interpolated a secret before logging.  It is a
    backstop, not a licence: the rule is still never to log a request body.
    """
    if not text:
        return text
    # Bearer FIRST, and the order is load-bearing. `Authorization: Bearer <jwt>`
    # matches the assignment rule too, whose value stops at whitespace — so it
    # would consume the literal word "Bearer" as the secret and leave the token
    # itself in the line.
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    return _SECRET_ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)


class ContextFilter(logging.Filter):
    """Attach the current request id and principal to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = request_id_var.get()
        if not getattr(record, "principal", None):
            record.principal = principal_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "principal"):
            value = getattr(record, key, "")
            if value:
                payload[key] = value
        for key, value in record.__dict__.items():
            if key in _STANDARD or key in payload or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) \
                else str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # default=str so an unserialisable extra degrades to its repr instead of
        # losing the whole line to a TypeError inside the logger.
        return redact(json.dumps(payload, default=str, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    """The readable local format, redacted on the same terms as JSON."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        # The filter normally supplies these, but a record formatted directly
        # (a library's own handler, a test) has never been through it — and a
        # missing key here raises inside logging, losing the line entirely.
        for key in ("request_id", "principal"):
            if not hasattr(record, key):
                setattr(record, key, "")
        return redact(super().format(record))


_HANDLER_TAG = "easycustoms-configured"


def configure_logging(settings=None) -> None:
    """Install the formatter and levels.  Idempotent.

    Replaces our own handler rather than adding one, because startup runs many
    times in a test session and once per worker process, and stacked handlers
    print every line as many times as they have accumulated.
    """
    from .config import get_settings

    settings = settings or get_settings()
    root = logging.getLogger()

    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_TAG, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, _HANDLER_TAG, True)
    handler.setFormatter(_pick_formatter(settings.log_format))
    handler.addFilter(ContextFilter())
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # The point of the exercise: this app's own level must not drag the
    # libraries that print invoice contents and passwords down with it.
    third_party = ("DEBUG" if settings.log_third_party_debug
                   else max(logging.WARNING, root.level))
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(third_party)

    # uvicorn installs its own handlers and would otherwise emit a second,
    # unformatted copy of every access line beside ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True


def _pick_formatter(choice: str) -> logging.Formatter:
    choice = (choice or "auto").lower()
    if choice == "auto":
        # A container's stdout is a pipe, a developer's is a terminal. So the
        # deployment that most needs machine-readable logs gets them without
        # anyone remembering to ask.
        choice = "text" if sys.stdout.isatty() else "json"
    return TextFormatter() if choice == "text" else JsonFormatter()
