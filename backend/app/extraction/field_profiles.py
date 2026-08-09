"""Vendor field profiles — learned per-vendor field-allocation defaults.

Sibling of ``layout_memory`` (which remembers column POSITIONS): this store
remembers per-vendor field FACTS confirmed by reviewers or by uniformly
extracted, finalized jobs.  Today it holds a single fact — the vendor's
default country of origin — because that is the one field with a live failure
mode: an exporter that TRADES goods made elsewhere (Medtronic Singapore
shipping Irish stents) makes the exporter-country fallback systematically
wrong, and the reviewer had to bulk-stamp 25 items to fix it.  Once fixed and
finalized ONCE, this store makes that correction permanent for the vendor.

Learning (``record_coo_observation``, called on successful finalize only):

* the observation is the finalized items' ITEM_LEVEL/reviewer-resolved COO —
  never the exporter fallback and never a previous profile value, so the store
  can never learn from its own output;
* an observation counts only when >= 80% of the job's items carry such a COO
  and >= 80% of those agree on one code;
* a reviewer-confirmed observation (bulk COO stamp or per-item COO edits)
  always wins and replaces; a merely observed one needs
  ``_MIN_OBSERVED_DOCS`` agreeing jobs before it is ever consumed, and a
  contradicting observation clears an observed default (the vendor is not
  uniform after all).  Observed contradictions never erase a reviewer-
  confirmed default.

Consumption (``coo_default_for``, used by ``rules.coo``): the default fills a
MISSING item COO *before* the exporter-country fallback, always with a
reviewer-visible warning — it proposes, never decides.

Storage: ``<storage_dir>/vendor_field_profiles.json`` — tiny, human-
inspectable, written atomically.  Any store failure degrades to "no profile",
never breaks a job.  Gated by ``vendor_field_profiles_enabled``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from ..config import get_settings

log = logging.getLogger("easycustoms.extraction")

_MIN_OBSERVED_DOCS = 2          # observed-only defaults need this many agreeing jobs
_MIN_SHARE = 0.8                # item agreement AND coverage threshold


def _store_path():
    return get_settings().storage_dir / "vendor_field_profiles.json"


def _norm_vendor(name: str | None) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return key or None


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("profiles"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("vendor field-profile store unreadable (%s) — starting empty", e)
    return {"version": 1, "profiles": []}


def _save(data: dict) -> None:
    path = _store_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, path)                              # atomic on the same volume


def coo_default_for(exporter_name: str | None) -> str | None:
    """The vendor's remembered default COO, or None when nothing is confident
    enough to propose: reviewer-confirmed always, observed-only after
    ``_MIN_OBSERVED_DOCS`` agreeing finalized jobs."""
    if not get_settings().vendor_field_profiles_enabled:
        return None
    key = _norm_vendor(exporter_name)
    if not key:
        return None
    try:
        entry = next((p for p in _load()["profiles"] if p.get("vendor") == key), None)
    except Exception as e:                             # profiles must never break a job
        log.warning("vendor field-profile read failed (%s)", e)
        return None
    if not entry or not entry.get("coo_default"):
        return None
    if entry.get("coo_source") == "REVIEWER":
        return str(entry["coo_default"])
    if int(entry.get("coo_docs", 0)) >= _MIN_OBSERVED_DOCS:
        return str(entry["coo_default"])
    return None


def record_coo_observation(exporter_name: str | None,
                           item_coos: list[tuple[str | None, str | None]],
                           *, reviewer_confirmed: bool) -> None:
    """Record one finalized job's COO picture for ``exporter_name``.

    ``item_coos`` is ``[(coo_alpha2, coo_source), ...]`` for EVERY finalized
    item; only ITEM_LEVEL values count as evidence (fallback- and profile-
    sourced values are the store's own echo and are ignored).
    """
    if not get_settings().vendor_field_profiles_enabled:
        return
    key = _norm_vendor(exporter_name)
    if not key or not item_coos:
        return
    evidenced = [c for c, src in item_coos if c and src == "ITEM_LEVEL"]
    if len(evidenced) < max(1, _MIN_SHARE * len(item_coos)):
        return                                        # too few items carry real COO
    top = max(set(evidenced), key=evidenced.count)
    if evidenced.count(top) < _MIN_SHARE * len(evidenced):
        return                                        # the vendor's items disagree
    try:
        data = _load()
        entry = next((p for p in data["profiles"] if p.get("vendor") == key), None)
        if entry is None:
            entry = {"vendor": key, "display": str(exporter_name or "").strip()[:120],
                     "coo_default": None, "coo_source": None, "coo_docs": 0}
            data["profiles"].append(entry)
        if reviewer_confirmed:
            if entry.get("coo_default") != top or entry.get("coo_source") != "REVIEWER":
                entry["coo_docs"] = 0
            entry["coo_default"], entry["coo_source"] = top, "REVIEWER"
            entry["coo_docs"] = int(entry.get("coo_docs", 0)) + 1
        elif entry.get("coo_source") == "REVIEWER":
            # observed evidence never erases a deliberate reviewer decision;
            # an agreeing observation still strengthens it
            if entry.get("coo_default") == top:
                entry["coo_docs"] = int(entry.get("coo_docs", 0)) + 1
        elif entry.get("coo_default") == top:
            entry["coo_docs"] = int(entry.get("coo_docs", 0)) + 1
            entry["coo_source"] = entry.get("coo_source") or "OBSERVED"
        elif entry.get("coo_default"):
            # observed contradiction: the vendor is not uniform — stop proposing
            entry["coo_default"], entry["coo_source"], entry["coo_docs"] = None, None, 0
        else:
            entry["coo_default"], entry["coo_source"], entry["coo_docs"] = top, "OBSERVED", 1
        entry["last_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _save(data)
    except Exception as e:                             # profiles must never break finalize
        log.warning("vendor field-profile write failed (%s)", e)
