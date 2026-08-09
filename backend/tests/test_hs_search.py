"""HS database search + reviewed-HS selection (Detailed Review HS Search spec).

Search is deterministic, local and network-free; reviewed HS becomes
authoritative only through the allowlisted, DB-gated hs-review channel."""
import json
import socket

import pytest

from fastapi.testclient import TestClient

from app.config import SAMPLE_DIR
from app.database import SessionLocal, init_db
from app.main import app
from app.models import AuditEvent, Job
from app.reference.store import HsRecord, ReferenceStore, get_reference, hs_query_error

FINALIZE_BODY = {
    "manual_insurance_amount": "1665.49", "exchange_rate": "145.76",
    "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _reviewed_job(client):
    job_id = client.post("/api/jobs/demo").json()["job_id"]
    r = client.get(f"/api/jobs/{job_id}/critical-review")
    assert r.status_code == 200
    return job_id, r.json()


def _single_item_job(client, hs_code_raw):
    """Invoice-only job whose single goods row carries the given raw HS."""
    job_id = client.post("/api/jobs").json()["job_id"]
    fx = json.loads((SAMPLE_DIR / "fixtures" / "invoice.json").read_text())
    fx["rows"] = fx["rows"][:1]
    fx["rows"][0]["hs_code_raw"] = hs_code_raw
    fx["totals"] = None
    pdf = (SAMPLE_DIR / "sample_invoice.pdf").read_bytes()
    up = client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                     files={"file": ("inv.pdf", pdf, "application/pdf")},
                     data={"fixture": json.dumps(fx)})
    assert up.status_code == 200
    review = client.get(f"/api/jobs/{job_id}/critical-review").json()
    return job_id, review


def _hs_review(client, job_id, item_id, code, source="detailed_review_hs_search"):
    return client.post(f"/api/jobs/{job_id}/items/hs-review", json={
        "item_id": item_id, "final_hs_code": code, "hs_review_source": source})


# --------------------------------------------------------------------------- #
# Search endpoint
# --------------------------------------------------------------------------- #
def test_short_queries_rejected(client):
    for bad in ("", "a", "5", " 7 ", "5.", " . 8 "):
        r = client.get("/api/reference/hs", params={"q": bad})
        assert r.status_code == 400, bad
    # boundary accepts: two digits, two letters
    assert client.get("/api/reference/hs", params={"q": "84"}).status_code == 200
    assert client.get("/api/reference/hs", params={"q": "ox"}).status_code == 200
    assert hs_query_error("laptop") is None and hs_query_error("steel bolt") is None


def test_limit_clamped(client):
    r = client.get("/api/reference/hs", params={"q": "84", "limit": 500}).json()
    assert len(r) <= 50
    r = client.get("/api/reference/hs", params={"q": "84", "limit": 0}).json()
    assert len(r) == 1
    r = client.get("/api/reference/hs", params={"q": "84", "limit": -3}).json()
    assert len(r) == 1


def test_numeric_prefix_search(client):
    rows = client.get("/api/reference/hs", params={"q": "8471"}).json()
    assert rows and all(x["code"].startswith("8471") for x in rows)
    # an exact 11-digit query ranks its own record first with the top score
    target = rows[0]["code"]
    exact = client.get("/api/reference/hs", params={"q": target}).json()
    assert exact[0]["code"] == target and exact[0]["score"] == 100.0


def test_text_description_search(client):
    rows = client.get("/api/reference/hs", params={"q": "horse"}).json()
    assert any(x["code"] == "01012100000" for x in rows)
    for x in rows:
        assert "horse" in x["description"].lower()
    # multi-token: every token must match (relevance threshold)
    rows = client.get("/api/reference/hs", params={"q": "breeding horse"}).json()
    assert rows and all("breeding" in x["description"].lower() for x in rows)


def test_leading_zeros_preserved(client):
    rows = client.get("/api/reference/hs", params={"q": "0101"}).json()
    assert rows and all(x["code"].startswith("0101") for x in rows)
    assert all(len(x["code"]) == 11 for x in rows)      # strings, zeros intact


