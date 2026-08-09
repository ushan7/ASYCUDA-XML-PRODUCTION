"""Deterministic OCR scan for the two references a declaration cannot invent:
the parties' EXIM / IEC codes and the shipment's bill-of-lading number+date.

Both are *labelled* values — a document that carries them prints a label next
to them ("EXIM NO:", "B/L NO:") — so they can be read by code with no model in
the loop.  This module is the safety net under the extractor, not a
replacement for it: it only ever fills a field the payload left empty, and
every value it fills is reported as a warning so the reviewer sees where it
came from.

Two user rules drive it (2026-08-06):

* ALWAYS look for the EXIM number on the invoice — it is the importer code the
  XML blocks on, and it prints as often in a footer line as inside the
  consignee block;
* a bill of lading uploaded instead of an air waybill / delivery order must
  supply the shipment's transport reference, and the invoice usually prints
  that same B/L number.
"""
from __future__ import annotations

import re

from ..dates import parse_date

# --------------------------------------------------------------------------- #
# Line handling
# --------------------------------------------------------------------------- #
# OCR pages are markdown-ish: values often sit in the next table cell rather
# than after a colon ("| EXIM NO. | 1234567890123 |").  Cell pipes therefore
# separate tokens exactly like spaces do.
def _lines(text: str) -> list[str]:
    return [re.sub(r"[|*]+", " ", ln) for ln in (text or "").splitlines()]


# --------------------------------------------------------------------------- #
# EXIM / IEC
# --------------------------------------------------------------------------- #
# "EXIM CODE", "EXIM NO.", "EXIM REGD NO", "IEC", "IEC NO", "IE CODE",
# "IMPORTER-EXPORTER CODE", "IEC (PAN BASED)".  A PAN/VAT/GST/TIN label is
# deliberately NOT here: that number is not an EXIM code.
_EXIM_LABEL = re.compile(
    r"\b(?P<label>exim(?:\s*(?:code|no\.?|number|regd?\.?|reg\.?))*"
    r"|i\.?\s*e\.?\s*c\.?(?:\s*\(pan[^)]*\))?(?:\s*(?:code|no\.?|number))*"
    r"|i\.?\s*e\.?\s*code"
    r"|importer\s*[-/]?\s*exporter\s*code)\b[\s:.#=-]*", re.I)
# The code itself: alphanumeric, at least 8 characters (the declaration
# validator wants 13-15) — short enough to be a room number is never a code.
_EXIM_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{6,19}")
_EXIM_STRICT = re.compile(r"[A-Za-z0-9]{13,15}")

_IMPORTER_HINTS = ("consignee", "buyer", "importer", "bill to", "billed to",
                   "sold to", "ship to", "messrs", "m/s", "delivery to")
_EXPORTER_HINTS = ("exporter", "shipper", "seller", "supplier", "beneficiary",
                   "manufacturer", "from")


class RefHit:
    """One labelled value found in the OCR, with the proof it was found by."""

    __slots__ = ("value", "party", "label", "page_no", "quote")

    def __init__(self, value: str, party: str, label: str, page_no: int, quote: str):
        self.value, self.party, self.label = value, party, label
        self.page_no, self.quote = page_no, quote

    def __repr__(self) -> str:                       # pragma: no cover - debug aid
        return f"RefHit({self.value!r}, {self.party!r}, page={self.page_no})"


def _party_of(label: str, before: str) -> str:
    """Which party a code belongs to: the nearest party heading printed above
    it wins; with no heading in range, an Indian-style IEC is the exporter's
    (the shipper prints its own) and a plain EXIM is the importer's (the
    Nepali buyer's code is why the invoice states it at all)."""
    window = before[-320:].lower()
    pos_imp = max((window.rfind(h) for h in _IMPORTER_HINTS), default=-1)
    pos_exp = max((window.rfind(h) for h in _EXPORTER_HINTS), default=-1)
    if pos_imp >= 0 or pos_exp >= 0:
        return "IMPORTER" if pos_imp > pos_exp else "EXPORTER"
    return "EXPORTER" if re.match(r"i\.?\s*e", label.strip(), re.I) else "IMPORTER"


