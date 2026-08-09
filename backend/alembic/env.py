"""Alembic environment — one database name, taken from the app's own config.

The URL is NOT in alembic.ini.  It comes from EASYCUSTOMS_DATABASE_URL via
app.config, so `alembic upgrade head` can only ever migrate the database the
API and the worker actually serve.  A second copy of the URL in a second file
is how a schema gets upgraded on a database nobody is reading.

`target_metadata` is the live model metadata, which is what makes
`alembic revision --autogenerate` able to diff the models against a database.
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# backend/ on the path: alembic runs from backend/, but not as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402,F401  (imported for its side effect: registers the tables)
from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The database to migrate: EASYCUSTOMS_DATABASE_URL, unless overridden.

    alembic.ini leaves sqlalchemy.url empty so a normal `alembic upgrade head`
    can only reach the database the app itself serves.  Setting it explicitly —
    programmatically, or with `-x`/`--` on the command line — targets something
    else on purpose, which is what tests/test_migrations.py does with a throwaway
    file, and what migrating a restored copy by hand needs.
    """
    override = (config.get_main_option("sqlalchemy.url") or "").strip()
    if override:
        return override
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "EASYCUSTOMS_DATABASE_URL is empty — nothing to migrate.")
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`).

    Useful when a DBA applies the change by hand, or to read what a revision
    would do before letting it near production data.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against a live connection.

    A dedicated engine rather than app.database's: NullPool means this process
    opens one connection, applies the DDL and lets go, which matters when the
    migration runs as a deploy step next to a live API holding its own pool.

    render_as_batch is on for SQLite, whose ALTER TABLE cannot drop or alter a
    column — batch mode rewrites the table instead.  Postgres ignores it.
    """
    url = _database_url()
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
