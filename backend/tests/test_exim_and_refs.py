"""The deterministic labelled-reference scan (user rules 2026-08-06):

* ALWAYS look for the EXIM / IEC code on the invoice — it is the importer code
  the XML blocks on, and it prints as often in a footer line as inside the
  consignee block;
* read the bill-of-lading number and date the invoice states, so a sea/land
  job has its transport reference even with no transport document uploaded.

The scan never overwrites an extracted value and reports every fill.
"""
from types import SimpleNamespace

from app.domain.enums import DeclaredRole
from app.extraction.common_models import InvoiceChunkRaw
from app.extraction.document_refs import (
    backfill_document_refs,
    find_bill_of_lading,
    find_exim_codes,
)
from app.pipeline import resolve_context, to_critical_review

_PAGE = """COMMERCIAL INVOICE
Exporter: ACME EXPORTS PVT LTD, MUMBAI, INDIA
IEC NO: AABCA1234K
Consignee: KTM IMPORTS PVT LTD, KATHMANDU, NEPAL
EXIM CODE : 1234567890123
PAN NO: 987654321
B/L NO: HLCUBOM240012345   B/L DATE: 24-FEB-2026
"""


def _invoice_payload(**header):
    return InvoiceChunkRaw.model_validate({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "page_numbers": [1],
        "header": {"invoice_number_raw": "INV-1", **header},
        "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET",
                  "quantity_raw": "1", "line_total_raw": "10.00"}],
    })


# --------------------------------------------------------------------------- #
# the scan
# --------------------------------------------------------------------------- #
def test_exim_codes_are_attributed_to_the_party_they_print_under():
    hits = find_exim_codes({1: _PAGE})
    assert [(h.party, h.value) for h in hits] == [
        ("EXPORTER", "AABCA1234K"), ("IMPORTER", "1234567890123")]


def test_exim_is_read_out_of_a_table_cell_and_a_pan_is_not_an_exim():
    hits = find_exim_codes({1: "| EXIM NO. | 987654321098765 |\n| PAN | 300123456 |"})
    assert [(h.party, h.value) for h in hits] == [("IMPORTER", "987654321098765")]


def test_bill_of_lading_number_and_date_are_read_together():
    num, date = find_bill_of_lading({1: _PAGE})
    assert num.value == "HLCUBOM240012345"
    assert date.value == "24-FEB-2026"


def test_goods_rows_never_donate_a_bl_number():
    """Regression: "B/L" matched the "Bl" of "Blower Fan" on a goods row and
    handed the row's HS code back as the shipment's bill-of-lading number."""
    rows = ("19 Blower Fan 84145900000 PCS 3 0.92 2.76\n"
            "20 Blanket 63019000000 PCS 1 12.00 12.00\n"
            "21 Table 94036000000 PCS 2 30.00 60.00")
    assert find_bill_of_lading({1: rows}) == (None, None)


def test_a_bill_of_lading_mentioned_in_prose_yields_no_number():
    assert find_bill_of_lading({1: "3/3 original bill of lading required at destination"})[0] is None


def test_shipped_on_board_date_counts_as_the_bl_date():
    num, date = find_bill_of_lading(
        {1: "| BILL OF LADING NO | MAEU12345678 |\n| SHIPPED ON BOARD DATE | 05/03/2026 |"})
    assert (num.value, date.value) == ("MAEU12345678", "05/03/2026")


# --------------------------------------------------------------------------- #
# the backfill
# --------------------------------------------------------------------------- #
def test_backfill_fills_only_what_the_extractor_missed():
    payload = _invoice_payload(consignee={"name_raw": "KTM IMPORTS",
                                          "exim_code_raw": "9999999999999"})
    notes = backfill_document_refs(DeclaredRole.INVOICE, payload, {1: _PAGE})
    # the extracted importer code is never overwritten…
    assert payload.header.consignee.exim_code_raw == "9999999999999"
    # …but the exporter's, and the B/L reference, were missing and are filled
    assert payload.header.exporter.exim_code_raw == "AABCA1234K"
    assert payload.header.bill_of_lading_number_raw == "HLCUBOM240012345"
    assert payload.header.bill_of_lading_date_raw == "24-FEB-2026"
    assert any(n.startswith("EXIM_CODE_SCANNED") for n in notes)
    assert any(n.startswith("BL_NUMBER_SCANNED") for n in notes)


def test_backfill_gives_the_importer_the_code_when_nothing_was_extracted():
    payload = _invoice_payload()
    backfill_document_refs(DeclaredRole.INVOICE, payload, {1: _PAGE})
    assert payload.header.consignee.exim_code_raw == "1234567890123"


def test_backfill_is_a_no_op_when_the_document_prints_neither():
    payload = _invoice_payload(consignee={"name_raw": "KTM IMPORTS"})
    assert backfill_document_refs(DeclaredRole.INVOICE, payload,
                                  {1: "INVOICE\nWIDGET 1 PCS 10.00"}) == []
    assert payload.header.consignee.exim_code_raw is None


# --------------------------------------------------------------------------- #
# through the pipeline: the EXIM code reaches the review
# --------------------------------------------------------------------------- #
_INVOICE_RAW = {
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1],
    "header": {"invoice_number_raw": "INV-1", "invoice_date_raw": "01.07.2026",
               "currency_raw": "USD",
               "exporter": {"name_raw": "ACME LTD", "country_raw": "INDIA"},
               "consignee": {"name_raw": "KTM IMPORTS"},
               # printed outside both party blocks (footer line)
               "exim_code_raw": "1234567890123"},
    "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "WIDGET",
              "quantity_raw": "1", "uom_raw": "PCS", "unit_price_raw": "10.00",
              "line_total_raw": "10.00"}],
    "totals": {"grand_total_raw": "10.00"},
}


def _docs(raw):
    return [SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                            original_file_name="inv.pdf", raw_extraction=raw)]


def test_document_level_exim_becomes_the_importer_code():
    docs = _docs(_INVOICE_RAW)
    review = to_critical_review(resolve_context(docs), docs)
    assert review.importer.exim_code == "1234567890123"
    assert review.importer_exim_valid is True
    assert any(w.code == "IMPORTER_EXIM_FROM_DOCUMENT_BODY" for w in review.warnings)


def test_no_exim_anywhere_is_said_plainly():
    raw = {**_INVOICE_RAW, "header": {**_INVOICE_RAW["header"], "exim_code_raw": None}}
    docs = _docs(raw)
    review = to_critical_review(resolve_context(docs), docs)
    codes = {w.code for w in review.warnings}
    assert "IMPORTER_EXIM_NOT_ON_INVOICE" in codes
    assert review.importer_exim_valid is False
