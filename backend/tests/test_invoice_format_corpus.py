"""Invoice-layout corpus — the deterministic parser's breadth, pinned.

The parser only ever sees what a vendor chose to print, and vendors choose very
differently: the description header is "Description" or "Particular" or
"Bezeichnung" or "Mô tả hàng hóa"; the quantity is "Qty" or "Q'ty" or "Menge"
or "Số lượng"; the money total is "Amount" or "Betrag" or "Importe" or "Thành
tiền".  Every header this vocabulary does NOT know costs a whole document: the
column map fails, the parser stands down, and the row values come back from an
LLM that has no arithmetic gate behind it.

This file is the corpus that keeps that vocabulary honest.  Each case is a
realistic table — arithmetic-consistent rows under a printed total that matches
them, so a correct parse must own the page under the printed-totals gate, and a
column read wrongly cannot pass by accident.

Grouped by what each case is actually testing:

* ``LAYOUTS``      — the header/column shapes themselves, one per vendor idiom
* ``FIELD_CASES``  — cases where the row count is right but a specific FIELD
                     used to land in the wrong place (the expensive kind of bug:
                     silent, plausible, and wrong)
* ``REFUSALS``     — documents the parser must decline rather than misread

Cases carrying a ``why`` are regressions with a story; the rest are breadth.
"""
import pytest

from app.domain.enums import DeclaredRole
from app.extraction.table_parser import parse_pages
from app.numbers import detect_numeric_locale


def _parse(page: str):
    return parse_pages(DeclaredRole.INVOICE, {1: page}, {1: detect_numeric_locale(page)})


def _rows(page: str):
    res = _parse(page)
    pp = res.pages.get(1)
    return (pp.rows if pp else []), res


