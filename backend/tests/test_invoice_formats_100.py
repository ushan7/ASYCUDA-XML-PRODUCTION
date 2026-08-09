"""The 100-format corpus — one goods table per real-world invoice shape.

``tests/data/invoice_formats.json`` holds 112 realistic goods tables covering
the invoice formats a customs broker actually meets: 40-odd national layouts
(US, Canada, Mexico, Brazil, Argentina, Chile, Colombia, Peru, UK, Germany,
France, Italy, Spain, Netherlands, Belgium, Switzerland, Austria, Sweden,
Norway, Denmark, Finland, Poland, Czechia, Hungary, Romania, Bulgaria, Croatia,
Slovenia, Greece, Turkey, Russia, Ukraine, Egypt, Saudi Arabia, UAE, Israel,
Morocco, Tunisia, China, Japan, Korea, Thailand, Vietnam, Malaysia, Singapore,
Philippines, Hong Kong, Indonesia, India, Pakistan, Bangladesh, Sri Lanka,
Australia, New Zealand, South Africa, Nigeria, Kenya), the customs-specific
documents (proforma, consular, ATA carnet, repair-and-return, samples,
continuation pages, integrated packing list), the logistics and e-invoicing
shapes (forwarder house bill, courier, marketplace, ZUGFeRD, XRechnung, Peppol,
EDI 810), and the industry layouts (pharma, textile, footwear, electronics,
auto parts, machinery, chemicals, petroleum, agri-commodity).

Every fixture is arithmetically consistent under a printed total, so a page
that parses but reads a COLUMN wrongly cannot pass by accident — which is the
only way a corpus like this proves anything.

``KNOWN_GAPS`` is the honest half of this file. Those fixtures do not parse
correctly yet; they are run anyway and marked xfail, so the list can only
shrink and a fixture that starts passing fails loudly (``xpass``) until it is
promoted. Do not delete an entry to make the suite green — move it out of the
set when the parser actually handles it.
"""
import json
import re
from pathlib import Path

import pytest

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import parse_pages
from app.numbers import detect_numeric_locale, parse_decimal

FIXTURES = json.loads(
    (Path(__file__).parent / "data" / "invoice_formats.json").read_text(encoding="utf-8"))
BY_ID = {f["id"]: f for f in FIXTURES}

# Formats the deterministic parser does not yet read correctly.  Each is a real
# shape worth supporting; none is a broken fixture.  The clusters left are:
# per-row tax columns on EU invoices, service-line tables (legal/medical/
# engineering time entries), freight-charge tables with no goods quantity, and
# e-invoicing visual layouts whose "table" is a field dump.
KNOWN_GAPS = {
    "asia_east_vn_hoa_don_co_form", "asia_south_africa_ke_tax_invoice",
    "asia_south_africa_lk_commercial_invoice", "asia_south_africa_tn_facture",
    "customs_special_ata_carnet_temporary_export", "customs_special_consular_dual_language",
    "customs_special_intercompany_transfer_price", "customs_special_repair_return_hs9801",
    "east_med_gr_timologio_fpa", "east_med_il_tax_invoice_vat17",
    "industry_goods_agri_commodity_grade_crop_year_fobst",
    "industry_goods_machinery_de_exw_serial_baujahr", "logistics_digital_awb_aligned",
    "logistics_digital_courier_dhl", "logistics_digital_ff_house_bill",
    "logistics_digital_shipping_line", "logistics_digital_xrechnung_bt",
    "manual_services_in_pharma_batch_expiry", "manual_services_it_food_lotto_tmc",
    "manual_services_us_sf1034_voucher", "na_latam_br_nota_fiscal_exportacao",
    "na_latam_pe_factura_electronica", "nordic_baltic_cz_faktura_bilingual",
    "nordic_baltic_fi_lasku_alv", "nordic_baltic_fi_lasku_alv_viitenumero",
    "nordic_baltic_hu_szamla_afa", "nordic_baltic_ro_factura_tva",
    "western_eu_at_rechnung_versandkosten", "western_eu_ch_commercial_invoice_mwst",
    "western_eu_de_handelsrechnung", "western_eu_es_factura_intracomunitaria_irpf",
    "western_eu_fr_facture_commerciale", "western_eu_it_fattura_commerciale",
    "western_eu_nl_commercial_invoice_korting",
}

IDS = sorted(BY_ID)


def _norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _parse(fixture):
    locale = detect_numeric_locale(fixture["page"])
    res = parse_pages(DeclaredRole.INVOICE, {1: fixture["page"]}, {1: locale})
    pp = res.pages.get(1)
    return (pp.rows if pp else []), locale, res


@pytest.mark.parametrize("fid", IDS, ids=IDS)
def test_format_parses_to_its_printed_rows_and_fields(fid, request):
    f = BY_ID[fid]
    if fid in KNOWN_GAPS:
        request.node.add_marker(pytest.mark.xfail(
            strict=True, reason="known parser gap — see KNOWN_GAPS"))
    rows, locale, res = _parse(f)

    if not f.get("should_parse", True):
        assert rows == [], (
            f"{f['title']}: this shape has no parseable goods table and must be "
            f"handed to the LLM, not guessed at — got {len(rows)} rows")
        return

    assert len(rows) == f["expected_rows"], (
        f"{f['title']}: parsed {len(rows)} of {f['expected_rows']} rows. {res.notes}")

    exp, row = f["expected_first_row"], rows[0]
    assert _norm(row.description_raw) == _norm(exp["description"])
    for got, want, name in ((row.quantity_raw, exp["quantity"], "quantity"),
                            (row.unit_price_raw, exp["unit_price"], "unit price"),
                            (row.line_total_raw, exp["line_total"], "line total")):
        if not want:
            continue
        g, w = parse_decimal(got, locale=locale), parse_decimal(want, locale=locale)
        assert g is not None and w is not None and g == w, f"{name}: {got!r} != {want!r}"
    if exp["uom"]:
        assert _norm(row.uom_raw) == _norm(exp["uom"])
    if exp["hs_code"]:
        assert _norm(row.hs_code_raw) == _norm(exp["hs_code"])


@pytest.mark.parametrize("fid", [i for i in IDS if i not in KNOWN_GAPS], ids=lambda i: i)
def test_no_supported_format_reports_a_tariff_code_as_a_quantity(fid):
    """The column-shift signature: a bare 8+ digit run with no thousands or
    decimal separator anywhere is a tariff number that landed in the wrong
    field.  Separators are what tell it apart from a real bulk quantity —
    120,000.000 barrels is nine digits and perfectly legitimate, while
    48192000000 is an HS code that once shipped as a piece count."""
    f = BY_ID[fid]
    if not f.get("should_parse", True):
        pytest.skip("refusal case")
    rows, _locale, _res = _parse(f)
    for r in rows:
        raw = (r.quantity_raw or "").strip()
        assert not re.fullmatch(r"\d{8,}", raw), f"quantity {raw!r} looks like a tariff code"


def test_the_gap_list_only_names_real_fixtures():
    assert not (KNOWN_GAPS - set(BY_ID)), "KNOWN_GAPS names a fixture that no longer exists"
