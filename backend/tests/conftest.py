"""Suite-wide setup: every route is behind the login gate (app/auth.py).

Three jobs, all done before any test module is imported (pytest loads conftest
first), because `Settings` is read once and `get_settings` is lru_cached:

  1. configure an account, so the app under test is a CONFIGURED deployment
     rather than the fail-closed one;
  2. make every TestClient send that account's token by default, so the ~60
     existing modules keep exercising what they were written to exercise
     instead of asserting 401 sixty times over;
  3. give the run its OWN database, and delete it afterwards.

A test that wants an ANONYMOUS client drops the header itself:

    client.headers.pop("Authorization", None)

which is what tests/test_auth.py does to prove the gate actually refuses.
"""
import os
import shutil
import tempfile
import time

import pytest

os.environ.setdefault("EASYCUSTOMS_AUTH_USERNAME", "pytest-operator")
os.environ.setdefault("EASYCUSTOMS_AUTH_PASSWORD", "pytest-password")
# Fixed secret: tokens minted here must verify inside the app under test.
os.environ.setdefault("EASYCUSTOMS_AUTH_SECRET", "pytest-secret-not-for-deployment")
# The suite drives extraction through the HTTP route with replay fixtures.
os.environ.setdefault("EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS", "true")


# --------------------------------------------------------------------------- #
# One database per RUN, in a directory of its own, removed at the end.
#
# It used to be one fixed path — <tempdir>/ec_pytest.db — that ~30 modules each
# named for themselves with os.environ.setdefault, and that nothing ever
# deleted.  Runs therefore accumulated into one another: rows from every suite
# run this machine had ever done were still there for the next one to read.
#
# What made that expensive to diagnose is that it never failed as "stale data".
# It failed as a suite that got slower every run (291s -> 444s -> 501s -> 629s
# across four runs of identical code) and that failed a DIFFERENT single test
# each time — whichever listing or pagination test happened to read the
# accumulated rows in an order it did not expect.  By the time it was noticed
# the file held 1931 jobs and 941 MB.  One run leaves ~134 MB behind, so this
# is not a slow leak.
#
# Assigned, not setdefault: no module may pin the old shared file.  An
# EASYCUSTOMS_DATABASE_URL already in the environment still wins, which is what
# lets someone point the suite at Postgres deliberately.
# --------------------------------------------------------------------------- #
_STALE_DB_PREFIX = "ec-pytest-"
_db_dir = None


def _sweep_abandoned_databases(keep):
    """Remove run directories left behind by a crash or a hard kill.

    Only ones older than a day: a suite running RIGHT NOW in another process
    (or an xdist worker beside this one) owns a fresh directory, and sweeping
    that away would break a passing run to tidy up after a dead one.
    """
    cutoff = time.time() - 86_400
    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(_STALE_DB_PREFIX):
            continue
        path = os.path.join(root, name)
        if path == keep:
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


if not os.environ.get("EASYCUSTOMS_DATABASE_URL"):
    _db_dir = tempfile.mkdtemp(prefix=_STALE_DB_PREFIX)
    # Forward slashes: this goes into a sqlite:/// URL, where a Windows
    # backslash is an escape character, not a separator.
    _db_file = os.path.join(_db_dir, "ec_pytest.db").replace("\\", "/")
    os.environ["EASYCUSTOMS_DATABASE_URL"] = f"sqlite:///{_db_file}"
    _sweep_abandoned_databases(_db_dir)

    # ...and the STORAGE directory with it, for the same reason and one more.
    #
    # The suite was writing its uploads into backend/storage/ — the real one,
    # beside 3600+ job directories of actual customer paperwork. Nothing failed,
    # which is why it lasted: the run just left more litter in the one directory
    # on this machine that must not be casually written to. (test_repo_hygiene
    # catches those files becoming TRACKED; it cannot see them being created.)
    #
    # It also stopped being merely untidy once the vendor layout/profile stores
    # moved into the database: startup imports any legacy JSON it finds there,
    # so a developer's real remembered vendors were being loaded into the test
    # database and counted by tests that assert nothing was learned.
    #
    # Set inside this branch on purpose: someone pointing the suite at their own
    # DATABASE_URL is running it against a real deployment's data and should get
    # that deployment's storage too.
    os.environ.setdefault("EASYCUSTOMS_STORAGE_DIR", os.path.join(_db_dir, "storage"))


def pytest_sessionfinish(session, exitstatus):
    """Delete this run's database directory.

    The engine holds the file open and Windows refuses to unlink an open file,
    so drop the connection pool first.  Best effort throughout: a leftover
    directory is wasted temp space, never a failed run — and because every run
    now gets its own, a leftover can no longer change what the NEXT run sees,
    which was the actual defect.  _sweep_abandoned_databases collects it later.
    """
    if _db_dir is None:                      # caller supplied their own database
        return
    try:
        from app.database import engine
        engine.dispose()
    except Exception:                        # never fail a green run on cleanup
        pass
    shutil.rmtree(_db_dir, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402  (after the env above)

from app import auth  # noqa: E402

_original_init = TestClient.__init__


def _authenticated_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    token, _ = auth.issue_token(auth.configured_username() or "pytest-operator")
    self.headers["Authorization"] = f"Bearer {token}"


# Patched on the class, not injected per fixture: the existing modules build
# their own TestClient in module-scoped fixtures that this file cannot reach.
TestClient.__init__ = _authenticated_init


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    """One module's deliberate bad logins must not throttle the next module's.

    The failure window is a database table now (a per-process dict allowed N
    times the guess rate on an N-process deployment), so this needs a schema —
    but most modules never build one, and creating it for all ~1900 tests to
    serve the handful that sign in badly would be pure cost.  Best effort: if
    there is no table yet, there are no failures to clear either.
    """
    def _clear() -> None:
        try:
            auth.reset_throttle()
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture
def isolated_vendor_stores():
    """Empty the vendor layout / field-profile tables around a test.

    These two stores were JSON files under ``storage/``, and tests isolated them
    by pointing ``storage_dir`` at a tmp_path.  They are database tables now
    (they had to be: a whole-file read-modify-write silently loses one writer's
    entries when a second process records at the same moment), so isolation is
    per-TABLE rather than per-directory.

    It still matters for the same reason it did before: a layout remembered by
    an earlier test would non-deterministically feed itself into a later
    headerless-page case, which is precisely the parse those tests assert stands
    down.  Cleared on the way in as well as out, so a test is unaffected by
    whatever ran before it.
    """
    from app.database import SessionLocal, init_db
    from app.models import VendorFieldProfile, VendorLayout

    # The parser test modules never build a schema — they exercise pure parsing
    # and, until these stores were files, needed no database at all.  init_db is
    # idempotent (create_all, then a stamp/upgrade that no-ops once at head), so
    # the first test through here pays for it and the rest do not.
    init_db()

    def _clear() -> None:
        db = SessionLocal()
        try:
            db.query(VendorLayout).delete()
            db.query(VendorFieldProfile).delete()
            db.commit()
        finally:
            db.close()

    _clear()
    yield
    _clear()
