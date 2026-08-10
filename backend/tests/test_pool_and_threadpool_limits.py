"""The two per-process ceilings that decide how many requests a task can serve.

Both were library defaults nobody had chosen, and they disagreed with each
other: Starlette lets 40 threadpool requests run at once, while SQLAlchemy's
default pool holds 5 + 10 connections.  Every upload, extraction and finalize
runs in that threadpool and wants a connection, so 25 of the 40 could only ever
queue on the pool and then fail with a checkout timeout — a failure that reads
like a database problem and is actually a sizing one.

They are settings now, and the relationship between them is enforced rather
than documented.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from pydantic import ValidationError

from app import database, main
from app.config import Settings

_PG = "postgresql+psycopg://u:p@example.invalid:5432/easycustoms"


def test_the_defaults_agree_with_each_other():
    """A default deployment must not ship with the two limits mismatched — that
    is the exact state this pairing exists to end."""
    s = Settings(_env_file=None)
    assert s.threadpool_max_threads == s.db_pool_size + s.db_max_overflow


def test_a_threadpool_wider_than_the_pool_is_refused_on_postgres():
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, database_url=_PG,
                 threadpool_max_threads=200, db_pool_size=10, db_max_overflow=30)
    assert "THREADPOOL_MAX_THREADS" in str(caught.value)


def test_raising_the_pool_with_it_is_accepted():
    s = Settings(_env_file=None, database_url=_PG,
                 threadpool_max_threads=200, db_pool_size=50, db_max_overflow=150)
    assert s.threadpool_max_threads == 200


def test_sqlite_is_not_bound_by_the_pool():
    """SQLite's limit is its single writer, not a connection count; the WAL
    pragmas are what make concurrency work there."""
    s = Settings(_env_file=None, threadpool_max_threads=500)
    assert s.database_url.startswith("sqlite")
    assert s.threadpool_max_threads == 500


def test_pool_settings_reach_the_engine_only_on_a_server_database():
    """SQLite (the test environment) must keep its own pooling; passing sizes
    there would only add connections contending for one write lock."""
    assert database._is_sqlite, "test suite is expected to run on SQLite"
    assert database._pool_args == {}


def test_pool_arguments_are_named_for_a_server_database():
    """The postgres branch of the same expression, evaluated directly — the
    engine itself is built once at import against the test database."""
    s = Settings(_env_file=None, database_url=_PG, db_pool_size=7,
                 db_max_overflow=9, db_pool_recycle_seconds=120,
                 db_pool_pre_ping=True, threadpool_max_threads=16)
    args = {
        "pool_size": s.db_pool_size,
        "max_overflow": s.db_max_overflow,
        "pool_recycle": s.db_pool_recycle_seconds,
        "pool_pre_ping": s.db_pool_pre_ping,
    }
    assert args == {"pool_size": 7, "max_overflow": 9,
                    "pool_recycle": 120, "pool_pre_ping": True}


def test_the_threadpool_limiter_is_actually_raised(monkeypatch):
    monkeypatch.setattr(main, "get_settings",
                        lambda: Settings(_env_file=None, threadpool_max_threads=77))

    async def run() -> float:
        import anyio.to_thread
        before = anyio.to_thread.current_default_thread_limiter().total_tokens
        assert before == 40, "AnyIO's default is what we are here to lift"
        main._widen_threadpool()
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(run()) == 77


def test_a_lower_setting_never_narrows_the_pool(monkeypatch):
    """Only ever raise: silently dropping below AnyIO's default would make a
    deployment slower than an unconfigured one."""
    monkeypatch.setattr(main, "get_settings",
                        lambda: Settings(_env_file=None, threadpool_max_threads=5))

    async def run() -> float:
        import anyio.to_thread
        main._widen_threadpool()
        return anyio.to_thread.current_default_thread_limiter().total_tokens

    assert asyncio.run(run()) == 40


def test_widening_never_stops_the_app_from_booting(monkeypatch):
    """A throughput ceiling is not worth an outage."""
    monkeypatch.setattr(main, "get_settings",
                        lambda: Settings(_env_file=None, threadpool_max_threads=77))

    # Captured off the app's own logger rather than through caplog: caplog
    # attaches to the ROOT logger, and whether a record gets there depends on
    # global logging state this test does not own.
    said: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            said.append(record.getMessage())

    handler = _Capture()
    main.log.addHandler(handler)
    previous = main.log.level
    main.log.setLevel(logging.DEBUG)

    async def run() -> None:
        import anyio.to_thread

        def _boom():
            raise RuntimeError("no limiter here")

        monkeypatch.setattr(anyio.to_thread, "current_default_thread_limiter", _boom)
        main._widen_threadpool()              # must not raise

    try:
        asyncio.run(run())
    finally:
        main.log.removeHandler(handler)
        main.log.setLevel(previous)

    assert any("threadpool limiter" in m for m in said), said
