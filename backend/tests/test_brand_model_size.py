"""Deterministic BRAND / MODEL / SIZE resolver, parser capture, and the sibling
``.xls`` export (feature 2026-07-21).

Pins the user-decided rules:
  * BRAND — brand column first word (UPPER), else exporter first word, else NA.
  * MODEL — model/part/SKU code cell (UPPER), else a *labelled* code in the
    description; a bare size/word is never mined.  Else NA.
  * SIZE  — a size column verbatim, else a unit-bearing token from the
    description; garment letters are not guessed from prose.  Else NA.
  * Export .xls: blank/BRAND/MODEL/SIZE header, one row per item in XML order.
"""
import types
from types import SimpleNamespace

import pytest
import xlrd

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import parse_pages
from app.pipeline import finalize, resolve_context, to_critical_review
from app.review.critical_review import CriticalReviewConfirmation, merge_confirmation
from app.rules.brand_model_size import (
    NA,
    apply_edits,
    resolve_all,
    resolve_brand,
    resolve_model,
    resolve_size,
)
from app.xml.bms_export import build_bms_xls, checksum


# --------------------------------------------------------------------------- #
# BRAND
# --------------------------------------------------------------------------- #
def test_brand_from_column_first_word_upper():
    assert resolve_brand("Samsung Electronics Co", "Acme Traders") == "SAMSUNG"


def test_brand_falls_back_to_exporter_first_word():
    assert resolve_brand(None, "Abbott Laboratories") == "ABBOTT"
    assert resolve_brand("", "Abbott GmbH") == "ABBOTT"


def test_brand_na_when_nothing():
    assert resolve_brand(None, None) == NA
    assert resolve_brand("   ", "  ") == NA


# Courtesy / legal-form prefixes must not become the brand ("M/S" is ubiquitous
# on South Asian invoices — it previously yielded BRAND="M" on every row).
@pytest.mark.parametrize("exporter,expected", [
    ("M/S. Abbott Laboratories", "ABBOTT"),
    ("M/s Sky Moon Pvt Ltd", "SKY"),
    ("Messrs Bayer AG", "BAYER"),
    ("PT. Samsung Electronics Indonesia", "SAMSUNG"),
    ("The Nike Company", "NIKE"),
    ("A. Menarini Diagnostics", "MENARINI"),      # bare initial skipped
    ("Abbott Laboratories", "ABBOTT"),
    ("3M Company", "3M"),                          # a digit-led real name survives
    ('"Roche Diagnostics"', "ROCHE"),
])
def test_brand_skips_company_form_prefixes(exporter, expected):
    assert resolve_brand(None, exporter) == expected


# --------------------------------------------------------------------------- #
# MODEL
# --------------------------------------------------------------------------- #
def test_model_from_column_uppercased():
    assert resolve_model("01r6070", "ARCHITECT Reagent") == "01R6070"


def test_model_column_takes_first_token():
    assert resolve_model("01E3120 (Qty)", "x") == "01E3120"


# Vendors routinely print the label INSIDE the code cell; the label must be
# stripped and the code kept (this previously returned "REF" / "P/N" / "CFN").
@pytest.mark.parametrize("cell,expected", [
    ("REF 01R6070", "01R6070"),
    ("P/N: AB-12", "AB-12"),
    ("CFN 07K5901", "07K5901"),
    ("Model 4021", "4021"),
    ("ART. 12345", "12345"),
    ("Item Code: XY-9", "XY-9"),
    ("01R6070", "01R6070"),            # unlabelled cell unchanged
    ("STYLECODE", "STYLECODE"),        # digit-less cell is still trusted
])
def test_model_cell_is_label_aware(cell, expected):
    assert resolve_model(cell, "Some description") == expected


# --------------------------------------------------------------------------- #
# Placeholder cells behave as EMPTY for all three fields (they used to be
# emitted as data: a brand cell of "N/A" yielded BRAND="N")
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("placeholder", ["-", "--", "N/A", "n/a", "NIL", "none", "TBD", "0", "?"])
def test_placeholder_cells_are_treated_as_absent(placeholder):
    assert resolve_size(placeholder, "Plain Fabric") == NA
    assert resolve_model(placeholder, "Plain Fabric") == NA
    # brand falls through the placeholder to the exporter
    assert resolve_brand(placeholder, "Acme Industries Ltd") == "ACME"


