"""Deterministic rule-engine unit tests (report regression catalog)."""
from decimal import Decimal

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import (
    AirWaybillExtractionRaw, AirWaybillFormRaw, BankingExtractionRaw, Evidence, PackingListChunkRaw,
    PartyRaw, RawMoney, RawNumber, RoleValidation)
from app.extraction.validator import validate_airwaybill
from app.numbers import parse_decimal
from app.reference.store import get_reference
from app.rules import airwaybill_authority, coo, hs_resolver, supplementary_unit
from app.rules.banking import resolve_banking
from app.rules.freight import (
    allocate_cost, awb_charge_total, prorate_to_authority, select_freight)
from app.rules.models import WorkItem

ref = get_reference()


def _item(seq=1, desc="Widget", qty="10", hs=None, coo_raw="CN", uom="PCS"):
    return WorkItem(seq, "INV1", "24/02/2026", seq, str(seq), desc, Decimal(qty), uom,
                    Decimal("1"), Decimal(qty), "USD", hs_code_raw=hs, country_of_origin_raw=coo_raw)


# ---- reference data ----
def test_reference_loaded():
    assert len(ref.hs_by_11) > 6000
    assert len(ref.banks) == 279
    assert ref.hs_exact("85044090900").description == "Other static converters"


# ---- HAWB / MAWB authority ----
def _awb():
    mawb = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Master Air Waybill",
        primary_awb_number_raw="555-12345678", mawb_number_raw="555-12345678", carrier_raw="Demo Air Cargo",
        gross_weight=RawNumber(value_raw="330"), chargeable_weight=RawNumber(value_raw="443"),
        pieces_or_packages=RawNumber(value_raw="24"))
    hawb = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="DEMOHAWB0057", mawb_number_raw="555-12345678", issuer_raw="ABC Logistics",
        gross_weight=RawNumber(value_raw="199"), pieces_or_packages=RawNumber(value_raw="12"))
    return AirWaybillExtractionRaw(role_validation=RoleValidation(expected_role=DeclaredRole.AIR_WAYBILL), forms=[mawb, hawb])


def test_hawb_overrides_mawb():
    a = airwaybill_authority.resolve_shipment_authority([_awb()])
    assert a.selected_authority_type == "HAWB"
    assert a.gross_weight == Decimal("199")   # not 330
    assert a.packages == Decimal("12")        # not 24


def test_chargeable_never_gross():
    a = airwaybill_authority.resolve_shipment_authority([_awb()])
    assert a.gross_weight != Decimal("443")   # chargeable must never be gross


def test_house_stays_hawb_despite_mawb_number():
    a = airwaybill_authority.resolve_shipment_authority([_awb()])
    cls = {c.logical_form_id: c.decision for c in a.classifications}
    assert cls["h"] == "HAWB" and cls["m"] == "MAWB"


def test_gross_label_validation_rejects_chargeable():
    form = AirWaybillFormRaw(logical_form_id="x",
        gross_weight=RawNumber(value_raw="443", evidence=Evidence(page_no=1, label="Chargeable Weight", quote="443")))
    payload = AirWaybillExtractionRaw(role_validation=RoleValidation(expected_role=DeclaredRole.AIR_WAYBILL), forms=[form])
    errors = validate_airwaybill(payload, {1: "443 chargeable weight"})
    assert any("GROSS_WEIGHT_LABEL_INVALID" in e for e in errors)


def _payload(*forms):
    return AirWaybillExtractionRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.AIR_WAYBILL), forms=list(forms))


def _charge_form(**boxes):
    return AirWaybillFormRaw(logical_form_id="x", **{
        k: RawMoney(amount_raw=v, currency_raw="EUR") for k, v in boxes.items()})


def test_awb_weight_charge_reported_as_freight_is_sent_for_repair():
    """freight_amount must be the Total Prepaid box; the reference AWB's
    4653.00 weight charge contradicts its own 4708.00 total (2026-07-21)."""
    errors = validate_airwaybill(_payload(_charge_form(
        freight_amount="4653.00", total_prepaid="4708.00", weight_charge="4653.00",
        other_charges_total="55.00")), {1: "total prepaid 4708.00"})
    assert any("AWB_FREIGHT_NOT_GRAND_TOTAL" in e for e in errors)
    # ...and the correct pick passes
    assert not validate_airwaybill(_payload(_charge_form(
        freight_amount="4,708.00", total_prepaid="4708.00", weight_charge="4653.00",
        other_charges_total="55.00")), {1: "total prepaid 4708.00"})


