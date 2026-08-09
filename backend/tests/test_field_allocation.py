"""Field allocation — the 2026-07-30 live-job root causes, pinned.

Job 88491b56 (Medtronic invoice 4050032873) misallocated every soft field:
DESCRIPTION carried batch + qty echo + COO, COO fell back to the exporter
(Singapore, for Irish stents), MODEL took the GTIN barcode, SIZE missed the
``2.50X12RX`` dimension.  One OCR-misread quantity also disowned eleven pages
to the LLM.  These tests pin the deterministic fixes end-to-end:

* table parser: full-name COO capture, GTIN-skipping model, size-header
  dedupe against the qty column, exact-arithmetic quantity repair;
* rules.field_allocation: description splitting + COO mining + audit gates;
* rules.coo: progressive normalization of labelled/trailing-word raw COO;
* brand_model_size: GTIN never a model, letter-suffixed dimension sizes;
* extraction schema + prompt: the fields are DEFINED for the LLM;
* validator: the provable model/line-number swap is repairable.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.common_models import InvoiceChunkRaw, InvoiceLineRaw, PackingRowRaw
from app.extraction.openai_extractor import SYSTEM
from app.extraction.table_parser import _cells, _header_map, parse_pages
from app.extraction.validator import validate_invoice
from app.reference.store import get_reference
from app.rules.brand_model_size import resolve_model, resolve_size
from app.rules.coo import resolve_coo_for_item
from app.rules.field_allocation import (
    allocate_description,
    audit_items,
    normalize_coo_candidate,
)
from app.rules.invoice_authority import finalize_invoices
from app.rules.models import WorkItem


@pytest.fixture(scope="module")
def ref():
    return get_reference()


# --------------------------------------------------------------------------- #
# the live Medtronic layout, end-to-end through the table parser
# --------------------------------------------------------------------------- #
HEADER = "| MODEL NO. | DESCRIPTION | QTY SHIPPED | U/M | UNIT PRICE (USD) TOTAL (USD) |"
SEP = "| --- | --- | --- | --- | --- |"
ROW_OK = ("| RSINT25012X 00763000478896 | STENT RSINT25012X MICROTRAC 2.50X12RX "
          "Batch: 0013032995 3 EA COO: Ireland | 3 | EA | 225,00 675,00 |")
# the page-2 row that disowned the page: OCR read qty 28, money says 26 exactly
ROW_QTY_MISREAD = ("| RSINT27518X 00763000478933 | STENT RSINT27518X MICROTRAC 2.75X18RX "
                   "Batch: 0013032996 26 EA COO: Ireland | 28 | EA | 225,00 5.850,00 |")


def _parse_medtronic(*rows):
    page = "\n".join([HEADER, SEP, *rows])
    return parse_pages(DeclaredRole.INVOICE, {1: page}, {1: "EU"})


def test_parser_captures_model_coo_and_owns_the_medtronic_row():
    res = _parse_medtronic(ROW_OK)
    pp = res.pages[1]
    assert pp.confirmed and len(pp.rows) == 1
    r = pp.rows[0]
    assert r.model_raw == "RSINT25012X"            # the GTIN barcode is never the model
    assert r.country_of_origin_raw == "Ireland"    # full-name COO captured, not dropped
    assert (r.quantity_raw, r.uom_raw) == ("3", "EA")
    assert (r.unit_price_raw, r.line_total_raw) == ("225,00", "675,00")


def test_parser_derives_the_misread_quantity_instead_of_disowning_the_page():
    res = _parse_medtronic(ROW_OK, ROW_QTY_MISREAD)
    pp = res.pages[1]
    assert pp.confirmed and len(pp.rows) == 2      # the whole page stays parser-owned
    assert pp.rows[1].quantity_raw == "26"         # 5.850,00 / 225,00 exactly
    assert any("derived from total / price" in n for n in res.notes)


def test_qty_repair_is_exact_only_a_near_miss_still_disowns():
    bad = ("| RSINT27518X 00763000478933 | STENT X Batch: 1 26 EA COO: Ireland "
           "| 28 | EA | 225,00 5.851,00 |")       # 5851/225 = 26.004... — not exact
    res = _parse_medtronic(bad)
    assert not res.pages[1].confirmed


def test_mapped_origin_column_full_name_is_carried_not_dropped():
    page = "\n".join([
        "| Sl | Description | Origin | Qty | UOM | Rate | Amount |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| 1 | LEATHER WALLET | CHINA | 10 | PCS | 5.00 | 50.00 |",
    ])
    res = parse_pages(DeclaredRole.INVOICE, {1: page}, {1: "US"})
    assert res.pages[1].rows[0].country_of_origin_raw == "CHINA"


def test_size_header_never_claims_the_merged_qty_column():
    # Abbott "Qty UoM (Size)": qty, uom and size all matched one cell — SIZE
    # must be dropped so the quantity cell can never be reported as a size
    m = _header_map(_cells("| Line | Material | Material Description | Qty UoM (Size) "
                           "| Unit Price | Extended Price | GST |"))
    assert m is not None and "size" not in m
    m2 = _header_map(_cells("| Sl | Description | Size | Qty | Rate | Amount |"))
    assert m2 is not None and m2["size"] == 2      # a real size column still maps


# --------------------------------------------------------------------------- #
# description allocation
# --------------------------------------------------------------------------- #
MEDTRONIC_DESC = "STENT RSINT25012X MICROTRAC 2.50X12RX Batch: 0013032995 3 EA COO: Ireland"


def test_split_medtronic_description(ref):
    a = allocate_description(MEDTRONIC_DESC, ref)
    assert a.description == "STENT RSINT25012X MICROTRAC 2.50X12RX"
    assert a.coo_raw == "Ireland"
    assert a.batch_raw == "0013032995"
    assert "Batch: 0013032995" in (a.annotation or "")


def test_split_handles_llm_folded_abbott_subline(ref):
    a = allocate_description(
        "MLTGNT DIRECT LDL (Qty) Batch/Expiry Date (25) 69698UQ01/25.Jan.2028 "
        "Country of Origin JP Tariff Code 38220090", ref)
    assert a.description == "MLTGNT DIRECT LDL"
    assert a.coo_raw == "JP"


def test_coc_ocr_variant_of_coo_is_understood(ref):
    # live job page 6: OCR misread "COO" as "COC" — still split, still mined
    a = allocate_description(
        "STENT RSINT35015X MICROTRAC 3.50X15RX Batch: 0012082827 1 EA COC: Ireland "
        "0012697686 2 EA COC: Ireland", ref)
    assert a.description == "STENT RSINT35015X MICROTRAC 3.50X15RX"
    assert a.coo_raw == "Ireland"
    # normalize-gated: a certificate-of-conformity reference is never a country
    b = allocate_description("COC CERTIFIED PIPE FITTINGS", ref)
    assert b.description == "COC CERTIFIED PIPE FITTINGS"
    assert b.coo_raw is None


def test_leading_previous_row_overflow_is_stripped(ref):
    a = allocate_description(
        "0012747989 20 EA COO: Ireland STENT RSINT27522X MICROTRAC 2.75X22RX "
        "Batch: 0013032997 4 EA COO: Ireland", ref)
    assert a.description == "STENT RSINT27522X MICROTRAC 2.75X22RX"
    assert a.coo_raw == "Ireland"                  # the row's OWN (trailing) COO


def test_origin_first_description_keeps_its_name_but_yields_the_coo(ref):
    a = allocate_description("GENUINE MADE IN ITALY WALLET", ref)
    assert a.description == "GENUINE MADE IN ITALY WALLET"   # WALLET is not annotation
    assert a.coo_raw == "ITALY"


def test_glued_coo_clause_never_lets_a_real_word_escape_the_cut_check(ref):
    # "COO:Ireland" spans ONE token; the word after it is real description
    # material, so no cut may fire (regression: token-count arithmetic
    # mis-stepped over glued clauses)
    a = allocate_description("LEATHER BAG COO:Ireland WALLET", ref)
    assert a.description == "LEATHER BAG COO:Ireland WALLET"
    b = allocate_description("LEATHER BAG COO:Ireland", ref)
    assert b.description == "LEATHER BAG"
    assert b.coo_raw == "Ireland"


def test_plain_descriptions_are_never_touched(ref):
    for text in ("MLTGNT DIRECT LDL", "ICT SERUM CAL(10X10)", "Job Lot Mixed Hardware",
                 "SINGLE ORIGIN COFFEE", "Steel Rod 12 Batch", "BATCH MIXER 400"):
        a = allocate_description(text, ref)
        assert a.description == text, text
        assert a.batch_raw is None


def test_terminal_batch_cut_works_without_a_reference_store():
    a = allocate_description(MEDTRONIC_DESC, ref=None)
    assert a.description == "STENT RSINT25012X MICROTRAC 2.50X12RX"


# --------------------------------------------------------------------------- #
# COO progressive normalization + resolution ladder
# --------------------------------------------------------------------------- #
def test_normalize_coo_candidate(ref):
    assert normalize_coo_candidate("COO: Ireland", ref) == "IE"
    assert normalize_coo_candidate("Ireland Tariff Code", ref) == "IE"
    assert normalize_coo_candidate("Made in China", ref) == "CN"
    assert normalize_coo_candidate("United Arab Emirates", ref) == "AE"
    assert normalize_coo_candidate("WALLET", ref) is None      # bare non-country word
    assert normalize_coo_candidate(None, ref) is None


def _item(**kw):
    defaults = dict(xml_item_sequence=1, source_invoice_number="I", source_invoice_date="",
                    source_invoice_item_index=1, source_invoice_item_no=None,
                    description_raw="X", quantity=1, invoice_uom_raw="PCS",
                    unit_price=1, line_total=1, currency="USD")
    defaults.update(kw)
    return WorkItem(**defaults)


def test_labelled_raw_coo_resolves_item_level(ref):
    it = resolve_coo_for_item(_item(country_of_origin_raw="COO: Ireland"), "Singapore", ref)
    assert (it.coo_alpha2, it.coo_source) == ("IE", "ITEM_LEVEL")


# --------------------------------------------------------------------------- #
# model / size resolvers
# --------------------------------------------------------------------------- #
def test_gtin_is_never_a_model():
    assert resolve_model("00763000478896", None) == "NA"           # barcode-only cell
    assert resolve_model("RSINT25012X 00763000478896", None) == "RSINT25012X"
    assert resolve_model("00763000478896 RSINT25012X", None) == "RSINT25012X"


def test_letter_digit_code_beats_a_stray_count_in_the_cell():
    assert resolve_model("Qty 5 01R6070", None) == "01R6070"


def test_dimension_with_letter_suffix_yields_the_numeric_size():
    assert resolve_size(None, "STENT RSINT25012X MICROTRAC 2.50X12RX") == "2.50X12"
    assert resolve_size(None, "ICT SERUM CAL(10X10)") == "10X10"


# --------------------------------------------------------------------------- #
# ingest integration: the WorkItem comes out allocated
# --------------------------------------------------------------------------- #
def _chunk(desc, coo=None, inv_no=None):
    return InvoiceChunkRaw.model_validate({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "header": {"exporter": {"name_raw": "MEDTRONIC INTERNATIONAL LTD",
                                "country_raw": "Singapore"},
                   "invoice_number_raw": inv_no,
                   "invoice_date_raw": "24-FEB-2026" if inv_no else None},
        "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": desc,
                  "country_of_origin_raw": coo, "quantity_raw": "3", "uom_raw": "EA",
                  "unit_price_raw": "225.00", "line_total_raw": "675.00"}],
    })


def test_ingest_splits_description_and_mines_the_coo(ref):
    inv = finalize_invoices([_chunk(MEDTRONIC_DESC)], ref=ref)
    it = inv.items[0]
    assert it.description_raw == "STENT RSINT25012X MICROTRAC 2.50X12RX"
    assert it.evidence_description_raw == MEDTRONIC_DESC       # packing match keeps the print
    assert it.country_of_origin_raw == "Ireland"
    assert any(w.code == "DESCRIPTION_ANNOTATION_TRIMMED" for w in it.warnings)


def test_ingest_never_overrides_an_extractor_captured_coo(ref):
    inv = finalize_invoices([_chunk(MEDTRONIC_DESC, coo="IE")], ref=ref)
    assert inv.items[0].country_of_origin_raw == "IE"


def test_numbered_invoice_never_shadows_the_reference_store(ref):
    """2026-07-30 live 500: a NUMBERED invoice builds a roster InvoiceRef under
    the same local name as the ReferenceStore parameter, and the allocator then
    called InvoiceRef.normalize_country on any COO-labelled description.  The
    unnumbered fixtures above never create the roster entry, so only this shape
    exercises the store-wired path end to end."""
    inv = finalize_invoices([_chunk(MEDTRONIC_DESC, inv_no="4050032873")], ref=ref)
    it = inv.items[0]
    assert it.description_raw == "STENT RSINT25012X MICROTRAC 2.50X12RX"
    assert it.country_of_origin_raw == "Ireland"
    assert inv.invoice_refs and inv.invoice_refs[0].number == "4050032873"
    assert inv.invoice_refs[0].item_count == 1     # the roster entry still counts items


# --------------------------------------------------------------------------- #
# audit gates (P5)
# --------------------------------------------------------------------------- #
def test_audit_flags_barcode_model_and_annotation_bearing_description():
    it = _item()
    it.model = "00763000478896"
    it.description_raw = "STENT X Batch: 123 3 EA COO: Ireland"
    audit_items([it])
    codes = {w.code for w in it.warnings}
    assert {"MODEL_BARCODE_ONLY", "DESCRIPTION_EXTRA_INFO"} <= codes


def test_audit_is_silent_on_clean_items():
    it = _item()
    it.model = "RSINT25012X"
    it.description_raw = "STENT RSINT25012X MICROTRAC 2.50X12RX"
    audit_items([it])
    assert not it.warnings


# --------------------------------------------------------------------------- #
# the LLM contract: prompt + schema actually define these fields (P2)
# --------------------------------------------------------------------------- #
def test_prompt_defines_the_soft_fields():
    for needle in ("country_of_origin_raw", "model_raw", "size_raw", "brand_raw",
                   "batch_no_raw", "GTIN"):
        assert needle in SYSTEM, f"SYSTEM prompt no longer defines {needle}"


def test_schema_fields_carry_descriptions():
    for model, fields in ((InvoiceLineRaw, ("description_raw", "brand_raw", "model_raw",
                                            "size_raw", "country_of_origin_raw")),
                          (PackingRowRaw, ("description_raw", "model_raw",
                                           "country_of_origin_raw"))):
        schema = model.model_json_schema()
        for f in fields:
            spec = schema["properties"][f]
            blob = str(spec)
            assert "description" in str(spec).lower() and len(blob) > 60, (
                f"{model.__name__}.{f} lost its schema description — the LLM sees only "
                f"a bare field name again")


def test_invoice_rows_have_annotation_homes():
    for f in ("batch_no_raw", "lot_no_raw", "serial_no_raw", "expiry_date_raw"):
        assert f in InvoiceLineRaw.model_fields


# --------------------------------------------------------------------------- #
# validator: the provable model/line-number swap is sent for repair
# --------------------------------------------------------------------------- #
def _swap_payload(model, line_no):
    return InvoiceChunkRaw.model_validate({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "rows": [{"source_page_no": 1, "source_row_index": 1, "description_raw": "STENT",
                  "line_no_raw": line_no, "model_raw": model,
                  "quantity_raw": "3", "line_total_raw": "675.00"}],
    })


def test_validator_catches_the_model_line_no_swap():
    errors = validate_invoice(_swap_payload("00763000478896", "RSINT25012X"), {1: "STENT"})
    assert any("GTIN/EAN barcode" in e for e in errors)


def test_validator_accepts_correct_model_and_plain_line_numbers():
    errors = validate_invoice(_swap_payload("RSINT25012X", "10"), {1: "STENT"})
    assert not any("GTIN/EAN barcode" in e for e in errors)