def _value_after(rest: str, follow: str, pattern: re.Pattern,
                 reject: tuple[str, ...]) -> str | None:
    """First plausible value after a label — on the label's own line, else at
    the start of the following line (labels above their value in table cells)."""
    for chunk in (rest, follow):
        for m in pattern.finditer(chunk or ""):
            tok = m.group(0).strip(" .,;:-")
            if tok.upper() in reject or not any(c.isdigit() for c in tok):
                continue
            return tok
        if (chunk or "").strip():
            break            # the label's own line had text but no value: stop
    return None


_REJECT = ("NO", "NO.", "NUMBER", "CODE", "N/A", "NA", "NIL", "NONE", "DATE", "TBA")


def find_exim_codes(ocr_pages: dict[int, str]) -> list[RefHit]:
    """Every labelled EXIM/IEC code in the document, in page order."""
    hits: list[RefHit] = []
    for page_no in sorted(ocr_pages):
        lines = _lines(ocr_pages[page_no])
        for i, line in enumerate(lines):
            for m in _EXIM_LABEL.finditer(line):
                rest = line[m.end():]
                follow = lines[i + 1] if i + 1 < len(lines) else ""
                value = (_value_after(rest, follow, _EXIM_STRICT, _REJECT)
                         or _value_after(rest, follow, _EXIM_VALUE, _REJECT))
                if not value:
                    continue
                before = "\n".join(lines[max(0, i - 12):i]) + line[:m.start()]
                hits.append(RefHit(value, _party_of(m.group("label"), before),
                                   m.group("label").strip(), page_no, line.strip()[:120]))
    return hits


# --------------------------------------------------------------------------- #
# Bill of lading (sea) / consignment note (land)
# --------------------------------------------------------------------------- #
# The `(?![A-Za-z])` guards are load-bearing: without them "B/L" matched the
# "Bl" of "Blower Fan" on a goods row and handed an HS code back as the
# shipment's bill-of-lading number (caught on the demo invoice, 2026-08-06).
_BL_LABEL = re.compile(
    r"\b(?P<label>(?:master|house|ocean|original|combined\s*transport|negotiable)?\s*"
    r"(?:b\s*[/.]\s*l|bl|bill\s+of\s+lading|sea\s*waybill|mbl|hbl|obl"
    r"|consignment\s*note|lorry\s*receipt|railway\s*receipt|truck\s*receipt))"
    r"(?![A-Za-z])\s*(?:no\.?|number|#)?[\s:.#=-]*", re.I)
_BL_DATE_LABEL = re.compile(
    r"\b(?:b\s*[/.]\s*l|bl|bill\s+of\s+lading|shipped\s+on\s+board|on\s*board|"
    r"consignment\s*note|lorry\s*receipt|railway\s*receipt)(?![A-Za-z])\s*"
    r"(?:date|dt\.?|dated)\b[\s:.#=-]*", re.I)