def test_awb_freight_missing_other_charges_is_sent_for_repair():
    errors = validate_airwaybill(_payload(_charge_form(
        freight_amount="4653.00", weight_charge="4653.00", other_charges_total="55.00")),
        {1: "awc 55.00"})
    assert any("AWB_FREIGHT_EXCLUDES_OTHER_CHARGES" in e for e in errors)
    # no other charges printed -> the weight charge IS the whole freight
    assert not validate_airwaybill(_payload(_charge_form(
        freight_amount="4653.00", weight_charge="4653.00")), {1: "x"})


# ---- authority ladder test cases (design rule catalog) ----
def test_tc1_hawb_beats_full_mawb_document():
    mawb = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="176-12345678", issuer_raw="Airline Carrier Ltd",
        shipper=PartyRaw(name_raw="ABC Freight Forwarder"),
        consignee=PartyRaw(name_raw="Nepal Destination Agent"),
        gross_weight=RawNumber(value_raw="850", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="60"))
    hawb = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="HKG2409981", issuer_raw="Logistics Company",
        shipper=PartyRaw(name_raw="Actual Supplier Ltd."),
        consignee=PartyRaw(name_raw="Actual Importer Pvt. Ltd."),
        gross_weight=RawNumber(value_raw="135", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="12"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(mawb, hawb)])
    cls = {c.logical_form_id: c.decision for c in a.classifications}
    assert cls["m"] == "MAWB" and cls["h"] == "HAWB"
    assert a.selected_authority_type == "HAWB"
    assert a.gross_weight == Decimal("135") and a.packages == Decimal("12")
    assert any(w.code == "MAWB_DIFFERS" for w in a.warnings)


def test_tc2_single_awb_fallback():
    awb = AirWaybillFormRaw(logical_form_id="s", document_title_raw="Air Waybill",
        primary_awb_number_raw="157-98765432",
        gross_weight=RawNumber(value_raw="200"), pieces_or_packages=RawNumber(value_raw="15"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(awb)])
    assert a.selected_authority_type == "SINGLE_AWB"
    assert a.gross_weight == Decimal("200") and a.packages == Decimal("15")
    assert any(w.code == "AWB_FALLBACK" for w in a.warnings)


def test_tc3_hawb_beats_packing_list_totals():
    hawb = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="SHA111", gross_weight=RawNumber(value_raw="100"),
        pieces_or_packages=RawNumber(value_raw="8"))
    packing = PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST),
        total_gross_weight=RawNumber(value_raw="105"), total_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(hawb)], [packing])
    assert a.selected_authority_type == "HAWB"
    assert a.gross_weight == Decimal("100") and a.packages == Decimal("8")


def test_tc4_mixed_do_packing_is_not_true_do():
    mixed = AirWaybillFormRaw(logical_form_id="x", document_title_raw="Delivery Order / Packing List",
        gross_weight=RawNumber(value_raw="120"), pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(mixed)])
    assert all(c.decision != "TRUE_DO" for c in a.classifications)
    assert a.selected_authority_type == "PACKING_LIST"   # last-resort fallback only
    assert a.gross_weight == Decimal("120")
    assert any(w.code == "PACKING_FALLBACK" for w in a.warnings)


def test_tc5_hawb_stays_hawb_with_printed_mawb_number():
    hawb = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="SHA12345", mawb_number_raw="176-12345678",
        gross_weight=RawNumber(value_raw="90"), pieces_or_packages=RawNumber(value_raw="7"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(hawb)])
    assert a.classifications[0].decision == "HAWB"
    assert a.selected_authority_type == "HAWB"
    assert a.gross_weight == Decimal("90") and a.packages == Decimal("7")


def test_true_do_beats_mawb_186kg_11ctn_regression():
    # real-world failure: DO page said 11 pcs / 186 kg, MAWB said 26 / 387
    do = AirWaybillFormRaw(logical_form_id="do", document_title_raw="DELIVERY ORDER",
        hawb_number_raw="STN 8245881", mawb_number_raw="217-08414545",
        gross_weight=RawNumber(value_raw="186.0", unit_raw="kg"),
        pieces_or_packages=RawNumber(value_raw="11"))
    mawb = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="217-08414545", mawb_number_raw="217-08414545",
        issuer_raw="THAI AIRWAYS INTERNATIONAL",
        shipper=PartyRaw(name_raw="GLOBAL FORWARDING (SINGAPORE) PTE LTD"),
        consignee=PartyRaw(name_raw="SWIFT WORLD LOGISTICS PVT LTD"),
        gross_weight=RawNumber(value_raw="387.0", unit_raw="K"),
        chargeable_weight=RawNumber(value_raw="603.5"),
        pieces_or_packages=RawNumber(value_raw="26"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(do, mawb)])
    assert a.selected_authority_type == "TRUE_DO"
    assert a.gross_weight == Decimal("186.0") and a.packages == Decimal("11")
    assert a.gross_weight != Decimal("603.5")            # chargeable never gross
    assert any(w.code == "MAWB_DIFFERS" for w in a.warnings)
    assert a.hawb_number == "STN 8245881" and a.mawb_number == "217-08414545"