# --------------------------------------------------------------------------- #
# 1. Layouts — every one of these must parse to exactly its printed rows
# --------------------------------------------------------------------------- #
LAYOUTS = {
"cn_generic": ("China · generic commercial invoice", 3, """
| ITEM NO. | DESCRIPTION OF GOODS | QUANTITY | UNIT | UNIT PRICE | AMOUNT |
| --- | --- | --- | --- | --- | --- |
| 1 | COTTON T-SHIRT | 500 | PCS | 2.50 | 1250.00 |
| 2 | POLYESTER JACKET | 200 | PCS | 8.00 | 1600.00 |
| 3 | DENIM TROUSERS | 300 | PCS | 6.50 | 1950.00 |
| TOTAL |  |  |  |  | 4800.00 |
"""),
"in_gst": ("India · GST tax invoice with a taxable-value column", 2, """
| Sr. No. | Particulars | HSN/SAC | Qty | UOM | Rate | Amount | Taxable Value | IGST % | IGST Amt |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | SURGICAL FORCEPS | 90183900 | 50 | PCS | 120.00 | 6000.00 | 6000.00 | 12 | 720.00 |
| 2 | SCALPEL BLADE | 90183200 | 200 | PCS | 15.00 | 3000.00 | 3000.00 | 12 | 360.00 |
| Total |  |  |  |  |  | 9000.00 | 9000.00 |  | 1080.00 |
"""),
"us_style": ("USA · part number + extended price", 2, """
| Line | Part Number | Description | Qty Shipped | U/M | Unit Price | Extended Price |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | RX-4410-B | BEARING ASSEMBLY | 24 | EA | 45.00 | 1080.00 |
| 2 | RX-9920 | SEAL KIT | 100 | EA | 12.50 | 1250.00 |
| Total Amount |  |  |  |  |  | 2330.00 |
"""),
"uk_commodity_code": ("UK · 'Commodity Code' is the tariff column", 2, """
| Item | Goods Description | Commodity Code | Quantity | Unit | Price Each | Net Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | STAINLESS STEEL FLANGE | 73079300 | 40 | PCS | 22.00 | 880.00 |
| 2 | CARBON STEEL PIPE | 73043900 | 15 | MTR | 60.00 | 900.00 |
| TOTAL GBP |  |  |  |  |  | 1780.00 |
"""),
"merged_qty_uom": ("quantity and unit merged in one cell", 2, """
| S/N | Description | HS Code | Qty | Unit Price | Total |
| --- | --- | --- | --- | --- | --- |
| 1 | LED PANEL LIGHT | 94054090 | 100 PCS | 9.00 | 900.00 |
| 2 | LED DRIVER | 85044090 | 250 PCS | 4.00 | 1000.00 |
| TOTAL USD |  |  |  |  | 1900.00 |
"""),
"no_line_no": ("no serial-number column at all", 2, """
| Description of Merchandise | HS Code | Quantity | Unit | Rate | Value |
| --- | --- | --- | --- | --- | --- |
| CERAMIC FLOOR TILE 600X600 | 69072300 | 1200 | SQM | 5.50 | 6600.00 |
| CERAMIC WALL TILE 300X600 | 69072200 | 800 | SQM | 4.25 | 3400.00 |
| TOTAL VALUE |  |  |  |  | 10000.00 |
"""),
"coo_column": ("per-row country of origin", 2, """
| No | Product Description | HS Code | Country of Origin | Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | HYDRAULIC PUMP | 84136090 | DE | 5 | SET | 400.00 | 2000.00 |
| 2 | CONTROL VALVE | 84812000 | IT | 12 | PCS | 150.00 | 1800.00 |
| Grand Total |  |  |  |  |  |  | 3800.00 |
"""),
"pharma_batch": ("pharma · batch, expiry and pack columns", 2, """
| Sl | Product Name | Batch No | Exp. Date | HSN | Qty | Pack | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | AMOXICILLIN 500MG CAP | B2401 | 06/2027 | 30041020 | 500 | BOX | 3.20 | 1600.00 |
| 2 | PARACETAMOL 650MG TAB | B2402 | 09/2027 | 30049099 | 800 | BOX | 1.50 | 1200.00 |
| Total |  |  |  |  |  |  |  | 2800.00 |
"""),
"textile_style": ("apparel · style / colour / size columns", 2, """
| Sr | Style No | Description | Colour | Size | HS Code | Qty | Unit | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ST-7788 | LADIES BLOUSE | NAVY | M | 62064000 | 600 | PCS | 4.75 | 2850.00 |
| 2 | ST-7789 | LADIES BLOUSE | WHITE | L | 62064000 | 400 | PCS | 4.75 | 1900.00 |
| TOTAL |  |  |  |  |  |  |  |  | 4750.00 |
"""),
"footwear_prs": ("footwear · pairs and dozens", 2, """
| S.No | Article | Description | HS Code | Qty | Unit | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | AR-100 | LEATHER FORMAL SHOE | 64039900 | 240 | PRS | 18.00 | 4320.00 |
| 2 | AR-200 | CANVAS SNEAKER | 64041900 | 50 | DZN | 96.00 | 4800.00 |
| TOTAL USD |  |  |  |  |  |  | 9120.00 |
"""),
"chemical_drum": ("chemicals · 'Chemical Name', drums and litres", 2, """
| Item | Chemical Name | CAS No | HS Code | Quantity | UOM | Unit Price | Total Price |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | SODIUM HYDROXIDE 99% | 1310-73-2 | 28151100 | 40 | DRUM | 85.00 | 3400.00 |
| 2 | ACETIC ACID GLACIAL | 64-19-7 | 29152100 | 2000 | LTR | 1.20 | 2400.00 |
| TOTAL |  |  |  |  |  |  | 5800.00 |
"""),
"weight_per_row": ("per-row net weight column", 2, """
| No. | Description | HS Code | Qty | Unit | Net Weight (KG) | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | ALUMINIUM INGOT | 76011000 | 20 | MT | 20000 | 1800.00 | 36000.00 |
| 2 | COPPER WIRE ROD | 74071000 | 5 | MT | 5000 | 7200.00 | 36000.00 |
| TOTAL |  |  |  |  |  |  | 72000.00 |
"""),
"discount_col": ("per-row discount with a net price column", 2, """
| # | Description | Qty | Unit | List Price | Discount % | Net Price | Line Total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | OFFICE CHAIR | 60 | PCS | 50.00 | 10 | 45.00 | 2700.00 |
| 2 | OFFICE DESK | 30 | PCS | 120.00 | 10 | 108.00 | 3240.00 |
| Total |  |  |  |  |  |  | 5940.00 |
"""),
"de_full": ("Germany · fully German header", 2, """
| Pos. | Artikelnummer | Bezeichnung | Menge | Einheit | Einzelpreis | Betrag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | A-1001 | KUGELLAGER 6205 | 200 | STK | 3,50 | 700,00 |
| 2 | A-1002 | DICHTUNGSRING | 500 | STK | 0,80 | 400,00 |
| Gesamtbetrag |  |  |  |  |  | 1.100,00 |
"""),
"fr_full": ("France · French header", 2, """
| Réf. | Désignation | Code SH | Quantité | Unité | Prix unitaire | Montant |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PARFUM EAU DE TOILETTE 100ML | 33030010 | 300 | PCS | 12,00 | 3600,00 |
| 2 | CRÈME HYDRATANTE 50ML | 33049900 | 500 | PCS | 6,00 | 3000,00 |
| Total |  |  |  |  |  | 6600,00 |
"""),
"es_full": ("Spain · Spanish header", 2, """
| Nº | Descripción | Código SA | Cantidad | Unidad | Precio unitario | Importe |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | ACEITE DE OLIVA VIRGEN 1L | 15091000 | 1000 | LTR | 4,50 | 4500,00 |
| 2 | ACEITUNAS VERDES 500G | 20057000 | 800 | PCS | 1,25 | 1000,00 |
| Total |  |  |  |  |  | 5500,00 |
"""),
"it_full": ("Italy · Italian header", 2, """
| N. | Descrizione | Codice HS | Quantità | Unità | Prezzo unitario | Importo |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PIASTRELLE IN GRES 60X60 | 69072300 | 500 | SQM | 9,00 | 4500,00 |
| 2 | COLLA PER PIASTRELLE 25KG | 32149000 | 200 | BAG | 7,50 | 1500,00 |
| Totale |  |  |  |  |  | 6000,00 |
"""),
"nl_full": ("Netherlands · Dutch header", 2, """
| Nr | Omschrijving | GN-code | Aantal | Eenheid | Prijs per stuk | Bedrag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | BLOEMBOLLEN TULP | 06011010 | 5000 | PCS | 0,40 | 2000,00 |
| 2 | BLOEMBOLLEN NARCIS | 06011010 | 3000 | PCS | 0,35 | 1050,00 |
| Totaal |  |  |  |  |  | 3050,00 |
"""),
"pt_full": ("Brazil / Portugal · Portuguese header", 2, """
| Item | Descrição | NCM | Quantidade | Unidade | Preço unitário | Valor total |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | CAFÉ TORRADO EM GRÃOS | 09012100 | 2000 | KG | 6,00 | 12000,00 |
| 2 | AÇÚCAR CRISTAL | 17019900 | 5000 | KG | 0,80 | 4000,00 |
| Total |  |  |  |  |  | 16000,00 |
"""),
"tr_full": ("Turkey · Turkish header (dotless ı)", 2, """
| Sıra | Açıklama | GTIP | Miktar | Birim | Birim Fiyat | Tutar |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PAMUKLU KUMAS | 52081200 | 3000 | MTR | 2,20 | 6600,00 |
| 2 | POLYESTER IPLIK | 54023300 | 1500 | KG | 3,00 | 4500,00 |
| Toplam |  |  |  |  |  | 11100,00 |
"""),
"vn_full": ("Vietnam · Vietnamese header (đ and tone marks)", 2, """
| STT | Mô tả hàng hóa | Mã HS | Số lượng | Đơn vị | Đơn giá | Thành tiền |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | GIAY THE THAO NAM | 64041100 | 400 | PRS | 11.00 | 4400.00 |
| 2 | AO THUN COTTON | 61091000 | 1000 | PCS | 3.20 | 3200.00 |
| Tổng cộng |  |  |  |  |  | 7600.00 |
"""),
"id_full": ("Indonesia · Indonesian header", 2, """
| No | Uraian Barang | Kode HS | Jumlah | Satuan | Harga Satuan | Total Harga |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | KOPI ARABIKA BIJI | 09011100 | 3000 | KG | 7.50 | 22500.00 |
| 2 | MINYAK KELAPA SAWIT | 15111000 | 10000 | KG | 1.05 | 10500.00 |
| Jumlah |  |  |  |  |  | 33000.00 |
"""),
"cn_bilingual": ("China · CJK/English bilingual header", 2, """
| 序号 No. | 品名 Description | 数量 Quantity | 单位 Unit | 单价 Unit Price | 金额 Amount |
| --- | --- | --- | --- | --- | --- |
| 1 | SOLAR PANEL 450W | 200 | PCS | 95.00 | 19000.00 |
| 2 | SOLAR INVERTER 5KW | 50 | PCS | 260.00 | 13000.00 |
| 合计 Total |  |  |  |  | 32000.00 |
"""),
"ar_bilingual": ("UAE · Arabic/English bilingual header", 2, """
| رقم No. | الوصف Description | HS Code | الكمية Qty | الوحدة Unit | السعر Unit Price | المبلغ Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | DATES MEDJOOL 5KG BOX | 08041020 | 400 | BOX | 18.00 | 7200.00 |
| 2 | ARABIC COFFEE BEANS 1KG | 09011100 | 600 | PCS | 9.00 | 5400.00 |
| المجموع Total |  |  |  |  |  | 12600.00 |
"""),
"ru_bilingual": ("Russia · Cyrillic/English bilingual header", 2, """
| № | Наименование Description | Код ТН ВЭД HS Code | Кол-во Quantity | Ед. Unit | Цена Unit Price | Сумма Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | BIRCH PLYWOOD 18MM | 44123300 | 800 | SHT | 22.00 | 17600.00 |
| 2 | PINE SAWN TIMBER | 44071100 | 60 | CBM | 180.00 | 10800.00 |
| Итого Total |  |  |  |  |  | 28400.00 |
"""),
"th_bilingual": ("Thailand · Thai/English bilingual header", 2, """
| ลำดับ No. | รายการ Description | HS Code | จำนวน Quantity | หน่วย Unit | ราคา Unit Price | รวม Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | JASMINE RICE 5KG BAG | 10063030 | 1000 | BAG | 6.50 | 6500.00 |
| 2 | CANNED PINEAPPLE 565G | 20082000 | 2400 | CAN | 0.85 | 2040.00 |
| รวมทั้งสิ้น Total |  |  |  |  |  | 8540.00 |
"""),
"jp_qty_apostrophe": ("Japan · \"Q'ty\" abbreviation", 2, """
| No. | Commodity | HS Code | Q'ty | Unit | Unit Price (USD) | Amount (USD) |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | CNC LATHE SPINDLE | 84669390 | 4 | UNIT | 3200.00 | 12800.00 |
| 2 | TOOL HOLDER BT40 | 84662090 | 60 | PCS | 55.00 | 3300.00 |
| Total Amount |  |  |  |  |  | 16100.00 |
"""),
"kr_amount_only": ("Korea · amount only, no unit rate printed", 2, """
| Item No | Goods | HS CODE | Q'TY | UNIT | AMOUNT |
| --- | --- | --- | --- | --- | --- |
| 1 | AUTOMOTIVE BRAKE PAD | 87083010 | 800 | SET | 9600.00 |
| 2 | AUTOMOTIVE OIL FILTER | 84212300 | 1500 | PCS | 4500.00 |
| TOTAL AMOUNT |  |  |  |  | 14100.00 |
"""),
"ae_reexport": ("UAE · re-export with origin and value columns", 2, """
| SL | DESCRIPTION | ORIGIN | HS CODE | QTY | UOM | RATE (USD) | VALUE (USD) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | MOBILE PHONE ACCESSORIES | CN | 85177900 | 2000 | PCS | 1.80 | 3600.00 |
| 2 | POWER BANK 10000MAH | CN | 85076000 | 500 | PCS | 6.40 | 3200.00 |
| TOTAL |  |  |  |  |  |  | 6800.00 |
"""),
"currency_symbol": ("money cells carrying a currency symbol", 2, """
| # | Description | HS | Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | WOODEN DINING CHAIR | 94016100 | 80 | PCS | $35.00 | $2,800.00 |
| 2 | WOODEN DINING TABLE | 94036000 | 20 | PCS | $150.00 | $3,000.00 |
| TOTAL USD |  |  |  |  |  | $5,800.00 |
"""),
"qty_unit_slash": ("combined 'Qty./ Unit' header with no-space cells", 2, """
| Sn | Description of Goods | HSN/SAC | Qty./ Unit | Rate In INR | Amount In INR |
| --- | --- | --- | --- | --- | --- |
| 1 | STAINLESS STEEL BOWL | 73239390 | 500PC | 60.00 | 30000.00 |
| 2 | STAINLESS STEEL TRAY | 73239390 | 300PC | 90.00 | 27000.00 |
| Total |  |  |  |  | 57000.00 |
"""),
"proforma": ("proforma wording · 'Ordered Qty' / 'Line Value'", 2, """
| Item | Product Description | Tariff Code | Ordered Qty | UoM | Price/Unit | Line Value |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | INDUSTRIAL SEWING MACHINE | 84523000 | 10 | UNIT | 850.00 | 8500.00 |
| 2 | SPARE NEEDLE SET | 84529900 | 200 | SET | 7.50 | 1500.00 |
| Total Value |  |  |  |  |  | 10000.00 |
"""),
"nos_unit": ("Indian 'Nos' unit", 2, """
| Sr No | Description of Goods | HSN Code | Quantity | Units | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | MS ANGLE 50X50X5 | 72161000 | 1200 | NOS | 12.00 | 14400.00 |
| 2 | MS CHANNEL 100X50 | 72163100 | 400 | NOS | 28.00 | 11200.00 |
| Total |  |  |  |  |  | 25600.00 |
"""),
"multi_currency_row": ("per-row currency column", 2, """
| # | Description | HS | Qty | Unit | Currency | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | FROZEN SHRIMP 16/20 | 03061700 | 5000 | KG | USD | 8.20 | 41000.00 |
| 2 | FROZEN SQUID TUBES | 03074300 | 3000 | KG | USD | 4.60 | 13800.00 |
| TOTAL USD |  |  |  |  |  |  | 54800.00 |
"""),
"brand_col": ("explicit brand column beside the model", 2, """
| No | Brand | Model | Description | HS Code | Qty | Unit | Unit Price | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | HIKVISION | DS-2CD1043 | IP CAMERA 4MP | 85258900 | 100 | PCS | 38.00 | 3800.00 |
| 2 | HIKVISION | DS-7608NI | NVR 8 CHANNEL | 85219090 | 40 | PCS | 95.00 | 3800.00 |
| Grand Total |  |  |  |  |  |  |  | 7600.00 |
"""),
"thousand_sep_qty": ("quantities and money with thousands separators", 2, """
| No. | Description | HS Code | Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PAPER A4 80GSM | 48025500 | 12,000 | REAM | 2.10 | 25,200.00 |
| 2 | CARBONLESS PAPER ROLL | 48099000 | 3,500 | ROLL | 1.40 | 4,900.00 |
| TOTAL USD |  |  |  |  |  | 30,100.00 |
"""),
"sqft_yds": ("imperial trade units", 2, """
| # | Description | HS Code | Quantity | UOM | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | HANDMADE WOOL CARPET | 57011000 | 2500 | SQFT | 12.00 | 30000.00 |
| 2 | JUTE FABRIC ROLL | 53101010 | 4000 | YDS | 1.50 | 6000.00 |
| TOTAL |  |  |  |  |  | 36000.00 |
"""),
"space_thousands": ("EU · space thousands with a decimal comma", 2, """
| Pos | Bezeichnung | HS Code | Menge | Einheit | Einzelpreis | Betrag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | WERKZEUGSTAHL BLOCK | 72285000 | 12 | STK | 1 250,00 | 15 000,00 |
| 2 | SCHNEIDEINSATZ CNMG | 82090000 | 500 | STK | 9,00 | 4 500,00 |
| Gesamtbetrag |  |  |  |  |  | 19 500,00 |
"""),
"swiss_apostrophe": ("Switzerland · apostrophe thousands, 'Zolltarif'", 2, """
| Pos | Bezeichnung | Zolltarif | Menge | Einheit | Einzelpreis | Betrag |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | UHRWERK ETA 2824 | 91081100 | 500 | STK | 45.00 | 22'500.00 |
| 2 | UHRENARMBAND LEDER | 91131000 | 800 | STK | 12.00 | 9'600.00 |
| Total |  |  |  |  |  | 32'100.00 |
"""),
"merged_packsize": ("merged 'qty UOM (pack size)' cell", 2, """
| Sales Order Item | Material Number | Material Description | Quantity | Unit Price | Total |
| --- | --- | --- | --- | --- | --- |
| 10 | 01E3120 | GUIDING CATHETER 6F | 25 EA (1/EA) | 24.46 | 611.50 |
| 20 | 01E3140 | GUIDING CATHETER 7F | 40 EA (1/EA) | 24.46 | 978.40 |
| Total Amount |  |  |  |  | 1589.90 |
"""),
"wide_table": ("15 columns with a full tax breakdown", 2, """
| Sr | Item Code | Description | HSN | Batch | Mfg | Exp | Qty | UOM | Rate | Amount | Disc | Taxable | GST% | Total |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | IC-100 | VITAMIN C TABLET | 30045010 | V101 | 01/25 | 12/27 | 1000 | BOX | 4.00 | 4000.00 | 0 | 4000.00 | 12 | 4480.00 |
| 2 | IC-200 | VITAMIN D CAPSULE | 30045020 | V202 | 02/25 | 01/28 | 500 | BOX | 6.00 | 3000.00 | 0 | 3000.00 | 12 | 3360.00 |
| Total |  |  |  |  |  |  |  |  |  | 7000.00 |  | 7000.00 |  | 7840.00 |
"""),
"section_subtotals": ("sections with their own subtotals", 3, """
| # | Description | HS Code | Qty | Unit | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | STEEL BOLT M12 | 73181500 | 5000 | PCS | 0.40 | 2000.00 |
| 2 | STEEL NUT M12 | 73181600 | 5000 | PCS | 0.20 | 1000.00 |
| Sub Total |  |  |  |  |  | 3000.00 |
| 3 | WASHER FLAT M12 | 73182200 | 8000 | PCS | 0.10 | 800.00 |
| Sub Total |  |  |  |  |  | 800.00 |
| Grand Total |  |  |  |  |  | 3800.00 |
"""),
"amount_only_no_unit": ("no unit column and no rate — amount only", 2, """
| Item | Goods Description | Commodity Code | Quantity | Value |
| --- | --- | --- | --- | --- |
| 1 | ASSORTED SPARE PARTS | 84879000 | 40 | 1200.00 |
| 2 | PACKING MATERIAL | 48191000 | 100 | 300.00 |
| Total Value |  |  |  | 1500.00 |
"""),
"qty_in_words_tail": ("an amount-in-words line below the totals", 2, """
| Item | Description | HS Code | Quantity | Unit | Unit Rate | Total |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | PVC PIPE 110MM | 39172300 | 500 | MTR | 4.00 | 2000.00 |
| 2 | PVC ELBOW 110MM | 39174000 | 800 | PCS | 1.25 | 1000.00 |
| TOTAL |  |  |  |  |  | 3000.00 |
| Amount in words | THREE THOUSAND US DOLLARS ONLY |  |  |  |  |  |
"""),
}


