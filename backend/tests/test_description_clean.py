"""Deterministic item-description cleaner + code-only detector (audit 2026-07-20).

Pins the two failure modes the audit found on the H749… Quantum Apex extraction:
  * Mode A — a trailing Batch / Mfg Dt / Exp Dt annotation folded into the name;
  * Mode B — OCR kept only the bare part code and dropped the product name.
plus the false-positive guards that keep real names (dimensions, "Job Lot",
"Parking Lot", trailing model numbers) intact.
"""
import pytest

from app.rules.description_clean import clean_description, is_code_only

# the exact strings the user reported (item 1 carried a leading tab)
ITEM1 = ("\tH7493912408200 NC Quantum Apex MR 8mm x 2.00mm Batch / Mfg Dt / Exp Dt: "
         "36654442 - 02/JUN/2025 - 01/JUN/2028 36352488 - 23/APR/2025 - 22/APR/2028")
ITEM1_CLEAN = "H7493912408200 NC Quantum Apex MR 8mm x 2.00mm"


# --------------------------------------------------------------------------- #
# Mode A — trailing batch/mfg/expiry annotation is trimmed
# --------------------------------------------------------------------------- #
def test_item1_batch_mfg_exp_tail_is_trimmed():
    clean, trimmed = clean_description(ITEM1)
    assert clean == ITEM1_CLEAN
    assert trimmed is not None and trimmed.startswith("Batch")
    assert "36654442" in trimmed and "02/JUN/2025" in trimmed


def test_dimensions_are_preserved_exactly():
    # the mm dimensions must never be mistaken for annotation and dropped
    clean, _ = clean_description(ITEM1)
    assert clean.endswith("8mm x 2.00mm")


def test_various_label_forms_are_trimmed():
    cases = {
        "Gadget Lot: 12345": "Gadget",
        "Widget Batch 36654442": "Widget",
        "Tube Mfg Dt 01/JAN/2025 Exp Dt 01/JAN/2027": "Tube",
        "Cream Best Before 01/2025": "Cream",
        "Wire Serial No 998877": "Wire",
        "Vial Lot No 12 Exp 12/2026": "Vial",
    }
    for raw, expected in cases.items():
        clean, trimmed = clean_description(raw)
        assert clean == expected, f"{raw!r} -> {clean!r}"
        assert trimmed is not None


def test_label_less_trailing_date_run_is_trimmed():
    raw = "NC Apex MR 12mm x 2.00mm 36654442 - 02/JUN/2025 - 01/JUN/2028"
    clean, trimmed = clean_description(raw)
    assert clean == "NC Apex MR 12mm x 2.00mm"
    assert trimmed == "36654442 - 02/JUN/2025 - 01/JUN/2028"


# --------------------------------------------------------------------------- #
# false-positive guards — real names must survive untouched
# --------------------------------------------------------------------------- #
def test_clean_name_is_unchanged():
    for raw in ("NC Quantum Apex MR 12mm x 2.00mm", "Adaptar", "Arm Sleeve",
                "Artificial Eye Lashes"):
        clean, trimmed = clean_description(raw)
        assert clean == raw and trimmed is None


def test_lot_as_a_real_word_is_not_a_label():
    # "Lot" / "Batch" only trim when an all-filler trailing run follows them
    for raw in ("Job Lot 50 Assorted Widgets 2024", "Parking Lot Sign",
                "Bulk Lot Mixed Fabric"):
        clean, trimmed = clean_description(raw)
        assert clean == raw and trimmed is None


def test_trailing_lone_number_is_kept_as_a_possible_model_code():
    # a bare trailing number without a label or a date is never stripped
    for raw in ("Widget 12345", "Diary 2025", "Filter Model 4021"):
        clean, trimmed = clean_description(raw)
        assert clean == raw and trimmed is None


def test_leading_annotation_is_not_trimmed():
    # only TRAILING annotation is removed; a name after the label stays
    raw = "Best Before 01/2025 Milk Powder"
    clean, trimmed = clean_description(raw)
    assert clean == raw and trimmed is None


def test_word_containing_exp_is_not_a_label():
    for raw in ("Export Grade Basmati Rice 2024", "Expander Tool 25mm"):
        clean, trimmed = clean_description(raw)
        assert clean == raw and trimmed is None