def test_multi_awb_without_labels_infers_lower_as_house():
    big = AirWaybillFormRaw(logical_form_id="big", document_title_raw="Air Waybill",
        primary_awb_number_raw="176-11111111",
        gross_weight=RawNumber(value_raw="500"), pieces_or_packages=RawNumber(value_raw="50"))
    small = AirWaybillFormRaw(logical_form_id="small", document_title_raw="Air Waybill",
        primary_awb_number_raw="GF88771",
        gross_weight=RawNumber(value_raw="100"), pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(big, small)])
    assert a.gross_weight == Decimal("100") and a.packages == Decimal("10")
    assert a.selected_authority_type == "HAWB"
    assert a.selected_form_id == "small"     # never blindly the highest weight/PCS AWB


def test_two_awbs_lower_weight_always_wins_even_when_mislabeled():
    """User rule 2026-07-18 (real failure: system took MAWB's 163 kg / 10 ctn
    instead of the HAWB's 78 kg / 5 ctn): with two air waybills the LOWER
    weight/carton form is ALWAYS the consignment authority, even when the
    heavier (consolidated) form scores as the house document."""
    heavy = AirWaybillFormRaw(logical_form_id="heavy", document_title_raw="House Air Waybill",
        primary_awb_number_raw="160-11223344",
        gross_weight=RawNumber(value_raw="163", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="10"))
    light = AirWaybillFormRaw(logical_form_id="light", document_title_raw="Air Waybill",
        primary_awb_number_raw="GF55221",
        gross_weight=RawNumber(value_raw="78", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="5"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(heavy, light)])
    assert a.gross_weight == Decimal("78") and a.packages == Decimal("5")
    assert a.selected_form_id == "light" and a.selected_authority_type == "HAWB"
    assert any(w.code == "AWB_LOWER_WEIGHT_SELECTED" for w in a.warnings)


def test_two_awbs_equal_weight_keeps_classified_choice():
    a1 = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="HKG991", gross_weight=RawNumber(value_raw="120"),
        pieces_or_packages=RawNumber(value_raw="9"))
    a2 = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="176-22222222", issuer_raw="Airline Carrier",
        gross_weight=RawNumber(value_raw="120"), pieces_or_packages=RawNumber(value_raw="9"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(a1, a2)])
    assert a.selected_form_id == "h" and a.gross_weight == Decimal("120")
    assert not any(w.code == "AWB_LOWER_WEIGHT_SELECTED" for w in a.warnings)


def test_lower_weight_with_more_pieces_warns_conflict():
    lo = AirWaybillFormRaw(logical_form_id="lo", document_title_raw="Air Waybill",
        primary_awb_number_raw="GF77001", gross_weight=RawNumber(value_raw="78"),
        pieces_or_packages=RawNumber(value_raw="12"))
    hi = AirWaybillFormRaw(logical_form_id="hi", document_title_raw="Air Waybill",
        primary_awb_number_raw="176-33333333", gross_weight=RawNumber(value_raw="163"),
        pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(lo, hi)])
    assert a.gross_weight == Decimal("78")            # lower weight still wins
    assert any(w.code == "AWB_WEIGHT_CARTON_CONFLICT" for w in a.warnings)


def test_field40_weight_based_even_when_labels_are_inverted():
    """User correction 2026-07-18 (real Field 40 failure): with HAWB
    KCNKTM0028 (78 kg) and MAWB 160-06359872 (163 kg), Field 40 must carry the
    HAWB number — decided by the WEIGHTS, never by the scored labels (here the
    heavy master is mis-titled as a house document)."""
    heavy = AirWaybillFormRaw(logical_form_id="heavy", document_title_raw="House Air Waybill",
        mawb_number_raw="160-06359872",
        gross_weight=RawNumber(value_raw="163", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="10"))
    light = AirWaybillFormRaw(logical_form_id="light", document_title_raw="Air Waybill",
        hawb_number_raw="KCNKTM0028",
        gross_weight=RawNumber(value_raw="78", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="5"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(heavy, light)])
    assert a.hawb_number == "KCNKTM0028" and a.mawb_number == "160-06359872"
    assert a.gross_weight == Decimal("78") and a.packages == Decimal("5")
    value, reason = airwaybill_authority.derive_field40(a)
    assert value == "KCNKTM0028"
    assert "lower" in reason