@pytest.mark.parametrize("case", sorted(LAYOUTS), ids=sorted(LAYOUTS))
def test_layout_parses_to_its_printed_rows(case):
    note, expected, page = LAYOUTS[case]
    rows, res = _rows(page)
    assert len(rows) == expected, f"{note}: parsed {len(rows)} of {expected} rows — {res.notes}"


@pytest.mark.parametrize("case", sorted(LAYOUTS), ids=sorted(LAYOUTS))
def test_layout_line_values_reconcile_to_the_printed_total(case):
    """Every case prints a total its rows add up to, so a page that is owned but
    whose money columns were read wrongly cannot pass this."""
    _note, _expected, page = LAYOUTS[case]
    _rows_, res = _rows(page)
    assert any("matches the printed invoice total" in n for n in res.notes), res.notes


@pytest.mark.parametrize("case", sorted(LAYOUTS), ids=sorted(LAYOUTS))
def test_layout_never_reports_a_quantity_as_its_own_hs_code(case):
    """The 2026-08-03 signature: a shifted column map put an 11-digit tariff
    number in the quantity field.  No goods row on earth ships 48 billion of
    anything, so an absurd quantity is a column-map failure by definition."""
    _note, _expected, page = LAYOUTS[case]
    rows, _res = _rows(page)
    for r in rows:
        digits = len((r.quantity_raw or "").replace(",", "").replace(".", "").strip())
        assert digits < 8, f"quantity {r.quantity_raw!r} looks like a code, not a count"


