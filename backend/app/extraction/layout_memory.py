"""Vendor layout memory — remembered table column maps per document role.

Recurring exporters print stable table layouts.  Every successful parser run
records its header-derived column map here; when a future document's header
rows are unreadable (bad scan), the parser retries with the remembered maps
instead of standing down entirely.  Safety does NOT rest on this store:
every row still has to arithmetic-verify (qty x price == total), so a wrong
remembered layout can never fabricate rows — it simply fails to confirm and
the LLM path takes over.

Storage: the ``vendor_layout`` table, one row per (role, header signature).

It was ``<storage_dir>/vendor_layouts.json`` until the store had to survive a
second process.  The file was read whole, edited in memory and rewritten whole,
so two processes recording a layout at the same moment each started from the
same snapshot and the second `os.replace` silently discarded what the first had
learned.  Atomic for one writer, lossy for two — and a local file is invisible
to another container regardless.  A row per key makes writers independent, and
the read-modify-write that genuinely remains (the counters) happens under a row
lock.  Any store failure still degrades to "no memory", never breaks extraction.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..database import side_store_session
from ..domain.enums import DeclaredRole
from ..models import VendorLayout

log = logging.getLogger("easycustoms.extraction")

_MIN_ROWS_TO_RECORD = 3


def stored_layouts(role: DeclaredRole) -> list[dict]:
    """Remembered column maps for ``role``, most-proven first.

    ``POSITIONAL`` entries are skipped: they could only ever have been written
    by a parse that itself came from memory (see :func:`record_layout`), and
    that key matches every headerless document of the role rather than one
    vendor's table — a self-confirming entry that gets stronger each time it is
    wrong.  Skipping on READ retires the ones already in the store.
    """
    try:
        with side_store_session() as db:
            rows = db.scalars(
                select(VendorLayout)
                .where(VendorLayout.role == role.value,
                       VendorLayout.header_signature.notin_(("", "POSITIONAL")))
                .order_by(VendorLayout.confirmed_rows.desc(), VendorLayout.docs.desc())
            ).all()
            return [r.mapping for r in rows if r.mapping]
    except Exception as e:                            # memory must never break extraction
        log.warning("vendor layout store read failed (%s)", e)
        return []


def record_layout(role: DeclaredRole, mapping: dict | None, header_signature: str | None,
                  vendor_hint: str | None, confirmed_rows: int) -> None:
    """Upsert a proven layout (keyed by role + header signature).

    A layout is only ever recorded from a parse that read the document's OWN
    header — a signature is required.  A parse WITHOUT one came from this store
    in the first place, so recording it is the store confirming itself: the
    2026-08-03 job stored a borrowed 6-column map under the vendor whose
    7-column invoice it had just misread, scored it on the 15 rows it got
    wrong, and left it keyed to match every future headerless invoice.
    """
    if not mapping or not header_signature or confirmed_rows < _MIN_ROWS_TO_RECORD:
        return
    # Two attempts, because SELECT-then-INSERT is a race a row lock cannot
    # close: `FOR UPDATE` locks a row, and for a layout nobody has recorded yet
    # there is no row to lock — so two extractions of the same new vendor both
    # find nothing and both insert.  One wins the unique constraint and the
    # other's evidence would just be dropped, which is the same silent loss the
    # JSON file had.  On the retry the row exists and takes the locked path.
    for attempt in (1, 2):
        try:
            with side_store_session(write=True) as db:
                entry = _locked_layout(db, role.value, header_signature)
                if entry is None:
                    db.add(VendorLayout(
                        role=role.value, header_signature=header_signature, mapping=mapping,
                        vendor_hint=vendor_hint, confirmed_rows=int(confirmed_rows), docs=1))
                else:
                    entry.mapping = mapping
                    # max(), not assignment: a later parse that confirmed fewer
                    # rows saw a shorter document, not a worse layout.
                    entry.confirmed_rows = max(int(entry.confirmed_rows or 0),
                                               int(confirmed_rows))
                    entry.docs = int(entry.docs or 0) + 1
                    if vendor_hint and not entry.vendor_hint:
                        entry.vendor_hint = vendor_hint
            return
        except IntegrityError:
            if attempt == 2:                          # pragma: no cover - defensive
                log.warning("vendor layout write lost a race twice for %s/%s",
                            role.value, header_signature)
                return
            continue
        except Exception as e:
            log.warning("vendor layout store write failed (%s)", e)
            return


def _locked_layout(db, role: str, signature: str) -> VendorLayout | None:
    """The row for this key, locked for update where the database can.

    The counters are a read-modify-write, so two writers recording the same
    layout at the same time would otherwise both read `docs=4` and both write
    `docs=5`.  `with_for_update` is what holds the row across both statements on
    Postgres; SQLite has no row lock to take and is covered instead by the
    process-local write lock in database.side_store_session — which is the whole
    scope there, SQLite being the single-process deployment by definition.
    """
    stmt = select(VendorLayout).where(VendorLayout.role == role,
                                      VendorLayout.header_signature == signature)
    if db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def import_legacy_json(path) -> int:
    """Carry a pre-table ``vendor_layouts.json`` into the database, once.

    Without this the cutover silently forgets every layout a deployment has
    proven — the store's whole value is that it accumulates, so dropping it is
    not a clean slate, it is a regression the reviewer notices as documents that
    used to parse deterministically going back to the LLM path.

    Returns the number of rows imported.  Never raises: a broken legacy file is
    a reason to start empty, not a reason to fail startup.
    """
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        layouts = data.get("layouts") if isinstance(data, dict) else None
        if not isinstance(layouts, list):
            return 0
    except FileNotFoundError:
        return 0
    except Exception as e:
        log.warning("legacy vendor layout file unreadable (%s) — not imported", e)
        return 0

    imported = 0
    try:
        with side_store_session(write=True) as db:
            for old in layouts:
                if not isinstance(old, dict):
                    continue
                role, sig = old.get("role"), old.get("header_signature")
                mapping = old.get("mapping")
                # The same rule the writer applies: a POSITIONAL entry is the
                # store confirming itself and must not survive the migration.
                if not role or not mapping or sig in (None, "", "POSITIONAL"):
                    continue
                if _locked_layout(db, role, sig) is not None:
                    continue                          # already carried over
                db.add(VendorLayout(
                    role=role, header_signature=sig, mapping=mapping,
                    vendor_hint=old.get("vendor_hint"),
                    confirmed_rows=int(old.get("confirmed_rows", 0) or 0),
                    docs=int(old.get("docs", 0) or 0)))
                imported += 1
    except Exception as e:
        log.warning("legacy vendor layout import failed (%s)", e)
        return 0
    return imported