def test_field40_equal_gross_uses_mawb_number():
    h = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="KCNKTM0028", gross_weight=RawNumber(value_raw="120"),
        pieces_or_packages=RawNumber(value_raw="9"))
    m = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        mawb_number_raw="160-06359872", issuer_raw="Airline Carrier",
        gross_weight=RawNumber(value_raw="120"), pieces_or_packages=RawNumber(value_raw="9"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(h, m)])
    value, reason = airwaybill_authority.derive_field40(a)
    assert value == "160-06359872"
    assert "same gross" in reason


def test_awb_numbers_always_extracted_via_format_fallback():
    """Rule 1: both numbers are extracted even when the dedicated hawb/mawb
    fields are empty — each form's own AWB number is classified by format
    (letters = forwarder/house, 3+8 digits = airline/master)."""
    house = AirWaybillFormRaw(logical_form_id="h", document_title_raw="Air Waybill",
        primary_awb_number_raw="KCNKTM0028",
        gross_weight=RawNumber(value_raw="78"), pieces_or_packages=RawNumber(value_raw="5"))
    master = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="160-06359872",
        gross_weight=RawNumber(value_raw="163"), pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(house, master)])
    assert a.hawb_number == "KCNKTM0028"
    assert a.mawb_number == "160-06359872"
    value, _ = airwaybill_authority.derive_field40(a)
    assert value == "KCNKTM0028"                     # lower gross -> HAWB number


def test_merged_form_raises_suspect_warning():
    # legacy stored extraction: DO page merged into the master's form
    merged = AirWaybillFormRaw(logical_form_id="f1", source_pages=[1, 2],
        document_title_raw="Air Waybill", primary_awb_number_raw="217-08414545",
        hawb_number_raw="STN 8245881", mawb_number_raw="217-08414545",
        issuer_raw="THAI AIRWAYS INTERNATIONAL",
        shipper=PartyRaw(name_raw="GLOBAL FORWARDING (SINGAPORE) PTE LTD"),
        consignee=PartyRaw(name_raw="SWIFT WORLD LOGISTICS PVT LTD"),
        gross_weight=RawNumber(value_raw="387.0"), pieces_or_packages=RawNumber(value_raw="26"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(merged)])
    assert any(w.code == "AWB_MERGED_SUSPECT" for w in a.warnings)


def test_packing_totals_are_last_fallback():
    packing = PackingListChunkRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.PACKING_LIST),
        total_gross_weight=RawNumber(value_raw="105", unit_raw="KG"),
        total_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([], [packing])
    assert a.selected_authority_type == "PACKING_LIST"
    assert a.gross_weight == Decimal("105") and a.packages == Decimal("10")
    assert any(w.code == "PACKING_FALLBACK" for w in a.warnings)


def test_validator_flags_unextracted_house_page():
    # only the master form was extracted although page 1 is a delivery order
    # printing the consignment's own pcs/weight (the 186 kg / 11 ctn failure)
    master_only = AirWaybillFormRaw(logical_form_id="f1", source_pages=[1, 2],
        document_title_raw="Air Waybill",
        gross_weight=RawNumber(value_raw="387.0",
                               evidence=Evidence(page_no=2, label="Gross Weight", quote="387.0 K")),
        pieces_or_packages=RawNumber(value_raw="26",
                                     evidence=Evidence(page_no=2, quote="26")))
    payload = _payload(master_only)
    pages = {1: "DELIVERY ORDER  No. of pcs 11 weight 186.0kg  MAWB No: 217-08414545  HAWB No: STN 8245881",
             2: "Air Waybill THAI AIRWAYS 26 387.0 K"}
    errors = validate_airwaybill(payload, pages)
    assert any("HOUSE_LEVEL_PAGE_NOT_EXTRACTED" in e for e in errors)
    # once the DO page has its own form citing page-1 evidence, the error clears
    do_form = AirWaybillFormRaw(logical_form_id="f2", source_pages=[1],
        document_title_raw="DELIVERY ORDER",
        gross_weight=RawNumber(value_raw="186.0",
                               evidence=Evidence(page_no=1, label="weight", quote="weight 186.0kg")),
        pieces_or_packages=RawNumber(value_raw="11",
                                     evidence=Evidence(page_no=1, quote="No. of pcs 11")))
    errors2 = validate_airwaybill(_payload(master_only, do_form), pages)
    assert not any("HOUSE_LEVEL_PAGE_NOT_EXTRACTED" in e for e in errors2)


