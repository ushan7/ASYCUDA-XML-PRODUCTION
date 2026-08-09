"""HAWB / MAWB / Delivery-Order classification and shipment authority.

Selects the single shipment-level authority document that supplies BOTH the
final gross weight and the final package/carton count, on a strict ladder:

    1.  HAWB          house air waybill — highest authority, never overridden
    1.5 BILL_OF_LADING  the sea/land transport document (bill of lading, sea
                      waybill, consignment note): the house-level document of
                      a non-air shipment, so it outranks a delivery order
                      issued against it (with a master and a house B/L the
                      LOWER gross weight is the house one, as for air)
    2.  TRUE_DO       document titled plainly "Delivery Order"
                      (a combined "Delivery Order / Packing List" is NOT a DO)
    3.  TRACKING      cargo / consignment tracking page
    3.5 HAWB (values unreadable) — a classified house air waybill whose gross
                      weight extraction could not read STILL outranks the
                      master: the authority is the house document with the
                      missing numbers left for manual review entry, never the
                      master's consolidated values (real 78 kg/5 ctn vs
                      163 kg/10 ctn failure, 2026-07-19)
    4.  SINGLE_AWB    exactly one usable AWB, even if it is a master
    5.  PACKING_LIST  mixed DO/packing form or packing-list totals — last resort

Classification is deterministic scoring with human-readable reasons.  A form
titled *House Air Waybill* stays HAWB even when it prints a MAWB number
(house documents routinely reference their master); MAWB may never override
HAWB.  Chargeable, volumetric, dimensional and net weight are NEVER accepted
as gross weight, and item lines are never summed when a shipment-level
authority exists.
"""
from __future__ import annotations

import re

from ..domain.errors import ValidationMessage
from ..numbers import parse_decimal
from .models import AwbClassification, ShipmentAuthority, ValueAuthority

_AIRLINE_ISSUERS = ("cathay", "qatar", "emirates", "turkish", "airindia", "air india", "himalaya",
                    "nepal airlines", "china", "singapore", "thai", "etihad", "korean", "airline",
                    "airways", "carrier", "cargo")
_FORWARDERS = ("logistics", "forward", "forwarding", "courier", "express", "cargo services",
               "freight", "movers", "shipping", "consolidat")
# party-role keywords: a shipper/consignee with one of these looks like a
# forwarder/agent (master-level party), not the actual exporter/importer
_PARTY_LOGISTICS = ("logistics", "forward", "freight", "cargo", "courier", "express",
                    "consolidat", "shipping", "clearing", "agent", "movers")

_HAWB_TITLE = ("house air waybill", "house awb", "hawb", "house waybill", "house bill", "house")
_MAWB_TITLE = ("master air waybill", "master awb", "mawb", "master")


def _norm(s: str | None) -> str:
    return (s or "").lower()


def _looks_airline_number(num: str | None) -> bool:
    """3-digit airline prefix + 8 digits, e.g. '176-12345678'."""
    if not num:
        return False
    digs = num.replace("-", "").replace(" ", "")
    return len(digs) == 11 and digs.isdigit()


def _forwarder_style_number(num: str | None) -> bool:
    return bool(num) and any(ch.isalpha() for ch in num)


# Sea and land transport documents. Nepal's land customs sees the ocean B/L of
# the sea leg plus (or instead of) a road/rail consignment note; all of them are
# the same thing for the declaration — the transport document whose number and
# date Field 9 prints (user rule 2026-08-06).
_BL_TITLE = ("bill of lading", "bill_of_lading", "b/l", "sea waybill", "seaway bill",
             "ocean waybill", "combined transport", "multimodal transport",
             "consignment note", "lorry receipt", "railway receipt", "truck receipt",
             "way bill (sea)")


