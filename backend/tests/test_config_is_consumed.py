"""A setting that describes behaviour must be read by that behaviour (A6).

`config.py` declared an OCR fallback provider, a `quality_gate` fallback mode
and an `ocr_min_confidence` threshold.  All three were env-driven, validated,
documented in `.env.example` and published on `/api/config` — and no code path
read any of them.  The running `backend/.env` carried
`OCR_FALLBACK_ENABLED=true` and `OCR_MIN_CONFIDENCE=0.80`, which reads as "a
quality gate is checking my OCR before it becomes customs evidence".  Nothing
was.  Twelve more flags (Mistral header/footer/table-format, the OpenAI PDF
fallback, the audit modes, the repair-candidate switches) were the same.

Config that claims protection it does not provide is worse than no config: it
is the reason nobody goes looking for the protection.

This test walks the Settings fields and fails when one is never read outside
`config.py`, so the next dead flag is caught at the commit that adds it rather
than at the audit that finds it years later.
"""
import pathlib
import re

import pytest

from app.config import Settings

_APP = pathlib.Path(__file__).resolve().parent.parent / "app"

# Fields that legitimately have no `settings.<name>` reader.
_EXEMPT = {
    # read via the SettingsConfigDict / pydantic machinery, not by name
    "model_config",
    # consumed through the resolved_*() helpers in config.py, which callers use
    # by name (resolved_llm_model / resolved_fast_llm_model / resolved_*_key)
    "mistral_api_key", "llm_api_key", "llm_model",
    "openai_reasoning_enabled", "openai_reasoning_model",
    "openai_reasoning_fallback_model",
    # read by database.py / storage.py at import time via get_settings()
    "database_url", "storage_dir",
    # consumed through request_body_limit_bytes() in config.py, which callers
    # use by name (main.RequestBodyLimit) — the raw field is only the override,
    # the ceiling itself is derived from max_upload_mb.
    "max_request_mb",
    # consumed through resolved_monthly_document_cap() in config.py, which
    # metering.resolved_cap uses by name.  Same shape as max_request_mb: the raw
    # field is the OPERATOR'S OVERRIDE (None = "take the default for the auth
    # provider"), never the effective number — reading it directly is the bug
    # this exemption exists to keep out, because it would miss both the
    # per-provider default and the per-account account_quota row.
    "usage_monthly_document_cap",
}


def _app_sources() -> str:
    # backend/worker.py is a Settings consumer that lives OUTSIDE app/ (it is
    # an entry point, like main but for the queue) — scan it too, or every
    # worker-only knob would need a spurious exemption here.
    sources = [p.read_text(encoding="utf-8")
               for p in _APP.rglob("*.py")
               if p.name != "config.py" and "__pycache__" not in p.parts]
    sources.append((_APP.parent / "worker.py").read_text(encoding="utf-8"))
    return "\n".join(sources)


@pytest.mark.parametrize("name", sorted(set(Settings.model_fields) - _EXEMPT))
def test_every_setting_has_a_reader(name):
    """`settings.<name>` (or `s.<name>`) appears somewhere outside config.py."""
    sources = _app_sources()
    assert re.search(rf"\.{re.escape(name)}\b", sources), (
        f"Settings.{name} is declared but never read outside config.py. Either wire it "
        f"up in the same commit, or delete it — a setting nobody obeys still tells the "
        f"operator that something is configurable, and .env lines like "
        f"OCR_FALLBACK_ENABLED=true read as a guarantee.")


def test_the_removed_ocr_quality_gate_stays_removed():
    """Named explicitly: these were the ones that claimed a protection."""
    for gone in ("ocr_fallback_provider", "ocr_fallback_enabled", "ocr_fallback_mode",
                 "ocr_min_confidence", "ocr_save_raw_response",
                 "ocr_save_normalized_evidence"):
        assert gone not in Settings.model_fields, (
            f"{gone} is back. If an OCR quality gate now exists, delete this assertion "
            f"in the commit that implements it.")


def test_a_stale_env_line_is_inert_not_fatal():
    """Removing a field must not break an operator's existing .env."""
    s = Settings(_env_file=None, OCR_FALLBACK_ENABLED="true", OCR_MIN_CONFIDENCE="0.8")
    assert not hasattr(s, "ocr_fallback_enabled")
    assert s.ocr_provider == "mistral"        # the real settings still load


def test_env_example_does_not_advertise_a_setting_that_does_nothing():
    root = _APP.parent.parent
    example = (root / ".env.example").read_text(encoding="utf-8")
    declared = {f"EASYCUSTOMS_{n.upper()}" for n in Settings.model_fields}
    declared |= {n.upper() for n in Settings.model_fields}
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        assert key in declared, (
            f"{key} is documented in .env.example but is not a Settings field — it "
            f"configures nothing.")