_BL_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9/-]{3,29}")
_DATE_TOKEN = re.compile(
    r"\d{1,2}[-/.\s][A-Za-z]{3,9}[-/.\s,]\s*\d{2,4}"
    r"|[A-Za-z]{3,9}[-/.\s]\d{1,2}[-/.\s,]\s*\d{4}"
    r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")
# Words that follow a "bill of lading" mention in prose rather than as a label
# ("bill of lading is required", "surrender bill of lading") — never a number.
_BL_REJECT = _REJECT + ("COPY", "ORIGINAL", "SURRENDER", "TELEX", "DRAFT")


def find_bill_of_lading(ocr_pages: dict[int, str]) -> tuple[RefHit | None, RefHit | None]:
    """(number, date) — the first labelled B/L number in the document and the
    first B/L date, each with its page and printed line."""
    number = date_hit = None
    for page_no in sorted(ocr_pages):
        lines = _lines(ocr_pages[page_no])
        for i, line in enumerate(lines):
            if number is None:
                m = _BL_LABEL.search(line)
                if m:
                    follow = lines[i + 1] if i + 1 < len(lines) else ""
                    # a "B/L DATE" label is not a number label
                    if not _BL_DATE_LABEL.match(line[m.start():]):
                        value = _value_after(line[m.end():], follow, _BL_VALUE, _BL_REJECT)
                        if value and not _DATE_TOKEN.fullmatch(value):
                            number = RefHit(value, "SHIPMENT", m.group("label").strip(),
                                            page_no, line.strip()[:120])
            if date_hit is None:
                d = _BL_DATE_LABEL.search(line)
                if d:
                    window = line[d.end():] or (lines[i + 1] if i + 1 < len(lines) else "")
                    t = _DATE_TOKEN.search(window)
                    if t and parse_date(t.group(0)) is not None:
                        date_hit = RefHit(t.group(0).strip(), "SHIPMENT",
                                          "B/L date", page_no, line.strip()[:120])
        if number is not None and date_hit is not None:
            break
    return number, date_hit


# --------------------------------------------------------------------------- #
# Backfill (called by the extraction service — code, never the LLM)
# --------------------------------------------------------------------------- #
def backfill_document_refs(role, payload, ocr_pages: dict[int, str]) -> list[str]:
    """Fill EXIM / bill-of-lading fields the payload left empty from the OCR.

    Never overwrites an extracted value; returns one warning per field filled
    so the review shows the provenance.
    """
    from ..domain.enums import DeclaredRole
    from .common_models import InvoiceHeaderRaw, PartyRaw

    notes: list[str] = []
    if role == DeclaredRole.INVOICE:
        header = payload.header or InvoiceHeaderRaw()
        for hit in find_exim_codes(ocr_pages):
            attr = "consignee" if hit.party == "IMPORTER" else "exporter"
            party = getattr(header, attr, None) or PartyRaw()
            if party.exim_code_raw:
                continue
            party.exim_code_raw = hit.value
            setattr(header, attr, party)
            notes.append(
                f"EXIM_CODE_SCANNED: the {attr}'s EXIM code was not extracted; page "
                f"{hit.page_no} prints {hit.label!r} against {hit.value!r} "
                f"({hit.quote!r}) and it was used. Verify it in Critical Review.")
            if not header.exim_code_raw:
                header.exim_code_raw = hit.value
        num, dt = find_bill_of_lading(ocr_pages)
        if num and not header.bill_of_lading_number_raw:
            header.bill_of_lading_number_raw = num.value
            notes.append(
                f"BL_NUMBER_SCANNED: the invoice prints a bill-of-lading reference on page "
                f"{num.page_no} ({num.quote!r}); {num.value!r} was read from it.")
        if dt and not header.bill_of_lading_date_raw:
            header.bill_of_lading_date_raw = dt.value
            notes.append(
                f"BL_DATE_SCANNED: bill-of-lading date {dt.value!r} read from page "
                f"{dt.page_no} ({dt.quote!r}).")
        if payload.header is None and (header.exim_code_raw or header.bill_of_lading_number_raw
                                       or header.exporter or header.consignee):
            payload.header = header

    elif role == DeclaredRole.AIR_WAYBILL:
        forms = list(payload.forms or [])
        if not forms or any(f.bill_of_lading_number_raw for f in forms):
            return notes
        num, dt = find_bill_of_lading(ocr_pages)
        if not num:
            return notes
        # the form the number prints on, else the first form
        target = next((f for f in forms if num.page_no in (f.source_pages or [])), forms[0])
        if target.primary_awb_number_raw == num.value or target.mawb_number_raw == num.value:
            return notes                        # already carried as this form's own number
        target.bill_of_lading_number_raw = num.value
        notes.append(
            f"BL_NUMBER_SCANNED: form {target.logical_form_id} carries no bill-of-lading "
            f"number; page {num.page_no} prints {num.quote!r} and {num.value!r} was read "
            f"from it. Verify it in Critical Review.")
        if dt and not target.bill_of_lading_date_raw:
            target.bill_of_lading_date_raw = dt.value
            notes.append(f"BL_DATE_SCANNED: bill-of-lading date {dt.value!r} read from page "
                         f"{dt.page_no} ({dt.quote!r}).")
    return notes