def test_only_exact_db_records(client):
    ref = get_reference()
    for q in ("84", "steel", "horse", "0101"):
        for x in client.get("/api/reference/hs", params={"q": q}).json():
            assert x["code"] in ref.hs_by_11
            assert x["description"] == ref.hs_by_11[x["code"]].description
            assert x["unit"] == ref.hs_by_11[x["code"]].unit


def test_deterministic_sort_and_tiebreak(client):
    a = client.get("/api/reference/hs", params={"q": "8471"}).json()
    b = client.get("/api/reference/hs", params={"q": "8471"}).json()
    assert a == b                                        # deterministic
    scores = [x["score"] for x in a]
    assert scores == sorted(scores, reverse=True)        # score descending
    for i in range(1, len(a)):                           # hs11 ascending on ties
        if a[i]["score"] == a[i - 1]["score"]:
            assert a[i]["code"] > a[i - 1]["code"]


def test_explanation_never_ranked():
    store = ReferenceStore()
    store.hs_by_11["11111111111"] = HsRecord(
        "11111111111", "steel bolt", "UNT", explanation="zzqmarker in explanation only")
    store.hs_by_11["22222222222"] = HsRecord(
        "22222222222", "zzqmarker in description", "UNT", explanation="")
    from app.reference.store import hs_text_tokens
    for code, rec in store.hs_by_11.items():
        store.hs_desc_tokens[code] = hs_text_tokens(rec.description)
    hits = store.search_hs("zzqmarker", 10)
    assert [r.hs11 for r, _ in hits] == ["22222222222"]  # explanation match invisible


# --------------------------------------------------------------------------- #
# Reviewed HS selection (hs-review channel)
# --------------------------------------------------------------------------- #
def test_exact_reviewed_hs_accepted_and_supplementary_recalculated(client):
    job_id, review = _reviewed_job(client)
    row = review["item_details"][0]
    ref = get_reference()
    kgm = next(c for c, r in sorted(ref.hs_by_11.items()) if r.unit == "KGM")
    resp = _hs_review(client, job_id, row["item_id"], kgm)
    assert resp.status_code == 200
    body = resp.json()
    assert body["final_hs_code"] == kgm
    new_row = next(x for x in body["critical_review"]["item_details"]
                   if x["item_id"] == row["item_id"])
    assert new_row["final_hs"] == kgm
    assert new_row["hs_confidence"] == "1.00" and new_row["hs_explicit"] is True
    assert new_row["hs_low_confidence"] is False
    assert new_row["hs_source"] == "DETAILED_REVIEW_HS_SEARCH"
    assert new_row["sup_unit"] == "KGM"                  # derived from DB unit, not client
    db = SessionLocal()
    try:
        ev = db.query(AuditEvent).filter_by(job_id=job_id,
                                            event_code="ITEM_HS_REVIEWED").one()
        assert ev.payload["final_hs_code"] == kgm and ev.payload["event_id"]
    finally:
        db.close()


def test_invalid_reviewed_hs_rejected(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]
    ref = get_reference()
    assert "99999999999" not in ref.hs_by_11
    r = _hs_review(client, job_id, item_id, "99999999999")
    assert r.status_code == 422 and r.json()["code"] == "HS_NOT_IN_DATABASE"
    r = _hs_review(client, job_id, item_id, "85044090900", source="llm_suggestion")
    assert r.status_code == 422 and r.json()["code"] == "HS_REVIEW_SOURCE_INVALID"
    r = _hs_review(client, job_id, "src:nope", "85044090900")
    assert r.status_code == 404
    # nothing was stored, review untouched
    db = SessionLocal()
    try:
        assert not (db.get(Job, job_id).item_mutations or {}).get("hs_selections")
    finally:
        db.close()


def test_partial_hs_never_final(client):
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]
    for partial in ("8504", "850440", "85044090", "8504409090"):
        r = _hs_review(client, job_id, item_id, partial)
        assert r.status_code == 422
        assert r.json()["code"] == "HS_NOT_11_DIGITS"