def _document_kind(form) -> str:
    """Separate non-AWB shipment documents (B/L, DO, tracking) from real AWBs."""
    text = f"{_norm(form.document_title_raw)} {_norm(getattr(form, 'document_kind_raw', None))}"
    if "delivery order" in text or "delivery_order" in text:
        if "packing" in text:
            return "MIXED_DO_PACKING"           # combined DO/PL is never a true DO
        if "waybill" in text or "awb" in text.replace("delivery_order", ""):
            return "AWB"                        # an AWB that merely mentions its DO
        return "TRUE_DO"
    # A bill of lading is not an air waybill even when the upload box says
    # "Air Waybill": the box is the transport-document box.  Its own number
    # field settles it when the title is silent (a scanned B/L whose title the
    # OCR lost still carries `bill_of_lading_number_raw` and no AWB number).
    if any(k in text for k in _BL_TITLE) and "air waybill" not in text:
        return "BILL_OF_LADING"
    if (getattr(form, "bill_of_lading_number_raw", None)
            and not (form.hawb_number_raw or form.mawb_number_raw
                     or form.primary_awb_number_raw) and "waybill" not in text):
        return "BILL_OF_LADING"
    if "tracking" in text:
        return "TRACKING"
    return "AWB"


def _own_bl_number(form) -> str | None:
    """This form's own bill-of-lading number.

    A B/L-kind form that carries its number in `primary_awb_number_raw` (the
    generic 'this document's reference' field an older extraction filled) still
    yields it — the number is the document's identity, not the field it landed
    in."""
    if form is None:
        return None
    own = (getattr(form, "bill_of_lading_number_raw", None) or "").strip() or None
    if own:
        return own
    if _document_kind(form) == "BILL_OF_LADING":
        return (form.primary_awb_number_raw or "").strip() or None
    return None


def _decide(hawb: int, mawb: int) -> str:
    if hawb >= mawb + 30:
        return "HAWB"
    if mawb >= hawb + 30:
        return "MAWB"
    return "UNKNOWN_AWB"


def classify_form(form) -> AwbClassification:
    """Deterministic HAWB/MAWB scoring for one logical form, with reasons."""
    gross = parse_decimal(form.gross_weight.value_raw) if form.gross_weight else None
    charge = parse_decimal(form.chargeable_weight.value_raw) if form.chargeable_weight else None
    pkgs = parse_decimal(form.pieces_or_packages.value_raw) if form.pieces_or_packages else None
    awb_no = form.hawb_number_raw or form.primary_awb_number_raw or form.mawb_number_raw

    kind = _document_kind(form)
    if kind != "AWB":
        if kind == "BILL_OF_LADING":
            awb_no = _own_bl_number(form) or awb_no
        return AwbClassification(form.logical_form_id, kind, 0, 0, gross, charge, pkgs, awb_no,
                                 confidence=100, reasons=[f"titled {kind.replace('_', ' ').lower()}"])

    title = f"{_norm(form.document_title_raw)} {_norm(getattr(form, 'document_kind_raw', None))}"
    issuer = _norm(f"{form.issuer_raw or ''} {form.carrier_raw or ''}")
    shipper = _norm(form.shipper.name_raw if form.shipper else None)
    consignee = _norm(form.consignee.name_raw if form.consignee else None)
    hawb = mawb = 0
    reasons: list[str] = []

    house_titled = any(k in title for k in _HAWB_TITLE)
    master_titled = any(k in title for k in _MAWB_TITLE)
    if house_titled:
        hawb += 100
        reasons.append("+100 HAWB: house title")
    if master_titled:
        mawb += 100
        reasons.append("+100 MAWB: master title")

    if form.hawb_number_raw:
        hawb += 60
        reasons.append("+60 HAWB: HAWB number field present")
    house_signal = house_titled or bool(form.hawb_number_raw)
    if form.mawb_number_raw:
        if house_signal:
            # A HAWB routinely prints its MAWB number — that must not flip it.
            reasons.append("MAWB number ignored: house documents routinely reference their master")
        else:
            mawb += 60
            reasons.append("+60 MAWB: MAWB number field present")

    # the document's OWN reference: on a house doc that is the HAWB number
    own = form.hawb_number_raw if (house_signal and form.hawb_number_raw) else awb_no
    if _looks_airline_number(own):
        mawb += 40
        reasons.append(f"+40 MAWB: airline-format AWB number {own}")
    elif _forwarder_style_number(own):
        hawb += 20
        reasons.append(f"+20 HAWB: forwarder-style reference {own}")

    if any(k in issuer for k in _FORWARDERS):
        hawb += 30
        reasons.append("+30 HAWB: issuer looks like forwarder/logistics")
    elif any(k in issuer for k in _AIRLINE_ISSUERS):
        mawb += 30
        reasons.append("+30 MAWB: issuer looks like airline")

    if shipper:
        if any(k in shipper for k in _PARTY_LOGISTICS):
            mawb += 30
            reasons.append("+30 MAWB: shipper looks like forwarder/consolidator")
        else:
            hawb += 30
            reasons.append("+30 HAWB: shipper looks like the actual exporter/supplier")
    if consignee:
        if any(k in consignee for k in _PARTY_LOGISTICS):
            mawb += 30
            reasons.append("+30 MAWB: consignee looks like destination agent/forwarder")
        else:
            hawb += 30
            reasons.append("+30 HAWB: consignee looks like the actual importer/buyer")

    decision = _decide(hawb, mawb)
    return AwbClassification(form.logical_form_id, decision, hawb, mawb, gross, charge, pkgs,
                             awb_no, confidence=min(100, abs(hawb - mawb)), reasons=reasons)


