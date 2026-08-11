"""The per-job lock has to hold across PROCESSES, not just across threads.

`job_lock` used to be a module-level `dict[str, threading.Lock]` with a comment
stating the assumption it rested on: "the deployment is a single uvicorn
process".  That assumption is what capped the app at `--workers 1`.  With two
workers — let alone two containers behind a load balancer — a finalize and an
hs-review on the SAME declaration ran with no mutual exclusion at all, and the
loser's read-modify-write silently overwrote the winner's.

On Postgres the lock is now a transaction-scoped advisory lock.  These tests
pin the two properties that make that safe, neither of which the SQLite test
suite can reach on its own:

  * the SQL we emit is the transaction-scoped function (`pg_advisory_xact_lock`),
    not the session-scoped one — session-scoped locks survive the rollback that
    a crashed request needs to release them, and break behind a transaction-mode
    connection pooler;
  * a lock_timeout expiry is reported as job contention, while any OTHER
    database fault keeps propagating as itself.

The SQLite path keeps the in-process lock, and gains an eviction rule: the dict
had no removal at all, so it grew by one entry per job id for the life of the
process.
"""
from __future__ import annotations

import threading

import pytest
from sqlalchemy.exc import DBAPIError

from app import services
from app.config import Settings
from app.domain.errors import BlockingValidationError


# --------------------------------------------------------------------------- #
# A session that reports a dialect and records what was executed.  Enough to
# assert the SQL contract without a live Postgres in the test environment.
# --------------------------------------------------------------------------- #
class _Dialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _Bind:
    def __init__(self, name: str) -> None:
        self.dialect = _Dialect(name)


class FakeSession:
    def __init__(self, dialect: str = "postgresql", raise_on_lock: Exception | None = None):
        self._bind = _Bind(dialect)
        self._raise = raise_on_lock
        self.executed: list[tuple[str, dict | None]] = []

    def get_bind(self):
        return self._bind

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if self._raise is not None and "pg_advisory_xact_lock" in sql:
            raise self._raise
        return None

    def sql(self) -> str:
        return "\n".join(s for s, _ in self.executed)


def _dbapi_error(sqlstate: str) -> DBAPIError:
    class _Orig(Exception):
        pass

    orig = _Orig("lock timeout")
    orig.sqlstate = sqlstate
    return DBAPIError("SELECT pg_advisory_xact_lock(...)", {}, orig)


# --------------------------------------------------------------------------- #
# Postgres path
# --------------------------------------------------------------------------- #
def test_postgres_takes_a_transaction_scoped_advisory_lock():
    db = FakeSession("postgresql")
    with services.job_lock(db, "job-abc"):
        pass
    sql = db.sql()
    assert "pg_advisory_xact_lock" in sql, "the job lock must be taken on Postgres"
    assert "pg_advisory_lock(" not in sql, (
        "a SESSION-scoped advisory lock would survive the rollback that releases a "
        "crashed request's lock, and is unsafe behind a transaction-mode pooler")


def test_the_lock_key_is_namespaced_and_derived_from_the_job_id():
    db = FakeSession("postgresql")
    with services.job_lock(db, "job-abc"):
        pass
    sql, params = db.executed[-1]
    assert "hashtext" in sql
    assert params == {"ns": services._ADVISORY_LOCK_NAMESPACE, "job": "job-abc"}
    # int4: a wider namespace would not fit pg_advisory_xact_lock(int4, int4)
    assert 0 < services._ADVISORY_LOCK_NAMESPACE <= 2_147_483_647


def test_no_lock_timeout_is_set_when_the_setting_is_off(monkeypatch):
    """0 (the default) means "wait", which is what the in-process lock did."""
    monkeypatch.setattr(services, "get_settings",
                        lambda: Settings(_env_file=None, job_lock_timeout_seconds=0))
    db = FakeSession("postgresql")
    with services.job_lock(db, "job-abc"):
        pass
    assert "lock_timeout" not in db.sql()