def test_explicit_selection_resolves_auto_low_confidence(client):
    # 8-digit invoice HS -> DB prefix completion -> AUTO_LOW_CONFIDENCE proposal
    job_id, review = _reviewed_job(client)
    # craft the low-confidence state on a dedicated single-item job
    job_id, review = _single_item_job(client, "85044090")
    row = review["item_details"][0]
    # "Adaptar" matches no code in the 85044090 band, so it resolves to the
    # broad "Other static converters" catch-all — an AUTO_LOW_CONFIDENCE guess
    assert row["final_hs"].startswith("85044090")
    assert row["hs_source"].startswith("INVOICE_HS_COMPLETED_8")
    assert row["hs_low_confidence"] is True and float(row["hs_confidence"]) < 1.0
    # reviewer confirms the exact current code (low-confidence confirm action)
    resp = _hs_review(client, job_id, row["item_id"], row["final_hs"],
                      source="detailed_review")
    assert resp.status_code == 200
    new_row = resp.json()["critical_review"]["item_details"][0]
    assert new_row["final_hs"] == row["final_hs"]        # retained, not changed
    assert new_row["hs_low_confidence"] is False
    assert new_row["hs_confidence"] == "1.00" and new_row["hs_explicit"] is True
    assert new_row["hs_source"] == "DETAILED_REVIEW"


def test_hs_review_invalidates_xml_but_invalid_submission_does_not(client):
    job_id, review = _reviewed_job(client)
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=review["review_fingerprint"]))
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    item_id = review["item_details"][0]["item_id"]
    # invalid submission: rejected AND leaves the generated XML alone
    r = _hs_review(client, job_id, item_id, "99999999999")
    assert r.status_code == 422
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 200
    # valid selection invalidates the XML artifact
    r = _hs_review(client, job_id, item_id, "01012100000")
    assert r.status_code == 200
    assert client.get(f"/api/jobs/{job_id}/xml").status_code == 404
    assert client.get(f"/api/jobs/{job_id}").json()["has_declaration"] is False


def test_corrupt_stored_selection_blocks_xml_not_serialized(client):
    # Defense in depth: an invalid selection can only exist via store corruption;
    # it must never reach XML and must surface a precise blocking finding.
    job_id, review = _reviewed_job(client)
    item_id = review["item_details"][0]["item_id"]
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.item_mutations = {"schema": 1, "revision": 1, "ordered_item_ids": [],
                              "manual_items": [], "tombstones": [],
                              "hs_selections": {item_id: {
                                  "final_hs_code": "99999999999",
                                  "hs_review_source": "detailed_review_hs_search"}}}
        db.commit()
    finally:
        db.close()
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert any(w["code"] == "HS_REVIEW_REJECTED" for w in fresh["warnings"])
    row = next(x for x in fresh["item_details"] if x["item_id"] == item_id)
    assert row["final_hs"] != "99999999999"              # cascade proposal retained
    fin = client.post(f"/api/jobs/{job_id}/finalize",
                      json=dict(FINALIZE_BODY,
                                review_fingerprint=fresh["review_fingerprint"]))
    decl = fin.json()
    assert decl["ready_for_xml"] is False              # verdict: not clean
    assert any(m["code"] == "HS_REVIEW_REJECTED" for m in decl["blocking_errors"])
    assert all(it["hs_code_11"] != "99999999999" for it in decl["items"])
    # warn-mode generates the test XML anyway — but the invalid reviewed code
    # must NEVER be serialized into it (safe cascade proposal is used instead)
    xml = client.get(f"/api/jobs/{job_id}/xml")
    assert xml.status_code == 200
    assert b"99999999999" not in xml.content


def test_hs_selection_survives_recompute_and_row_reorder(client):
    job_id, review = _reviewed_job(client)
    rows = review["item_details"]
    target = rows[2]
    assert _hs_review(client, job_id, target["item_id"], "01012100000").status_code == 200
    # structural mutation reorders rows; the selection follows the item_id
    first = rows[0]
    assert client.request("DELETE", f"/api/jobs/{job_id}/items/{first['item_id']}",
                          json={"confirmation_sn": "1"}).status_code == 200
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    moved = next(x for x in fresh["item_details"] if x["item_id"] == target["item_id"])
    assert moved["sn"] == 2 and moved["final_hs"] == "01012100000"
    assert moved["hs_explicit"] is True