# --------------------------------------------------------------------------- #
# 2. Field placement — right row count, wrong field, which is the silent kind
# --------------------------------------------------------------------------- #
def test_hs_code_is_taken_from_the_column_that_prints_it():
    _n, _e, page = LAYOUTS["uk_commodity_code"]
    rows, _ = _rows(page)
    assert [r.hs_code_raw for r in rows] == ["73079300", "73043900"], (
        "'Commodity Code' is what EU/UK invoices call the tariff column; without "
        "it the printed HS was dropped and the resolver guessed one from the text")


def test_description_column_after_the_numbers_is_still_the_description():
    page = """
| Item | HS Code | Qty | Unit | Unit Price | Amount | Description of Goods |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 85287200 | 150 | PCS | 120.00 | 18000.00 | LED TELEVISION 43 INCH |
| 2 | 85176200 | 300 | PCS | 25.00 | 7500.00 | WIFI ROUTER DUAL BAND |
| TOTAL |  |  |  |  | 25500.00 |  |
"""
    rows, _ = _rows(page)
    assert [r.description_raw for r in rows] == ["LED TELEVISION 43 INCH", "WIFI ROUTER DUAL BAND"], (
        "the mapped description was ignored when it sat after the quantity, and "
        "the positional fallback declared the HS-code cell as the goods text")


