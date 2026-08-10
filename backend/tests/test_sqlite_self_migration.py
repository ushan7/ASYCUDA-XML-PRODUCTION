"""An existing SQLite file is carried to the migration head at startup.

``create_all`` creates missing TABLES and never ALTERs an existing one, so on
the SQLite path a column added after a database was created simply is not
there.  The app still comes up — and then fails on the first query that names
the column, which is startup itself (recover_interrupted_extractions), against
a file holding real declarations.

That is not hypothetical: it is what the first column added after
``_migrate_sqlite`` was frozen actually did.  The freeze was correct — per
column hand-written ALTERs diverge from the migrations that Postgres runs —
but it left the SQLite path with no way to apply the revisions it was told to
put new columns in.  This closes that, so the two backends run the SAME
revisions and only differ in who triggers them.

The state that needs the most care is a PRE-ALEMBIC file (a broker's laptop
from before migrations existed): unstamped, but NOT at head, so it must be
stamped at the baseline and upgraded rather than simply stamped where it
stands.  Getting that backwards replays an ADD COLUMN against a column that is
already there.
"""
import sqlite3

import pytest
from sqlalchemy import create_engine, text

from app import database


def _columns(path, table="uploaded_document"):
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _alembic_version(path):
    con = sqlite3.connect(path)
    try:
        return [r[0] for r in con.execute("select * from alembic_version")]
    except sqlite3.OperationalError:
        return None                      # no version table: pre-alembic
    finally:
        con.close()


@pytest.fixture
def pre_alembic_db(tmp_path, monkeypatch):
    """A database as it stood before migrations existed: baseline schema, no
    version table, and a row in it that must survive."""
    from alembic import command

    path = tmp_path / "legacy.db"
    url = f"sqlite:///{str(path).replace(chr(92), '/')}"

    monkeypatch.setattr(database.settings, "database_url", url, raising=False)
    config = database._alembic_config()
    command.upgrade(config, database.BASELINE_REVISION)      # schema as of the baseline
    engine = create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))     # ...as if alembic never ran
        conn.execute(text(
            "INSERT INTO customs_job (id, status, version, rule_set_version, exchange_rate, "
            "created_by, owner_key, created_at, updated_at) VALUES "
            "('job-1', 'UPLOADING', 1, '', '', '', '', '2026-01-01', '2026-01-01')"))
        conn.execute(text(
            "INSERT INTO uploaded_document (id, job_id, declared_role, "
            "upload_index_within_role, original_file_name, content_type, byte_size, "
            "sha256, storage_key, status, created_at) VALUES "
            "('doc-1', 'job-1', 'INVOICE', 0, 'invoice.pdf', 'application/pdf', 10, "
            "'abc', '/tmp/x.pdf', 'EXTRACTED', '2026-01-01')"))

    monkeypatch.setattr(database, "engine", engine, raising=False)
    yield path
    engine.dispose()


def test_pre_alembic_file_is_stamped_at_the_baseline_and_upgraded(pre_alembic_db):
    assert _alembic_version(pre_alembic_db) is None
    assert "extraction_provenance" not in _columns(pre_alembic_db)

    database.init_db()

    from alembic.script import ScriptDirectory
    head = ScriptDirectory.from_config(database._alembic_config()).get_current_head()
    assert _alembic_version(pre_alembic_db) == [head]
    assert "extraction_provenance" in _columns(pre_alembic_db)


def test_the_existing_rows_survive_and_are_backfilled(pre_alembic_db):
    """A migration that empties a broker's workspace to add a column would be
    worse than the missing column."""
    database.init_db()

    con = sqlite3.connect(pre_alembic_db)
    try:
        rows = list(con.execute(
            "select id, original_file_name, extraction_provenance from uploaded_document"))
    finally:
        con.close()
    # OCR is a claim about these rows, not a filler: before this column existed
    # the only non-OCR path was the fixture hook, which is refused unless a
    # deployment turns it on.
    assert rows == [("doc-1", "invoice.pdf", "OCR")]


def test_running_it_twice_is_a_no_op(pre_alembic_db):
    """Startup happens on every restart, and --reload makes that often."""
    database.init_db()
    first = _alembic_version(pre_alembic_db)
    database.init_db()
    assert _alembic_version(pre_alembic_db) == first


def test_a_brand_new_file_is_stamped_at_head_not_replayed(tmp_path, monkeypatch):
    """create_all raises a NEW file at the current models, so its columns are
    already there.  Stamping it at the baseline instead would then try to add a
    column that exists — which is why 'did this file exist' is the thing the
    decision turns on, not 'is it stamped'."""
    path = tmp_path / "fresh.db"
    url = f"sqlite:///{str(path).replace(chr(92), '/')}"
    monkeypatch.setattr(database.settings, "database_url", url, raising=False)
    engine = create_engine(url, future=True)
    monkeypatch.setattr(database, "engine", engine, raising=False)
    try:
        database.init_db()                      # must not raise
        from alembic.script import ScriptDirectory
        head = ScriptDirectory.from_config(database._alembic_config()).get_current_head()
        assert _alembic_version(path) == [head]
        assert "extraction_provenance" in _columns(path)
    finally:
        engine.dispose()