def test_deleting_item_drops_its_hs_selection(client):
    job_id, review = _reviewed_job(client)
    target = review["item_details"][0]
    assert _hs_review(client, job_id, target["item_id"], "01012100000").status_code == 200
    assert client.request("DELETE", f"/api/jobs/{job_id}/items/{target['item_id']}",
                          json={"confirmation_sn": "1"}).status_code == 200
    db = SessionLocal()
    try:
        overlay = db.get(Job, job_id).item_mutations
        assert target["item_id"] not in (overlay.get("hs_selections") or {})
    finally:
        db.close()


def test_stale_job_snapshot_cannot_clobber_concurrent_delete(client):
    """Adversarial-review finding: the endpoint loads the Job BEFORE the lock,
    so a mutation could run on a stale overlay snapshot and silently resurrect
    a just-deleted item.  Deterministic reproduction: two sessions load the job
    at revision 0; A deletes the item (commits under the lock); B — still
    holding the pre-delete snapshot — must re-read under the lock and 404."""
    from app import services
    from app.review.item_mutations import ItemMutationError

    job_id, review = _reviewed_job(client)
    target = review["item_details"][0]
    dbA, dbB = SessionLocal(), SessionLocal()
    try:
        jobA, jobB = dbA.get(Job, job_id), dbB.get(Job, job_id)
        _ = (jobB.item_mutations, jobB.status)        # force-load the stale snapshot
        services.delete_job_item(dbA, jobA, target["item_id"], {"confirmation_sn": "1"})
        with pytest.raises(ItemMutationError) as exc:
            services.review_item_hs(dbB, jobB, {
                "item_id": target["item_id"], "final_hs_code": "01012100000",
                "hs_review_source": "detailed_review_hs_search"})
        assert exc.value.status_code == 404
    finally:
        dbA.close()
        dbB.close()
    db = SessionLocal()
    try:
        overlay = db.get(Job, job_id).item_mutations
        assert any(t["item_id"] == target["item_id"] for t in overlay["tombstones"])
        assert overlay["revision"] == 1               # the delete survived, exactly once
        assert target["item_id"] not in (overlay.get("hs_selections") or {})
    finally:
        db.close()


def test_hs_overrides_require_review_fingerprint(client):
    """Adversarial-review finding: SN-keyed hs_overrides without the
    fingerprint lock can land on the wrong item after a reorder."""
    job_id, review = _reviewed_job(client)
    body = dict(FINALIZE_BODY, hs_overrides={"1": "01012100000"})
    r = client.post(f"/api/jobs/{job_id}/finalize", json=body)
    assert r.status_code == 409
    assert any(m["code"] == "HS_OVERRIDES_REQUIRE_FINGERPRINT"
               for m in r.json()["blocking_errors"])
    ok = client.post(f"/api/jobs/{job_id}/finalize",
                     json=dict(body, review_fingerprint=review["review_fingerprint"]))
    assert ok.status_code == 200 and ok.json()["ready_for_xml"] is True


def test_search_and_review_make_zero_network_calls(client, monkeypatch):
    job_id, review = _reviewed_job(client)          # seed BEFORE blocking egress
    item_id = review["item_details"][0]["item_id"]

    def _no_net(*a, **k):
        raise AssertionError("network call attempted during HS search/review")

    # Block the outbound-connection primitives every HTTP/AI client needs.
    # (socket.socket itself stays intact: Windows asyncio builds an internal
    # localhost socketpair — patching it deadlocks the in-process test client.)
    monkeypatch.setattr(socket, "create_connection", _no_net)
    monkeypatch.setattr(socket, "getaddrinfo", _no_net)
    assert client.get("/api/reference/hs", params={"q": "8471"}).status_code == 200
    assert client.get("/api/reference/hs", params={"q": "steel bolt"}).status_code == 200
    assert _hs_review(client, job_id, item_id, "01012100000").status_code == 200
