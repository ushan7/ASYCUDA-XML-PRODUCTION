"""The vendor memory stores had to stop being files.

Both were whole-file read-modify-write: load the JSON, edit it in memory,
``os.replace`` it back.  That is atomic for ONE writer and lossy for two — two
processes recording at the same moment each started from the same snapshot, and
the second write silently discarded what the first had learned.  A local file is
also invisible to a second container by construction.  Together those made the
stores one of the reasons the app was pinned to `--workers 1`.

What these tests pin:
  * concurrent writers ACCUMULATE instead of overwriting each other;
  * the upsert stays one row per key, and the counters are not lost;
  * a deployment's existing JSON is carried into the tables rather than dropped
    (losing it is not a clean slate — it sends documents that used to parse
    deterministically back to the LLM path, and reinstates an exporter-country
    COO fallback a reviewer had already corrected by hand);
  * every failure still degrades to "no memory", never breaks a job.
"""
from __future__ import annotations

import json
import threading

from app.database import SessionLocal
from app.domain.enums import DeclaredRole
from app.extraction import field_profiles, layout_memory
from app.models import VendorFieldProfile, VendorLayout

MAPPING = {"desc": 1, "qty": 2, "uom": 3, "price": 4, "total": 5, "n_cols": 6}


def _layouts() -> list[VendorLayout]:
    db = SessionLocal()
    try:
        return db.query(VendorLayout).all()
    finally:
        db.close()


def _profiles() -> list[VendorFieldProfile]:
    db = SessionLocal()
    try:
        return db.query(VendorFieldProfile).all()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The property the file could not provide