def test_hawb_without_readable_values_never_falls_back_to_master():
    """Real failure 2026-07-19: the house AWB (5 ctn / 78 kg) was extracted
    without readable numbers, so the ladder silently stamped the master's
    consolidated 10 ctn / 163 kg. The house document must STAY the authority —
    missing values go to manual review entry, never to the master."""
    house = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="KCNKTM0031", issuer_raw="ABC Logistics")   # no gross, no pieces
    master = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="160-06359872", mawb_number_raw="160-06359872",
        issuer_raw="Airline Carrier",
        gross_weight=RawNumber(value_raw="163", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(house, master)])
    assert a.selected_authority_type == "HAWB" and a.selected_form_id == "h"
    assert a.gross_weight is None and a.packages is None     # manual entry, NOT 163/10
    assert any(w.code == "HAWB_VALUES_UNREADABLE" for w in a.warnings)
    assert any("163" in str(w) for w in a.warnings if w.code == "HAWB_VALUES_UNREADABLE")


def test_hawb_with_pieces_but_unreadable_gross_keeps_house_pieces():
    house = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="KCNKTM0031",
        pieces_or_packages=RawNumber(value_raw="5"))             # gross unreadable
    master = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="160-06359872", issuer_raw="Airline Carrier",
        gross_weight=RawNumber(value_raw="163", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="10"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(house, master)])
    assert a.selected_authority_type == "HAWB" and a.selected_form_id == "h"
    assert a.packages == Decimal("5")                            # house count, never 10
    assert a.gross_weight is None                                # never 163
    assert any(w.code == "HAWB_VALUES_UNREADABLE" for w in a.warnings)


def test_single_usable_awb_with_unreadable_second_awb_warns():
    master = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        primary_awb_number_raw="157-98765432",
        gross_weight=RawNumber(value_raw="200"), pieces_or_packages=RawNumber(value_raw="15"))
    ghost = AirWaybillFormRaw(logical_form_id="g", document_title_raw="Air Waybill",
        primary_awb_number_raw="GF9911")                         # no readable values
    a = airwaybill_authority.resolve_shipment_authority([_payload(master, ghost)])
    assert a.selected_authority_type == "SINGLE_AWB"
    assert any(w.code == "SECOND_AWB_UNREADABLE" for w in a.warnings)


def test_multi_hawb_prefers_invoice_related_form():
    h1 = AirWaybillFormRaw(logical_form_id="h1", document_title_raw="House Air Waybill",
        hawb_number_raw="SHA100", gross_weight=RawNumber(value_raw="100"),
        pieces_or_packages=RawNumber(value_raw="8"))
    h2 = AirWaybillFormRaw(logical_form_id="h2", document_title_raw="House Air Waybill",
        hawb_number_raw="SHA200", gross_weight=RawNumber(value_raw="100"),
        pieces_or_packages=RawNumber(value_raw="8"),
        invoice_references_raw=["INV-777"])
    a = airwaybill_authority.resolve_shipment_authority(
        [_payload(h1, h2)], invoice_numbers={"INV-777"})
    assert a.selected_form_id == "h2"


def test_authority_value_provenance_exposed():
    a = airwaybill_authority.resolve_shipment_authority([_awb()])
    gwa, cta = a.gross_weight_authority, a.carton_authority
    assert gwa is not None and cta is not None
    assert gwa.document_id == "h" and gwa.document_type == "HAWB"
    assert gwa.value == Decimal("199") and cta.value == Decimal("12")
    assert gwa.reasons and cta.reasons
    payload = gwa.as_payload()
    assert payload["value"] == "199" and payload["document_type"] == "HAWB"


