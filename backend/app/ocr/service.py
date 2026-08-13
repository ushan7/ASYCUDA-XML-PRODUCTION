"""OCR provider selection.

Default is Mistral OCR.  If ``mistral`` is selected but no key is configured,
we fall back to the offline pypdf text-layer provider with a logged warning so
the app still boots (and the bundled demo / tests keep working without keys).

That fallback is decided HERE, per extraction — nothing at boot used to ask the
question, which is why a keyless deployment started clean and only revealed
itself in a log line.  Under ``auth_provider=supabase`` the same question is now
also asked at boot and the process refuses to start
(``config.Settings._check_auth_provider_config``); this module's behaviour is
unchanged, and ``config.live_ocr_key_missing`` is the ONE predicate both use so
the two cannot disagree about whether a key is present.
"""
from __future__ import annotations

import logging

from ..config import LIVE_OCR_PROVIDER, get_settings
from .base import OcrProvider
from .offline import OfflineOcrProvider

log = logging.getLogger("easycustoms.ocr")


def get_ocr_provider() -> OcrProvider:
    settings = get_settings()
    if settings.live_ocr_key_missing():
        log.warning("OCR provider %r selected but no key set — falling back to offline pypdf OCR.",
                    settings.ocr_provider)
    elif settings.ocr_provider == LIVE_OCR_PROVIDER:
        from .mistral import MistralOcrProvider

        return MistralOcrProvider()
    return OfflineOcrProvider()
