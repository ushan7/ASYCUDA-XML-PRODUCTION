"""One line of JSON per event, correlated by request, with secrets scrubbed.

Nothing configured the API's logging at all: uvicorn's defaults produced human
prose, which is fine on one machine and useless across a fleet. With several API
tasks and a worker fleet behind a load balancer, "why did that reviewer's
finalize fail" means reading interleaved lines from processes that never say
which request they belong to.

The assertion that matters most here is not the JSON shape — it is that the
protections are ENFORCED rather than documented. This app's logs are one
careless line away from holding an importer's invoice contents or an operator's
password, and `sqlalchemy.engine` at DEBUG prints every statement with its bound
parameters. A comment saying "do not enable DEBUG in production" is not a
control.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import logging_setup
from app.config import Settings, get_settings
from app.logging_setup import JsonFormatter, TextFormatter, configure_logging, redact
from app.main import app


def _record(msg, level=logging.INFO, **extra):
    record = logging.LogRecord("easycustoms.test", level, __file__, 1, msg, (), None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


# --------------------------------------------------------------------------- #
# Redaction — the backstop that cannot be bypassed by choosing a format
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("line, secret", [
    ('{"password": "hunter2"}', "hunter2"),
    ("password=hunter2", "hunter2"),
    ("EASYCUSTOMS_AUTH_SECRET=abc123def", "abc123def"),
    ('{"api_key": "sk-live-1234"}', "sk-live-1234"),
    ("apikey: sk-live-1234", "sk-live-1234"),
    ('{"token":"eyJhbGciOi"}', "eyJhbGciOi"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc", "eyJhbGciOiJIUzI1NiJ9.abc"),
    ('{"anon_key": "sb_publishable_xyz"}', "sb_publishable_xyz"),
])
def test_credentials_never_reach_the_output(line, secret):
    out = redact(line)
    assert secret not in out
    assert logging_setup.REDACTED in out


def test_redaction_applies_to_both_formats():
    """Choosing a format must not choose whether secrets are printed."""
    msg = 'signing in with password=hunter2'
    assert "hunter2" not in JsonFormatter().format(_record(msg))
    assert "hunter2" not in TextFormatter().format(_record(msg))


def test_redaction_survives_an_fstring():
    """The rule is still never to log a request body, but a line that already
    interpolated a secret is exactly the mistake a backstop is for."""
    password = "hunter2"
    assert "hunter2" not in JsonFormatter().format(
        _record(f"login failed for password={password}"))


def test_ordinary_text_is_left_alone():
    """A scrubber that mangles normal messages gets turned off."""
    msg = "extracted 119 items from INVOICE #2 in 4.1s"
    assert redact(msg) == msg


# --------------------------------------------------------------------------- #
# JSON shape
# --------------------------------------------------------------------------- #
def test_a_line_is_one_json_object():
    out = JsonFormatter().format(_record("hello"))
    parsed = json.loads(out)
    assert parsed["message"] == "hello"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "easycustoms.test"
    assert parsed["ts"].endswith("+00:00")


def test_context_fields_are_top_level_and_queryable():
    """The point of JSON here: filter on principal or status in CloudWatch
    Insights instead of matching substrings."""
    out = json.loads(JsonFormatter().format(
        _record("GET /api/jobs -> 200", request_id="abc123", principal="user-a",
                http_status=200, duration_ms=12.5)))
    assert out["request_id"] == "abc123"
    assert out["principal"] == "user-a"
    assert out["http_status"] == 200
    assert out["duration_ms"] == 12.5


def test_an_unserialisable_extra_does_not_lose_the_line():
    out = json.loads(JsonFormatter().format(_record("x", weird=object())))
    assert "weird" in out


def test_an_exception_is_carried():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = _record("failed")
        record.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in out["exception"]


# --------------------------------------------------------------------------- #
# The DEBUG control
# --------------------------------------------------------------------------- #
def test_app_debug_does_not_drag_the_libraries_down_with_it():
    """`sqlalchemy.engine` at DEBUG prints every statement WITH ITS BOUND
    PARAMETERS — party names, invoice values, the declaration itself. `httpx`
    at DEBUG prints request bodies, which on the login route is the password."""
    configure_logging(Settings(_env_file=None, log_level="DEBUG", log_format="text"))
    assert logging.getLogger().level == logging.DEBUG
    for name in ("sqlalchemy.engine", "httpx", "openai", "mistralai"):
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_third_party_debug_is_a_separate_deliberate_switch():
    configure_logging(Settings(_env_file=None, log_level="DEBUG", log_format="text",
                               log_third_party_debug=True))
    assert logging.getLogger("sqlalchemy.engine").level == logging.DEBUG


def test_an_unknown_level_or_format_is_refused_at_boot():
    with pytest.raises(Exception):
        Settings(_env_file=None, log_level="CHATTY")
    with pytest.raises(Exception):
        Settings(_env_file=None, log_format="yaml")


def test_configuring_twice_does_not_duplicate_every_line():
    """Startup runs many times in a test session and once per worker process;
    stacked handlers print each line as many times as they accumulated."""
    settings = Settings(_env_file=None, log_format="json")
    configure_logging(settings)
    configure_logging(settings)
    configure_logging(settings)
    ours = [h for h in logging.getLogger().handlers
            if getattr(h, "easycustoms-configured", False)]
    assert len(ours) == 1


def test_auto_format_picks_json_when_stdout_is_not_a_terminal(monkeypatch):
    """A container's stdout is a pipe and a developer's is a terminal, so the
    deployment that most needs machine-readable logs gets them without anyone
    remembering to ask."""
    import sys

    class _NotATty:
        def isatty(self):
            return False

    class _ATty:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdout", _NotATty())
    assert isinstance(logging_setup._pick_formatter("auto"), JsonFormatter)
    monkeypatch.setattr(sys, "stdout", _ATty())
    assert isinstance(logging_setup._pick_formatter("auto"), TextFormatter)


# --------------------------------------------------------------------------- #
# Correlation, end to end
# --------------------------------------------------------------------------- #
def test_every_response_carries_a_request_id():
    with TestClient(app) as client:
        r = client.get("/api/health")
    assert r.headers["X-Request-ID"]


def test_an_edge_supplied_id_is_reused_so_the_lines_join_up():
    with TestClient(app) as client:
        r = client.get("/api/health", headers={"X-Request-ID": "edge-abc-123"})
    assert r.headers["X-Request-ID"] == "edge-abc-123"


def test_a_hostile_request_id_cannot_inject_structure():
    """Read for correlation only and never for a decision — but it still ends
    up inside a log record, so it is stripped and capped."""
    with TestClient(app) as client:
        r = client.get("/api/health",
                       headers={"X-Request-ID": '"}\n{"level":"CRITICAL","message":"fake'})
    got = r.headers["X-Request-ID"]
    assert "\n" not in got and '"' not in got and "{" not in got


def test_a_refused_request_is_still_logged_and_identified():
    """The context middleware is OUTERMOST on purpose: a gate whose refusals are
    invisible is a gate nobody can debug, and 401s are what operators call
    about."""
    with TestClient(app) as client:
        client.headers.pop("Authorization", None)
        client.cookies.clear()
        r = client.get("/api/jobs")
    assert r.status_code == 401
    assert r.headers["X-Request-ID"]


def test_the_access_log_records_the_path_but_never_the_query_string(caplog):
    """/api/reference/hs carries the reviewer's search terms. A full-URL access
    log is how those reach an aggregator nobody thought of as holding customer
    data."""
    with caplog.at_level(logging.INFO, logger="easycustoms.api"):
        with TestClient(app) as client:
            client.get("/api/reference/hs", params={"q": "widgets-of-interest"})
    lines = [r for r in caplog.records if getattr(r, "http_path", None)]
    assert lines, "the access log produced nothing"
    assert any(r.http_path == "/api/reference/hs" for r in lines)
    assert "widgets-of-interest" not in caplog.text


def test_the_access_log_carries_status_and_duration(caplog):
    with caplog.at_level(logging.INFO, logger="easycustoms.api"):
        with TestClient(app) as client:
            client.get("/api/health")
    entry = next(r for r in caplog.records if getattr(r, "http_path", "") == "/api/health")
    assert entry.http_status == 200
    assert entry.http_method == "GET"
    assert entry.duration_ms >= 0


def test_the_request_id_does_not_leak_between_requests():
    """ContextVars are reset in a finally, so one request's id cannot be
    attributed to the next one served by the same worker."""
    with TestClient(app) as client:
        client.get("/api/health", headers={"X-Request-ID": "first"})
    assert logging_setup.current_request_id() == ""
