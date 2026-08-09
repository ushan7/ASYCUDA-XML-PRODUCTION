"""Vendor layout memory — remembered table column maps per document role.

Recurring exporters print stable table layouts.  Every successful parser run
records its header-derived column map here; when a future document's header
rows are unreadable (bad scan), the parser retries with the remembered maps
instead of standing down entirely.  Safety does NOT rest on this store:
every row still has to arithmetic-verify (qty x price == total), so a wrong
remembered layout can never fabricate rows — it simply fails to confirm and
the LLM path takes over.

Storage: ``<storage_dir>/vendor_layouts.json`` — tiny, human-inspectable,
written atomically.  Any store failure degrades to "no memory", never breaks
extraction.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from ..config import get_settings
from ..domain.enums import DeclaredRole

log = logging.getLogger("easycustoms.extraction")

_MIN_ROWS_TO_RECORD = 3


def _store_path():
    return get_settings().storage_dir / "vendor_layouts.json"


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("layouts"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("vendor layout store unreadable (%s) — starting empty", e)
    return {"version": 1, "layouts": []}


def stored_layouts(role: DeclaredRole) -> list[dict]:
    """Remembered column maps for ``role``, most-proven first.

    ``POSITIONAL`` entries are skipped: they could only ever have been written
    by a parse that itself came from memory (see :func:`record_layout`), and
    that key matches every headerless document of the role rather than one
    vendor's table — a self-confirming entry that gets stronger each time it is
    wrong.  Skipping on READ retires the ones already in the store.
    """
    try:
        layouts = [l for l in _load()["layouts"]
                   if l.get("role") == role.value and l.get("mapping")
                   and l.get("header_signature") not in (None, "", "POSITIONAL")]
        layouts.sort(key=lambda l: (-int(l.get("confirmed_rows", 0)), -int(l.get("docs", 0))))
        return [l["mapping"] for l in layouts]
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
    try:
        data = _load()
        sig = header_signature
        entry = next((l for l in data["layouts"]
                      if l.get("role") == role.value and l.get("header_signature") == sig), None)
        if entry is None:
            entry = {"role": role.value, "header_signature": sig, "mapping": mapping,
                     "vendor_hint": vendor_hint, "confirmed_rows": 0, "docs": 0}
            data["layouts"].append(entry)
        entry["mapping"] = mapping
        entry["confirmed_rows"] = max(int(entry.get("confirmed_rows", 0)), int(confirmed_rows))
        entry["docs"] = int(entry.get("docs", 0)) + 1
        if vendor_hint and not entry.get("vendor_hint"):
            entry["vendor_hint"] = vendor_hint
        entry["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

        path = _store_path()
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)                          # atomic on the same volume
    except Exception as e:
        log.warning("vendor layout store write failed (%s)", e)
