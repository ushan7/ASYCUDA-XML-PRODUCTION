"""Live smoke test — verify Mistral OCR + OpenAI keys against the real APIs.

Run from the backend directory once your keys are set:

    export EASYCUSTOMS_MISTRAL_API_KEY=...   # or MISTRAL_API_KEY
    export EASYCUSTOMS_LLM_API_KEY=...        # or OPENAI_API_KEY
    python scripts/live_smoke_test.py

It runs Mistral OCR on a bundled sample PDF, then OpenAI structured extraction
for that role, and prints the evidence-backed raw facts.  No customs decisions
are made here — this only proves the live extraction layer works end to end.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.domain.enums import DeclaredRole
from app.ocr.service import get_ocr_provider
from app.extraction.service import extract_document

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_banking_doc.pdf"
ROLE = DeclaredRole.BANKING


def main() -> int:
    s = get_settings()
    print("OCR provider        :", s.ocr_provider, "| key set:", bool(s.resolved_mistral_key()))
    print("Extraction provider :", s.extraction_provider, "| key set:", bool(s.resolved_openai_key()))
    print("LLM model           :", s.resolved_llm_model())
    if not (s.resolved_mistral_key() and s.resolved_openai_key()):
        print("\n[!] One or both keys are missing — the app will fall back to offline extraction.")
        print("    Set EASYCUSTOMS_MISTRAL_API_KEY and EASYCUSTOMS_LLM_API_KEY to test live.")

    print(f"\n>> Running OCR ({s.ocr_provider}) on {SAMPLE.name} ...")
    ocr = get_ocr_provider().run(document_id="smoke", declared_role=ROLE,
                                 file_path=str(SAMPLE), sha256="smoke")
    print(f"   OCR pages: {len(ocr.pages)} | provider used: {ocr.ocr_provider}")
    print("   page 1 preview:", (ocr.full_text()[:200] or "(empty)").replace("\n", " "))

    print(f"\n>> Running extraction ({s.extraction_provider}) for role {ROLE.value} ...")
    result = extract_document(ROLE, ocr)
    print("   provider used   :", result.provider)
    print("   role match      :", result.role_match)
    print("   warnings        :", result.warnings)
    print("   errors          :", result.errors)
    p = result.payload
    print("   sender BIC      :", getattr(p, "sender_bic_raw", None))
    print("   amount          :", getattr(getattr(p, "amount", None), "amount_raw", None))
    print("   payment terms   :", getattr(p, "payment_terms_raw", None))
    print("\nDone. If 'provider used' is 'openai'/'mistral', your live keys work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
