"""Step-1 hardening: deterministic pre-LLM gates.

* numeric-locale detection per page + locale-aware parse_decimal
* goods-row manifest from OCR markdown tables enforced as ROW_ANCHOR_MISSING

The final test reproduces the original sindhu failure shape (an extraction
that silently kept a small subset of rows) and proves the manifest gate now
rejects it deterministically.
"""
from decimal import Decimal

from app.extraction.common_models import InvoiceChunkRaw
from app.extraction.manifest import build_manifest, goods_row_anchors, uncovered_anchors
from app.extraction.validator import validate_invoice
from app.numbers import detect_numeric_locale, parse_decimal

US_PAGE = ("|  MODEL NO. | DESCRIPTION | QTY | U/M | UNIT PRICE | TOTAL |\n"
           "|  RONYX22515X 00763000248437 | STENT RONYX22515X ONYX 2.25X15RX | 3 | EA | 320.00 | 960.00  |\n"
           "|  RONYX22522X 00763000248451 | STENT RONYX22522X ONYX 2.25X22RX | 2 | EA | 320.00 | 640.00  |")

EU_PAGE = ("|  RONYX25018X 00763000248536 | STENT RONYX25018X ONYX 2.50X18RX | 5 | EA | 320,00 | 1.600,00  |\n"
           "|  RONYX25022X 00763000248543 | STENT RONYX25022X ONYX 2.50X22RX | 5 | EA | 320,00 | 1.600,00  |")


# --------------------------------------------------------------------------- #
# locale detection + locale-aware parsing
# --------------------------------------------------------------------------- #
def test_detect_locale():
    assert detect_numeric_locale(US_PAGE) == "US"
    assert detect_numeric_locale(EU_PAGE) == "EU"
    assert detect_numeric_locale("TOTAL (USD) | 1.600.00 |") == "EU"      # corrupted comma
    assert detect_numeric_locale("Weight: 13,000KG Dimensions 121X40") == "EU"
    assert detect_numeric_locale("no numbers here") is None


def test_parse_decimal_locale_disambiguation():
    assert parse_decimal("1.600", locale="EU") == Decimal("1600")
    assert parse_decimal("1.600", locale="US") == Decimal("1.600")
    assert parse_decimal("1.600") == Decimal("1.600")                     # unchanged default
    assert parse_decimal("13,000", locale="EU") == Decimal("13.000")     # 3-decimal kg label
    assert parse_decimal("13,000", locale="US") == Decimal("13000")
    assert parse_decimal("1.234,56", locale="EU") == Decimal("1234.56")
    assert parse_decimal("1.600,00", locale="EU") == Decimal("1600.00")


# --------------------------------------------------------------------------- #
# manifest construction
# --------------------------------------------------------------------------- #
def test_manifest_anchors_from_markdown_tables():
    anchors = goods_row_anchors(1, US_PAGE)
    assert len(anchors) == 2
    assert "00763000248437" in anchors[0].tokens
    assert "RONYX22515X" in anchors[0].tokens


def test_manifest_ignores_fragments_headers_and_free_text():
    page = ("|  MODEL NO. | DESCRIPTION | QTY | U/M | UNIT PRICE | TOTAL |\n"     # header
            "|  00763000726577 | Batch: 232529045 20 EA COO: Mexico |  |  |   |\n"  # fragment
            "|  SSCC: 4000000145550494 | Box: SG CARTON J | Dimensions: 121X40X44CM |  |  | Weight: 13,000KG |\n"
            "PLEASE QUOTE INVOICE NUMBER FOR ALL PAYMENTS\n"
            "|  TOTAL (USD) | 89,975.52  |")
    assert goods_row_anchors(1, page) == []


def test_manifest_skips_pages_without_tables():
    assert build_manifest({1: "plain text page, no tables, demo fixture style"}) == {}


# --------------------------------------------------------------------------- #
# the gate: uncovered anchors are hard validation errors
# --------------------------------------------------------------------------- #
def _payload(rows):
    return InvoiceChunkRaw.model_validate({
        "role_validation": {"expected_role": "INVOICE", "matches_expected_role": True},
        "rows": rows,
    })


ROW_22515 = {"source_page_no": 1, "source_row_index": 1,
             "description_raw": "STENT RONYX22515X ONYX 2.25X15RX", "quantity_raw": "3",
             "uom_raw": "EA", "unit_price_raw": "320.00", "line_total_raw": "960.00"}


def test_covered_anchor_passes():
    row2 = dict(ROW_22515, source_row_index=2, description_raw="STENT RONYX22522X ONYX 2.25X22RX")
    errors = validate_invoice(_payload([ROW_22515, row2]), {1: US_PAGE})
    assert not [e for e in errors if "ROW_ANCHOR_MISSING" in e]


def test_missing_row_is_hard_error():
    errors = validate_invoice(_payload([ROW_22515]), {1: US_PAGE})
    missing = [e for e in errors if "ROW_ANCHOR_MISSING" in e]
    assert len(missing) == 1 and "RONYX22522X" in missing[0]


def test_sindhu_failure_shape_is_now_rejected():
    """The original disaster: a 'corrected' extraction kept 1 row per page
    subset while other pages printed goods rows — must now fail loudly."""
    pages = {1: US_PAGE, 2: EU_PAGE}
    errors = validate_invoice(_payload([ROW_22515]), pages)
    assert any("ROW_ANCHOR_MISSING" in e and "page 1" in e for e in errors)   # dropped sibling row
    assert any("PAGE_ROWS_MISSING" in e and "page 2" in e for e in errors)    # dropped whole page
    # coverage is token-based, so an OCR-faithful extraction always passes;
    # a subset resend can never validate again
    covered = [ROW_22515,
               dict(ROW_22515, source_row_index=2, description_raw="STENT RONYX22522X 2.25X22RX"),
               dict(ROW_22515, source_page_no=2, description_raw="STENT RONYX25018X", line_total_raw="1.600,00"),
               dict(ROW_22515, source_page_no=2, source_row_index=2, description_raw="STENT RONYX25022X",
                    line_total_raw="1.600,00")]
    assert not [e for e in validate_invoice(_payload(covered), pages) if "MISSING" in e]


def test_uncovered_anchor_matches_on_gtin_or_part():
    # extraction echoed only the GTIN in line_no_raw — still covered
    row = dict(ROW_22515, description_raw="stent item", line_no_raw="00763000248437")
    row2 = dict(ROW_22515, source_row_index=2, description_raw="stent 22522",
                line_no_raw="RONYX22522X")
    assert uncovered_anchors(_payload([row, row2]).rows, {1: US_PAGE},
                             ("line_no_raw", "description_raw")) == []
