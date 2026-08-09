"""Per-job customs office / declaration model / procedure / transport selection
(user rule 2026-08-01: the tool serves every ASYCUDA office in Nepal, starting
from Kathmandu — TIA00/IM 4/4000/000 are review DEFAULTS, not constants; the
Box 25/26 transport modes have NO default and must be chosen per job).

Covers: the six new reference tables and their endpoints, the finalize-time
confirmation overrides reaching the XML, the reference-gated blockers
(model line, office, procedure membership, Box1↔Box37 cascade, required
modes), the durable /regime selection overlay (survives recomputes, stales
in-flight fingerprints), and Sad_flow derivation for export types.
"""
from decimal import Decimal

import pytest
from lxml import etree

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app
from app.reference.store import get_reference

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


def _finalize(client, job_id, review, **extra):
    return client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=review["review_fingerprint"], **extra))


def _xml(client, job_id):
    r = client.get(f"/api/jobs/{job_id}/xml")
    assert r.status_code == 200
    return etree.fromstring(r.content)


# --------------------------------------------------------------------------- #
# Reference tables
# --------------------------------------------------------------------------- #
def test_reference_store_counts_and_quirks():
    ref = get_reference()
    assert len(ref.office_by_code) == 71
    assert ref.office_by_code["TIA00"] == "TIA Customs Office"
    assert "BRJ01" in ref.office_by_code and "CBR01" in ref.office_by_code
    assert len(ref.declaration_models) == 17
    assert ("IM", "4") in ref.declaration_model_pairs
    assert ("IM", "1") not in ref.declaration_model_pairs        # lines, not a grid
    assert ("MIS", "5") in ref.declaration_model_pairs
    # ANNEX 1: 55 dropdown rows, 9100 duplicated in the source (both kept);
    # membership dict dedupes consciously
    assert len(ref.extended_procedures) == 55
    assert sum(1 for c, _ in ref.extended_procedures if c == "9100") == 2
    assert "4000" in ref.extended_proc_by_code and "9100" in ref.extended_proc_by_code
    # ANNEX 3: 182 rows; gaps in the numbering are IN THE SOURCE
    assert len(ref.national_proc_by_code) == 182
    assert "000" in ref.national_proc_by_code and "256" in ref.national_proc_by_code
    for gap in ("330", "361", "363", "374", "375"):
        assert gap not in ref.national_proc_by_code
    assert len(ref.transport_mode_by_code) == 9
    assert ref.transport_mode_by_code["01"] == "By Air"
    assert len(ref.incoterm_by_code) == 11 and "CIF" in ref.incoterm_by_code


def test_reference_endpoints(client):
    assert len(client.get("/api/reference/customs-offices").json()) == 71
    models = client.get("/api/reference/declaration-models").json()
    assert len(models) == 17
    assert {"type": "IM", "code": "4", "description": "Permanent Import"} in models
    assert len(client.get("/api/reference/extended-procedures").json()) == 55
    assert len(client.get("/api/reference/national-procedures").json()) == 182
    assert len(client.get("/api/reference/transport-modes").json()) == 9
    assert len(client.get("/api/reference/incoterms").json()) == 11
    cfg = client.get("/api/config").json()
    counts = cfg["reference_counts"]
    assert counts["customs_offices"] == 71 and counts["national_procedures"] == 182
    # a default that steers output is published, never silent — and the modes
    # deliberately have no default at all
    dflt = cfg["declaration_defaults"]
    assert dflt["customs_office_code"] == "TIA00"
    assert dflt["extended_customs_procedure"] == "4000"
    assert "border_mode" not in dflt and "inland_mode_of_transport" not in dflt