def _comparative_rescore(classifications: list[AwbClassification]) -> None:
    """+20 house to the AWB with lower pieces AND gross; +20 master to the higher."""
    awbs = [c for c in classifications
            if c.decision in ("HAWB", "MAWB", "UNKNOWN_AWB") and c.gross_weight is not None]
    if len(awbs) < 2:
        return
    lo = min(awbs, key=lambda c: c.gross_weight)
    hi = max(awbs, key=lambda c: c.gross_weight)
    if lo is hi or not lo.gross_weight < hi.gross_weight:
        return
    if lo.packages is not None and hi.packages is not None and lo.packages > hi.packages:
        return
    lo.hawb_score += 20
    lo.reasons.append("+20 HAWB: lower pieces/gross weight than related AWB (consignment-specific)")
    hi.mawb_score += 20
    hi.reasons.append("+20 MAWB: higher pieces/gross weight than related AWB (consolidated cargo)")
    for c in (lo, hi):
        c.decision = _decide(c.hawb_score, c.mawb_score)
        c.confidence = min(100, abs(c.hawb_score - c.mawb_score))


def _norm_ref(ref: str | None) -> str:
    return re.sub(r"\s+", "", ref or "").upper()


def _pick(cands: list[AwbClassification], by_id: dict | None = None,
          invoice_numbers: set[str] | None = None) -> AwbClassification:
    """Prefer a candidate carrying BOTH gross weight and packages, then the one
    most clearly related to the shipment's invoices (its printed invoice
    references match the invoice roster), then confidence."""
    def _invoice_related(c: AwbClassification) -> bool:
        if not by_id or not invoice_numbers:
            return False
        form = by_id.get(c.logical_form_id)
        refs = {_norm_ref(r) for r in (getattr(form, "invoice_references_raw", None) or [])}
        return bool(refs & invoice_numbers)

    return sorted(cands, key=lambda c: (c.packages is None, not _invoice_related(c),
                                        -c.confidence))[0]


