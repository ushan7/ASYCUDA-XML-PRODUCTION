"""Final blocking validation of the merged declaration.

XML is produced only when there are zero blocking errors.  Reconciliation uses
Decimal with a small rounding tolerance; a genuinely impossible set of values
blocks rather than being silently distorted.  Header gates mirror the Critical
Review spec: importer EXIM 13–15 alphanumeric, supported package kind, Field 40
present for air shipments, single invoice currency, bank code/name as an exact
reference pair, payment-term code in the reference.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..domain.enums import Severity
from ..domain.errors import ValidationMessage
from ..reference.store import get_reference
from ..review.critical_review import PACKAGE_TYPES
from .models import MergedDeclaration

_TOL = Decimal("0.05")

# Blocking codes that warn mode may NEVER bypass.  Warn mode exists so a
# reviewer can test an otherwise-complete XML in real ASYCUDA; that premise
# fails when the declaration has no item weights at all, because there is
# nothing meaningful to test and the file would assert a zero gross weight per
# item.  These stop the build regardless of EASYCUSTOMS_XML_STRICT_BLOCKING.
#
# INVOICE_DUPLICATE_DOCUMENT fails the same premise from the other direction:
# the declaration is not incomplete, it is WRONG — every goods row of one
# invoice is present twice, so the customs value and the duty are overstated
# (2x for a single invoice uploaded twice).  There is nothing to learn from
# testing that file in ASYCUDA, and it is the one warn-mode case where the
# unresolved condition is invisible in the item grid the reviewer checks.
#
# The carton codes join them for the same reason on the other column: a
# package count that does not reconcile to the authorised total, or that sits
# off the 0.01 lattice, is rejected by ASYCUDA rather than tested by it.
WARN_MODE_HARD_CODES = frozenset({
    "GROSS_ALLOCATION_IMPOSSIBLE",
    "REVIEWED_GROSS_EXCEEDS_AUTHORITY",
    "REVIEWED_GROSS_TOTAL_MISMATCH",
    "WEIGHT_RECONCILIATION_IMPOSSIBLE",
    "INVOICE_DUPLICATE_DOCUMENT",
    "REVIEWED_CTN_EXCEEDS_AUTHORITY",
    "REVIEWED_CTN_TOTAL_MISMATCH",
    "CARTON_LATTICE_VIOLATION",
    "CARTON_RECONCILIATION_FAILED",
})
_EXIM_RE = re.compile(r"[A-Za-z0-9]{13,15}")


def validate_declaration(decl: MergedDeclaration) -> MergedDeclaration:
    ref = get_reference()
    errors: list[ValidationMessage] = []
    warnings: list[ValidationMessage] = []

    if not decl.items:
        errors.append(ValidationMessage.blocking("INVOICE_NO_VALID_SOURCE", "Declaration has no items"))

    # escalate engine-level conditions that the spec defines as hard blockers
    for w in list(decl.warnings):
        if w.code in ("MIXED_INVOICE_CURRENCIES", "INVOICE_DUPLICATE_DOCUMENT"):
            errors.append(ValidationMessage.blocking(w.code, w.message))
            decl.warnings.remove(w)

    # ---- header gates (Critical Review spec) ------------------------------ #
    if not _EXIM_RE.fullmatch(decl.parties.consignee_code or ""):
        errors.append(ValidationMessage.blocking(
            "IMPORTER_EXIM_INVALID",
            f"Importer EXIM code {decl.parties.consignee_code!r} must be 13–15 alphanumeric characters."))
    if decl.package_type_code not in PACKAGE_TYPES:
        errors.append(ValidationMessage.blocking(
            "PACKAGE_TYPE_UNSUPPORTED",
            f"Package type {decl.package_type_code!r} is not one of {', '.join(PACKAGE_TYPES)}."))

    # ---- regime & transport gates (reference-gated per-job selections) ----- #
    if (decl.declaration_type, decl.gen_procedure_code) not in ref.declaration_model_pairs:
        errors.append(ValidationMessage.blocking(
            "DECLARATION_MODEL_INVALID",
            f"Declaration type {decl.declaration_type!r} {decl.gen_procedure_code!r} is not one "
            f"of the 17 Box-1 model lines (declaration_models reference)."))
    if decl.customs_office_code not in ref.office_by_code:
        errors.append(ValidationMessage.blocking(
            "CUSTOMS_OFFICE_INVALID",
            f"Customs clearance office {decl.customs_office_code!r} is not in the NECAS office "
            f"reference ({len(ref.office_by_code)} offices)."))
    border_office = decl.border_office_code or decl.customs_office_code
    if border_office and border_office not in ref.office_by_code:
        errors.append(ValidationMessage.blocking(
            "CUSTOMS_OFFICE_INVALID",
            f"Border office {border_office!r} is not in the NECAS office reference."))
    if decl.extended_customs_procedure not in ref.extended_proc_by_code:
        errors.append(ValidationMessage.blocking(
            "PROCEDURE_INVALID",
            f"Extended customs procedure {decl.extended_customs_procedure!r} is not in "
            f"ANNEX 1 (extended_procedures reference)."))
    elif (decl.gen_procedure_code
          and decl.extended_customs_procedure[:1] != decl.gen_procedure_code):
        errors.append(ValidationMessage.blocking(
            "PROCEDURE_TYPE_MISMATCH",
            f"Extended procedure {decl.extended_customs_procedure!r} does not belong to "
            f"declaration model {decl.declaration_type} {decl.gen_procedure_code} — Box 37 "
            f"must start with the Box-1 general-procedure digit (ASYCUDA convention)."))
    if decl.national_customs_procedure not in ref.national_proc_by_code:
        errors.append(ValidationMessage.blocking(
            "PROCEDURE_INVALID",
            f"National customs procedure {decl.national_customs_procedure!r} is not in "
            f"ANNEX 3 (national_procedures reference — gaps in the numbering are normal)."))
    # Box 25/26: no silent default (user rule 2026-08-01) — the reviewer must
    # pick both modes; an unpicked mode blocks (warn mode may still build a
    # test XML, which then carries an empty <Mode/>).
    missing_modes = [n for n, v in (("border (Box 25)", decl.border_mode),
                                    ("inland (Box 26)", decl.inland_mode_of_transport)) if not v]
    if missing_modes:
        errors.append(ValidationMessage.blocking(
            "TRANSPORT_MODE_REQUIRED",
            f"Mode of transport not selected: {' and '.join(missing_modes)}. There is no "
            f"default — choose from the transport-mode reference in Critical Review."))
    for label, mode in (("Border", decl.border_mode), ("Inland", decl.inland_mode_of_transport)):
        if mode and mode not in ref.transport_mode_by_code:
            errors.append(ValidationMessage.blocking(
                "TRANSPORT_MODE_INVALID",
                f"{label} mode of transport {mode!r} is not in the reference list (01–09)."))
    if not decl.field_40_summary_declaration.strip():
        if decl.shipment_authority_type in ("HAWB", "TRUE_DO", "TRACKING", "SINGLE_AWB"):
            errors.append(ValidationMessage.blocking(
                "FIELD_40_MISSING",
                "Field 40 (Previous_doc/Summary_declaration) is compulsory for air shipments — "
                "review the derived value or enter it manually."))
        else:
            warnings.append(ValidationMessage.warning(
                "FIELD_40_EMPTY", "Field 40 left empty (allowed for Bill of Lading shipments)."))
    if not decl.manifest_number.strip():
        warnings.append(ValidationMessage.warning(
            "MANIFEST_MISSING", "Manifest number not entered; <Manifest_reference_number> will be empty."))
    if not decl.field_18_identity.strip():
        warnings.append(ValidationMessage.warning(
            "FIELD_18_EMPTY", "Field 18 transport identity not entered."))
    if not decl.field_21_identity.strip():
        warnings.append(ValidationMessage.warning(
            "FIELD_21_EMPTY", "Field 21 border transport identity not entered."))

    # importer/exporter country codes
    if decl.parties.consignee_country_code and decl.parties.consignee_country_code not in ref.valid_alpha2:
        errors.append(ValidationMessage.blocking(
            "IMPORTER_COUNTRY_INVALID",
            f"Importer country {decl.parties.consignee_country_code!r} is not a valid alpha-2 code."))
    if decl.parties.exporter_country_code and decl.parties.exporter_country_code not in ref.valid_alpha2:
        errors.append(ValidationMessage.blocking(
            "EXPORTER_COUNTRY_INVALID",
            f"Exporter country {decl.parties.exporter_country_code!r} is not a valid alpha-2 code."))

    # bank pair: both blank or an exact reference pair
    bank_code, bank_name = decl.bank_code.strip(), decl.bank_name.strip()
    if bool(bank_code) != bool(bank_name):
        errors.append(ValidationMessage.blocking(
            "BANK_PAIR_INCOMPLETE", "Bank code and bank name must either both be blank or both be populated."))
    elif bank_code:
        rec = next((b for b in ref.banks if b.code == bank_code), None)
        if rec is None or rec.name.strip().lower() != bank_name.lower():
            errors.append(ValidationMessage.blocking(
                "BANK_PAIR_MISMATCH",
                f"Bank {bank_code!r} / {bank_name!r} is not an exact pair in the bank reference."))

    # payment terms: code must exist in the reference when populated
    if decl.terms_code and decl.terms_code not in ref.terms_by_code:
        errors.append(ValidationMessage.blocking(
            "PAYMENT_TERM_INVALID", f"Payment-term code {decl.terms_code!r} is not in the reference table."))
    if not decl.terms_code:
        warnings.append(ValidationMessage.warning("PAYMENT_TERMS_UNRESOLVED", "Payment-term code unresolved"))
    if not decl.bank_code:
        warnings.append(ValidationMessage.warning("BANK_UNRESOLVED", "Bank code unresolved"))

    # freight/insurance double-count guard (CIF/CIP incoterms)
    inco = (decl.incoterm_code or "").upper()
    if inco in ("CIF", "CIP") and not decl.exclude_freight_insurance_confirmed and (
            Decimal(decl.valuation.external_freight_foreign or "0") > 0
            or Decimal(decl.valuation.insurance_national or "0") > 0):
        warnings.append(ValidationMessage.warning(
            "DOUBLE_COUNT_RISK",
            f"Incoterm {inco} usually includes freight/insurance in the goods value; confirm the "
            "invoice values exclude them (Critical Review checkbox) to rule out double counting."))

    # ---- item-level gates --------------------------------------------------- #
    sum_gross = Decimal("0")
    sum_pkgs = Decimal("0")
    for it in decl.items:
        seq = it.xml_item_sequence
        if len(it.hs_code_11) != 11 or not it.hs_code_11.isdigit():
            errors.append(ValidationMessage.blocking("HS_MANUAL_REVIEW", f"Item {seq}: HS not an official 11-digit code",
                                                     scope="ITEM", item_sequence=seq))
        if len(it.coo_alpha2) != 2:
            errors.append(ValidationMessage.blocking("COO_UNRESOLVED", f"Item {seq}: COO unresolved",
                                                     scope="ITEM", item_sequence=seq))
        if Decimal(it.supplementary_quantity or "0") <= 0:
            errors.append(ValidationMessage.blocking("SUPPLEMENTARY_QTY_INVALID", f"Item {seq}: supplementary qty <= 0",
                                                     scope="ITEM", item_sequence=seq))
        g = Decimal(it.gross_weight_kg or "0")
        n = Decimal(it.net_weight_kg or "0")
        if g > 0 and not (n < g):
            errors.append(ValidationMessage.blocking("WEIGHT_RECONCILIATION_IMPOSSIBLE",
                                                     f"Item {seq}: net {n} not < gross {g}", scope="ITEM", item_sequence=seq))
        sum_gross += g
        sum_pkgs += Decimal(it.package_count or "0")
        # bubble up item-level blocking warnings produced by the engines
        for w in it.warnings:
            (errors if w.severity == Severity.BLOCKING else warnings).append(w)

    auth_gross = Decimal(decl.valuation.total_weight or "0")
    if auth_gross > 0 and abs(sum_gross - auth_gross) > _TOL:
        errors.append(ValidationMessage.blocking(
            "WEIGHT_RECONCILIATION_IMPOSSIBLE",
            f"Sum of item gross {sum_gross} != authorised total {auth_gross}"))
    auth_pkgs = Decimal(decl.total_number_of_packages or "0")
    if auth_pkgs > 0 and abs(sum_pkgs - auth_pkgs) > _TOL:
        errors.append(ValidationMessage.blocking(
            "CARTON_RECONCILIATION_FAILED",
            f"Sum of item packages {sum_pkgs} != authorised total {auth_pkgs}"))

    # de-duplicate
    decl.blocking_errors = _dedupe(errors)
    decl.warnings = _dedupe(decl.warnings + warnings)
    decl.ready_for_xml = not decl.blocking_errors
    return decl


def _dedupe(msgs: list[ValidationMessage]) -> list[ValidationMessage]:
    seen = set()
    out = []
    for m in msgs:
        key = (m.code, m.item_sequence, m.message)
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out