def test_a_configured_timeout_is_set_transaction_locally(monkeypatch):
    monkeypatch.setattr(services, "get_settings",
                        lambda: Settings(_env_file=None, job_lock_timeout_seconds=5))
    db = FakeSession("postgresql")
    with services.job_lock(db, "job-abc"):
        pass
    sql, params = db.executed[0]
    assert "set_config" in sql and "lock_timeout" in sql
    # is_local=true: the timeout must not leak onto the next request that
    # borrows this pooled connection.
    assert "true" in sql
    assert params == {"ms": "5000"}


def test_a_lock_timeout_is_reported_as_job_contention(monkeypatch):
    monkeypatch.setattr(services, "get_settings",
                        lambda: Settings(_env_file=None, job_lock_timeout_seconds=5))
    db = FakeSession("postgresql", raise_on_lock=_dbapi_error("55P03"))
    with pytest.raises(BlockingValidationError) as caught:
        with services.job_lock(db, "job-abc"):
            pass
    # 409 through main.py's BlockingValidationError handler, with a sentence the
    # reviewer can act on — not a 500.
    assert caught.value.message.code == "JOB_BUSY"


def test_any_other_database_fault_still_propagates(monkeypatch):
    """Only 55P03 means contention.  A connection fault reported as "another
    change is running" would send the reviewer to wait for nothing."""
    monkeypatch.setattr(services, "get_settings",
                        lambda: Settings(_env_file=None, job_lock_timeout_seconds=5))
    db = FakeSession("postgresql", raise_on_lock=_dbapi_error("08006"))
    with pytest.raises(DBAPIError):
        with services.job_lock(db, "job-abc"):
            pass


def test_sqlstate_is_read_from_either_driver_spelling():
    psycopg3 = _dbapi_error("55P03")
    assert services._sqlstate(psycopg3) == "55P03"

    class _Orig2(Exception):
        pgcode = "55P03"                      # psycopg2

    assert services._sqlstate(DBAPIError("s", {}, _Orig2())) == "55P03"


# --------------------------------------------------------------------------- #
# SQLite path — the in-process lock, and the leak it used to have
# --------------------------------------------------------------------------- #
def test_sqlite_uses_the_in_process_lock_and_touches_no_sql():
    db = FakeSession("sqlite")
    with services.job_lock(db, "job-sqlite"):
        pass
    assert db.executed == [], "SQLite has no advisory locks to take"


def test_the_lock_table_does_not_grow_without_bound():
    """One entry per job id, never removed, was a leak keyed by every job the
    process had ever opened."""
    before = len(services._JOB_LOCKS)
    for i in range(200):
        with services._in_process_job_lock(f"job-{i}"):
            pass
    assert len(services._JOB_LOCKS) == before
    assert len(services._JOB_LOCK_WAITERS) == 0


def test_an_entry_survives_while_another_thread_is_waiting_for_it():
    """Eviction is refcounted, not "delete on release": dropping the entry while
    a second thread was blocked on it would hand the next caller a DIFFERENT
    lock object and let both run the critical section at once."""
    held = threading.Event()
    release = threading.Event()
    observed: list[int] = []

    def first():
        with services._in_process_job_lock("contended"):
            held.set()
            release.wait(5)

    def second():
        held.wait(5)
        observed.append(len(services._JOB_LOCKS))   # entry must still be there
        with services._in_process_job_lock("contended"):
            pass

    t1, t2 = threading.Thread(target=first), threading.Thread(target=second)
    t1.start()
    t2.start()
    held.wait(5)
    # while one holds and one waits, exactly one entry exists for the job
    assert services._JOB_LOCK_WAITERS.get("contended") == 2
    release.set()
    t1.join(5)
    t2.join(5)
    assert "contended" not in services._JOB_LOCKS
    assert "contended" not in services._JOB_LOCK_WAITERS


def test_the_entry_is_released_when_the_body_raises():
    with pytest.raises(RuntimeError):
        with services._in_process_job_lock("boom"):
            raise RuntimeError("boom")
    assert "boom" not in services._JOB_LOCKS
    assert "boom" not in services._JOB_LOCK_WAITERS


def test_the_lock_actually_serialises_two_threads():
    """The property the whole mechanism exists for."""
    inside = []
    overlaps = []

    def worker():
        for _ in range(50):
            with services._in_process_job_lock("serialise"):
                inside.append(1)
                if len(inside) > 1:
                    overlaps.append(len(inside))
                inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert overlaps == []