def test_model_labelled_in_description():
    assert resolve_model(None, "Air Filter Model 4021 Heavy Duty") == "4021"
    assert resolve_model(None, "Bearing P/N: AB-12/34") == "AB-12/34"
    assert resolve_model(None, "Reagent Cat No 01R6070") == "01R6070"
    assert resolve_model(None, "Widget Article No 12345") == "12345"
    assert resolve_model(None, "Bracket Ref: R-2024") == "R-2024"


def test_model_na_without_column_or_label():
    # a bare size token or plain word is never mistaken for a model
    assert resolve_model(None, "Samsung SSD 128GB") == NA
    assert resolve_model(None, "Plain Cotton T-Shirt") == NA


@pytest.mark.parametrize("desc", [
    "Artificial Eye Lashes",     # 'art' must not match inside 'Artificial'
    "Cartoon Sticker Set",       # 'art' must not match inside 'Cartoon'
    "Partition Board",           # 'part' must not match inside 'Partition'
    "Modeling Clay",             # 'model' must not match inside 'Modeling'
    "Reference Guide Book",      # label present but the next token has no digit
    "Style Guide Blue",          # 'style' label, following word has no digit
])
def test_model_no_false_positive_from_english_words(desc):
    # regression: a label-shaped prefix of an ordinary word never yields a model
    assert resolve_model(None, desc) == NA


# --------------------------------------------------------------------------- #
# SIZE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,expected", [
    ("Samsung SSD 1TB", "1TB"),
    ("USB Drive 128GB", "128GB"),
    ("Cough Syrup 500ml Bottle", "500ml"),
    ("Water 2L", "2L"),
    ("Steel Rod 42mm dia", "42mm"),
    ("Carton 10x20cm", "10x20cm"),
    ("Reagent 10 x 113 mL", "10 x 113 mL"),
    ("Monitor 55 Inch", "55 Inch"),
    ("Rice Bag 5kg", "5kg"),
    ("Shoe 40 EU", "40 EU"),
])
def test_size_from_description(desc, expected):
    assert resolve_size(None, desc) == expected


def test_size_column_is_trusted_and_normalised():
    # a size COLUMN is authoritative: a pack config is kept exactly as printed…
    assert resolve_size("10 × 113 mL", "anything") == "10 × 113 mL"
    assert resolve_size("1 EA", "anything") == "1 EA"
    # …but a short form is still expanded to a full word
    assert resolve_size("XL", "Plain T-Shirt") == "EXTRA LARGE"
    assert resolve_size("M", "Plain T-Shirt") == "MEDIUM"


def test_size_na_when_nothing_found():
    assert resolve_size(None, "Plain Cotton Fabric") == NA
    assert resolve_size(None, "") == NA


# --------------------------------------------------------------------------- #
# SIZE — a quantity is never a size (the 2026-07-21 defect, fixed at the root:
# count/packaging units are absent from the vocabulary entirely)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,expected", [
    ("Card Reader 5 PCS", NA),                       # qty only -> no size at all
    ("ARCHITECT Reagent 1 EA", NA),
    ("Widget 10 PCS 500ml", "500ml"),                # spec after the qty still wins
    ("Reagent Kit 2 EA 10 x 113 mL", "10 x 113 mL"),
    ("Bolt M8 4 PCS 40mm", "40mm"),
    ("5 PCS Cotton Towel 10x20cm", "10x20cm"),       # qty first, spec later
    ("Hand Sanitizer 20 NOS 250 ml", "250 ml"),
    ("Samsung SSD 1TB 2 PCS", "1TB"),
])
def test_size_never_reports_a_quantity(desc, expected):
    assert resolve_size(None, desc) == expected


# --------------------------------------------------------------------------- #
# SIZE — word sizes, always full words, never a short form
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,expected", [
    ("T-Shirt L", "LARGE"),
    ("Polo Shirt XL", "EXTRA LARGE"),
    ("Hoodie 2XL", "EXTRA EXTRA LARGE"),
    ("Jacket XXL", "EXTRA EXTRA LARGE"),
    ("Vest XS", "EXTRA SMALL"),
    ("Cotton Shirt Large", "LARGE"),
    ("Trousers Medium", "MEDIUM"),
    ("Cap Free Size", "FREE SIZE"),
    ("Shirt S/M/L", "SMALL/MEDIUM/LARGE"),
])
def test_size_word_sizes_are_expanded(desc, expected):
    assert resolve_size(None, desc) == expected


# --------------------------------------------------------------------------- #
# SIZE — a letter bound to a number stays a UNIT; standalone it is a word size
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desc,expected", [
    ("Bottle 1L", "1L"),                 # litre, not LARGE
    ("Water Tank 20 L", "20 L"),
    ("Cable 5M", "5M"),                  # metres, not MEDIUM
    ("Rope 20 M", "20 M"),
    ("Plate 20 X 30MM", "20 X 30MM"),
    ("Bottle 1L Large", "1L"),           # a measured spec outranks a word size
])
def test_size_unit_letter_beats_word_size(desc, expected):
    assert resolve_size(None, desc) == expected


