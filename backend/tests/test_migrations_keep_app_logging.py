"""Running a migration must not switch the application's logging off.

`alembic/env.py` calls `logging.config.fileConfig`, whose `disable_existing_loggers`
argument defaults to **True**: every logger that already exists and is not named
in `alembic.ini` is disabled outright.  `alembic.ini` names root, sqlalchemy and
alembic — so that set is all of `easycustoms.*`.

This is not confined to the `alembic` command line.  `app/database.py` stamps
and upgrades a SQLite database from inside `init_db()`, i.e. inside the API
process during startup, so the app came up and then ran with its own logging
dead for the life of the process — on a deployment's FIRST boot (fresh database
-> stamp) and on the first boot after any schema change (-> upgrade).  Those are
the two boots whose logs matter most.

The symptom is invisible by construction: nothing errors, log lines simply stop
existing.  It surfaced as a test in an unrelated file whose log assertion passed
alone and failed in the suite, because the suite builds a fresh SQLite database
and therefore runs a stamp.
"""
from __future__ import annotations

import logging
from logging.config import fileConfig

from app.database import _alembic_config

APP_LOGGERS = ("easycustoms.api", "easycustoms.db", "easycustoms.auth")


def _emits(logger: logging.Logger) -> bool:
    """Whether a record reaches a handler attached to this logger."""
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = _Capture()
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        logger.warning("probe")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return seen == ["probe"]


def test_app_loggers_survive_alembic_fileconfig():
    """The exact call env.py makes, against the real alembic.ini."""
    for name in APP_LOGGERS:
        assert _emits(logging.getLogger(name)), f"{name} was already disabled"

    ini = _alembic_config().config_file_name
    assert ini, "alembic.ini should be resolvable"
    fileConfig(ini, disable_existing_loggers=False)

    for name in APP_LOGGERS:
        assert _emits(logging.getLogger(name)), (
            f"{name} stopped emitting after a migration configured logging. "
            f"alembic/env.py must pass disable_existing_loggers=False — it runs "
            f"inside the API process on SQLite, not only from the command line.")


def test_env_py_passes_disable_existing_loggers_false():
    """Pinned as source, because the failure mode is silence: if someone drops
    the argument again, nothing raises and no test that merely runs a migration
    would notice."""
    import pathlib

    env = (pathlib.Path(__file__).resolve().parent.parent
           / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "disable_existing_loggers=False" in env, (
        "alembic/env.py calls fileConfig without disable_existing_loggers=False; "
        "its default of True disables every easycustoms.* logger for the life of "
        "the process that ran the migration.")