# --------------------------------------------------------------------------- #
def test_concurrent_writers_do_not_overwrite_each_other(isolated_vendor_stores):
    """Twelve distinct layouts recorded at once must all survive.

    With the JSON file this is the exact case that lost entries: each writer
    loaded the same snapshot and the last `os.replace` won.
    """
    errors: list[Exception] = []

    def record(i: int) -> None:
        try:
            layout_memory.record_layout(DeclaredRole.INVOICE, dict(MAPPING, n_cols=i),
                                        f"SIGNATURE-{i}", f"Vendor {i}", 10 + i)
        except Exception as e:                        # pragma: no cover - diagnostic
            errors.append(e)

    threads = [threading.Thread(target=record, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert errors == []
    assert len(_layouts()) == 12, "a concurrent writer's entry was lost"
    assert len(layout_memory.stored_layouts(DeclaredRole.INVOICE)) == 12


def test_repeated_records_upsert_one_row_and_keep_the_best_evidence(isolated_vendor_stores):
    for confirmed in (12, 40, 7):
        layout_memory.record_layout(DeclaredRole.INVOICE, MAPPING, "SAME-SIG",
                                    "Vendor A", confirmed)
    rows = _layouts()
    assert len(rows) == 1
    assert rows[0].docs == 3
    # max, not last: a later parse confirming fewer rows saw a shorter document,
    # not a worse layout.
    assert rows[0].confirmed_rows == 40


def test_most_proven_layout_is_offered_first(isolated_vendor_stores):
    layout_memory.record_layout(DeclaredRole.INVOICE, dict(MAPPING, n_cols=6), "WEAK", None, 4)
    layout_memory.record_layout(DeclaredRole.INVOICE, dict(MAPPING, n_cols=9), "STRONG", None, 55)
    assert layout_memory.stored_layouts(DeclaredRole.INVOICE)[0]["n_cols"] == 9


def test_layouts_are_scoped_to_their_role(isolated_vendor_stores):
    layout_memory.record_layout(DeclaredRole.INVOICE, MAPPING, "SIG", None, 10)
    assert layout_memory.stored_layouts(DeclaredRole.PACKING_LIST) == []


def test_concurrent_profile_observations_all_count(isolated_vendor_stores):
    """coo_docs is a counter, so two finalizes for one vendor must not both read
    the same "before" and both write their own "after"."""
    def observe() -> None:
        field_profiles.record_coo_observation(
            "Medtronic International Ltd", [("IE", "ITEM_LEVEL")] * 10,
            reviewer_confirmed=False)

    threads = [threading.Thread(target=observe) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    rows = _profiles()
    assert len(rows) == 1, "one vendor must be one row"
    assert rows[0].coo_docs == 6, "a concurrent observation was lost"


# --------------------------------------------------------------------------- #
# Carry-over from the pre-table files
# --------------------------------------------------------------------------- #
def test_legacy_layouts_are_imported(isolated_vendor_stores, tmp_path):
    path = tmp_path / "vendor_layouts.json"
    path.write_text(json.dumps({"version": 1, "layouts": [
        {"role": "INVOICE", "header_signature": "SIG-A", "mapping": MAPPING,
         "vendor_hint": "Vendor A", "confirmed_rows": 30, "docs": 4},
        {"role": "PACKING_LIST", "header_signature": "SIG-B", "mapping": MAPPING,
         "confirmed_rows": 12, "docs": 1},
    ]}), encoding="utf-8")

    assert layout_memory.import_legacy_json(path) == 2
    entry = next(r for r in _layouts() if r.header_signature == "SIG-A")
    assert (entry.confirmed_rows, entry.docs, entry.vendor_hint) == (30, 4, "Vendor A")


def test_importing_twice_does_not_duplicate(isolated_vendor_stores, tmp_path):
    """The import is idempotent by key rather than by a marker file, so it is
    safe to run on every boot."""
    path = tmp_path / "vendor_layouts.json"
    path.write_text(json.dumps({"version": 1, "layouts": [
        {"role": "INVOICE", "header_signature": "SIG-A", "mapping": MAPPING,
         "confirmed_rows": 30, "docs": 4}]}), encoding="utf-8")

    assert layout_memory.import_legacy_json(path) == 1
    assert layout_memory.import_legacy_json(path) == 0
    assert len(_layouts()) == 1


def test_legacy_positional_entries_are_not_imported(isolated_vendor_stores, tmp_path):
    """A POSITIONAL entry came from the store confirming itself, and matches
    every headerless document of its role. The migration is where it dies."""
    path = tmp_path / "vendor_layouts.json"
    path.write_text(json.dumps({"version": 1, "layouts": [
        {"role": "INVOICE", "header_signature": "POSITIONAL", "mapping": MAPPING,
         "confirmed_rows": 15, "docs": 2}]}), encoding="utf-8")

    assert layout_memory.import_legacy_json(path) == 0
    assert _layouts() == []


def test_legacy_profiles_are_imported_with_their_source(isolated_vendor_stores, tmp_path):
    path = tmp_path / "vendor_field_profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": [
        {"vendor": "medtronicinternationalltd", "display": "Medtronic International Ltd",
         "coo_default": "IE", "coo_source": "OBSERVED", "coo_docs": 4},
        {"vendor": "skymoonprivatelimited", "display": "SKY MOON PRIVATE LIMITED",
         "coo_default": "CN", "coo_source": "REVIEWER", "coo_docs": 1},
    ]}), encoding="utf-8")

    assert field_profiles.import_legacy_json(path) == 2
    # A REVIEWER default is consumed immediately; an OBSERVED one needs
    # _MIN_OBSERVED_DOCS agreeing jobs — and the imported count carries that.
    assert field_profiles.coo_default_for("SKY MOON PRIVATE LIMITED") == "CN"
    assert field_profiles.coo_default_for("Medtronic International Ltd") == "IE"


def test_a_missing_legacy_file_is_not_an_error(isolated_vendor_stores, tmp_path):
    assert layout_memory.import_legacy_json(tmp_path / "absent.json") == 0
    assert field_profiles.import_legacy_json(tmp_path / "absent.json") == 0


def test_a_corrupt_legacy_file_starts_empty_rather_than_failing(isolated_vendor_stores, tmp_path):
    """Startup calls this. A broken cache file must never stop the app booting."""
    path = tmp_path / "vendor_layouts.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert layout_memory.import_legacy_json(path) == 0


# --------------------------------------------------------------------------- #
# Degrade, never break
# --------------------------------------------------------------------------- #
def test_a_broken_store_never_breaks_extraction(isolated_vendor_stores, monkeypatch):
    def _explode():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(layout_memory, "side_store_session", _explode)
    assert layout_memory.stored_layouts(DeclaredRole.INVOICE) == []
    layout_memory.record_layout(DeclaredRole.INVOICE, MAPPING, "SIG", None, 10)  # no raise


def test_a_broken_store_never_breaks_a_job(isolated_vendor_stores, monkeypatch):
    def _explode():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(field_profiles, "side_store_session", _explode)
    assert field_profiles.coo_default_for("Anyone") is None
    field_profiles.record_coo_observation("Anyone", [("IE", "ITEM_LEVEL")] * 5,
                                          reviewer_confirmed=True)      # no raise