@pytest.mark.parametrize("case", ["uk_commodity_code", "chemical_drum", "fr_full",
                                  "cn_bilingual", "qty_in_words_tail"])
def test_line_counter_never_becomes_the_model(case):
    _n, _e, page = LAYOUTS[case]
    rows, _ = _rows(page)
    for r in rows:
        assert r.model_raw != r.line_no_raw or r.model_raw is None
        assert not (r.model_raw or "").strip().isdigit() or len(r.model_raw.strip()) > 3


@pytest.mark.parametrize("case", ["no_line_no", "coo_column", "pharma_batch", "proforma"])
def test_description_is_never_mined_for_a_model(case):
    """"Product Name"/"Product Description" matched the model pattern, so the
    model column WAS the description column and every row shipped the first word
    of its own description as its part number."""
    _n, _e, page = LAYOUTS[case]
    rows, _ = _rows(page)
    for r in rows:
        assert r.model_raw is None or not r.description_raw.startswith(r.model_raw), (
            f"model {r.model_raw!r} was chopped off the description {r.description_raw!r}")


def test_money_column_is_never_taken_as_the_model():
    page = """
| SL | Description | HS Code | Amount | Qty | Unit | Rate |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | COTTON YARN 30S | 52051200 | 12000.00 | 4000 | KG | 3.00 |
| 2 | POLYESTER YARN 150D | 54023300 | 7500.00 | 2500 | KG | 3.00 |
| Total |  |  | 19500.00 |  |  |  |
"""
    rows, _ = _rows(page)
    assert len(rows) == 2
    assert all(r.model_raw is None for r in rows), [r.model_raw for r in rows]


