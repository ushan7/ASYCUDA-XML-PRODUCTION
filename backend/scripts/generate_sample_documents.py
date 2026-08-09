"""Regenerate backend/sample_data/*.pdf as SYNTHETIC documents.

Why this script exists
----------------------
The five sample PDFs used to be real customer paperwork: a real importer, a
real exporter, a real broker's PAN, a real bank reference, a real insurer.  They
were tracked in git and pushed, and they were not merely pictures — every one
carried an extractable TEXT LAYER, so the party details came out of
`pypdf.extract_text()` in one line.  "Sample data" that is actually a customer's
shipment is a disclosure waiting for someone to clone the repo.

Rather than swap in five more opaque binaries and ask the next reader to trust
them, the samples are now GENERATED from the JSON fixtures beside them.  The
input is text in the repository, the generator is this file, and anyone can
re-run it and diff the result.  Nothing here describes a real shipment.

    python backend/scripts/generate_sample_documents.py

The writer is deliberately dependency-free (no reportlab): a base-14 Helvetica
text layer is a few hundred lines of PDF syntax, and adding a build-time
dependency to produce test fixtures is a worse trade than writing the bytes.
Keeping a real text layer matters — an image-only PDF would silently break the
offline OCR path, which is pypdf text extraction.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_data"
FIXTURES = SAMPLE_DIR / "fixtures"

PAGE_W, PAGE_H = 595, 842          # A4 at 72dpi
MARGIN_X, TOP_Y = 40, 800
LEADING = 12
FONT_SIZE = 9
LINES_PER_PAGE = (TOP_Y - 40) // LEADING


def _escape(text: str) -> str:
    """PDF string literal escaping, ASCII only.

    A stray unbalanced parenthesis in a product description terminates the
    string operator and corrupts the whole content stream, which is the one way
    a generated fixture could fail confusingly rather than loudly.
    """
    out = (text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))
    return out.encode("ascii", "replace").decode("ascii")


def _content_stream(lines: list[str]) -> bytes:
    body = [f"BT /F1 {FONT_SIZE} Tf {MARGIN_X} {TOP_Y} Td {LEADING} TL"]
    for line in lines:
        body.append(f"({_escape(line)}) Tj T*")
    body.append("ET")
    return "\n".join(body).encode("ascii")


def write_pdf(path: pathlib.Path, pages: list[list[str]]) -> None:
    """Emit a minimal but fully valid PDF with a Helvetica text layer."""
    objects: list[bytes] = []

    def add(raw: bytes) -> int:
        objects.append(raw)
        return len(objects)          # 1-based object number

    catalog_num, pages_num, font_num = 1, 2, 3
    objects.extend([b"", b"", b""])  # reserved, filled in below

    page_nums: list[int] = []
    for page_lines in pages:
        stream = _content_stream(page_lines)
        content_num = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        page_nums.append(add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_num, PAGE_W, PAGE_H, font_num, content_num)))

    kids = b" ".join(b"%d 0 R" % n for n in page_nums)
    objects[catalog_num - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_num
    objects[pages_num - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>"
                              % (kids, len(page_nums)))
    objects[font_num - 1] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog_num, xref_at))

    path.write_bytes(bytes(out))


def _paginate(header: list[str], rows: list[str], page_count: int) -> list[list[str]]:
    """Lay `rows` out under `header`, padded or trimmed to exactly page_count."""
    body_capacity = LINES_PER_PAGE - len(header) - 2
    pages: list[list[str]] = []
    for start in range(0, max(len(rows), 1), body_capacity):
        chunk = rows[start:start + body_capacity]
        pages.append(header + [""] + chunk)
        if len(pages) == page_count:
            break
    while len(pages) < page_count:
        pages.append(header + ["", "(this page intentionally carries no goods lines)"])
    for i, page in enumerate(pages, start=1):
        page.append("")
        page.append(f"Page {i} of {page_count}   -   SYNTHETIC DEMO DOCUMENT, NOT A REAL SHIPMENT")
    return pages


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build_invoice() -> list[list[str]]:
    fx = _fixture("invoice.json")
    h = fx["header"]
    exporter = (h["exporter"]["name_raw"] or "").replace("\n", " ")
    consignee = (h["consignee"]["name_raw"] or "").replace("\n", " ")
    header = [
        "SUMMARY INVOICE",
        f"EXPORTER: {exporter}",
        f"CONSIGNEE: {consignee}",
        f"EXIM/VAT: {h['consignee'].get('exim_code_raw','')}",
        f"Invoice No: {h['invoice_number_raw']}    Date: {h['invoice_date_raw']}",
        f"Currency: {h['currency_raw']}    Terms: {h['incoterm_raw']} "
        f"{h['incoterm_place_raw']}",
        f"Payment: {h['payment_terms_raw']}    LC No: {h.get('lc_reference_raw','')}",
        "",
        "SN  DESCRIPTION                                  HS          QTY   UNIT      AMOUNT",
    ]
    rows = []
    for i, r in enumerate(fx["rows"], start=1):
        desc = (r.get("description_raw") or "")[:42]
        rows.append(f"{i:<3} {desc:<44} {r.get('hs_code_raw','') or '':<11} "
                    f"{r.get('quantity_raw','') or '':>5} "
                    f"{r.get('unit_price_raw','') or '':>8} "
                    f"{r.get('line_total_raw','') or '':>10}")
    t = fx["totals"]
    rows += ["", f"{'GOODS SUBTOTAL':>62} {t.get('goods_subtotal_raw',''):>10}",
             f"{'GRAND TOTAL':>62} {t.get('grand_total_raw',''):>10}"]
    return _paginate(header, rows, 4)


def build_packing_list() -> list[list[str]]:
    fx = _fixture("packing_list.json")
    header = [
        "PACKING LIST",
        f"Packing List No: {fx.get('packing_list_number_raw','')}",
        f"Invoice Ref: {', '.join(fx.get('invoice_references_raw') or [])}",
        "",
        "SN  DESCRIPTION                                  QTY    NET KG   GROSS KG  CTN",
    ]
    rows = []
    for i, r in enumerate(fx["rows"], start=1):
        desc = (r.get("description_raw") or "")[:42]
        rows.append(f"{i:<3} {desc:<44} {r.get('quantity_raw','') or '':>5} "
                    f"{r.get('net_weight_raw','') or '':>8} "
                    f"{r.get('gross_weight_raw','') or '':>9} "
                    f"{r.get('carton_no_raw','') or '':>4}")
    gw = fx.get("total_gross_weight", {})
    pk = fx.get("total_packages", {})
    rows += ["", f"TOTAL GROSS WEIGHT: {gw.get('value_raw','')} {gw.get('unit_raw','')}",
             f"TOTAL PACKAGES: {pk.get('value_raw','')} {pk.get('unit_raw','')}"]
    return _paginate(header, rows, 3)


def build_air_waybill() -> list[list[str]]:
    fx = _fixture("air_waybill.json")
    pages = []
    for form in fx["forms"]:
        gw, cw = form.get("gross_weight", {}), form.get("chargeable_weight", {})
        pcs, fr = form.get("pieces_or_packages", {}), form.get("freight_amount", {})
        lines = [
            form.get("document_title_raw", "AIR WAYBILL").upper(),
            "",
            f"AWB No: {form.get('primary_awb_number_raw','')}",
            f"MAWB No: {form.get('mawb_number_raw','') or '-'}",
            f"HAWB No: {form.get('hawb_number_raw','') or '-'}",
            f"Carrier: {form.get('carrier_raw','') or '-'}",
            f"Issued by: {form.get('issuer_raw','') or '-'}",
            f"Shipper: {(form.get('shipper') or {}).get('name_raw','-')}",
            f"Consignee: {(form.get('consignee') or {}).get('name_raw','-')}",
            "",
            f"Gross Weight: {gw.get('value_raw','')} {gw.get('unit_raw','')}",
            f"Chargeable Weight: {cw.get('value_raw','')} {cw.get('unit_raw','')}",
            f"Pieces/Packages: {pcs.get('value_raw','')} {pcs.get('unit_raw','')}",
            f"Freight: {fr.get('amount_raw','-')} {fr.get('currency_raw','')}",
            f"Freight Payment: {form.get('freight_payment_status_raw','')}",
            "",
            "SYNTHETIC DEMO DOCUMENT, NOT A REAL SHIPMENT",
        ]
        pages.append(lines)
    return pages


def build_banking() -> list[list[str]]:
    fx = _fixture("banking.json")
    amt = fx.get("amount", {})
    header = [
        "DOCUMENTARY CREDIT ADVICE",
        f"SWIFT Message Type: {fx.get('swift_message_type_raw','')}",
        f"Sender BIC: {fx.get('sender_bic_raw','')}",
        f"Issuing Bank: {fx.get('issuing_or_applicant_bank_name_raw','')}",
        f"Credit Reference: {fx.get('reference_number_raw','')}",
        f"Value Date: {fx.get('issue_or_value_date_raw','')}",
        f"Amount: {amt.get('amount_raw','')} {amt.get('currency_raw','')}",
        f"Payment Terms: {fx.get('payment_terms_raw','')}",
        f"Draft Tenor: {fx.get('draft_tenor_raw','')}",
        f"Invoice References: {', '.join(fx.get('invoice_references_raw') or [])}",
    ]
    rows = [f"Freight mention: {m.get('amount_raw','')} {m.get('currency_raw','')}"
            for m in fx.get("freight_mentions") or []]
    return _paginate(header, rows, 4)


def build_insurance() -> list[list[str]]:
    header = [
        "MARINE / AIR CARGO INSURANCE CERTIFICATE",
        "",
        "Insurer: Example Insurance Company Limited",
        "Insurer PAN/VAT No: 900000002",
        "Policy No: POLDEMO0000001",
        "Insured: Demo Importers Pvt.Ltd, Lalitpur",
        "Voyage: CHINA to KATHMANDU by air",
        "Sum Insured: 2319.78 USD",
        "Premium: 12.50 USD",
        "Cover: All Risks, warehouse to warehouse",
    ]
    return _paginate(header, [], 2)


BUILDERS = {
    "sample_invoice.pdf": build_invoice,
    "sample_packing_list.pdf": build_packing_list,
    "sample_airwaybill.pdf": build_air_waybill,
    "sample_banking_doc.pdf": build_banking,
    "sample_insurance.pdf": build_insurance,
}


def main() -> int:
    for name, builder in BUILDERS.items():
        target = SAMPLE_DIR / name
        write_pdf(target, builder())
        print(f"wrote {target.relative_to(ROOT.parent)} "
              f"({target.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