def _prefer_lower_awb(chosen: AwbClassification,
                      classifications: list[AwbClassification],
                      warnings: list[ValidationMessage]) -> AwbClassification:
    """User rule 2026-07-18: with two (or more) air waybills the consignment
    authority is ALWAYS the one with the lower gross weight (and cartons) —
    the house waybill covers only this consignment, the master covers the
    whole consolidation.  This is an absolute guard applied AFTER label
    scoring, because a mis-scored house/master label must never make the
    heavier (consolidated) weight the shipment authority."""
    from decimal import Decimal

    awbs = [c for c in classifications
            if c.decision in ("HAWB", "MAWB", "UNKNOWN_AWB") and c.gross_weight is not None]
    if len(awbs) < 2 or chosen not in awbs:
        return chosen
    inf = Decimal("Infinity")
    lowest = min(awbs, key=lambda c: (c.gross_weight,
                                      c.packages if c.packages is not None else inf))
    if lowest is chosen or lowest.gross_weight == chosen.gross_weight:
        return chosen
    warnings.append(ValidationMessage.warning(
        "AWB_LOWER_WEIGHT_SELECTED",
        f"Two air waybills found — authority switched to the LOWER-weight form "
        f"{lowest.logical_form_id} ({lowest.gross_weight} / {lowest.packages} pcs) over "
        f"{chosen.logical_form_id} ({chosen.gross_weight} / {chosen.packages} pcs): the lower "
        f"weight/carton document is the house-level consignment authority; the heavier one "
        f"is consolidated master cargo."))
    if (lowest.packages is not None and chosen.packages is not None
            and lowest.packages > chosen.packages):
        warnings.append(ValidationMessage.warning(
            "AWB_WEIGHT_CARTON_CONFLICT",
            f"The lower-weight AWB ({lowest.logical_form_id}) carries MORE pieces "
            f"({lowest.packages} vs {chosen.packages}) — verify gross weight and cartons "
            "in Critical Review."))
    return lowest


def _packing_totals(packing_payloads: list | None):
    """Last non-null totals across packing chunks (documents print totals at the end)."""
    gross = pkgs = unit = None
    for chunk in packing_payloads or []:
        if chunk.total_gross_weight and chunk.total_gross_weight.value_raw:
            g = parse_decimal(chunk.total_gross_weight.value_raw)
            if g is not None:
                gross, unit = g, chunk.total_gross_weight.unit_raw
        if chunk.total_packages and chunk.total_packages.value_raw:
            p = parse_decimal(chunk.total_packages.value_raw)
            if p is not None:
                pkgs = p
    return gross, pkgs, unit


