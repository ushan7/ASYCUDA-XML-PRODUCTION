"""A live provider with no key is refused at boot — on `supabase` only.

THE FAILURE THIS CLOSES.  OCR and extraction each fall back to an OFFLINE
reader when the live provider's key is missing, logging a warning and carrying
on.  The process therefore boots clean, answers /api/health, accepts the
document, and hands the reviewer a complete-looking ASYCUDA declaration built
from facts nothing ever read off the paperwork — every deterministic rule
downstream working perfectly on invented input.  Nothing 500s and nothing turns
red.  Step 5 of docs/deploy-staging.md, a human reading the journal for the word
`offline`, was the whole control between that and a broker filing a fabricated
declaration.

WHAT IS AND IS NOT REFUSED.  The refusal is `supabase` AND a live provider named
AND its key absent.  It does not touch `local` (the broker's laptop, the bundled
demo and this test suite, where running with no keys at all is the documented
zero-setup path), and it does not touch `offline` on any provider — an explicit
choice is not a misconfiguration.  The fallback itself is unchanged.

WHAT IT CANNOT COVER, stated here so nobody reads a green suite as more than it
is: a key that is PRESENT but expired, revoked or out of quota.  That is a
different failure and it already behaves differently — the live provider is
constructed, the vendor call raises, and services.extract_document marks the
document FAILED with `EXTRACTION_FAILED (...)` and re-raises.  It surfaces as an
error.  Only ABSENCE is silent, and only absence is refused here.
"""
from __future__ import annotations

import pytest

from app.config import LIVE_EXTRACTION_PROVIDERS, LIVE_OCR_PROVIDER, Settings
from app.domain.enums import DeclaredRole
from app.ocr.base import OcrDocument, OcrPage

# A supabase deployment that is otherwise complete, so each test below turns on
# exactly one thing.  `_env_file=None` stops backend/.env being read; the vendor
# keys are ALSO always passed explicitly, as init kwargs (the highest-priority
# source), because a developer's shell exporting MISTRAL_API_KEY would otherwise
# decide the result — the same reason the auth_secret tests say `auth_secret=None`
# out loud rather than by omission.
_SUPABASE = {"auth_provider": "supabase",
             "supabase_url": "https://project.supabase.co",
             "supabase_anon_key": "anon",
             "auth_secret": "0" * 64}
_NO_KEYS = {"mistral_api_key": None, "llm_api_key": None}
_BOTH_KEYS = {"mistral_api_key": "sk-mistral", "llm_api_key": "sk-openai"}


def _settings(**over) -> Settings:
    return Settings(_env_file=None, **{**_SUPABASE, **_NO_KEYS, **over})


# --------------------------------------------------------------------------- #
# The refusal
# --------------------------------------------------------------------------- #
def test_a_live_ocr_provider_without_a_key_is_refused_at_boot():
    with pytest.raises(Exception) as caught:
        _settings(ocr_provider=LIVE_OCR_PROVIDER, extraction_provider="offline")
    message = str(caught.value)
    assert "EASYCUSTOMS_MISTRAL_API_KEY" in message, "name the variable to set"
    assert "EASYCUSTOMS_OCR_PROVIDER=mistral" in message, "name what selected it"


def test_a_live_extraction_provider_without_a_key_is_refused_at_boot():
    with pytest.raises(Exception) as caught:
        _settings(ocr_provider="offline", extraction_provider="openai")
    message = str(caught.value)
    assert "EASYCUSTOMS_OPENAI_API_KEY" in message
    assert "EASYCUSTOMS_EXTRACTION_PROVIDER=openai" in message


def test_the_default_shape_is_the_one_that_gets_refused():
    """The whole point: nobody has to choose `mistral`/`openai` to reach this.
    They are the DEFAULTS, so a supabase deployment that says nothing about
    providers and forgets the keys is exactly the deployment being stopped."""
    with pytest.raises(Exception) as caught:
        _settings()
    message = str(caught.value)
    assert "EASYCUSTOMS_MISTRAL_API_KEY" in message, "both halves are reported at once"
    assert "EASYCUSTOMS_OPENAI_API_KEY" in message, (
        "reporting one at a time would cost two deploy cycles to fix one .env")


def test_langroid_is_a_live_provider_too():
    """It spends the same OpenAI key.  Listing it in LIVE_EXTRACTION_PROVIDERS
    rather than special-casing `openai` is what keeps the two in step."""
    assert "langroid" in LIVE_EXTRACTION_PROVIDERS
    with pytest.raises(Exception) as caught:
        _settings(ocr_provider="offline", extraction_provider="langroid")
    assert "EASYCUSTOMS_EXTRACTION_PROVIDER=langroid" in str(caught.value)


@pytest.mark.parametrize("placeholder", ["***", "your_mistral_key_here", "   ", ""])
def test_a_placeholder_key_counts_as_absent(placeholder):
    """The realistic way to get this wrong is copying .env.example and filling in
    every line but these two.  `_real_key` is the single predicate the refusal
    and the fallback both go through, so a value one treats as usable cannot be
    one the other quietly treats as missing."""
    with pytest.raises(Exception) as caught:
        _settings(ocr_provider=LIVE_OCR_PROVIDER, extraction_provider="offline",
                  mistral_api_key=placeholder)
    assert "EASYCUSTOMS_MISTRAL_API_KEY" in str(caught.value)


