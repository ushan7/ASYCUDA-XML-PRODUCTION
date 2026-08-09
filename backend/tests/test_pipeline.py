"""End-to-end pipeline + golden ASYCUDA XML tests against the sample shipment."""
from decimal import Decimal

import pytest
from lxml import etree

from app.database import SessionLocal, init_db
from app import services
from app.demo import seed_demo_job


# Reviewer-confirmed values: manifest / field 18 / field 21 are manual-entry
# review fields.
CONFIRMATION = {
    "manual_insurance_amount": "1665.49",
    "exchange_rate": "145.76",
    "manifest_no": "2026/1436",
    "field_18_transport_identity": "BA16CHA8099",
    "field_21_transport_identity": "BA16CHA8099",
    "field_40_confirmed": True,
    "border_mode": "01", "inland_mode_of_transport": "09",
}


@pytest.fixture(scope="module")
def declaration():
    # The database is this run's alone and starts empty (tests/conftest.py),
    # so there is nothing to clear here. What used to stand in this place was
    # `os.remove("/tmp/ec_pytest.db")` — a hardcoded POSIX path that never
    # matched on Windows, and that on Linux would have unlinked the file after
    # the engine had already opened it.
    init_db()
    db = SessionLocal()
    job = seed_demo_job(db); db.commit()
    review = services.critical_review(db, job); db.commit()
    body = dict(CONFIRMATION, review_fingerprint=review["review_fingerprint"])
    decl = services.finalize_job(db, job, body)
    db.commit()
    art = services.latest_xml(db, job.id)
    return decl, art.xml_bytes, review


def test_demo_ready_for_xml(declaration):
    decl, _, _ = declaration
    assert decl["ready_for_xml"] is True
    assert not decl["blocking_errors"]


def test_item_count_and_order(declaration):
    decl, _, _ = declaration
    assert decl["total_number_of_items"] == 119
    seqs = [i["xml_item_sequence"] for i in decl["items"]]
    assert seqs == list(range(1, 120))                       # order preserved


def test_hawb_authority_totals(declaration):
    decl, _, _ = declaration
    assert decl["total_number_of_packages"] == "12"          # HAWB, not MAWB 24
    assert decl["valuation"]["total_weight"] == "199.0000"   # HAWB, not 330


def test_valuation_matches_sample(declaration):
    decl, _, _ = declaration
    v = decl["valuation"]
    assert v["total_invoice_foreign"] == "2108.89"
    assert v["total_invoice_national"] == "307391.81"
    assert v["external_freight_foreign"] == "1135.55"
    assert v["total_cif"] == "474575.07"


def test_bank_and_terms(declaration):
    decl, _, _ = declaration
    assert decl["bank_code"] == "11020000"
    assert decl["terms_code"] == "200"


def test_xml_well_formed_and_structure(declaration):
    _, xml, _ = declaration
    root = etree.fromstring(xml)
    assert root.tag == "ASYCUDA"
    assert len(root.findall("Item")) == 119
    assert root.findtext("Property/Nbers/Total_number_of_items") == "119"
    # 1 item on the main form + 3 per continuation sheet: 1 + ceil(118/3) = 41
    assert root.findtext("Property/Forms/Total_number_of_forms") == "41"
    assert root.findtext("Financial/Bank/Code") == "11020000"


def test_xml_hs_split_and_first_item_only(declaration):
    _, xml, _ = declaration
    root = etree.fromstring(xml)
    items = root.findall("Item")
    # HS split 8 + 3
    assert items[0].findtext("Tarification/HScode/Commodity_code") == "85044090"
    assert items[0].findtext("Tarification/HScode/Precision_1") == "900"
    # Summary_declaration (HAWB) on every item; Free_text_1 first item only
    assert all(i.findtext("Previous_doc/Summary_declaration").strip() == "DEMOHAWB0057" for i in items)
    ft = [i.findtext("Free_text_1") for i in items]
    assert ft[0] and sum(1 for x in ft if x and x.strip()) == 1


def test_weight_reconciliation_exact(declaration):
    decl, _, _ = declaration
    total = sum(Decimal(i["gross_weight_kg"]) for i in decl["items"])
    assert abs(total - Decimal("199.0")) <= Decimal("0.05")


def test_freight_reconciliation_exact(declaration):
    decl, _, _ = declaration
    total = sum(Decimal(i["item_external_freight_foreign"]) for i in decl["items"])
    assert total == Decimal("1135.55")


def test_every_item_has_official_hs_and_coo(declaration):
    decl, _, _ = declaration
    for it in decl["items"]:
        assert len(it["hs_code_11"]) == 11 and it["hs_code_11"].isdigit()
        assert len(it["coo_alpha2"]) == 2
        assert Decimal(it["supplementary_quantity"]) > 0


# --------------------------------------------------------------------------- #
# Detailed Review — item-level preview rows on the critical review payload
# --------------------------------------------------------------------------- #
def test_detailed_review_item_rows(declaration):
    _, _, review = declaration
    rows = review["item_details"]
    assert [r["sn"] for r in rows] == list(range(1, 120))        # invoice order
    first = rows[0]
    assert first["description"] and first["quantity"] and first["uom"]
    # row prices sum to the authoritative goods total shown in the summary
    assert sum(Decimal(r["total_price"]) for r in rows) == Decimal(review["calculated_goods_total"])
    # demo fixtures resolve every HS from the official DB
    assert all(len(r["final_hs"]) == 11 and r["final_hs"].isdigit() for r in rows)
    assert all(len(r["coo"]) == 2 for r in rows)
    # allocation preview reconciles exactly to the HAWB authority totals
    assert sum(Decimal(r["gross"]) for r in rows) == Decimal("199")
    assert sum(Decimal(r["ctn"]) for r in rows) == Decimal("12")
    assert all(Decimal(r["net"]) < Decimal(r["gross"]) for r in rows)
    # supplementary preview populated (code + name + qty)
    assert all(r["sup_unit"] and r["sup_name"] and Decimal(r["sup_qty"]) > 0 for r in rows)


