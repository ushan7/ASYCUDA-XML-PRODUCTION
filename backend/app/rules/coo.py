"""Country of Origin resolution (per item).

Valid item-level origin wins — including labelled / trailing-word raw values
("COO: Ireland", "Ireland Tariff Code") via the progressive normalizer in
``field_allocation``.  When the item prints nothing, the resolution ladder is:

1. **ITEM_LEVEL** — the row's own printed origin, normalize-gated;
2. **VENDOR_PROFILE** — this exporter's remembered default origin
   (``extraction.field_profiles``), learned from previously finalized /
   reviewer-corrected jobs.  Outranks the exporter fallback because it exists
   for exactly the case where that fallback is systematically wrong: a trader
   exporting goods made elsewhere (the 2026-07-30 live job declared Singapore
   for Irish stents).  Always warned, never silent;
3. **EXPORTER_FALLBACK** — the exporter's own country (with a warning);
4. blocking ``COO_UNRESOLVED``.

Never emits XX/ZZ/UNKNOWN/N/A/empty.  ``NA`` means Namibia, not null.
"""
from __future__ import annotations

from ..domain.errors import ValidationMessage
from ..extraction import field_profiles
from ..reference.store import ReferenceStore
from .field_allocation import normalize_coo_candidate
from .models import WorkItem


def resolve_coo_for_item(item: WorkItem, exporter_country_raw: str | None, ref: ReferenceStore,
                         profile_coo: str | None = None) -> WorkItem:
    item_code = (ref.normalize_country(item.country_of_origin_raw)
                 or normalize_coo_candidate(item.country_of_origin_raw, ref))
    if item_code:
        item.coo_alpha2 = item_code
        item.coo_source = "ITEM_LEVEL"
        return item

    if profile_coo:
        item.coo_alpha2 = profile_coo
        item.coo_source = "VENDOR_PROFILE"
        item.warnings.append(ValidationMessage.warning(
            "COO_VENDOR_PROFILE",
            f"Item {item.xml_item_sequence}: item-level COO missing/invalid; used this "
            f"exporter's remembered origin {profile_coo} (learned from previously reviewed "
            f"shipments) — verify.",
            scope="ITEM", item_sequence=item.xml_item_sequence, field="coo",
        ))
        return item

    exporter_code = ref.normalize_country(exporter_country_raw)
    if exporter_code:
        item.coo_alpha2 = exporter_code
        item.coo_source = "EXPORTER_FALLBACK"
        item.warnings.append(ValidationMessage.warning(
            "COO_EXPORTER_FALLBACK",
            f"Item {item.xml_item_sequence}: item-level COO missing/invalid; used exporter country {exporter_code}.",
            scope="ITEM", item_sequence=item.xml_item_sequence, field="coo",
        ))
        return item

    item.warnings.append(ValidationMessage.blocking(
        "COO_UNRESOLVED",
        f"Item {item.xml_item_sequence}: neither item origin ({item.country_of_origin_raw!r}) "
        f"nor exporter country ({exporter_country_raw!r}) maps to a canonical Alpha-2 code.",
        scope="ITEM", item_sequence=item.xml_item_sequence, field="coo",
    ))
    return item


def resolve_coo_all(items: list[WorkItem], exporter_country_raw: str | None, ref: ReferenceStore,
                    exporter_name: str | None = None) -> list[WorkItem]:
    profile_coo = field_profiles.coo_default_for(exporter_name) if exporter_name else None
    if profile_coo and ref.normalize_country(profile_coo) != profile_coo:
        profile_coo = None                        # a corrupt store entry never reaches an item
    return [resolve_coo_for_item(it, exporter_country_raw, ref, profile_coo=profile_coo)
            for it in items]