def test_real_part_codes_are_still_kept():
    """The guards above must not cost the model column where one really exists."""
    for case, expected in (("us_style", ["RX-4410-B", "RX-9920"]),
                           ("textile_style", ["ST-7788", "ST-7789"]),
                           ("de_full", ["A-1001", "A-1002"]),
                           ("merged_packsize", ["01E3120", "01E3140"]),
                           ("brand_col", ["DS-2CD1043", "DS-7608NI"])):
        _n, _e, page = LAYOUTS[case]
        rows, _ = _rows(page)
        assert [r.model_raw for r in rows] == expected, case


def test_shipped_quantity_outranks_ordered_quantity():
    """A declaration describes the consignment, not the purchase order.  With
    "Qty Ordered" printed first, first-match-wins took the ORDER quantity, its
    arithmetic then failed against the money and the page went to the LLM."""
    page = """
| Line | Part No | Description | Qty Ordered | Qty Shipped | U/M | Unit Price | Extended |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | BRK-550 | BRAKE DISC FRONT | 200 | 180 | PCS | 14.00 | 2520.00 |
| 2 | BRK-770 | BRAKE DISC REAR | 150 | 150 | PCS | 11.00 | 1650.00 |
| Total |  |  |  |  |  |  | 4170.00 |
"""
    rows, _ = _rows(page)
    assert [r.quantity_raw for r in rows] == ["180", "150"]


