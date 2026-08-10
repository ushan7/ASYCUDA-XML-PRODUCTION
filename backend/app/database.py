"""SQLAlchemy engine + session.

DB-agnostic: the same models run on SQLite (default, zero setup) and Postgres
(set EASYCUSTOMS_DATABASE_URL).  JSON payloads use the portable JSON type.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger("easycustoms.db")

# The revision a database built by create_all BEFORE migrations existed is
# already at.  Named once because two places need it and they must not drift:
# the Postgres instructions below, and the SQLite self-migration.
BASELINE_REVISION = "fa843d18c61e"


class Base(DeclarativeBase):
    pass


settings = get_settings()

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}

# Pool sizing is DELIBERATE on a server database and left alone on SQLite.
#
# On SQLite the bound is the single writer, not the pool, and the pragmas below
# are what make concurrency work; passing pool sizes there would only add
# connections contending for the same write lock.
#
# On Postgres the pool is the one setting whose wrong value fails in aggregate
# rather than locally — see the note on Settings.db_pool_size.  It is spelled
# out here so that the number of connections one process can hold is a value
# someone chose, not a library default nobody read.
_pool_args = {} if _is_sqlite else {
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    "pool_recycle": settings.db_pool_recycle_seconds,
    "pool_pre_ping": settings.db_pool_pre_ping,
}
engine = create_engine(settings.database_url, connect_args=_connect_args,
                       future=True, **_pool_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# --------------------------------------------------------------------------- #
# A SEPARATE, deliberately tiny pool for best-effort side stores.
#
# The vendor layout and field-profile stores are consulted from `app/rules/` and
# `app/pipeline.py`, which hold no session and must not start to: they are the
# deterministic layer, and threading a Session into it to reach a cache is
# exactly the coupling that keeps those functions pure and testable.  So those
# stores open their own session — and that is a NESTED checkout, taken while the
# request's own transaction is already holding a connection.
#
# On the main pool that is a deadlock waiting for peak load: `threadpool_max_threads`
# requests can each hold one connection and each want a second, and the pool has
# no more to give.  Nothing recovers; every thread waits out pool_timeout.
#
# Its own pool makes that impossible.  Small, because these are single-row reads
# and upserts measured in milliseconds; and fail-FAST, because a side store that
# cannot get a connection must degrade to "no memory" immediately (every caller
# already treats any failure that way) rather than stall a declaration for 30
# seconds to consult a cache.
# --------------------------------------------------------------------------- #
_SIDE_POOL_SIZE = 1
_SIDE_POOL_MAX_OVERFLOW = 2
_SIDE_POOL_TIMEOUT_SECONDS = 2

_side_pool_args = {} if _is_sqlite else {
    "pool_size": _SIDE_POOL_SIZE,
    "max_overflow": _SIDE_POOL_MAX_OVERFLOW,
    "pool_timeout": _SIDE_POOL_TIMEOUT_SECONDS,
    "pool_recycle": settings.db_pool_recycle_seconds,
    "pool_pre_ping": settings.db_pool_pre_ping,
}
side_engine = create_engine(settings.database_url, connect_args=_connect_args,
                            future=True, **_side_pool_args)
SideSessionLocal = sessionmaker(bind=side_engine, autoflush=False,
                                expire_on_commit=False, future=True)


# Serialises side-store WRITES on SQLite.  Those writes are read-modify-writes
# (counters, and the COO source state machine), which take two statements and so
# need something held across both.  Postgres has that: the stores select their
# row FOR UPDATE.  SQLite has no row lock to take, and its file lock is only
# held for the write itself — so two threads can both read docs=2 and both
# write 3, losing one.
#
# A process-local lock is the whole scope on SQLite, because SQLite IS the
# single-process deployment: one file, one writer, a broker's laptop.  Same
# split, for the same reason, as services.job_lock.
_SIDE_WRITE_LOCK = threading.Lock()


@contextmanager
def side_store_session(*, write: bool = False) -> Iterator[Session]:
    """A short, independent transaction for a best-effort side store.

    Pass ``write=True`` for a read-modify-write, which is what the lock above
    serialises on SQLite.  Reads do not take it: a cache lookup must never wait
    behind a writer.

    Independent also means the write is NOT rolled back with the caller's own
    transaction.  For these stores that is the right trade and worth stating:
    they are proposal-only caches (a remembered layout still has to
    arithmetic-verify every row; a remembered COO is only ever offered with a
    reviewer-visible warning), so learning from a job whose finalize later failed
    costs a re-verified proposal, not a wrong declared value.
    """
    with (_SIDE_WRITE_LOCK if (write and _is_sqlite) else nullcontext()):
        session = SideSessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


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

    SQLite builds itself, and now MIGRATES itself.  ``create_all`` raises a new
    file at the current models, and ``_apply_sqlite_migrations`` carries an
    existing one forward — a broker's laptop still needs no migration command
    to open the app, which is the point of the SQLite path.

    That second half is not optional, and its absence was a real outage rather
    than a theoretical one: ``_migrate_sqlite`` was frozen at the alembic
    baseline on the stated understanding that "new columns go in an alembic
    revision, which serves both", but nothing on the SQLite path ever RAN a
    revision.  So the first column added after the freeze was created on fresh
    files and missing on every existing one, and the app came up and then
    failed on the first query touching it — at startup, against a database
    holding real work.

    Postgres is CHECKED, never built.  ``create_all`` creates missing tables
    but never ALTERs an existing one: bringing up an API against a Postgres
    database silently missing a column added since it was created surfaces
    later as a query error on a reviewer's screen.  Refusing at startup, with
    the command that fixes it, is the same trade auth.py makes — a
    misconfigured deployment must be visibly broken rather than quietly wrong.
    SQLite may self-migrate where Postgres may not because the file is local,
    single-tenant and the app's own: there is no second writer to race and no
    DBA whose change window this would run inside.
    """
    from . import models  # noqa: F401  (register models)

    if settings.database_url.startswith("sqlite"):
        # The question is whether this database already HAS the application's
        # schema — not whether a file is present at the path.  Those differ, and
        # both mistakes are real:
        #
        #   * always create_all, then replay the ladder: on an existing database
        #     create_all silently creates any table added since that database
        #     was made, and the revision that creates the same table then fails
        #     with "table already exists".  Latent until a revision added a
        #     TABLE — every earlier one added a column, and create_all never
        #     ALTERs, so nothing caught it for two revisions;
        #   * skip create_all whenever the FILE exists: an empty file (a stopped
        #     first run, `touch`, a wiped volume) is then treated as a
        #     pre-alembic database, stamped at the baseline, and the upgrade
        #     ALTERs tables that were never created.
        #
        # Asking the schema itself distinguishes the three states that actually
        # exist, and each needs different treatment.
        has_schema = _sqlite_has_schema()
        if not has_schema:
            # Fresh: built from the models, which puts it at head — and
            # _apply_sqlite_migrations stamps it there rather than replaying.
            Base.metadata.create_all(bind=engine)
        else:
            # Existing: carried forward by the ladder alone.  (_migrate_sqlite
            # is the pre-alembic column ladder; it no-ops once past that.)
            _migrate_sqlite()
        _apply_sqlite_migrations(pre_existing=has_schema)
        import_legacy_side_stores()
        return
    _require_schema_at_head()
    import_legacy_side_stores()


