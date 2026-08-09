"""OpenAI extractor tests with an injected fake client (no key / package needed).

Verifies the live-extraction wiring: schema validation, the evidence/repair
loop, and that a supplied fixture short-circuits the LLM entirely.
"""
import json

from app.domain.enums import DeclaredRole
from app.extraction.openai_extractor import OpenAIExtractor
from app.ocr.base import OcrDocument, OcrPage

OCR_TEXT = ("SWIFT FIN 700 Documentary Credit. Sender CTZNNPKAXXX. "
            "Amount USD 7,023.17. Drafts at 75 DAYS FROM AWB DATE. "
            "Under Letter of Credit. Invoice DEMO-209-1.")

VALID = json.dumps({
    "role_validation": {"expected_role": "BANKING", "matches_expected_role": True},
    "sender_bic_raw": "CTZNNPKAXXX",
    "bank_swift_raw": "CTZNNPKAXXX",
    "amount": {"amount_raw": "7,023.17", "currency_raw": "USD",
               "evidence": {"page_no": 1, "quote": "Amount USD 7,023.17"}},
    "payment_terms_raw": "Under Letter of Credit",
    "draft_tenor_raw": "75 DAYS FROM AWB DATE",
    "warnings": [],
})


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kw):
        content = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        msg = type("M", (), {"content": content})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, responses):
        self.chat = type("Chat", (), {"completions": _FakeCompletions(responses)})()


def _ocr():
    return OcrDocument(document_id="d", declared_role=DeclaredRole.BANKING,
                       pages=[OcrPage(page_no=1, plain_text=OCR_TEXT)])


def test_openai_extractor_happy_path():
    ex = OpenAIExtractor(client=_FakeClient([VALID]))
    payload, warnings = ex.extract(DeclaredRole.BANKING, _ocr())
    assert payload.sender_bic_raw == "CTZNNPKAXXX"
    assert payload.amount.amount_raw == "7,023.17"
    assert ex._client.chat.completions.calls == 1


def test_openai_extractor_repairs_invalid_json():
    ex = OpenAIExtractor(client=_FakeClient(["{ not valid json", VALID]))
    payload, warnings = ex.extract(DeclaredRole.BANKING, _ocr())
    assert payload.sender_bic_raw == "CTZNNPKAXXX"
    assert ex._client.chat.completions.calls == 2      # repaired on 2nd round


def test_fixture_short_circuits_llm():
    ex = OpenAIExtractor(client=_FakeClient([]))        # would IndexError if called
    fixture = {"role_validation": {"expected_role": "BANKING", "matches_expected_role": True},
               "sender_bic_raw": "CTZNNPKAXXX"}
    payload, warnings = ex.extract(DeclaredRole.BANKING, _ocr(), fixture=fixture)
    assert payload.sender_bic_raw == "CTZNNPKAXXX"
    assert ex._client.chat.completions.calls == 0       # LLM never called