def test_unit_named_once_in_the_quantity_header_reaches_every_row():
    """"| PCS |" as the quantity header states the unit for the whole column.
    It is the document's own word, so it is read — unlike a default, which is
    an assumption and is never made."""
    page = """
| No | Description | HS Code | PCS | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- |
| 1 | GLASS TUMBLER 300ML | 70133700 | 2400 | 0.75 | 1800.00 |
| 2 | GLASS JUG 1.5L | 70139900 | 600 | 2.50 | 1500.00 |
| TOTAL |  |  |  |  | 3300.00 |
"""
    rows, _ = _rows(page)
    assert [r.uom_raw for r in rows] == ["PCS", "PCS"]


def test_no_unit_column_leaves_the_unit_empty():
    """The other half of the same contract: nothing printed, nothing invented."""
    _n, _e, page = LAYOUTS["amount_only_no_unit"]
    rows, _ = _rows(page)
    assert all(r.uom_raw is None for r in rows)


def test_european_unit_spellings_are_units():
    _n, _e, page = LAYOUTS["de_full"]
    rows, _ = _rows(page)
    assert [r.uom_raw for r in rows] == ["STK", "STK"]


def test_carried_forward_balance_is_not_a_goods_row_and_not_a_mismatch():
    """Page 3 of a long invoice: its rows sum to the printed total MINUS what
    the earlier pages carried, and reading that as a broken column map handed a
    correct parse to the LLM."""
    page = """
| Sr | Description | HS Code | Qty | Unit | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| Brought Forward |  |  |  |  |  | 12000.00 |
| 8 | ALUMINIUM WINDOW FRAME | 76109000 | 300 | PCS | 20.00 | 6000.00 |
| 9 | ALUMINIUM DOOR HANDLE | 83024100 | 500 | PCS | 4.00 | 2000.00 |
| Total |  |  |  |  |  | 20000.00 |
"""
    rows, res = _rows(page)
    assert [r.description_raw for r in rows] == ["ALUMINIUM WINDOW FRAME", "ALUMINIUM DOOR HANDLE"]
    assert any("matches the printed invoice total" in n for n in res.notes), res.notes