def import_legacy_side_stores() -> None:
    """Carry the pre-table vendor JSON stores into the database.

    The layout and field-profile stores used to be files under ``storage/``.
    Dropping their contents at the cutover is not a clean slate: a proven layout
    going missing sends documents that used to parse deterministically back to
    the LLM path, and a missing field profile silently reinstates the exporter-
    country COO fallback that a reviewer had already corrected by hand.

    Idempotent by key rather than by a marker file, and deliberately so: it
    re-reads the (tiny) files every boot and skips anything already present, so
    there is no rename to fail, no marker to lose, and no half-migrated state if
    a boot is interrupted.  Delete the JSON files once you are satisfied to
    finish the cutover.

    Racy only in the harmless direction: two instances starting together may both
    try to insert the same key, the unique constraint refuses one, and that
    importer logs and gives up on a store the other has just filled.
    """
    for name, importer in (("vendor_layouts.json", "layout_memory"),
                           ("vendor_field_profiles.json", "field_profiles")):
        path = settings.storage_dir / name
        if not path.exists():
            continue
        try:
            module = __import__(f"app.extraction.{importer}", fromlist=["import_legacy_json"])
            count = module.import_legacy_json(path)
        except Exception as e:                        # never block startup for a cache
            log.warning("could not import legacy %s (%s) — the store starts empty", name, e)
            continue
        if count:
            log.info("imported %s remembered entries from legacy %s; delete the file "
                     "once you are satisfied, the database is authoritative now",
                     count, name)


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
            f"    cd backend && alembic stamp {BASELINE_REVISION} && alembic upgrade head\n"
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


def _sqlite_has_schema() -> bool:
    """Whether this database already holds the application's tables.

    ``customs_job`` is the probe because it has existed since the baseline, so
    its presence means "some version of this app built this database" — which is
    the fact ``init_db`` branches on.  Deliberately not a file-existence check:
    an empty file is a FRESH database that happens to have a path, and treating
    it as an existing one stamps a schema that is not there.
    """
    from sqlalchemy import inspect

    with engine.connect() as conn:
        return inspect(conn).has_table("customs_job")


def _alembic_config():
    from alembic.config import Config

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    config = Config(str(ini))
    # script_location is relative to backend/ in the ini; absolute so this works
    # whichever directory the process was started from.
    config.set_main_option("script_location", str(ini.parent / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def _apply_sqlite_migrations(*, pre_existing: bool) -> None:
    """Bring a SQLite file to the migration head, without anyone typing a command.

    Three states, and the version table tells them apart:

      * already stamped -> upgrade (a no-op when it is current);
      * unstamped and BRAND NEW -> create_all built it at head, so stamp head;
      * unstamped and PRE-EXISTING -> it predates alembic, so stamp the
        baseline (``_migrate_sqlite`` has just carried it there) and upgrade.

    Errors are raised, never swallowed.  A database that could not be migrated
    is one whose next query fails on a missing column, and doing that to a
    declaration workspace at some later, arbitrary moment is strictly worse
    than refusing to start.
    """
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = _alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()

    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    if current is None:
        stamp_at = BASELINE_REVISION if pre_existing else head
        command.stamp(config, stamp_at)
        log.info("sqlite database stamped at %s (%s)", stamp_at,
                 "pre-existing file" if pre_existing else "newly created")
        current = stamp_at

    if current != head:
        log.info("upgrading sqlite database %s -> %s", current, head)
        command.upgrade(config, "head")


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
