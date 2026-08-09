"""OCR provider selection.

Default is Mistral OCR.  If ``mistral`` is selected but no key is configured,
we fall back to the offline pypdf text-layer provider with a logged warning so
the app still boots (and the bundled demo / tests keep working without keys).
"""
from __future__ import annotations

import logging

from ..config import get_settings
from .base import OcrProvider
from .offline import OfflineOcrProvider

log = logging.getLogger("easycustoms.ocr")


def get_ocr_provider() -> OcrProvider:
    settings = get_settings()
    if settings.ocr_provider == "mistral":
        if settings.resolved_mistral_key():
            from .mistral import MistralOcrProvider

            return MistralOcrProvider()
        log.warning("OCR provider 'mistral' selected but no key set — falling back to offline pypdf OCR.")
    return OfflineOcrProvider()
