"""SQLAlchemy engine + session.

DB-agnostic: the same models run on SQLite (default, zero setup) and Postgres
(set EASYCUSTOMS_DATABASE_URL).  JSON payloads use the portable JSON type.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("easycustoms.db")


class Base(DeclarativeBase):
    pass


settings = get_settings()

_connect_args = ({"check_same_thread": False, "timeout": 30}
                 if settings.database_url.startswith("sqlite") else {})
engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        """Write-ahead logging, applied to every connection.

        SQLite's default rollback journal holds a whole-database EXCLUSIVE lock
        for the length of every write, so a reader that arrives mid-write waits
        out the busy timeout and then fails outright.  Documents extract
        concurrently and each commits its OCR envelope as soon as the scan is
        paid for, so that window is hit routinely — and the failure lands on a
        document whose OCR has already been bought.  WAL lets readers run
        against the last committed snapshot while a writer works, which is what
        makes parallel extraction safe on one file.

        ``journal_mode`` is a property of the database file (set once, kept);
        ``synchronous`` is per-connection, so it has to be re-applied here.
        NORMAL is the documented companion to WAL: durable across a process
        crash, and only a power loss can cost the last commits — for a queue of
        re-runnable extractions that is the right trade for not fsyncing every
        write of a 200 MB file.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            row = cur.fetchone()
            mode = str(row[0]).lower() if row else ""
            cur.execute("PRAGMA synchronous=NORMAL")
            if mode != "wal":
                # Refused, not raised — SQLite answers with the mode it kept.
                # Network shares have no shared memory, so WAL is unavailable
                # there; say so rather than let it look like it took effect.
                log.warning("SQLite kept journal_mode=%s (WAL refused — a network or "
                            "read-only filesystem?): concurrent extractions may hit "
                            "'database is locked'", mode or "unknown")
        finally:
            cur.close()


def init_db() -> None:
    """Make the database usable, or refuse to start.

    The two backends are handled differently ON PURPOSE:

    SQLite builds itself.  ``create_all`` plus the additive step below have
    always been enough, a broker's laptop should not need a migration command
    to open the app, and the test suite runs on throwaway files — so that path
    is unchanged and does not even import alembic.

    Postgres is CHECKED, never built.  ``create_all`` creates missing tables
    but never ALTERs an existing one, and ``_migrate_sqlite`` returns
    immediately on any other dialect: together they would bring up an API
    against a Postgres database silently missing a column added since it was
    created, and the failure would surface later as a query error on a
    reviewer's screen.  Refusing at startup, with the command that fixes it,
    is the same trade auth.py makes — a misconfigured deployment must be
    visibly broken rather than quietly wrong.
    """
    from . import models  # noqa: F401  (register models)

    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
        _migrate_sqlite()
        return
    _require_schema_at_head()


class SchemaOutOfDateError(RuntimeError):
    """The database is not at the revision this code was written against."""


def _require_schema_at_head() -> None:
    """Verify a non-SQLite database is at the migration head.  Never writes.

    Three ways to be wrong, three different fixes, so they are three different
    messages — "run the migrations" is useless advice to someone whose database
    predates migrations, where an upgrade would try to CREATE tables that exist.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    config = Config(str(ini))
    # script_location is relative to backend/ in the ini; make it absolute so
    # the check works no matter which directory the process was started from.
    config.set_main_option("script_location", str(ini.parent / "alembic"))
    head = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        has_tables = inspect(conn).has_table("customs_job")

    if current == head:
        log.info("database schema at %s", head)
        return
    if current is None and not has_tables:
        raise SchemaOutOfDateError(
            f"{_safe_url()} is empty. Create the schema before starting:\n"
            f"    cd backend && alembic upgrade head")
    if current is None and has_tables:
        raise SchemaOutOfDateError(
            f"{_safe_url()} has tables but no migration history — it predates "
            f"alembic (built by create_all).\nRecord where it already is, then "
            f"upgrade:\n"
            f"    cd backend && alembic stamp fa843d18c61e && alembic upgrade head\n"
            f"Do NOT run 'upgrade' alone: it would try to create tables that "
            f"already exist.")
    raise SchemaOutOfDateError(
        f"{_safe_url()} is at migration {current}, but this code expects "
        f"{head}.\nApply the missing migrations:\n"
        f"    cd backend && alembic upgrade head")


def _safe_url() -> str:
    """The database URL with any password removed — this text reaches logs."""
    try:
        return make_url(settings.database_url).render_as_string(hide_password=True)
    except Exception:
        return "the configured database"


def _migrate_sqlite() -> None:
    """create_all never ALTERs existing tables — add columns introduced after a
    DB file was created (SQLite deployments only; additive and idempotent).

    FROZEN as of the alembic baseline (2026-08-09).  Do not add a case here for
    a new column: this runs on SQLite only, so a column added here would be
    missing on Postgres, which is the exact failure the migrations exist to
    prevent.  New columns go in an alembic revision, which serves both.  What
    remains is only the ladder that carries a pre-alembic SQLite file up to the
    baseline before it can be stamped.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(customs_job)"))}
        if cols and "item_mutations" not in cols:
            conn.execute(text("ALTER TABLE customs_job ADD COLUMN item_mutations JSON"))
        if cols and "review_selections" not in cols:
            conn.execute(text("ALTER TABLE customs_job ADD COLUMN review_selections JSON"))
        if cols and "hs_history" not in cols:
            conn.execute(text("ALTER TABLE customs_job ADD COLUMN hs_history JSON"))
        if cols and "owner_key" not in cols:
            conn.execute(text(
                "ALTER TABLE customs_job ADD COLUMN owner_key VARCHAR(160) DEFAULT ''"))
            # Backfill: every job that exists predates ownership, and on a
            # single-operator deployment they are all that operator's.  Leaving
            # them blank instead would either hide a broker's entire history
            # behind a new access check, or leave a permanent unowned tier that
            # the multi-user cutover has to special-case forever.
            from .auth import configured_username
            owner = configured_username()
            if owner:
                conn.execute(text("UPDATE customs_job SET owner_key = :owner "
                                  "WHERE owner_key IS NULL OR owner_key = ''"),
                             {"owner": owner})
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_customs_job_owner_key "
                "ON customs_job (owner_key)"))


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