def test_validator_house_hint_variants_flagged():
    """Label variants forwarders actually print (HOUSE AWB, H.A.W.B, H/AWB)
    must trigger the uncovered-house-page check; a lone MAWB page must not."""
    master_only = AirWaybillFormRaw(logical_form_id="f1", source_pages=[1, 2],
        document_title_raw="Air Waybill",
        gross_weight=RawNumber(value_raw="163",
                               evidence=Evidence(page_no=2, label="Gross Weight", quote="163")),
        pieces_or_packages=RawNumber(value_raw="10",
                                     evidence=Evidence(page_no=2, quote="10")))
    for house_text in ("HOUSE AWB NO: SHA12345 GROSS WEIGHT 78.00 KGS",
                       "H.A.W.B NO SHA12345  5 CTN  78.00 KG",
                       "H/AWB: SHA12345 TOTAL 78 KG"):
        errors = validate_airwaybill(_payload(master_only),
                                     {1: house_text, 2: "Air Waybill 10 pcs 163 kg"})
        assert any("HOUSE_LEVEL_PAGE_NOT_EXTRACTED" in e for e in errors), house_text
    # a page that only shows the master's own number/weight is NOT house-hinted
    errors = validate_airwaybill(_payload(master_only),
                                 {1: "MAWB NO: 160-11112222 CONSOLIDATION 163 KG",
                                  2: "Air Waybill 10 pcs 163 kg"})
    assert not any("HOUSE_LEVEL_PAGE_NOT_EXTRACTED" in e for e in errors)


def test_checkpoint1_hawb_number_bound_to_lower_weight_form():
    """User checkpoint 2026-07-19: with two air waybills the LOWER-weight one
    IS the house document — its own number is THE HAWB number (gross, ctn and
    Field 40 all come from it), even when the heavier form mis-prints a
    'HAWB No.' field that would win a first-form-order scan."""
    heavy = AirWaybillFormRaw(logical_form_id="heavy", document_title_raw="House Air Waybill",
        hawb_number_raw="FAKE123", primary_awb_number_raw="160-11223344",
        gross_weight=RawNumber(value_raw="163", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="10"))
    light = AirWaybillFormRaw(logical_form_id="light", document_title_raw="Air Waybill",
        primary_awb_number_raw="GF555",
        gross_weight=RawNumber(value_raw="78", unit_raw="KG"),
        pieces_or_packages=RawNumber(value_raw="5"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(heavy, light)])
    assert a.selected_form_id == "light"
    assert a.gross_weight == Decimal("78") and a.packages == Decimal("5")
    assert a.hawb_number == "GF555"            # the lower-weight form's own number
    assert a.mawb_number == "160-11223344"     # the heavier form's own number
    value, reason = airwaybill_authority.derive_field40(a)
    assert value == "GF555" and "lower" in reason


def test_checkpoint2_equal_weights_ctn_from_hawb_field40_mawb():
    """Equal gross weights: Field 40 carries the MAWB number, but the carton
    count and gross weight stay the HAWB's (final authority)."""
    h = AirWaybillFormRaw(logical_form_id="h", document_title_raw="House Air Waybill",
        hawb_number_raw="HKG991", gross_weight=RawNumber(value_raw="120"),
        pieces_or_packages=RawNumber(value_raw="9"))
    m = AirWaybillFormRaw(logical_form_id="m", document_title_raw="Air Waybill",
        mawb_number_raw="160-06359872", issuer_raw="Airline Carrier",
        gross_weight=RawNumber(value_raw="120"), pieces_or_packages=RawNumber(value_raw="11"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(h, m)])
    assert a.selected_form_id == "h" and a.selected_authority_type == "HAWB"
    assert a.gross_weight == Decimal("120") and a.packages == Decimal("9")   # never 11
    value, reason = airwaybill_authority.derive_field40(a)
    assert value == "160-06359872" and "same gross" in reason


def test_checkpoint3_single_awb_field40_uses_its_number():
    awb = AirWaybillFormRaw(logical_form_id="s", document_title_raw="Air Waybill",
        primary_awb_number_raw="157-98765432",
        gross_weight=RawNumber(value_raw="200"), pieces_or_packages=RawNumber(value_raw="15"))
    a = airwaybill_authority.resolve_shipment_authority([_payload(awb)])
    assert a.selected_authority_type == "SINGLE_AWB"
    assert a.gross_weight == Decimal("200") and a.packages == Decimal("15")
    value, reason = airwaybill_authority.derive_field40(a)
    assert value == "157-98765432" and "Single" in reason


# ---- HS resolver ----
def test_hs_exact_accept():
    it = hs_resolver.resolve_hs_for_item(_item(hs="85044090900"), ref)
    assert it.final_hs_code_11 == "85044090900" and it.hs_source == "INVOICE_HS_EXACT"


def test_hs_llm_hint_must_be_8_digits():
    # an 11-digit "LLM" answer is rejected; only 8-digit hints are expanded via DB
    it = hs_resolver.resolve_hs_for_item(_item(hs=None), ref, llm_hint_fn=lambda d: ["85044090900"])
    assert it.final_hs_code_11 is None  # 11-digit rejected -> manual review
    it2 = hs_resolver.resolve_hs_for_item(_item(hs=None), ref, llm_hint_fn=lambda d: ["85044090"])
    assert it2.final_hs_code_11 and it2.hs_source == "LLM_HS8"