def test_all_annotation_never_empties_the_description():
    # a cell that is only an annotation is kept verbatim, never blanked
    raw = "Batch 123 Exp 01/JAN/2025"
    clean, trimmed = clean_description(raw)
    assert clean == raw and trimmed is None


def test_blank_and_whitespace():
    assert clean_description("") == ("", None)
    assert clean_description("   ") == ("", None)
    assert clean_description("  Widget   Blue  ") == ("Widget Blue", None)


# --------------------------------------------------------------------------- #
# Mode B — code-only detector
# --------------------------------------------------------------------------- #
def test_code_only_fires_on_bare_part_and_gtin():
    assert is_code_only("H7493912412200") is True
    assert is_code_only("00763000726577") is True          # GTIN
    assert is_code_only("01E3120") is True                  # digit-led part
    assert is_code_only("  RONYX22515X ") is True


def test_code_only_false_when_a_name_is_present():
    for desc in ("H7493912408200 NC Quantum Apex MR 8mm x 2.00mm",
                 "NC Quantum Apex MR 12mm x 2.00mm",
                 "Bolt M8 x 40mm", "Adaptar", "Air Freshener"):
        assert is_code_only(desc) is False


def test_code_only_false_on_empty():
    assert is_code_only("") is False
    assert is_code_only("   ") is False


def test_code_only_ignores_barcode_label_prefix():
    assert is_code_only("GTIN 04006381333931") is True
    assert is_code_only("SKU 01E3120") is True


# --------------------------------------------------------------------------- #
# Adversarial regression corpus (audit 2026-07-20 verification workflow).
# Each case broke an earlier token-run implementation; pinned so the
# anchor-based cleaner can never regress on them.
# --------------------------------------------------------------------------- #
# (input, expected_clean) — must be left UNCHANGED (false-positive guards)
_MUST_KEEP = [
    # shop specs / grades that a naive date regex mistook for dates
    "Sandpaper Grit 40/60", "Machine Screw Thread 10/24", "Hex Nut Size 8/32",
    "Knitting Wool 10/20", "Sieve Mesh 20/40", "Wire Gauge 12/24",
    "Cement Bag OPC-53", "Steel Rod SAE-1018", "MDF Board MDF-18",
    "Steel Pipe Schedule PVC-40", "Plywood Sheet BWP-19", "Insulation Board XPS-50",
    # protected label-as-word phrases (no value follows the trigger)
    "Assorted Bandages Job Lot", "Sanitizer Parking Lot", "Steel Rod 12 Batch",
    "Export Grade Basmati Rice 2024", "Expander Tool 25mm",
    # leading (non-trailing) annotation
    "Best Before 01/2025 Milk Powder",
    # a bare trailing number is a possible model code, not an annotation
    "Widget 12345", "Diary 2025",
]

