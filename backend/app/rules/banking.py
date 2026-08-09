"""Banking resolution.

Resolves the Nepal bank code and ASYCUDA payment-term code from the canonical
CSV tables.  BIC is matched on its 8-char base; unknown payment terms return
REVIEW (never silently default to L/C, ADR-006).  Draft tenors such as
"75 DAYS FROM AWB DATE" are preserved raw for the approved-policy mapping.
"""
from __future__ import annotations

from ..config import get_settings
from ..domain.errors import ValidationMessage
from ..numbers import parse_decimal
from ..reference.store import ReferenceStore
from .models import BankingResolution


def resolve_banking(payload, ref: ReferenceStore) -> BankingResolution:
    settings = get_settings()
    warnings: list[ValidationMessage] = []
    bank_code = bank_name = None
    bank_state = "ABSENT"

    if payload is not None:
        bank, method = ref.resolve_bank(
            payload.bank_swift_raw or payload.sender_bic_raw,
            payload.issuing_or_applicant_bank_name_raw,
        )
        if bank:
            bank_code, bank_name = bank.code, bank.name
            bank_state = "RESOLVED"
        else:
            bank_state = ("AMBIGUOUS" if (payload.bank_swift_raw or payload.sender_bic_raw
                                          or payload.issuing_or_applicant_bank_name_raw) else "ABSENT")
            warnings.append(ValidationMessage.warning(
                "BANK_UNRESOLVED", "Could not match a canonical Nepal bank; review required."))

    terms_code = terms_desc = None
    payment_state = "ABSENT"
    if payload is not None:
        raw_terms = payload.payment_terms_raw or payload.draft_tenor_raw
        code, method = ref.resolve_terms_code(raw_terms)
        if code:
            terms_code, terms_desc = code, ref.terms_description(code)
            payment_state = "RESOLVED"
        elif settings.default_unknown_payment_terms_to_lc:
            terms_code, terms_desc = "200", ref.terms_description("200")
            payment_state = "RESOLVED"
            warnings.append(ValidationMessage.warning(
                "PAYMENT_TERMS_DEFAULTED", "Unknown payment terms defaulted to L/C by config."))
        else:
            payment_state = "REVIEW_REQUIRED" if raw_terms else "ABSENT"
            warnings.append(ValidationMessage.warning(
                "PAYMENT_TERMS_UNRESOLVED",
                f"Payment terms {raw_terms!r} did not map to an approved code; review required."))

    amount = parse_decimal(payload.amount.amount_raw) if (payload and payload.amount) else None
    return BankingResolution(
        bank_code=bank_code,
        bank_name=bank_name,
        terms_code=terms_code,
        terms_description=terms_desc,
        amount=amount,
        currency=(payload.amount.currency_raw if (payload and payload.amount) else None),
        reference_number=(payload.reference_number_raw if payload else None),
        draft_tenor_raw=(payload.draft_tenor_raw if payload else None),
        invoice_references=(payload.invoice_references_raw if payload else []),
        swift_raw=((payload.bank_swift_raw or payload.sender_bic_raw) if payload else None),
        value_date_raw=(payload.issue_or_value_date_raw if payload else None),
        payment_terms_raw=(payload.payment_terms_raw if payload else None),
        bank_resolution_state=bank_state,
        payment_resolution_state=payment_state,
        warnings=warnings,
    )