# --------------------------------------------------------------------------- #
# Expanded Critical Review — mandatory contents and gates
# --------------------------------------------------------------------------- #
def test_review_carries_mandatory_sections(declaration):
    _, _, review = declaration
    assert review["invoice_numbers"] == ["DEMO-209-1"]
    assert review["invoice_dates"] == ["24-FEB-2026"]
    assert review["invoice_roster"][0]["item_count"] == 119
    assert review["goods_currency"] == "USD"                  # invoice currency only
    assert review["hawb_no"] == "DEMOHAWB0057" and review["mawb_no"] == "555-12345678"
    # Field 40 rule: HAWB gross (199) differs from MAWB gross (330) -> HAWB number
    assert review["field_40_previous_document"] == "DEMOHAWB0057"
    assert review["bank_code"] == "11020000" and review["bank_resolution_state"] == "RESOLVED"
    assert review["payment_term_code"] == "200" and review["swift_code"] == "CTZNNPKAXXX"
    assert review["bank_reference"] == "LCDEMO0000000001"
    assert review["bank_amount"] == "7023.17" and review["bank_date"] == "24-FEB-2026"
    assert review["manual_freight_amount"] == "1135.55"
    assert review["package_type"] == "CT"
    assert review["importer_exim_valid"] is True
    assert review["total_number_of_forms"] == 41
    assert review["review_fingerprint"]
    assert "MAWB NO:555-12345678 HAWB:DEMOHAWB0057" in review["field_9_invoice_transport_document"]


def test_xml_reviewed_header_fields(declaration):
    _, xml, _ = declaration
    root = etree.fromstring(xml)
    assert root.findtext("Identification/Manifest_reference_number") == "2026/1436"
    # The Declarant block is always emitted empty — no code, name, PAN or
    # address ever reaches the XML.
    declarant = root.find("Declarant")
    assert declarant is not None
    assert not (declarant.findtext("Declarant_code") or "")
    assert not (declarant.findtext("Declarant_name") or "")
    mot = "Transport/Means_of_transport/"
    assert root.findtext(mot + "Departure_arrival_information/Identity") == "BA16CHA8099"
    assert root.findtext(mot + "Border_information/Identity") == "BA16CHA8099"
    assert root.findtext(mot + "Border_information/Nationality") == "NP"
    assert root.findtext(mot + "Border_information/Mode") == "01"
    first = root.findall("Item")[0]
    assert first.findtext("Packages/Kind_of_packages_code") == "CT"
    assert first.findtext("Packages/Kind_of_packages_name") == "Carton"
    assert first.findtext("Previous_doc/Previous_document_reference") == "DATE:24/02/2026,($7023.17 OF LC)"
    assert first.findtext("Free_text_1") == "LC NO:LCDEMO0000000001,$7023.17"
    # Field 9 reference-XML style in Financial_name
    fname = root.findtext("Traders/Financial/Financial_name")
    assert fname.splitlines()[0] == "MAWB NO:555-12345678 HAWB:DEMOHAWB0057"
    assert "INVOICE NO:DEMO-209-1 DT: 24-FEB-2026" in fname
    assert "LC NO:LCDEMO0000000001, DT:24-FEB-2026" in fname


def test_stale_review_fingerprint_forces_rereview():
    db = SessionLocal()
    job = seed_demo_job(db); db.commit()
    services.critical_review(db, job); db.commit()
    body = dict(CONFIRMATION, review_fingerprint="not-the-current-fingerprint")
    out = services.finalize_job(db, job, body); db.commit()
    assert out["status"] == "REVIEW_STALE"


def test_field40_rule_same_weight_prefers_mawb():
    from decimal import Decimal as D
    from app.rules.airwaybill_authority import derive_field40
    from app.rules.models import AwbClassification, ShipmentAuthority

    def cls(decision, gross, num):
        return AwbClassification(logical_form_id=num, decision=decision, hawb_score=0,
                                 mawb_score=0, gross_weight=gross, chargeable_weight=None,
                                 packages=None, awb_number=num)

    same = ShipmentAuthority("HAWB", D("199"), D("12"), "HAWB1", "180-11111111",
                             [cls("HAWB", D("199"), "HAWB1"), cls("MAWB", D("199"), "180-11111111")])
    assert derive_field40(same)[0] == "180-11111111"          # same weight -> MAWB
    diff = ShipmentAuthority("HAWB", D("199"), D("12"), "HAWB1", "180-11111111",
                             [cls("HAWB", D("199"), "HAWB1"), cls("MAWB", D("330"), "180-11111111")])
    assert derive_field40(diff)[0] == "HAWB1"                 # different -> HAWB
    single = ShipmentAuthority("SINGLE_AWB", D("330"), D("24"), None, "180-22222222",
                               [cls("MAWB", D("330"), "180-22222222")])
    assert derive_field40(single)[0] == "180-22222222"        # single AWB -> its number
    bl = ShipmentAuthority("UNKNOWN", None, None, None, None, [])
    assert derive_field40(bl)[0] == ""                        # Bill of Lading -> empty