# --------------------------------------------------------------------------- #
# What must still boot
# --------------------------------------------------------------------------- #
def test_a_supabase_deployment_with_keys_boots():
    s = _settings(**_BOTH_KEYS)
    assert s.ocr_live_ready and s.extraction_live_ready


def test_offline_stays_a_valid_explicit_choice():
    """Not a refusal of offline — a refusal of CLAIMING live and not paying for
    it.  A deployment that says `offline` outright has made a decision, and the
    audit trail records the provider that ran either way."""
    s = _settings(ocr_provider="offline", extraction_provider="offline")
    assert not (s.live_ocr_key_missing() or s.live_extraction_key_missing())
    assert not (s.ocr_live_ready or s.extraction_live_ready)


def test_a_single_operator_deployment_with_no_keys_still_boots():
    """The path this refusal must not break, and the reason it is not widened:
    `local` with the default live providers and no keys at all is the bundled
    demo, the quickstart and this whole test suite.  Whoever is misled there is
    the person who chose it."""
    s = Settings(_env_file=None, auth_provider="local", **_NO_KEYS)
    assert s.ocr_provider == LIVE_OCR_PROVIDER and s.extraction_provider == "openai"
    assert s.live_ocr_key_missing() and s.live_extraction_key_missing()


def test_a_fresh_clone_with_no_configuration_at_all_boots():
    assert Settings(_env_file=None, **_NO_KEYS).auth_provider == "local"


def test_a_key_the_vendor_would_reject_is_not_this_refusals_business():
    """Scope, asserted rather than described.  Absence is what is silent; a
    present-but-dead key constructs the live provider and fails LOUDLY at the
    vendor call (services.extract_document -> status FAILED, EXTRACTION_FAILED
    in the audit trail, exception re-raised).  Refusing to boot on one would
    also mean this process validating credentials against a third party at
    startup, which is a different feature with a different failure mode."""
    s = _settings(mistral_api_key="sk-revoked-yesterday", llm_api_key="sk-out-of-quota")
    assert s.ocr_live_ready and s.extraction_live_ready


# --------------------------------------------------------------------------- #
# The refusal and the fallback are ONE predicate
#
# The constraint that matters.  A refusal that disagrees with the fallback it
# guards either fires on a deployment that would have run happily, or — the
# direction that costs somebody a declaration — stays silent while the fallback
# substitutes synthetic facts.  So these do not re-state the predicate; they run
# the real dispatch functions and check the branch actually taken.
# --------------------------------------------------------------------------- #
_KEY_CASES = [(LIVE_OCR_PROVIDER, None), (LIVE_OCR_PROVIDER, "sk-live"),
              (LIVE_OCR_PROVIDER, "***"), ("offline", None), ("offline", "sk-live")]


@pytest.mark.parametrize("provider,key", _KEY_CASES)
def test_the_ocr_fallback_takes_the_branch_the_refusal_predicts(provider, key, monkeypatch):
    from app.ocr import service as ocr_service

    s = Settings(_env_file=None, auth_provider="local",
                 ocr_provider=provider, mistral_api_key=key, llm_api_key=None)
    monkeypatch.setattr(ocr_service, "get_settings", lambda: s)
    ran = ocr_service.get_ocr_provider().name

    assert (ran == "offline") == (s.live_ocr_key_missing() or provider != LIVE_OCR_PROVIDER)
    if s.live_ocr_key_missing():
        assert ran == "offline", "the refusal claims this deployment reads nothing"
    else:
        assert (ran == "mistral") == (provider == LIVE_OCR_PROVIDER)


@pytest.mark.parametrize("provider,key", [("openai", None), ("openai", "sk-live"),
                                          ("openai", "your_openai_key_here"),
                                          ("offline", None), ("offline", "sk-live")])
def test_the_extraction_fallback_takes_the_branch_the_refusal_predicts(
        provider, key, monkeypatch):
    from app.extraction import openai_extractor
    from app.extraction import service as ex_service

    s = Settings(_env_file=None, auth_provider="local",
                 extraction_provider=provider, llm_api_key=key, mistral_api_key=None)
    monkeypatch.setattr(ex_service, "get_settings", lambda: s)

    class _Stub:                      # no network: the branch is what is under test
        def extract(self, *a, **kw):
            return object(), []

        def usage(self):
            return None

    monkeypatch.setattr(openai_extractor, "OpenAIExtractor", _Stub)
    ocr = OcrDocument(document_id="d", declared_role=DeclaredRole.INVOICE,
                      pages=[OcrPage(page_no=1, plain_text="INVOICE")])
    _payload, _warnings, ran, _usage = ex_service._run_provider(
        DeclaredRole.INVOICE, ocr, None)

    if s.live_extraction_key_missing():
        assert ran == "offline", "the refusal claims this deployment invents its facts"
    else:
        assert ran == ("openai" if provider == "openai" else "offline")