# --------------------------------------------------------------------------- #
# 3. Refusals — a document the parser cannot read must go to the LLM, not ship
# --------------------------------------------------------------------------- #
def test_line_values_that_miss_the_printed_total_stand_the_parser_down():
    page = """
| SN | Description | HS Code | Qty | Unit | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | WIDGET A | 84879000 | 100 | PCS | 5.00 | 500.00 |
| 2 | WIDGET B | 84879000 | 200 | PCS | 3.00 | 600.00 |
| TOTAL |  |  |  |  |  | 45000.00 |
"""
    res = _parse(page)
    assert res.pages == {}
    assert any("matches none of the printed invoice totals" in n for n in res.notes)


def test_total_qty_column_is_never_read_as_the_money_total():
    """"Total Qty" begins with the same word as the money column.  The old
    lookahead excluded weights but not COUNTS, so the money-total slot landed on
    a piece count — and an amount-only invoice has no arithmetic to catch it."""
    page = """
| S/N | Description | HS Code | CTNS | Qty/CTN | Total Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | SOAP BAR 100G | 34011100 | 100 | 48 | 4800 | PCS | 0.30 | 1440.00 |
| 2 | SHAMPOO 200ML | 33051000 | 60 | 24 | 1440 | PCS | 0.90 | 1296.00 |
| TOTAL |  |  |  |  |  |  |  | 2736.00 |
"""
    rows, _ = _rows(page)
    assert [r.line_total_raw for r in rows] == ["1440.00", "1296.00"]
    assert [r.quantity_raw for r in rows] == ["4800", "1440"]


def test_hs_code_beside_the_unit_column_is_not_a_quantity():
    page = """
| Sr | Description | HS Code | Unit | Qty | Rate | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | LADIES HANDBAG | 42021900000 | PCS | 300 | 12.00 | 3600.00 |
| 2 | TRAVEL LUGGAGE | 42021200000 | PCS | 120 | 30.00 | 3600.00 |
| TOTAL |  |  |  |  |  | 7200.00 |
"""
    rows, _ = _rows(page)
    assert [r.quantity_raw for r in rows] == ["300", "120"]
    assert [r.hs_code_raw for r in rows] == ["42021900000", "42021200000"]


def test_zero_value_line_disowns_its_page_rather_than_confirming():
    """A free-of-charge sample still has to be declared, and the parser has no
    value to declare it at — so the page goes to the LLM whole.  Pinned because
    it looks like a bug from the outside and is the intended contract."""
    page = """
| # | Description | HS Code | Qty | Unit | Unit Price | Amount |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | CERAMIC MUG 350ML | 69120010 | 2000 | PCS | 1.20 | 2400.00 |
| 2 | CERAMIC MUG SAMPLE FOC | 69120010 | 20 | PCS | 0.00 | 0.00 |
| TOTAL |  |  |  |  |  | 2400.00 |
"""
    res = _parse(page)
    assert res.confirmed_row_count() == 0
    assert res.pages[1].suspicious_leftover == 1