# --------------------------------------------------------------------------- #
# Defaults preserved end-to-end (the TIA00/IM4 path is byte-compatible)
# --------------------------------------------------------------------------- #
def test_default_regime_reaches_xml(client):
    job_id, review = _reviewed_job(client)
    # review seeds: defaults for office/model/procedures, EMPTY for the modes
    assert review["customs_office_code"] == "TIA00"
    assert review["declaration_type"] == "IM" and review["gen_procedure_code"] == "4"
    assert review["extended_customs_procedure"] == "4000"
    assert review["national_customs_procedure"] == "000"
    assert review["border_mode"] == "" and review["inland_mode_of_transport"] == ""
    assert any(w["code"] == "TRANSPORT_MODE_UNSELECTED" for w in review["warnings"])
    r = _finalize(client, job_id, review)
    assert r.status_code == 200 and r.json()["ready_for_xml"] is True
    root = _xml(client, job_id)
    assert root.findtext("Property/Sad_flow") == "I"
    assert root.findtext("Identification/Office_segment/Customs_clearance_office_code") == "TIA00"
    assert root.findtext("Identification/Type/Type_of_declaration") == "IM"
    assert root.findtext("Identification/Type/Declaration_gen_procedure_code") == "4"
    assert root.findtext("Transport/Border_office/Code") == "TIA00"
    assert root.findtext("Transport/Means_of_transport/Border_information/Mode") == "01"
    assert root.findtext("Transport/Means_of_transport/Inland_mode_of_transport") == "09"
    assert root.findtext("Transport/Container_flag") == "false"
    assert root.findtext("Transport/Location_of_goods") == "TIA...IM/GODOWN"
    assert root.findtext("Transport/Place_of_loading/Code") == "NPKTM"
    # Name deliberately EMPTY — ASYCUDA derives it from the code (ADR-011)
    assert (root.findtext("Transport/Place_of_loading/Name") or "") == ""
    items = root.findall("Item")
    assert all(i.findtext("Tarification/Extended_customs_procedure") == "4000" for i in items)
    assert all(i.findtext("Tarification/National_customs_procedure") == "000" for i in items)


def test_confirmation_overrides_reach_xml(client):
    job_id, review = _reviewed_job(client)
    r = _finalize(client, job_id, review,
                  customs_office_code="BRJ01",
                  border_mode="04", inland_mode_of_transport="02",
                  extended_customs_procedure="4070",
                  national_customs_procedure="256",
                  location_of_goods="BRJ...IM/GODOWN",
                  container_flag=True,
                  place_of_loading_code="INRXL")
    assert r.status_code == 200
    root = _xml(client, job_id)
    office = root.findtext("Identification/Office_segment/Customs_clearance_office_code")
    assert office == "BRJ01"
    # office NAME derived from the reference, and Border_office follows
    assert root.findtext("Identification/Office_segment/Customs_Clearance_office_name") == \
        "Birgunj Customs Office"
    assert root.findtext("Transport/Border_office/Code") == "BRJ01"
    assert root.findtext("Transport/Border_office/Name") == "Birgunj Customs Office"
    assert root.findtext("Transport/Means_of_transport/Border_information/Mode") == "04"
    assert root.findtext("Transport/Means_of_transport/Inland_mode_of_transport") == "02"
    assert root.findtext("Transport/Container_flag") == "true"
    assert root.findtext("Transport/Location_of_goods") == "BRJ...IM/GODOWN"
    assert root.findtext("Transport/Place_of_loading/Code") == "INRXL"
    items = root.findall("Item")
    assert all(i.findtext("Tarification/Extended_customs_procedure") == "4070" for i in items)
    assert all(i.findtext("Tarification/National_customs_procedure") == "256" for i in items)


def test_export_type_flips_sad_flow(client):
    job_id, review = _reviewed_job(client)
    r = _finalize(client, job_id, review,
                  declaration_type="EX", gen_procedure_code="1",
                  extended_customs_procedure="1000")
    assert r.status_code == 200
    root = _xml(client, job_id)
    assert root.findtext("Property/Sad_flow") == "E"
    assert root.findtext("Identification/Type/Type_of_declaration") == "EX"
    assert root.findtext("Identification/Type/Declaration_gen_procedure_code") == "1"


# --------------------------------------------------------------------------- #
# Reference-gated blockers (warn mode still builds a test XML)
# --------------------------------------------------------------------------- #
def _blocking_codes(resp) -> set:
    return {b["code"] for b in resp.json()["blocking_errors"]}


def test_modes_are_required_no_silent_default(client):
    job_id, review = _reviewed_job(client)
    body = dict(FINALIZE_BODY, review_fingerprint=review["review_fingerprint"])
    del body["border_mode"], body["inland_mode_of_transport"]
    r = client.post(f"/api/jobs/{job_id}/finalize", json=body)
    assert r.json()["ready_for_xml"] is False
    assert "TRANSPORT_MODE_REQUIRED" in _blocking_codes(r)
    # warn mode: the test XML still exists, with EMPTY mode elements
    root = _xml(client, job_id)
    assert (root.findtext("Transport/Means_of_transport/Border_information/Mode") or "") == ""