def resolve_shipment_authority(awb_payloads: list, packing_payloads: list | None = None,
                               invoice_numbers: set[str] | None = None) -> ShipmentAuthority:
    forms = [f for p in awb_payloads for f in p.forms]
    by_id = {f.logical_form_id: f for f in forms}
    classifications = [classify_form(f) for f in forms]
    _comparative_rescore(classifications)
    warnings: list[ValidationMessage] = []
    inv_nos = {_norm_ref(n) for n in (invoice_numbers or set()) if n and _norm_ref(n)}

    # Rule 1 (user 2026-07-18): ALWAYS extract both numbers from the uploaded
    # documents.  Dedicated fields first; fall back to each form's own AWB
    # number by FORMAT — forwarder-style (letters) = house, 3+8 digit airline
    # format = master — so a missing hawb_number_raw field never loses the
    # house number (root cause of a wrong Field 40).
    #
    # Checkpoint rule (user 2026-07-19): with two air waybills of DIFFERENT
    # weights, the lower-weight document IS the house air waybill — so the
    # HAWB number must be THAT form's own number and the MAWB number the
    # heavier form's own number, never whichever form happens to print a
    # house/master number field first (a mis-labelled heavy form must not
    # donate the "HAWB" number).
    def _own_house_number(form) -> str | None:
        if form is None:
            return None
        return form.hawb_number_raw or (
            form.primary_awb_number_raw
            if _forwarder_style_number(form.primary_awb_number_raw) else None)

    def _own_master_number(form) -> str | None:
        if form is None:
            return None
        return form.mawb_number_raw or (
            form.primary_awb_number_raw
            if _looks_airline_number(form.primary_awb_number_raw) else None)

    hawb_no = mawb_no = None
    weighted = [c for c in classifications
                if c.decision in ("HAWB", "MAWB", "UNKNOWN_AWB") and c.gross_weight is not None]
    if len(weighted) >= 2:
        lo = min(weighted, key=lambda c: c.gross_weight)
        hi = max(weighted, key=lambda c: c.gross_weight)
        if lo.gross_weight < hi.gross_weight:
            hawb_no = _own_house_number(by_id.get(lo.logical_form_id))
            mawb_no = _own_master_number(by_id.get(hi.logical_form_id))
    if not hawb_no:
        hawb_no = next((f.hawb_number_raw for f in forms if f.hawb_number_raw), None)
    if not mawb_no:
        mawb_no = next((f.mawb_number_raw for f in forms if f.mawb_number_raw), None)
    if not hawb_no:
        hawb_no = next((f.primary_awb_number_raw for f in forms
                        if _forwarder_style_number(f.primary_awb_number_raw)), None)
    if not mawb_no:
        mawb_no = next((f.primary_awb_number_raw for f in forms
                        if _looks_airline_number(f.primary_awb_number_raw)), None)

    # ---- bill of lading (sea / land) --------------------------------------- #
    # Same rule as the house air waybill: with a master and a house B/L the
    # HOUSE document (the lower gross weight) is this consignment's transport
    # document, so its number is the one Field 9 prints.
    bl_classes = [c for c in classifications if c.decision == "BILL_OF_LADING"]
    bl_weighted = [c for c in bl_classes if c.gross_weight is not None]
    bl_form = None
    if bl_weighted:
        bl_form = by_id.get(min(bl_weighted, key=lambda c: c.gross_weight).logical_form_id)
    elif bl_classes:
        bl_form = by_id.get(bl_classes[0].logical_form_id)
    bl_no = _own_bl_number(bl_form) or next(
        (n for f in forms if (n := _own_bl_number(f))), None)
    bl_date = next((d for f in forms
                    if (d := (getattr(f, "bill_of_lading_date_raw", None) or "").strip())
                    and (_own_bl_number(f) == bl_no or bl_no is None)), None)
    if bl_date is None:
        bl_date = next((d for f in forms
                        if (d := (getattr(f, "bill_of_lading_date_raw", None) or "").strip())), None)
    if len(bl_weighted) > 1:
        warnings.append(ValidationMessage.warning(
            "BL_HOUSE_SELECTED",
            f"Two bills of lading found; the lower-weight (house-level) document supplies the "
            f"shipment totals and the B/L number {bl_no or '(unreadable)'}. Verify against the "
            f"document before finalizing."))

    # A multi-page form carrying both numbers without a house title usually
    # means the house/DO page was merged into the master instead of being its
    # own form — its house-level pcs/weight are then silently lost.
    for f in forms:
        if (f.hawb_number_raw and f.mawb_number_raw and len(set(f.source_pages)) > 1
                and "house" not in _norm(f.document_title_raw)):
            warnings.append(ValidationMessage.warning(
                "AWB_MERGED_SUSPECT",
                f"Form {f.logical_form_id} spans pages {sorted(set(f.source_pages))} and carries both "
                f"HAWB and MAWB numbers; the house-level page may not have been extracted as its own "
                f"form. Re-extract or verify gross weight and packages against the house document."))

    def _authority(chosen: AwbClassification, kind: str) -> ShipmentAuthority:
        form = by_id.get(chosen.logical_form_id)
        mawbs = [c for c in classifications if c.decision == "MAWB" and c is not chosen]
        for m in mawbs:
            if chosen.gross_weight is None:
                break        # HAWB_VALUES_UNREADABLE already explains the ignored master
            if (m.gross_weight is not None and m.gross_weight != chosen.gross_weight) or (
                    m.packages is not None and chosen.packages is not None and m.packages != chosen.packages):
                warnings.append(ValidationMessage.warning(
                    "MAWB_DIFFERS",
                    f"{kind} selected as final authority ({chosen.gross_weight} / {chosen.packages} pcs). "
                    f"MAWB differs ({m.gross_weight} / {m.packages} pcs) and was ignored because MAWB "
                    f"may represent consolidated airline-level cargo."))
                break
        if chosen.packages is None:
            warnings.append(ValidationMessage.warning(
                "PACKAGES_NOT_ON_AUTHORITY",
                f"Selected {kind} authority has no pieces/packages value — enter the HOUSE-level "
                f"count manually; a master air waybill's count is never auto-used."))

        sel_reason = f"selected as {kind} shipment authority"
        def _va(value, unit, label, missing_what) -> ValueAuthority:
            reasons = [sel_reason] + list(chosen.reasons)
            if value is None:
                reasons.append(f"{missing_what} not readable on the authority document — manual "
                               f"review entry required; master air waybill values are never auto-used")
            return ValueAuthority(chosen.logical_form_id, kind, value, unit, label,
                                  chosen.confidence, reasons)

        gross_ev = form.gross_weight.evidence if form and form.gross_weight else None
        return ShipmentAuthority(
            selected_authority_type=kind,
            gross_weight=chosen.gross_weight,
            packages=chosen.packages,
            hawb_number=hawb_no,
            mawb_number=mawb_no,
            bill_of_lading_number=bl_no,
            bill_of_lading_date=bl_date,
            classifications=classifications,
            warnings=warnings,
            selected_form_id=chosen.logical_form_id,
            gross_weight_unit=(form.gross_weight.unit_raw if form and form.gross_weight else None),
            package_source_label=(form.package_source_label_raw if form else None),
            gross_weight_authority=_va(
                chosen.gross_weight,
                (form.gross_weight.unit_raw if form and form.gross_weight else None),
                (gross_ev.label if gross_ev else None), "gross weight"),
            carton_authority=_va(
                chosen.packages, None,
                (form.package_source_label_raw if form else None), "pieces/packages"),
        )

    # ---- 1. HAWB — highest authority, never overridden by MAWB -----------
    # (guarded by the absolute lower-weight rule: a mis-scored label must not
    #  make the heavier consolidated AWB the consignment authority)
    hawbs = [c for c in classifications if c.decision == "HAWB" and c.gross_weight is not None]
    if hawbs:
        return _authority(_prefer_lower_awb(_pick(hawbs, by_id, inv_nos), classifications, warnings),
                          "HAWB")

    # ---- 1.5 bill of lading (sea / land) ---------------------------------
    # The B/L is the house-level transport document of a sea or land shipment:
    # it prints this consignment's own gross weight and package count, exactly
    # like a HAWB does for air, and outranks a delivery order issued against it.
    if bl_weighted:
        chosen_bl = min(bl_weighted, key=lambda c: c.gross_weight)
        return _authority(chosen_bl, "BILL_OF_LADING")
    if bl_classes:
        # classified but unreadable — still the authority; the reviewer enters
        # the values from the B/L itself (never from a DO or packing list that
        # may cover a different consignment)
        warnings.append(ValidationMessage.warning(
            "BL_VALUES_UNREADABLE",
            f"Bill of lading {bl_classes[0].logical_form_id} is the shipment authority but its "
            f"gross weight could not be read from the document. Enter the B/L's gross weight "
            f"and packages in Critical Review."))
        return _authority(bl_classes[0], "BILL_OF_LADING")

    # ---- 2. true Delivery Order ------------------------------------------
    dos = [c for c in classifications if c.decision == "TRUE_DO" and c.gross_weight is not None]
    if dos:
        warnings.append(ValidationMessage.warning(
            "DO_AUTHORITY", "Delivery Order used as gross/packages authority (no HAWB present)."))
        return _authority(_pick(dos, by_id, inv_nos), "TRUE_DO")

    # ---- 3. cargo / consignment tracking ---------------------------------
    tracking = [c for c in classifications if c.decision == "TRACKING" and c.gross_weight is not None]
    if tracking:
        warnings.append(ValidationMessage.warning(
            "TRACKING_AUTHORITY", "Tracking page used as gross/packages authority (no HAWB/DO present)."))
        return _authority(_pick(tracking, by_id, inv_nos), "TRACKING")

    # ---- 3.5 HAWB identified but its numbers were not readable -----------
    # The house document is STILL the highest authority: silently falling
    # through to the master's consolidated values is exactly the
    # 78 kg / 5 ctn vs 163 kg / 10 ctn blunder (real failure 2026-07-19).
    # The missing numbers stay None so Critical Review demands manual entry
    # from the house document — MAWB must never override HAWB.
    hawbs_unread = [c for c in classifications if c.decision == "HAWB"]
    if hawbs_unread:
        chosen = _pick(hawbs_unread, by_id, inv_nos)
        others = [c for c in classifications
                  if c is not chosen and c.decision in ("MAWB", "UNKNOWN_AWB")
                  and (c.gross_weight is not None or c.packages is not None)]
        detail = "; ".join(
            f"{o.logical_form_id}: {o.gross_weight if o.gross_weight is not None else '?'} kg / "
            f"{o.packages if o.packages is not None else '?'} pcs" for o in others)
        warnings.append(ValidationMessage.warning(
            "HAWB_VALUES_UNREADABLE",
            f"House air waybill {chosen.logical_form_id} is the shipment authority but its gross "
            f"weight could not be read from the document"
            + (f"; the other air waybill's values ({detail}) were NOT used — MAWB never overrides "
               f"HAWB" if detail else "")
            + ". Enter the HOUSE document's gross weight and cartons in Critical Review."))
        return _authority(chosen, "HAWB")

    # ---- 4. AWB fallback ---------------------------------------------------
    usable = [c for c in classifications
              if c.decision in ("MAWB", "UNKNOWN_AWB") and c.gross_weight is not None]
    if len(usable) == 1:
        unread = [c for c in classifications
                  if c is not usable[0] and c.decision in ("MAWB", "UNKNOWN_AWB")
                  and c.gross_weight is None]
        if unread:
            warnings.append(ValidationMessage.warning(
                "SECOND_AWB_UNREADABLE",
                f"A second air waybill ({', '.join(c.logical_form_id for c in unread)}) carries no "
                f"readable gross weight — verify the selected AWB is the house-level consignment "
                f"document and not the consolidated master before finalizing."))
        warnings.append(ValidationMessage.warning(
            "AWB_FALLBACK", "Single AWB used as fallback because no HAWB/DO/tracking authority was found."))
        return _authority(usable[0], "SINGLE_AWB")
    if len(usable) > 1:
        # user rule 2026-07-18: with two air waybills the LOWER weight/carton
        # form is ALWAYS the house-level consignment authority — never the
        # heavier consolidated master
        from decimal import Decimal
        lo = min(usable, key=lambda c: (c.gross_weight,
                                        c.packages if c.packages is not None else Decimal("Infinity")))
        warnings.append(ValidationMessage.warning(
            "AWB_HOUSE_INFERRED",
            "House-level AWB inferred from lower gross weight/cartons; explicit HAWB labels "
            "absent — verify before finalizing."))
        if any(lo.packages is not None and o.packages is not None and lo.packages > o.packages
               for o in usable if o is not lo):
            warnings.append(ValidationMessage.warning(
                "AWB_WEIGHT_CARTON_CONFLICT",
                "The lower-weight AWB carries MORE pieces than the other air waybill — verify "
                "gross weight and cartons in Critical Review."))
        return _authority(lo, "HAWB")

    if any(c.chargeable_weight is not None and c.gross_weight is None for c in classifications):
        warnings.append(ValidationMessage.warning(
            "CHARGEABLE_NOT_GROSS",
            "An AWB shows only chargeable/volume weight; that is never used as gross weight."))

    # ---- 5. last resort: mixed DO/packing form, then packing-list totals --
    mixed = [c for c in classifications if c.decision == "MIXED_DO_PACKING" and c.gross_weight is not None]
    if mixed:
        warnings.append(ValidationMessage.warning(
            "PACKING_FALLBACK",
            "Combined Delivery Order / Packing List used only because no HAWB, true DO, tracking "
            "page, or usable AWB was found."))
        return _authority(_pick(mixed, by_id, inv_nos), "PACKING_LIST")
    pl_gross, pl_pkgs, pl_unit = _packing_totals(packing_payloads)
    if pl_gross is not None:
        if not forms:
            warnings.append(ValidationMessage.warning(
                "AWB_MISSING", "No air waybill form available; gross/packages need another authority."))
        warnings.append(ValidationMessage.warning(
            "PACKING_FALLBACK",
            "Packing list totals used only because no HAWB, true DO, tracking page, or usable AWB "
            "was found."))
        pl_reason = ["packing-list totals used as last resort — no shipment-level authority document"]
        return ShipmentAuthority("PACKING_LIST", pl_gross, pl_pkgs, hawb_no, mawb_no,
                                 classifications, warnings,
                                 bill_of_lading_number=bl_no, bill_of_lading_date=bl_date,
                                 gross_weight_unit=pl_unit,
                                 gross_weight_authority=ValueAuthority(
                                     None, "PACKING_LIST", pl_gross, pl_unit,
                                     "total gross weight", 0, list(pl_reason)),
                                 carton_authority=ValueAuthority(
                                     None, "PACKING_LIST", pl_pkgs, None,
                                     "total packages", 0, list(pl_reason)))

    # ---- nothing usable ----------------------------------------------------
    if not classifications:
        warnings.append(ValidationMessage.warning(
            "AWB_MISSING", "No air waybill form available; gross/packages need another authority."))
    else:
        warnings.append(ValidationMessage.warning(
            "AWB_NO_GROSS", "No AWB form carries a usable actual gross weight."))
    return ShipmentAuthority("UNKNOWN", None, None, hawb_no, mawb_no, classifications, warnings,
                             bill_of_lading_number=bl_no, bill_of_lading_date=bl_date)