# (input, expected_clean) — a trailing annotation must be trimmed while the
# product name, dimensions AND any trailing model number survive
_MUST_TRIM = [
    ("Filter Model 4021 Batch 5567", "Filter Model 4021"),
    ("Cable 2024 Batch No 998", "Cable 2024"),
    ("Sensor 4021 Lot 88", "Sensor 4021"),
    ("Router Model 5300 Exp 12/2028", "Router Model 5300"),
    ("Amplifier 3000 Mfg Dt 01/2025", "Amplifier 3000"),
    ("USB-C Adapter Model 2024 Batch: 9910", "USB-C Adapter Model 2024"),
    ("Signal Booster Model 2025 Best Before 01/2029", "Signal Booster Model 2025"),
    ("Camera Model 8 Exp 2030", "Camera Model 8"),
    ("Vitamin C 500 Exp 12/2028", "Vitamin C 500"),
    ("Titanium Bar Grade 5 Batch 12345", "Titanium Bar Grade 5"),
    ("Bolt Class 8 Batch No 4432 Mfd 01/2025", "Bolt Class 8"),
    ("Hex Nut M12 Grade 10 Lot No 998", "Hex Nut M12 Grade 10"),
    ("Pipe Fitting 90 Batch/Exp Dt 2025/01/01", "Pipe Fitting 90"),
    # alphanumeric batch/lot/serial codes (not just pure numbers)
    ("Amoxicillin 500mg Batch AB1234", "Amoxicillin 500mg"),
    ("Cough Syrup 100ml Batch No B-778 Exp 12/2026", "Cough Syrup 100ml"),
    ("Vial 10ml Lot A12B3 Mfg 01/2025", "Vial 10ml"),
    ("Betadine 100ml MRP 120.00", "Betadine 100ml"),
    ("Radifocus Glidewire 0.035 x 150cm 5Fr Batch No AB-2291 Best Before 23-04-2028",
     "Radifocus Glidewire 0.035 x 150cm 5Fr"),
    ("NaviCross Support Catheter 2.6Fr x 135cm Lot: 7A/2024",
     "NaviCross Support Catheter 2.6Fr x 135cm"),
    # month-name dates, YYYY-MM, merged ranges, label glued to value
    ("Tomato Ketchup 500ml Best Before End Aug 2026", "Tomato Ketchup 500ml"),
    ("Cornflakes Cereal 750g Use By 10 NOV 2025", "Cornflakes Cereal 750g"),
    ("Frozen Peas 900g Best Before 2026-12", "Frozen Peas 900g"),
    ("Strawberry Jam 340g EXPIRY 2026/07", "Strawberry Jam 340g"),
    ("Frozen Peas 900g LOT 23A45B Best Before 2026-12", "Frozen Peas 900g"),
    ("Vitamin C Serum 02/JUN/2025-01/JUN/2028", "Vitamin C Serum"),
    ("Serum Exp.Dt:01/JUN/2028", "Serum"),
    # label-less trailing date run (OCR dropped the label): the batch-length
    # number is absorbed, the name + dimensions survive
    ("NC Apex MR 12mm x 2.00mm 36654442 - 02/JUN/2025 - 01/JUN/2028",
     "NC Apex MR 12mm x 2.00mm"),
]


@pytest.mark.parametrize("raw", _MUST_KEEP)
def test_adversarial_names_are_never_truncated(raw):
    clean, trimmed = clean_description(raw)
    assert clean == raw and trimmed is None


@pytest.mark.parametrize("raw,expected", _MUST_TRIM)
def test_adversarial_annotations_are_trimmed_without_losing_the_name(raw, expected):
    clean, trimmed = clean_description(raw)
    assert clean == expected, f"{raw!r} -> {clean!r}"
    assert trimmed is not None


# --------------------------------------------------------------------------- #
# Integration — finalize_invoices applies both, without perturbing matching
# --------------------------------------------------------------------------- #
from app.extraction.common_models import InvoiceChunkRaw  # noqa: E402
from app.rules.invoice_authority import finalize_invoices  # noqa: E402

_ROLE_OK = {"expected_role": "INVOICE", "matches_expected_role": True}


def _row(idx, desc):
    return {"source_page_no": 1, "source_row_index": idx, "description_raw": desc,
            "quantity_raw": "1", "uom_raw": "EA", "unit_price_raw": "100.00",
            "line_total_raw": "100.00"}


def _finalize(descs):
    chunk = InvoiceChunkRaw.model_validate(
        {"role_validation": _ROLE_OK,
         "rows": [_row(i + 1, d) for i, d in enumerate(descs)]})
    return finalize_invoices([chunk]).items


def test_finalize_cleans_description_and_preserves_original_for_matching():
    (item,) = _finalize([ITEM1])
    assert item.description_raw == ITEM1_CLEAN            # cleaned for the declaration
    # original printed text kept so packing-list matching is unchanged
    assert item.evidence_description_raw == ITEM1.strip()


def test_finalize_leaves_clean_description_and_sets_no_evidence_copy():
    (item,) = _finalize(["NC Quantum Apex MR 12mm x 2.00mm"])
    assert item.description_raw == "NC Quantum Apex MR 12mm x 2.00mm"
    assert item.evidence_description_raw is None          # nothing trimmed → no copy


def test_finalize_flags_code_only_row_for_review():
    (named, code_only) = _finalize(["NC Quantum Apex MR 8mm x 2.00mm", "H7493912412200"])
    assert not any(w.code == "DESCRIPTION_CODE_ONLY" for w in named.warnings)
    flags = [w for w in code_only.warnings if w.code == "DESCRIPTION_CODE_ONLY"]
    assert len(flags) == 1
    assert flags[0].scope == "ITEM" and flags[0].field == "description"
    assert flags[0].item_sequence == code_only.xml_item_sequence