def test_cascade_and_membership_blockers(client):
    job_id, review = _reviewed_job(client)
    # 5100 is a valid ANNEX-1 code but belongs to IM 5, not IM 4
    r = _finalize(client, job_id, review, extended_customs_procedure="5100")
    assert r.json()["ready_for_xml"] is False
    assert "PROCEDURE_TYPE_MISMATCH" in _blocking_codes(r)

    job_id, review = _reviewed_job(client)
    r = _finalize(client, job_id, review, customs_office_code="XXX99",
                  national_customs_procedure="330")       # 330 is a SOURCE gap
    codes = _blocking_codes(r)
    assert "CUSTOMS_OFFICE_INVALID" in codes and "PROCEDURE_INVALID" in codes

    job_id, review = _reviewed_job(client)
    r = _finalize(client, job_id, review, declaration_type="IM", gen_procedure_code="1")
    assert "DECLARATION_MODEL_INVALID" in _blocking_codes(r)


# --------------------------------------------------------------------------- #
# Durable /regime selections
# --------------------------------------------------------------------------- #
def test_regime_endpoint_persists_and_seeds_reviews(client):
    job_id, review = _reviewed_job(client)
    old_fingerprint = review["review_fingerprint"]
    r = client.post(f"/api/jobs/{job_id}/regime", json={
        "customs_office_code": "BRT03", "border_mode": "02",
        "inland_mode_of_transport": "02", "national_customs_procedure": "256"})
    assert r.status_code == 200
    cr = r.json()["critical_review"]
    assert cr["customs_office_code"] == "BRT03"
    assert cr["customs_office_name"] == "Biratnagar Customs Office, ICP"
    assert cr["border_office_code"] == "BRT03"            # follows the clearance office
    assert cr["border_mode"] == "02"
    assert not any(w["code"] == "TRANSPORT_MODE_UNSELECTED" for w in cr["warnings"])
    # durability: a fresh recompute still carries the selection
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    assert fresh["customs_office_code"] == "BRT03"
    assert fresh["national_customs_procedure"] == "256"
    # the selection staled the pre-selection review (regime_revision in basis)
    assert fresh["review_fingerprint"] != old_fingerprint
    stale = client.post(f"/api/jobs/{job_id}/finalize",
                        json=dict(FINALIZE_BODY, review_fingerprint=old_fingerprint))
    assert stale.status_code == 409 and stale.json()["status"] == "REVIEW_STALE"
    # finalize WITHOUT regime keys in the confirmation: stored selection wins
    body = dict(FINALIZE_BODY, review_fingerprint=fresh["review_fingerprint"])
    del body["border_mode"], body["inland_mode_of_transport"]
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=body)
    assert fin.status_code == 200 and fin.json()["ready_for_xml"] is True
    root = _xml(client, job_id)
    assert root.findtext("Identification/Office_segment/Customs_clearance_office_code") == "BRT03"
    assert root.findtext("Transport/Means_of_transport/Border_information/Mode") == "02"
    items = root.findall("Item")
    assert all(i.findtext("Tarification/National_customs_procedure") == "256" for i in items)
    # audit trail records the selection
    audit = client.get(f"/api/jobs/{job_id}/audit").json()
    assert any(e["code"] == "REGIME_SELECTED" for e in audit)


def test_regime_endpoint_rejects_bad_values(client):
    job_id, _ = _reviewed_job(client)
    assert client.post(f"/api/jobs/{job_id}/regime",
                       json={"customs_office_code": "NOPE1"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/regime",
                       json={"border_mode": "10"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/regime",
                       json={"national_customs_procedure": "330"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/regime",
                       json={"declaration_type": "IM", "gen_procedure_code": "1"}).status_code == 422
    assert client.post(f"/api/jobs/{job_id}/regime",
                       json={"favourite_color": "blue"}).status_code == 422
    # null reverts to the deployment default
    ok = client.post(f"/api/jobs/{job_id}/regime", json={"customs_office_code": "BRJ01"})
    assert ok.status_code == 200
    back = client.post(f"/api/jobs/{job_id}/regime", json={"customs_office_code": None})
    assert back.status_code == 200
    assert back.json()["critical_review"]["customs_office_code"] == "TIA00"


def test_regime_change_does_not_disturb_allocation(client):
    """Office/procedure choices are declaration identity — weights, cartons and
    valuation must not move."""
    job_id, review = _reviewed_job(client)
    client.post(f"/api/jobs/{job_id}/regime", json={
        "customs_office_code": "BHW01", "border_mode": "02",
        "inland_mode_of_transport": "09"})
    fresh = client.get(f"/api/jobs/{job_id}/critical-review").json()
    fin = client.post(f"/api/jobs/{job_id}/finalize", json=dict(
        FINALIZE_BODY, review_fingerprint=fresh["review_fingerprint"]))
    assert fin.status_code == 200
    decl = fin.json()
    assert decl["valuation"]["total_cif"] == "474575.07"
    total = sum(Decimal(i["gross_weight_kg"]) for i in decl["items"])
    assert abs(total - Decimal("199.0")) <= Decimal("0.05")