def derive_field40(ship: ShipmentAuthority) -> tuple[str, str]:
    """Field 40 (Item/Previous_doc/Summary_declaration) default, reviewer rule:

    * two air waybills (HAWB + MAWB) with the SAME gross weight -> MAWB number;
    * two air waybills with DIFFERENT gross weights            -> HAWB number;
    * only one air waybill present                             -> that (master) AWB number;
    * Bill of Lading shipment (no AWB at all)                  -> the B/L number;
    * no transport reference at all                            -> empty.

    Always reviewer-overridable in Critical Review; the reviewed value is
    stamped on every generated item.  Returns ``(value, reason)``.
    """
    awb_classes = [c for c in ship.classifications
                   if c.decision in ("HAWB", "MAWB", "UNKNOWN_AWB")]
    if not awb_classes and not (ship.hawb_number or ship.mawb_number):
        if ship.bill_of_lading_number:
            return ship.bill_of_lading_number, (
                "Bill of Lading shipment (no air waybill) — the B/L number is used.")
        return "", "No transport document reference found — Field 40 left empty."

    if ship.hawb_number and ship.mawb_number:
        # Compare the FORMS' weights directly (lower = house consignment,
        # higher = consolidated master) — never the scored HAWB/MAWB labels,
        # which a mis-classified document can invert (real Field 40 failure
        # corrected 2026-07-18).
        weights = sorted(c.gross_weight for c in awb_classes if c.gross_weight is not None)
        if len(weights) >= 2:
            lo, hi = weights[0], weights[-1]
            if lo == hi:
                return ship.mawb_number, (
                    f"HAWB and MAWB carry the same gross weight ({lo}) — MAWB number used.")
            return ship.hawb_number, (
                f"HAWB gross ({lo}) is lower than MAWB gross ({hi}) — HAWB number used.")
        return ship.hawb_number, (
            "Two air waybills with incomparable weights — HAWB (house-level) number used.")

    single = ship.mawb_number or ship.hawb_number or next(
        (c.awb_number for c in awb_classes if c.awb_number), None)
    if len(awb_classes) >= 2:
        which = "HAWB" if (ship.hawb_number and not ship.mawb_number) else "MAWB"
        return (single or ""), (f"Two air waybills but only the {which} number was readable — "
                                f"it is used; verify Field 40.")
    return (single or ""), "Single air waybill — its (master) number used."
