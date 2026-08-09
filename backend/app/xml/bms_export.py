"""Brand / Model / Size workbook — the sibling export built after the XML.

A pure serializer (no OCR / LLM / network): it takes the same validated
:class:`MergedDeclaration` the XML composer consumes and emits a legacy
BIFF ``.xls`` with one row per item, in exact ``xml_item_sequence`` order,
matching the reference layout:

    (index) | BRAND | MODEL | SIZE

The first header cell is intentionally blank (the index column), mirroring the
supplied sample.  Values are already resolved to ``NA`` when unknown, so this
module only lays them out — it never derives or edits anything.
"""
from __future__ import annotations

import hashlib
import io

import xlwt

BMS_TEMPLATE_VERSION = "brand-model-size-v1"

_HEADERS = ("", "BRAND", "MODEL", "SIZE")


def build_bms_xls(items) -> bytes:
    """Serialize the per-item brand/model/size table to ``.xls`` bytes.

    ``items`` is any sequence exposing ``xml_item_sequence`` / ``brand`` /
    ``model`` / ``size`` — the declaration's items at finalize, or the resolved
    work items when a reviewer edit rebuilds the workbook in place.
    """
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Sheet1")

    bold = xlwt.easyxf("font: bold on")
    for col, head in enumerate(_HEADERS):
        ws.write(0, col, head, bold)

    # invoice order == XML order; sort defensively so the workbook can never
    # disagree with the XML even if items arrive unordered.
    for offset, it in enumerate(sorted(items, key=lambda x: x.xml_item_sequence), start=1):
        ws.write(offset, 0, it.xml_item_sequence)
        ws.write(offset, 1, it.brand or "NA")
        ws.write(offset, 2, it.model or "NA")
        ws.write(offset, 3, it.size or "NA")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def checksum(xls_bytes: bytes) -> str:
    return hashlib.sha256(xls_bytes).hexdigest()
