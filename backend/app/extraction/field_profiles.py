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

Storage: the ``vendor_field_profile`` table, one row per normalised vendor key.
It was ``<storage_dir>/vendor_field_profiles.json``; see ``layout_memory`` for
why a whole-file read-modify-write could not survive a second process.  Any
store failure degrades to "no profile", never breaks a job.  Gated by
``vendor_field_profiles_enabled``.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import side_store_session
from ..models import VendorFieldProfile

log = logging.getLogger("easycustoms.extraction")

_MIN_OBSERVED_DOCS = 2          # observed-only defaults need this many agreeing jobs
_MIN_SHARE = 0.8                # item agreement AND coverage threshold


def _norm_vendor(name: str | None) -> str | None:
    key = re.sub(r"[^a-z0-9]", "", str(name or "").lower())
    return key or None


def _locked_profile(db, vendor: str) -> VendorFieldProfile | None:
    """The row for this vendor, locked for update where the database can.

    ``coo_docs`` is a counter and the source precedence is a state machine, so
    two finalizes for one vendor at the same moment must not both read the same
    "before" and both write their own "after".
    """
    stmt = select(VendorFieldProfile).where(VendorFieldProfile.vendor == vendor)
    if db.get_bind().dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


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
        with side_store_session() as db:
            entry = db.scalars(
                select(VendorFieldProfile).where(VendorFieldProfile.vendor == key)).first()
            if not entry or not entry.coo_default:
                return None
            if entry.coo_source == "REVIEWER":
                return str(entry.coo_default)
            if int(entry.coo_docs or 0) >= _MIN_OBSERVED_DOCS:
                return str(entry.coo_default)
            return None
    except Exception as e:                             # profiles must never break a job
        log.warning("vendor field-profile read failed (%s)", e)
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
    # Two attempts, because SELECT-then-INSERT is a race that a row lock cannot
    # close: `FOR UPDATE` locks a row, and the row does not exist yet, so two
    # finalizes for a NEW vendor both find nothing and both insert.  One wins on
    # the primary key and the other's observation would simply be dropped —
    # which is the same silent loss the JSON file had.  On the retry the row is
    # there, so it takes the normal locked-update path.
    for attempt in (1, 2):
        try:
            with side_store_session(write=True) as db:
                entry = _locked_profile(db, key)
                if entry is None:
                    entry = VendorFieldProfile(
                        vendor=key, display=str(exporter_name or "").strip()[:120],
                        coo_default=None, coo_source=None, coo_docs=0)
                    db.add(entry)
                _apply_coo_observation(entry, top, reviewer_confirmed=reviewer_confirmed)
            return
        except IntegrityError:
            if attempt == 2:                           # pragma: no cover - defensive
                log.warning("vendor field-profile write lost a race twice for %r", key)
                return
            continue
        except Exception as e:                         # profiles must never break finalize
            log.warning("vendor field-profile write failed (%s)", e)
            return


def _apply_coo_observation(entry: VendorFieldProfile, top: str, *,
                           reviewer_confirmed: bool) -> None:
    """The source-precedence state machine, applied to a locked (or new) row."""
    if reviewer_confirmed:
        if entry.coo_default != top or entry.coo_source != "REVIEWER":
            entry.coo_docs = 0
        entry.coo_default, entry.coo_source = top, "REVIEWER"
        entry.coo_docs = int(entry.coo_docs or 0) + 1
    elif entry.coo_source == "REVIEWER":
        # observed evidence never erases a deliberate reviewer decision;
        # an agreeing observation still strengthens it
        if entry.coo_default == top:
            entry.coo_docs = int(entry.coo_docs or 0) + 1
    elif entry.coo_default == top:
        entry.coo_docs = int(entry.coo_docs or 0) + 1
        entry.coo_source = entry.coo_source or "OBSERVED"
    elif entry.coo_default:
        # observed contradiction: the vendor is not uniform — stop proposing
        entry.coo_default, entry.coo_source, entry.coo_docs = None, None, 0
    else:
        entry.coo_default, entry.coo_source, entry.coo_docs = top, "OBSERVED", 1


def import_legacy_json(path) -> int:
    """Carry a pre-table ``vendor_field_profiles.json`` into the database, once.

    A dropped profile is not a clean slate: it is the reviewer's deliberate COO
    correction being forgotten, and the next shipment from that vendor silently
    going back to the exporter-country fallback that was wrong in the first
    place.  Never raises — a broken legacy file means start empty, not fail boot.
    """
    import json

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list):
            return 0
    except FileNotFoundError:
        return 0
    except Exception as e:
        log.warning("legacy vendor field-profile file unreadable (%s) — not imported", e)
        return 0

    imported = 0
    try:
        with side_store_session(write=True) as db:
            for old in profiles:
                if not isinstance(old, dict):
                    continue
                vendor = old.get("vendor")
                if not vendor or _locked_profile(db, vendor) is not None:
                    continue
                db.add(VendorFieldProfile(
                    vendor=vendor, display=str(old.get("display") or "")[:120],
                    coo_default=old.get("coo_default"), coo_source=old.get("coo_source"),
                    coo_docs=int(old.get("coo_docs", 0) or 0)))
                imported += 1
    except Exception as e:
        log.warning("legacy vendor field-profile import failed (%s)", e)
        return 0
    return imported