def test_hs_manual_override_db_gated():
    # accepted: exact official 11-digit code, supersedes the earlier block
    it = hs_resolver.resolve_hs_for_item(_item(hs=None), ref)
    assert it.final_hs_code_11 is None
    hs_resolver.apply_manual_hs([it], {1: "85044090900"}, ref)
    assert it.final_hs_code_11 == "85044090900"
    assert it.hs_source == "MANUAL_OVERRIDE"
    assert not any(w.code == "HS_MANUAL_REVIEW" for w in it.warnings)

    # rejected: an 8-digit PREFIX is not a decision.  It used to be completed
    # by taking the first sibling in the band, at 0.8 confidence — a weaker
    # rule than the item_id review channel, applied later, over the top of it.
    it8 = hs_resolver.resolve_hs_for_item(_item(hs=None), ref)
    hs_resolver.apply_manual_hs([it8], {"1": "85044090"}, ref)
    assert it8.final_hs_code_11 is None
    assert any(w.code == "HS_MANUAL_REVIEW" and "8 digits" in w.message
               for w in it8.warnings)

    # rejected: not an official code — the DB gate holds, item stays blocked
    itbad = hs_resolver.resolve_hs_for_item(_item(hs=None), ref)
    hs_resolver.apply_manual_hs([itbad], {1: "99999999999"}, ref)
    assert itbad.final_hs_code_11 is None
    assert any(w.code == "HS_MANUAL_REVIEW" for w in itbad.warnings)


def test_hs_unknown_blocks():
    it = hs_resolver.resolve_hs_for_item(_item(hs="99999999999"), ref)
    assert it.final_hs_code_11 is None
    assert any(w.code == "HS_MANUAL_REVIEW" for w in it.warnings)


# ---- COO ----
def test_coo_item_level_wins():
    it = coo.resolve_coo_for_item(_item(coo_raw="China"), "India", ref)
    assert it.coo_alpha2 == "CN" and it.coo_source == "ITEM_LEVEL"


def test_coo_exporter_fallback():
    it = coo.resolve_coo_for_item(_item(coo_raw=None), "China", ref)
    assert it.coo_alpha2 == "CN" and it.coo_source == "EXPORTER_FALLBACK"


def test_coo_na_is_namibia():
    assert ref.normalize_country("NA") == "NA"


def test_coo_unresolved_blocks():
    it = coo.resolve_coo_for_item(_item(coo_raw="Narnia"), "Mordor", ref)
    assert it.coo_alpha2 is None
    assert any(w.code == "COO_UNRESOLVED" for w in it.warnings)


# ---- supplementary units ----
def test_supp_unt_uses_quantity():
    it = _item(qty="11"); it.hs_tariff_unit = "UNT"; it.net_weight_kg = Decimal("2")
    supplementary_unit.resolve_supplementary_for_item(it)
    assert it.supplementary_unit_code == "UNT" and it.supplementary_quantity == Decimal("11.00")


def test_supp_kgm_uses_net_weight():
    it = _item(qty="11"); it.hs_tariff_unit = "KGM"; it.net_weight_kg = Decimal("1.064")
    supplementary_unit.resolve_supplementary_for_item(it)
    assert it.supplementary_unit_code == "KGM" and it.supplementary_quantity == Decimal("1.0640")


def test_supp_pair_divides_and_warns_odd():
    it = _item(qty="11", uom="PCS"); it.hs_tariff_unit = "PR"; it.net_weight_kg = Decimal("1")
    supplementary_unit.resolve_supplementary_for_item(it)
    assert it.supplementary_quantity == Decimal("6.00")   # round(11/2)=6 (rule, ADR-004)
    assert any(w.code == "SUPPLEMENTARY_ASSUMPTION" for w in it.warnings)


# ---- air-waybill charge total (Total Prepaid, never the weight charge) ----
def _d(v):
    return Decimal(v) if v is not None else None


def test_awb_total_prepaid_beats_weight_charge():
    """Reference AWB 235-41325852 (samples/max/awb.pdf): weight charge 4653.00
    + AWC 55.00 = Total Prepaid 4708.00 EUR. Taking the rate line's 4653.00
    undervalued the declaration (user report 2026-07-21)."""
    total, detail, warns = awb_charge_total(
        total_prepaid=_d("4708.00"), total_collect=None, weight_charge=_d("4653.00"),
        valuation_charge=None, tax_charge=None, other_charges=_d("55.00"),
        printed_freight=_d("4653.00"))
    assert total == Decimal("4708.00")
    assert "Total Prepaid" in detail and "4653.00" in detail and not warns