@pytest.mark.parametrize("desc", ["Filter Model L", "Steel Grade S", "Pump Type M",
                                  "Board Class L", "Sensor Series S"])
def test_size_designator_letter_is_not_a_size(desc):
    assert resolve_size(None, desc) == NA


def test_size_a4_not_read_as_amps():
    # "3 A4" must not become "3 A"
    assert resolve_size(None, "Copier Paper 3 A4 reams") != "3 A"


# --------------------------------------------------------------------------- #
# resolve_all — the in-place pass
# --------------------------------------------------------------------------- #
def _wi(**kw):
    base = dict(brand_raw=None, model_raw=None, size_raw=None,
                description_raw="", brand=None, model=None, size=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_resolve_all_sets_and_respects_preset():
    src = _wi(brand_raw="Abbott", model_raw="01R6070",
              description_raw="Reagent 10 x 113 mL")
    preset = _wi(brand_raw="X", model_raw="Y", size_raw="Z",
                 description_raw="d", brand="KEEP")     # brand pre-set
    resolve_all([src, preset], exporter_name="Global Traders")
    assert (src.brand, src.model, src.size) == ("ABBOTT", "01R6070", "10 x 113 mL")
    assert preset.brand == "KEEP"                       # pre-set value untouched
    assert preset.model == "Y" and preset.size == "Z"


def test_resolve_all_exporter_brand_fallback():
    it = _wi(description_raw="Generic Widget")
    resolve_all([it], exporter_name="Abbott Laboratories Pvt Ltd")
    assert it.brand == "ABBOTT" and it.model == NA and it.size == NA


# --------------------------------------------------------------------------- #
# Parser capture — brand/model/size columns land on the raw row
# --------------------------------------------------------------------------- #
def _parse_one(text):
    return parse_pages(DeclaredRole.INVOICE, {1: text}, {1: None}).pages[1]


def test_parser_captures_brand_model_size_columns():
    header = "| MODEL NO | BRAND | DESCRIPTION | SIZE | QTY | U/M | UNIT PRICE | TOTAL |"
    row = "| 01R6070 | Abbott | ARCHITECT Anti-HCV Reagent | 10 x 113 mL | 3 | EA | 100.00 | 300.00 |"
    pp = _parse_one("\n".join([header, row]))
    assert pp.confirmed and len(pp.rows) == 1
    r = pp.rows[0]
    assert r.model_raw == "01R6070"
    assert r.brand_raw == "Abbott"
    assert r.size_raw == "10 x 113 mL"


# --------------------------------------------------------------------------- #
# .xls export
# --------------------------------------------------------------------------- #
def _item(seq, brand, model, size):
    return types.SimpleNamespace(xml_item_sequence=seq, brand=brand, model=model, size=size)


def test_xls_layout_order_and_headers():
    items = [
        _item(1, "ABBOTT", "01H7301", "20 L"),
        _item(2, "ABBOTT", "01R6070", "10 × 113 mL"),
        _item(3, "ABBOTT", "07P9220", "NA"),
    ]
    sh = xlrd.open_workbook(file_contents=build_bms_xls(items)).sheet_by_index(0)
    assert [sh.cell_value(0, c) for c in range(4)] == ["", "BRAND", "MODEL", "SIZE"]
    assert sh.cell_value(1, 0) == 1 and sh.cell_value(1, 1) == "ABBOTT"
    assert sh.cell_value(2, 2) == "01R6070" and sh.cell_value(2, 3) == "10 × 113 mL"
    assert sh.cell_value(3, 3) == "NA"
    assert sh.nrows == 4


def test_xls_sorts_by_sequence():
    items = [_item(2, "B", "M2", "S2"), _item(1, "A", "M1", "S1")]
    sh = xlrd.open_workbook(file_contents=build_bms_xls(items)).sheet_by_index(0)
    assert sh.cell_value(1, 0) == 1 and sh.cell_value(1, 1) == "A"
    assert sh.cell_value(2, 0) == 2 and sh.cell_value(2, 1) == "B"


def test_xls_checksum_is_hex64():
    data = build_bms_xls([_item(1, "A", "M", "S")])
    cs = checksum(data)
    assert len(cs) == 64 and all(c in "0123456789abcdef" for c in cs)


# --------------------------------------------------------------------------- #
# End-to-end: raw invoice -> resolve_context -> finalize -> .xls
# --------------------------------------------------------------------------- #
_E2E_INVOICE = {
    "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
    "page_numbers": [1],
    "header": {"invoice_number_raw": "INV-BMS", "invoice_date_raw": "01.07.2026",
               "currency_raw": "USD",
               "exporter": {"name_raw": "Abbott Laboratories", "country_raw": "UNITED STATES"},
               "consignee": {"name_raw": "KTM IMPORTS", "exim_code_raw": "1234567890123"}},
    "rows": [
        # no brand column -> BRAND falls back to the exporter (ABBOTT); MODEL
        # from the model cell; SIZE parsed from the description
        {"source_page_no": 1, "source_row_index": 1,
         "description_raw": "ARCHITECT Reagent 10 x 113 mL", "model_raw": "01R6070",
         "quantity_raw": "3", "uom_raw": "EA", "unit_price_raw": "100.00", "line_total_raw": "300.00"},
        # per-row brand column wins; no code -> MODEL NA; SIZE from description
        {"source_page_no": 1, "source_row_index": 2,
         "description_raw": "Samsung SSD 1TB", "brand_raw": "Samsung",
         "quantity_raw": "1", "uom_raw": "PCS", "unit_price_raw": "200.00", "line_total_raw": "200.00"},
    ],
    "totals": {"grand_total_raw": "500.00"},
}


def _e2e_docs():
    return [SimpleNamespace(declared_role="INVOICE", upload_index_within_role=0,
                            original_file_name="inv.pdf", raw_extraction=_E2E_INVOICE)]


def test_end_to_end_brand_model_size_reaches_declaration_and_xls():
    docs = _e2e_docs()
    ctx = resolve_context(docs)
    assert [i.brand for i in ctx.items] == ["ABBOTT", "SAMSUNG"]
    assert [i.model for i in ctx.items] == ["01R6070", NA]
    assert [i.size for i in ctx.items] == ["10 x 113 mL", "1TB"]

    review = to_critical_review(ctx, docs)
    conf = CriticalReviewConfirmation(confirmed_gross_weight="10",
                                      confirmed_total_packages="2", bill_of_lading_no="MBL-1")
    reviewed, _ = merge_confirmation(review, conf)
    decl = finalize(ctx, reviewed)

    # frozen onto the declaration, in XML order
    assert [i.xml_item_sequence for i in decl.items] == [1, 2]
    assert [i.brand for i in decl.items] == ["ABBOTT", "SAMSUNG"]
    assert [i.model for i in decl.items] == ["01R6070", NA]
    assert [i.size for i in decl.items] == ["10 x 113 mL", "1TB"]

    sh = xlrd.open_workbook(file_contents=build_bms_xls(decl.items)).sheet_by_index(0)
    assert sh.nrows == 3
    assert [sh.cell_value(1, c) for c in range(4)] == [1, "ABBOTT", "01R6070", "10 x 113 mL"]
    assert [sh.cell_value(2, c) for c in range(4)] == [2, "SAMSUNG", "NA", "1TB"]


# --------------------------------------------------------------------------- #
# Reviewer overrides (apply_edits) — a stored value always beats the derived one
# --------------------------------------------------------------------------- #
def test_apply_edits_override_wins_over_deterministic():
    it = _wi(item_id="src:1", brand_raw="Abbott", model_raw="01R6070",
             description_raw="Reagent 10 x 113 mL")
    apply_edits([it], {"src:1": {"brand": "Roche", "size": "5 mL"}})
    resolve_all([it], exporter_name="Abbott Laboratories")
    assert it.brand == "Roche"          # reviewer value, verbatim (not upper-cased)
    assert it.size == "5 mL"
    assert it.model == "01R6070"        # untouched field still resolves deterministically


def test_apply_edits_absent_override_restores_deterministic():
    it = _wi(item_id="src:1", brand_raw="Abbott", description_raw="Widget 2L")
    apply_edits([it], {"src:1": {}})     # cleared -> nothing pre-set
    resolve_all([it], exporter_name="Global Traders")
    assert it.brand == "ABBOTT" and it.size == "2L"


def test_apply_edits_ignores_unknown_items_and_blanks():
    it = _wi(item_id="src:1", description_raw="Widget")
    apply_edits([it], {"src:OTHER": {"brand": "X"}, "src:1": {"brand": "   "}})
    resolve_all([it], exporter_name="Acme Ltd")
    assert it.brand == "ACME"            # blank override ignored, exporter fallback stands