def test_awb_freight_is_prorated_by_gross_never_chargeable_weight():
    """Authority weight 236 kg equals the waybill's own gross weight, so the
    whole 4708.00 is this consignment's freight. Dividing by the chargeable
    weight (550.0) instead would have produced 2020.62."""
    amount, note = prorate_to_authority(Decimal("4708.00"), Decimal("236.0"), Decimal("236"))
    assert amount == Decimal("4708.00") and note is None
    # a consolidated master covering more weight IS prorated
    amount, note = prorate_to_authority(Decimal("4708.00"), Decimal("472"), Decimal("236"))
    assert amount == Decimal("2354.00") and "prorated to authority 236 kg" in note
    # a lighter waybill is never scaled UP off a suspect authority weight
    assert prorate_to_authority(Decimal("4708.00"), Decimal("100"), Decimal("236"))[0] == Decimal("4708.00")


def test_awb_charge_total_reconstructs_when_no_grand_total_box():
    total, detail, warns = awb_charge_total(
        total_prepaid=None, total_collect=None, weight_charge=_d("4653.00"),
        valuation_charge=None, tax_charge=_d("12.00"), other_charges=_d("55.00"),
        printed_freight=_d("4653.00"))
    assert total == Decimal("4720.00") and not warns
    assert "no Total Prepaid/Collect box" in detail


def test_awb_charge_total_collect_and_mismatch_warning():
    total, detail, warns = awb_charge_total(
        total_prepaid=None, total_collect=_d("900.00"), weight_charge=_d("850.00"),
        valuation_charge=None, tax_charge=None, other_charges=_d("50.00"),
        printed_freight=None)
    assert total == Decimal("900.00") and "Total Collect" in detail and not warns
    # a grand total below its own component boxes means a misread box
    _, _, warns = awb_charge_total(
        total_prepaid=_d("800.00"), total_collect=None, weight_charge=_d("850.00"),
        valuation_charge=None, tax_charge=None, other_charges=_d("50.00"),
        printed_freight=None)
    assert [w.code for w in warns] == ["AWB_CHARGE_TOTAL_MISMATCH"]


def test_awb_charge_total_ignores_orphan_surcharges_and_blank_waybill():
    # other charges without a weight charge are not a freight total
    total, _, _ = awb_charge_total(None, None, None, None, None, _d("55.00"), _d("4653.00"))
    assert total == Decimal("4653.00")
    assert awb_charge_total(None, None, None, None, None, None, None)[0] is None


# ---- freight allocation ----
def test_freight_selection_max_and_no_lc_default():
    fr = select_freight(Decimal("100"), Decimal("120"), None, None, "USD")
    assert fr.effective_freight_foreign == Decimal("120.00") and fr.source == "AWB"


def test_freight_alloc_sums_exactly():
    items = [_item(1, qty="10"), _item(2, qty="20"), _item(3, qty="30")]
    for it in items: it.line_total = it.quantity  # value share = 10/20/30
    allocate_cost(items, Decimal("100.00"), "item_external_freight", basis="value")
    total = sum(it.item_external_freight for it in items)
    assert total == Decimal("100.00")


def test_bank_bic_base8_resolution():
    bank, method = ref.resolve_bank("CTZNNPKAXXX", None)
    assert bank.code == "11020000"


def test_payment_terms_unknown_not_defaulted():
    code, method = ref.resolve_terms_code("75 DAYS FROM AWB DATE")
    assert code is None and method == "REVIEW_REQUIRED"


# ---- banking amount parsing (Free_text_1 "LC NO:...,$<amount>") ----
def test_swift_decimal_comma_becomes_point():
    banking = resolve_banking(BankingExtractionRaw(
        role_validation=RoleValidation(expected_role=DeclaredRole.BANKING),
        sender_bic_raw="CTZNNPKAXXX",
        amount=RawMoney(amount_raw="#7023,17#", currency_raw="USD")), ref)
    assert banking.amount == Decimal("7023.17")
    assert f"${banking.amount}" == "$7023.17"  # builder's Free_text_1 amount format


def test_parse_decimal_separator_variants():
    assert parse_decimal("#7023,17#") == Decimal("7023.17")
    assert parse_decimal("7,023.17") == Decimal("7023.17")
    assert parse_decimal("1.234,56") == Decimal("1234.56")
    assert parse_decimal("(1,200)") == Decimal("-1200")
