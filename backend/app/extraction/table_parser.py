"""Deterministic table parser — code extracts goods rows, the LLM gets the rest.

Mistral OCR emits markdown tables, so most goods rows are machine-parseable
without any model: a header row (``MODEL NO | DESCRIPTION | QTY | U/M | UNIT
PRICE | TOTAL``) gives the column map, each data line gives the cells, and a
row is CONFIRMED only when it self-verifies — for invoices the arithmetic
``qty x unit price == line total`` must hold (locale-aware), so a confirmed
row cannot be numerically wrong in a way that matters.  One narrow repair is
allowed inside that gate: when the PRINTED quantity contradicts the money
while ``total / price`` is a clean positive integer, the quantity is derived
from the money (and the correction reported in the parser notes) instead of
disowning the whole page to the LLM over one misread digit.

A page is *parser-owned* only when every suspicious line on it (anything
carrying an identity token or a qty|UOM cell pair that is not a banner or a
header) became a confirmed row.  Any leftover — split rows, merged cells,
garbled fragments — sends the WHOLE page to the LLM fallback, so parser and
model never share a page and merging stays trivially ordered.  One class of
leftover is excused instead of disowning: a value-less continuation fragment
(the previous row's batch/COO breakdown printed as its own line, every
quantity/money column empty) can never be a goods row, and handing its page
to the LLM is how a fragment once gained invented values and became a phantom
declaration item.

No header-derived column map in the whole document -> the parser stands down
entirely and the historical LLM path runs unchanged.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ..domain.enums import DeclaredRole
from ..numbers import count_from_number_range, parse_decimal, q2
from ..units import normalize_weight_unit
from .common_models import Evidence, InvoiceLineRaw, PackingRowRaw, RawNumber
from .manifest import (
    _GTIN,
    _PART,
    _PART_DIGIT_LED,
    _QTY_UOM_MERGED,
    _QTY_UOM_NOSPACE,
    _UOM_CELL,
    qty_uom_cell_at,
)

PARSER_EVIDENCE_LABEL = "TABLE_PARSER"
# Parser evidence quotes the source line.  300 chars is plenty to identify a
# table row and keeps a 200-row packing list's stored payload small.
_QUOTE_CHARS = 300

# first-cell labels that mark repeating page furniture, never goods rows.
# Broad across vendors: matching here only SKIPS a line, and a real goods row
# almost never starts its identity cell with one of these words.
_BANNER = re.compile(
    r"^(?:PAGE\b|INVOICE/?CREDIT|INVOICE\s*NO|CUSTOMER\s*P\.?O|P\.?O\.?\s*(?:NO|DATE)|INCOTERMS?|"
    r"ORDER\s*(?:NO|DATE)|TERMS\b|\*{0,2}BILL-?TO|\*{0,2}SHIP-?TO|SOLD-?TO|DELIVER-?TO|"
    r"(?:SUB[- ]?|GRAND[- ]?)?TOTAL\b|AMOUNT\s*(?:DUE|IN\s*WORDS)|BALANCE\b|CARR(?:IED|Y)\s*FORWARD|"
    r"BROUGHT\s*FORWARD|CONTINUED\b|SSCC\b|GTIN$|SALES\s*ORDER|MODEL\s*NO|MATERIAL\s*NUMBER|"
    r"FREIGHT\b|INSURANCE\b|DISCOUNT\b|V\.?A\.?T\b|GST\b|TAX\b|REMARKS?\b|NOTES?\b|"
    r"NET\s*(?:WT|WEIGHT)|GROSS\s*(?:WT|WEIGHT))", re.IGNORECASE)

# Letters no amount of Unicode decomposition will separate — NFKD leaves them
# whole, so they need an explicit mapping before the header patterns (written
# unaccented) can see them.
_FOLD_EXTRA = str.maketrans({
    "Đ": "D", "đ": "d", "Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
    "ı": "i", "İ": "I", "ß": "ss", "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "Þ": "TH", "þ": "th", "Ð": "D", "ð": "d",
})


def fold_header(cell: str) -> str:
    """``"Désignation"`` -> ``"Designation"``, ``"Số lượng"`` -> ``"So luong"``.

    Header synonyms are matched against the FOLDED cell so every pattern below
    can be written in plain ASCII.  Two reasons this is not cosmetic: an OCR
    pass over a scanned European invoice drops or mangles diacritics
    unpredictably (``Quantité`` / ``Quantite`` / ``Quantitè`` are the same
    column), and writing each accented variant into the patterns by hand is how
    a vocabulary silently ends up covering only the spellings someone happened
    to think of.
    """
    folded = unicodedata.normalize("NFKD", (cell or "").translate(_FOLD_EXTRA))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    # Drop whole non-Latin scripts.  Bilingual headers put the local script
    # FIRST — "品名 Description", "الكمية / Quantity", "Наименование / Description"
    # — and every ^-anchored pattern then fails on a cell whose Latin half is
    # perfectly readable.  NFKD does not decompose CJK, Arabic, Cyrillic, Greek,
    # Hebrew or Thai, so they have to be removed explicitly.
    folded = re.sub(r"[^\x00-\x7F]+", " ", folded)
    folded = re.sub(r"\s{2,}", " ", folded).strip()
    # Removing the local-script half leaves its separator behind — "Разом без
    # ПДВ / Total excl. VAT" becomes "/ Total excl. VAT" — and a leading "/"
    # defeats every ^-anchored pattern, so the Latin half stays unreadable for
    # exactly the documents this folding exists to reach.
    return folded.strip(" /|,;:-").strip()


# Column-header synonyms across vendors, industries and languages.  A header row
# must expose at least a quantity AND a description column for the parser to
# build a map; everything else is optional enrichment.  Matched against
# `fold_header(cell)`, so patterns are written WITHOUT diacritics.
#
# Coverage is deliberately broad — English trade spellings plus German, French,
# Spanish, Italian, Dutch, Portuguese, Nordic, Polish, Czech, Hungarian,
# Romanian, Croatian, Slovenian, Turkish, Vietnamese, Indonesian and Malay,
# which is what actually arrives on export invoices.  Each foreign synonym that
# could collide with an English word is anchored (``^EINHEIT$``, ``^IMPORTE$``)
# rather than left to match anywhere in a cell.
_HDR_KEYS = {
    # NOTE: no bare "NO"/"NOS" — it false-matches "Box No.", "Order No.", "Sr
    # No.", etc.; a real quantity header is Qty/Quantity/Qnty/Pcs/Pieces.
    # "Q'TY"/"Q'ty" is the standard abbreviation on Japanese and Korean export
    # invoices and the apostrophe defeated a bare \bQTY.
    "qty": re.compile(r"\bQ'?TY|QUANTITY|QNTY|QUANT\b|\bPCS\b|PIECES|"
                      r"NO\.?\s*OF\s*(?:PCS|PIECES|UNITS)|"
                      # de / fr / es / it / nl / pt / tr / vn / id
                      r"^MENGE|^ANZAHL|^STUCKZAHL|QUANTITE|CANTIDAD|QUANTITA|"
                      r"^AANTAL(?!\s*(?:COLL?I|DOZEN|PALLET|KARTON))|HOEVEELHEID|QUANTIDADE|^MIKTAR|^SO\s*LUONG|"
                      r"^JUMLAH$|KUANTITAS|"
                      # nordic / cee
                      # "Antal kolli" / "Aantal colli" counts PACKAGES, not goods:
                      # borrowing it as the declared quantity turns 29 cartons
                      # into 29 pieces of machine-tool spares
                      r"^ANTALL?\b(?!\s*(?:KOLL?I|KARTON|PALL|COLL?I))|^MAARA|^ILOSC|"
                      r"^MNOZSTVI|MENNYISEG|CANTITATE|"
                      r"^KOLICINA\b|^Q\.?\s*TA\.?$|^CANT\.?$|^KOL\.?$|^QTE\.?$|"
                      # service-line tables bill HOURS or UNITS, and their
                      # quantity column is named accordingly.  "Units" is
                      # evaluated before the UOM key, so a goods table printing
                      # "Quantity | Units" still gives the unit column to `uom`.
                      r"^HOURS?$|^HRS?\.?$|^UNITS?$", re.I),
    "uom": re.compile(r"^U/?[OM]?M$|^UOM$|^UNIT$|^UNITS$|\bU\.?O\.?M\b|MEASURE|"
                      r"^EINHEIT|^STK$|^UNITE$|^UNIDAD$|^UNITA$|^EENHEID$|^UNIDADE$|"
                      r"^BIRIM$|^DON\s*VI$|^SATUAN$|^ENHET$|^ENHED$|^YKS\.?$|"
                      r"^J\.?\s*M\.?$|^MJ$|^ME\.?$|^UM$|^EM$|^MED\.?$|^JED\.?$|"
                      r"^BASIS$|^M\.?\s*E\.?$|^U$|^UD$|^UN$", re.I),
    "price": re.compile(r"UNIT\s*(?:PRICE|COST|RATE)|U\.?\s*PRICE|^PRICE|^RATE|^COST|"
                        r"RATE\s*PER|PRICE\s*(?:EACH|PER|/)|^P\s*/\s*U$|"
                        r"EINZELPREIS|STUCKPREIS|^PREIS|PRIX\s*UNITAIRE|^PRIX|"
                        r"PRECIO\s*UNITARIO|^PRECIO|PREZZO\s*UNITARIO|^PREZZO|"
                        r"PRIJS\s*PER|^PRIJS|PRECO\s*UNITARIO|^PRECO|"
                        r"BIRIM\s*FIYAT|^FIYAT|^DON\s*GIA|HARGA\s*SATUAN|"
                        r"^A-?PRIS|ENHETSPRIS|^STK\.?\s*PRIS|YKSIKKOHINTA|"
                        r"CENA\s*(?:JEDN|NETTO|ZA)|^CENA\b|EGYSEGAR|PRET\s*UNITAR|"
                        r"CIJENA|VALOR\s*UNIT|PRECO\s*UNIT|^P\.?\s*U\.?\s*H\.?\s*T|"
                        # "Standardkosten" is a cost BASIS printed beside the
                        # billed "Verrechnungspreis" on an intercompany invoice
                        # — taking it as the rate broke the row's arithmetic
                        r"UNIT\s*CHARGE|^CHARGE\b|PER\s*HOUR|^TARIF\b|"
                        r"VERRECHNUNGSPREIS", re.I),
    # money total — the negative lookahead keeps it off "TOTAL WEIGHT" /
    # "TOTAL G.W." columns, which are weights that happen to start with the
    # same word (they belong to wt_any / gross_wt / net_wt).
    # money total — the negative lookahead keeps it off columns that merely
    # START with the same word.  Weights were excluded from the first day;
    # COUNTS were not, so "Total Qty" / "Total Pcs" / "Total Ctns" mapped the
    # money-total slot onto a QUANTITY column and the row's declared value
    # became a piece count.  Only the arithmetic gate stood between that and a
    # shipped declaration — and an amount-only invoice has no arithmetic gate.
    "total": re.compile(r"^TOTAL(?!\s*(?:WEIGHT|WT\b|WGT|MASS|G\.?\s*W|N\.?\s*W|"
                        # "TOTAL NET WT (KGS)" is a WEIGHT column carrying a
                        # TOTAL prefix, not a money total
                        r"NET{1,2}O?\s*(?:WT|WEIGHT|WGT)|GROSS\s*(?:WT|WEIGHT|WGT)|"
                        r"QTY|QUANT|Q'?TY|PCS|PIECES|NOS?\b|CTNS?|CARTONS?|CASES?|"
                        r"BOXES|PACKAGES?|PKGS?|UNITS?\b|ITEMS?\b|LINES?\b))|"
                        r"LINE\s*TOTAL|EXT(?:ENDED|\.)?\s*(?:PRICE|VALUE|AMOUNT)|"
                        r"^AMOUNT|^AMT\b|^VALUE|^GROSS\s*(?:AMOUNT|VALUE)|"
                        r"NET\s*AMOUNT|LINE\s*(?:AMOUNT|VALUE)|"
                        r"^(?:ITEM\s*)?SUB[\s-]*TOTAL|"
                        # "FOB Value", "CIF Amount" — an incoterm-qualified
                        # money column is the line value on an export invoice
                        r"\b(?:FOB|CIF|CFR|CNF|EXW|DDP|DAP|FCA|CIP|CPT|FAS)\s*"
                        r"(?:VALUE|AMOUNT|PRICE|TOTAL)\b|"
                        # de / fr / es / it / nl / pt / tr / vn / id
                        r"^BETRAG|GESAMTPREIS|GESAMTBETRAG|^GESAMT|^MONTANT|"
                        r"^IMPORTE$|^IMPORTO$|^BEDRAG|^VALOR|^TUTAR|"
                        # "Jumlah (USD)" is the amount; bare "Jumlah" is the
                        # quantity and "Jumlah Koli" the package count — the
                        # parenthesised currency is what tells them apart
                        r"^THANH\s*TIEN|TOTAL\s*HARGA|^JUMLAH\s*\(|"
                        # nordic / cee
                        r"^BELOPP|\bBELOP\b|^BELOB|^YHTEENSA|^VEROTON|WARTOSC|"
                        r"NETTOBELOP|^CELKEM|^ERTEK|VALOARE|^IZNOS|^VREDNOST", re.I),
    # the assessable / taxable value column — the figure the invoice's own
    # grand total sums and the customs-declared goods value; preferred over the
    # plain qty x rate "amount" when both are printed.
    "taxable": re.compile(r"TAXABLE|ASSESS", re.I),
    # "PARTICULARS?" — the SINGULAR "PARTICULAR" is the standard description
    # header on Nepali/Indian trade invoices, and requiring the plural is what
    # made a fully-readable header unmappable (2026-08-03 live job: every other
    # column matched, `desc` missed, the whole header was rejected and the
    # parser fell back to another vendor's remembered layout — HS code read as
    # the quantity, rate read as the line total).
    #
    # "COMMODITY CODE" / "GOODS CODE" is the TARIFF column, not the description
    # — the negative lookaheads keep a document whose only commodity-ish header
    # is its HS column from having that column declared as the goods text.
    "desc": re.compile(r"DESCRI|DESCR\b|PARTICULARS?|COMMODITY(?!\s*CODE)|GOODS(?!\s*CODE)|"
                       r"(?:PRODUCT|ITEM|ARTICLE|MATERIAL|CHEMICAL|TRADE)\s*(?:NAME|DESC)|"
                       r"NATURE\s*OF\s*GOODS|MERCHANDISE|NOMENCLATURE|DESIGNATION|"
                       # de / fr / es / it / nl / pt / tr / vn / id
                       r"BEZEICHNUNG|BESCHREIBUNG|LIBELLE|MARCHANDISE|MERCANCIA|"
                       r"^ARTICULO|OMSCHRIJVING|BENAMING|ACIKLAMA|\bCINSI\b|"
                       r"BESKRIVELSE|BESKRIVNING|"
                       r"^MO\s*TA|TEN\s*HANG|HANG\s*HOA|URAIAN|NAMA\s*BARANG|"
                       # nordic / cee / service tables
                       r"BENAMNING|NIMIKE|^TUOTE|MEGNEVEZES|DENUMIRE|^NAZIV|"
                       r"PRODUSELOR|\bTOWARU\b|\bZBOZI\b|\bBLAGA\b|\bROBE\b|"
                       r"ARTIKELNAME|VARENAVN|VAREBESKRIV|ARTICLES?\s+OR\s+SERVICES?|"
                       r"SERVICES?\s+RENDERED|^PROFESSIONAL\s+SERVICES", re.I),
    # "Item No/Number" is a line number, not a product code — only "Item Code"
    # (and Part/SKU/Article/Catalog/…) identify the product.  Broadened across
    # vendors (2026-07-21) for the brand/model/size export: Model/Part/P·N/SKU/
    # Item Code/Catalogue/Article/Style/Drawing/Pattern/Series/OEM/Stock Code/
    # Spec Code/Type No/Design/MFG·Manufacturer Part.  Deliberately NOT
    # "Commodity/Goods Code" (that is the HS/tariff column) nor bare "Item No"
    # (that is the line counter).
    "model": re.compile(r"MODEL|PRODUCT\b|CFN|MATERIAL\s*(?:NUMBER|CODE)|^MATERIAL$|\bPART\b|"
                        r"\bSKU\b|ARTICLE|ITEM\s*CODE|CAT(?:ALOG(?:UE)?)?\s*(?:NO|#)|\bREF\b|"
                        r"\bP/?N\b|\bOEM\b|STYLE|DRAWING|PATTERN|SERIES|STOCK\s*(?:CODE|NO)|"
                        r"SPEC\s*CODE|TYPE\s*NO|DESIGN\s*(?:NO|CODE|NUMBER)|"
                        r"MFG\s*(?:PART|MODEL)|MANUFACTURER\s*(?:PART|MODEL|CODE|NUMBER)|"
                        r"ARTIKEL\s*(?:NR|NUMMER|CODE)|REFERENCE\s*(?:NO|CODE)|"
                        r"ARTIKELNR|VARENR|TUOTENRO|^KOD\b", re.I),
    # per-row brand / trademark / manufacturer (export-only).  "Manufacturer"
    # alone is a brand; "Manufacturer Part/Model/…" is a code and belongs to
    # `model` (negative lookahead keeps them apart).  "Made in" is COO, not brand.
    "brand": re.compile(r"BRAND|TRADE\s*MARK|TRADEMARK|TRADE\s*NAME|\bMAKE\b|\bMAKER\b|\bVENDOR\b|"
                        r"PRODUCER|ORIGIN\s*BRAND|FACTORY\s*BRAND|"
                        r"MANUFACTUR(?:ED\s*BY|ER)(?!\s*(?:PART|MODEL|CODE|NUMBER|NO))", re.I),
    # per-row size / dimension / capacity (export-only).  A single size column;
    # bare Weight and separate Length/Width/Height columns are left to the
    # description parser to avoid colliding with shipping-weight columns.
    "size": re.compile(r"\bSIZE\b|DIMENSION|MEASUREMENT|DIAMETER|CAPACITY|VOLUME|"
                       r"PACKING\s*SIZE|PRODUCT\s*SIZE|SIZE\s*CODE|SPECIFICATION", re.I),
    # "S.N"/"SN" must be ANCHORED: unanchored `S\.?N\b` matches inside "HSN
    # code", stealing the line_no slot (2026-07-19 incident — wrong column map
    # made the parser stand down and the last invoice page was lost).
    "line_no": re.compile(r"ITEM\s*(?:NUMBER|NO|#)|LINE\b|SERIAL\s*(?:NUMBER|NO|#)?|"
                          r"^S\.?\s*NO|^SL\b|^SR\b|^S\.?\s*N\b|^#$|^NO\.?$|^S/N$|"
                          # de / nl / fr-es-it / tr / vn / cee
                          r"^POS\.?$|^NR\.?$|^N[°º]?\.?$|^SIRA$|^STT$|^LP\.?$|"
                          r"^POR\.?$|^RIVI$|^LINJE$|^A/A$", re.I),
    # "COMMODITY CODE" and "CN CODE" are what EU/UK invoices call the tariff
    # column; without them the HS the document PRINTS was dropped and the
    # resolver fell back to guessing one from the description text.
    "hs": re.compile(r"\bH\.?S\.?\s*(?:CODE|NO)?|\bHSN\b|\bHTS\b|TARIFF\s*(?:CODE|NO)?|"
                     r"CUSTOMS\s*CODE|COMMODITY\s*CODE|GOODS\s*CODE|\bTARIC\b|\bCN\s*CODE\b|"
                     # fr / es / it / nl / br / tr / vn / id / cee
                     r"CODE\s*SH|CODIGO\s*SA|CODICE\s*HS|\bGN-?\s*CODE\b|^NCM$|^GTIP$|"
                     r"^MA\s*HS$|KODE\s*HS|\bKOD\s*CN\b|VAMTARIFA|TARIFNI|"
                     r"TARIFNA\s*OZNAKA|\bGTIP\b|\bNCM\b|\bHTSUS\b|ARANCELARI|"
                     r"VOCE\s*DOGANALE|\bSH\b|POSICION\s*ARANCEL|\bVTSZ\b|"
                     r"\bCODE?\s*NC\b|\bHSK\b|TULLINIMIKE|STATISTISK|TOLDTARIF|"
                     r"TULLTARIFF|VARENUMMER\s*HS", re.I),
    "coo": re.compile(r"COUNTRY\s*OF\s*ORIGIN|\bC\.?O\.?O\b|\bORIGIN\b|MADE\s*IN", re.I),
    # ---- packing-list columns (spec section 4, Condition 1 / Condition 2) --- #
    # A packing list's whole purpose is the per-row weight and carton columns.
    # They are mapped by HEADER ONLY — never positionally — because reading a
    # net-weight column as gross is a silent, undetectable declaration error.
    # "G.W.", "G/W", "G.W. (KGS)", "TOTAL G.W. (KGS)", "GROSS WT" — the unit
    # may or may not be bracketed, a TOTAL prefix is routine, and the slash
    # form is ubiquitous.  The anchored alternative used to reject both the
    # TOTAL prefix and the slash, so packing lists using those headers had
    # every printed per-item weight silently discarded.
    # Localized weight vocabulary.  A packing list has NO per-row arithmetic to
    # fall back on, so a weight header the map cannot read is a weight the
    # declaration simply loses — and the whole non-English half of the corpus
    # was losing every one.  Two false friends worth naming: Italian "lordo"
    # means GROSS, and Portuguese/Spanish "líquido" means NET.
    "gross_wt": re.compile(r"GROSS\s*(?:WT|WEIGHT|WGT)|BRUT(?:TO|O)?\b|BRUT{1,2}O?GEWICHT|"
                           r"PESO\s*LORDO|\bLORDO\b|POIDS\s*BRUT|BRUTTOGEWICHT|"
                           r"BRUTTOVIKT|BRUTTOVAEGT|BRUTTOVEKT|HRUBA\s*HMOTNOST|"
                           r"WAGA\s*BRUTTO|BRUTTO\s*(?:SULY|TOMEG)|GREUTATE\s*BRUTA|"
                           r"BRUTO\s*(?:TEZINA|MASA)|KOKONAISPAINO|"
                           r"^(?:TOTAL|TTL)?\s*G\s*[./-]?\s*W\b\.?\s*"
                           r"(?:[\(\[][^\)\]]*[\)\]]|[A-Za-z]{1,6}\.?)?\s*$", re.I),
    "net_wt": re.compile(r"NET{1,2}\s*(?:WT|WEIGHT|WGT)|NET{1,2}O\b|NET{1,2}O?GEWICHT|"
                         r"PESO\s*NET{1,2}O|POIDS\s*NET\b|NETTOGEWICHT|"
                         r"NETTOVIKT|NETTOVAEGT|NETTOVEKT|CISTA\s*HMOTNOST|"
                         r"WAGA\s*NET{1,2}O|NET{1,2}O\s*(?:SULY|TOMEG)|"
                         r"GREUTATE\s*NETA|NETO\s*(?:TEZINA|MASA)|NETTOPAINO|"
                         r"NET\s*AGIRLIK|"
                         r"PESO\s*LIQUIDO|\bLIQUIDO\b|"
                         r"^(?:TOTAL|TTL)?\s*N\s*[./-]?\s*W\b\.?\s*"
                         r"(?:[\(\[][^\)\]]*[\)\]]|[A-Za-z]{1,6}\.?)?\s*$", re.I),
    # a single unlabelled weight column ("WEIGHT (KG)", "Total Weight", "Item
    # Weight") — neither gross nor net; downstream classifies it against the
    # document's own printed totals.  "UNIT WEIGHT" is deliberately NOT here:
    # that is a per-unit rate, and mapping it declares one piece's weight as
    # the row's total.
    "wt_any": re.compile(r"^\s*(?:TOTAL|TTL|ITEM)?\s*(?:WEIGHT|WT\.?|WGT|MASS)\b", re.I),
    # HOW MANY cartons ("CTNS", "No. of packages") …
    # The package word is not always "carton": bales, chests, drums, bundles and
    # bags are the package basis for whole industries, and every European
    # language has its own ("colis", "colli", "bultos", "kolli", "Packstücke").
    # An unmapped package column loses the declaration's package COUNT.
    "ctn": re.compile(r"(?:NO\.?\s*OF\s*|ANZAHL\s*|NO\.?\s*)?"
                      r"(?:CTNS?|CARTONS?|CASES?|PKGS?|PACKAGES?|BOXES|PACKS?|PALLETS?|"
                      r"BALES?|CHESTS?|DRUMS?|BUNDLES?|BAGS?|ROLLS?|COILS?|CRATES?|SACKS?)\b"
                      r"|(?:TOTAL|QTY)\s*(?:CTNS?|CARTONS?)"
                      # de / fr / it-nl / es / se-dk-no / tr / pl-cz / pt
                      r"|\bKOLL?I\b|\bKOLLO\b|PACKSTUCKE|\bCOLIS\b|\bCOLLI\b|\bBULTOS?\b|"
                      r"\bKOLI\b|KARTONY|KARTONOK|\bVOLUMES?\b|\bCAJAS?\b|\bFARDOS?\b", re.I),
    # … versus WHICH carton ("C/NO", "Carton No", "Case No") — an identifier
    "ctn_no": re.compile(r"(?:C(?:TN|ARTON)?|CASE|BOX|PALLET|PKG|PACKAGE)\s*[./-]?\s*"
                         r"(?:NO|NOS|NUMBER|#)\b|\bC\s*/\s*NO\b|"
                         r"MARKS?\s*(?:&|AND)\s*(?:NOS?|NUMBERS?)|(?:KOLL?I|KOLLO|COLL?I|COLIS|BULTO|KARTON|BALE|CHEST|DRUM|BUNDLE)\s*(?:NR|NO|N°|NUM)\b", re.I),
    "pkg_type": re.compile(r"PACK(?:AGE|ING)?\s*TYPE|TYPE\s*OF\s*PACK|KIND\s*OF\s*PACK", re.I),
    "batch": re.compile(r"BATCH|\bLOT\b", re.I),
    "expiry": re.compile(r"EXPIR|\bEXP\.?\s*(?:DATE|DT)\b|^EXP\.?$|USE\s*BY|BEST\s*BEFORE", re.I),
}

# Column keys whose value is a mass and may carry a unit in the header cell.
_WEIGHT_KEYS = ("gross_wt", "net_wt", "wt_any")
# A header cell's unit hint: "GROSS WT (KGS)", "NET WEIGHT [KG]", "WT IN KGS".
_HDR_UNIT = re.compile(r"[\(\[]\s*([A-Za-z][A-Za-z/.]{0,7})\s*[\)\]]|\bIN\s+([A-Za-z]{1,8})\b", re.I)
# "TOTAL", "GRAND TOTAL:", "TOTAL GROSS WEIGHT" — a summary line, never a row.
# Tight on purpose: a goods description that merely contains the word "total"
# must not be swallowed, so the cell has to be essentially just the label.
_TOTAL_WORD = (r"TOTALS?|TOTAAL|TOTALE|TOTAUX|TOTALT|SUBTOTAL|SOMME|SUMME|SUMA|"
               r"GESAMT(?:BETRAG|PREIS|SUMME|MENGE)?|ENDBETRAG|NETTOBETRAG|"
               r"TOPLAM|GENEL\s*TOPLAM|TONG\s*(?:CONG|SO)|JUMLAH|IMPORTE\s*TOTAL|"
               r"I\s*ALT|SAMMENLAGT|RAZEM|CELKEM|OSSZESEN|UKUPNO|SKUPAJ|"
               r"LOPPUSUMMA|YHTEENSA|VEROTON")
_TOTALS_CELL = re.compile(
    rf"^(?:GRAND\s+|SUB[\s-]*|NET\s+)?(?:{_TOTAL_WORD})\b[\s:.\-]*"
    r"(?:(?:GROSS|NET{1,2}O?|G\.?\s*W\.?|N\.?\s*W\.?|WEIGHT|WT|CTNS?|CARTONS?|PACKAGES?|PKGS?|"
    r"BOXES|QTY|QUANTITY|PCS|PIECES|AMOUNT|VALUE|HARGA|BARANG|"
    r"EXCL(?:UDING|\.)?|INCL(?:UDING|\.)?|EXKL|BEFORE|AFTER|OF|DUE|"
    r"V\.?A\.?T|TAX|GST|DPH|DDV|PDV|TVA|IVA|MWST|MOMS|MVA|ALV|AFA|DDS|BTW|"
    r"[A-Z]{3}\b)[\s:.\-]*){0,4}$", re.I)
# The same label with ANY trailing text — used only where a false positive is
# harmless (see `_is_totals_line`).
_TOTALS_OPENER = re.compile(rf"^(?:GRAND\s+|SUB[\s-]*|NET\s+)?(?:{_TOTAL_WORD})\b", re.I)
# Labelled totals printed as free text rather than in a totals ROW.
_INLINE_TOTALS = {
    "gross_wt": re.compile(r"(?:TOTAL\s*)?(?:GROSS\s*(?:WEIGHT|WT|WGT)|G\.?\s*W\.?)\s*"
                           r"(?:TOTAL)?\s*[:=]?\s*([\d][\d.,]*)\s*([A-Za-z]{1,5})?", re.I),
    "net_wt": re.compile(r"(?:TOTAL\s*)?(?:NET{1,2}\s*(?:WEIGHT|WT|WGT)|N\.?\s*W\.?)\s*"
                         r"(?:TOTAL)?\s*[:=]?\s*([\d][\d.,]*)\s*([A-Za-z]{1,5})?", re.I),
    "ctn": re.compile(r"TOTAL\s*(?:NO\.?\s*OF\s*)?(?:CTNS?|CARTONS?|PACKAGES?|PKGS?|CASES?|BOXES)"
                      r"\s*[:=]?\s*([\d][\d.,]*)", re.I),
}


def _header_unit(cell: str) -> str | None:
    """The mass unit a weight-column HEADER states, or None.

    Bracketed or "IN …" forms are explicit and win, because they can safely
    carry a one-letter unit: ``WEIGHT (G)`` really is grams.  A bare trailing
    unit (``G.W. KGS``) is accepted only from two letters up — the ``G`` in
    ``G.W.`` is the word "gross", and reading it as grams would divide every
    weight in the column by a thousand.
    """
    m = _HDR_UNIT.search(cell or "")
    if m:
        token = m.group(1) or m.group(2)
        if token and normalize_weight_unit(token):
            return token
    for token in reversed(re.findall(r"[A-Za-z]+", cell or "")):
        if len(token) > 1 and normalize_weight_unit(token):
            return token
    return None


def _is_totals_line(cells: list[str], mapping: dict | None = None) -> bool:
    """A totals / subtotal / grand-total line — summary, never a goods row.

    Three readings, in falling order of certainty.

    1. The tight one (`_TOTALS_CELL`: a label and nothing but a label) may fire
       on ANY cell, because no goods description is exactly "Total".
    2. Label-then-anything, when the row prints nothing in its first cell —
       what a summary row looks like and a numbered goods row never does.
    3. Label-then-anything in the FIRST cell, when the row is also missing one
       of the things every goods row has (a quantity, a rate, a description).
       Needed because a totals label carries free trailing text no enumeration
       can keep up with — "TOTAL FOB SHENZHEN", "I alt ekskl. moms", "TOTAL
       DECLARED VALUE FOR CUSTOMS" — and each of those was being confirmed as
       an extra goods row.  The missing-column test is what protects a genuine
       product called "TOTAL STATION THEODOLITE", which prints all three.
    """
    folded = [fold_header(c) for c in cells]
    if any(_TOTALS_CELL.match(f) for f in folded if f.strip()):
        return True
    if not any(_TOTALS_OPENER.match(f) for f in folded if f.strip()):
        return False
    if cells and not (cells[0] or "").strip():
        return True
    if mapping and folded and _TOTALS_OPENER.match(folded[0]):
        def blank(key):
            i = mapping.get(key)
            return (not isinstance(i, int) or not (0 <= i < len(cells))
                    or not (cells[i] or "").strip())
        if any(blank(k) for k in ("qty", "price", "desc")):
            return True
    return False


def _split_value_unit(cell: str) -> tuple[str | None, str | None]:
    """``"12.5 KG"`` -> ``("12.5", "KG")``; ``"12.5"`` -> ``("12.5", None)``.

    Also accepts a leading unit (``"KG 12.5"``).  Anything else returns
    ``(None, None)`` — a cell we cannot read is never guessed at.
    """
    text = (cell or "").strip()
    # The number may be SPACE-GROUPED ("1 240,00" — Nordic, French, Russian).
    # Requiring a space-free run silently returned "no weight" for every row of
    # those documents, and a packing list has no arithmetic to notice the loss.
    # Groups must be exactly three digits, so "25 EA" still splits into value
    # and unit rather than being read as one number.
    num = r"\d{1,3}(?:[  ]\d{3})+(?:[.,]\d+)?|[\d][\d.,]*"
    m = re.fullmatch(rf"({num})\s*([A-Za-z]{{1,8}}\.?)?", text)
    if m:
        return m.group(1), (m.group(2) or None)
    m = re.fullmatch(rf"([A-Za-z]{{1,8}}\.?)\s*({num})", text)
    if m:
        return m.group(2), m.group(1)
    return None, None


# A carton NUMBER is not a carton COUNT — the shared helper that both the
# parser and the allocator's shared-carton grouping read.
carton_count_from_no = count_from_number_range

# per-row extras some vendors print inside the material/identity cell — free
# deterministic wins (feed the HS resolver / COO rules without any LLM).
# Labels are broad; a bare code with no label is left to the LLM/HS resolver.
_TARIFF_IN_CELL = re.compile(
    r"(?:H\.?S\.?\s*(?:CODE|N)?|HSN|HTS|Tariff(?:\s*Code)?|Customs\s*Code)\s*[:.]?\s*(\d[\d.]{5,11}\d)",
    re.I)
_COO_IN_CELL = re.compile(
    r"(?:Country\s*of\s*Origin\b|C\.?O\.?O\b|Origin\b|Made\s*in\b)\s*[:.]?\s*"
    # a 2-letter code, or a country NAME ("COO: Ireland") — the live Medtronic
    # failure was exactly this: the label printed a full name, the 2-letter-only
    # capture never fired, and the silent exporter fallback declared Singapore
    # for Irish goods.  Names are captured verbatim (trailing words and all) and
    # normalize-gated downstream (rules.coo / field_allocation), so junk dies
    # there instead of being invented here.
    r"([A-Za-z]{2}\b(?![.'\-])|[A-Za-z][A-Za-z .'\-]{2,39})", re.I)
# A unit-less quantity cell: digits and grouping punctuation only.  Deliberately
# shape-agnostic — a quantity is printed "1.850,00" in Argentina, "2 500" in
# Norway and "4,800" in Egypt, and enumerating those shapes here just moves the
# locale question to the wrong layer.  Anything this admits still has to survive
# `parse_decimal` (which rejects a merged "35,00 700,00" outright), so the loose
# pattern cannot turn two numbers into one.
_BARE_QTY = re.compile(r"\d[\d  .,]*")


def _printed_unit(cell: str) -> bool:
    """A UOM cell the vocabulary does not know but the document clearly prints
    as a unit — "PER CONTAINER", "CNTR", "10 x 10 Blister", "lev".

    20 chars, not 12: freight and pharma print their basis as the unit, and
    those are the document's own words.  Never a default and never invented —
    only ever the text sitting in a header-mapped UOM column.
    """
    text = (cell or "").strip()
    if not text or len(text) > 20:
        return False
    # NOT restricted to ASCII: the unit is printed in the document's own script
    # ("м3", "م2 / SQM", "CÁI/PCS", "doos / boîte"), and rejecting those left
    # the field empty on every non-Latin invoice.  It must simply not START
    # with a digit — that would be a quantity, not a unit.
    if text[0].isdigit():
        return False
    return bool(re.fullmatch(r"[^\d][^|]{0,19}", text)) and any(ch.isalpha() for ch in text)


@dataclass
class PageParse:
    page_no: int
    rows: list = field(default_factory=list)     # confirmed typed raw rows
    suspicious_leftover: int = 0                 # goods-looking lines not confirmed
    confirmed: bool = False                      # parser owns this page


@dataclass
class ParseResult:
    pages: dict[int, PageParse] = field(default_factory=dict)   # {} = parser stood down
    mapping: dict | None = None                  # active column map (for layout memory)
    header_signature: str | None = None          # normalized header cells, if a header was seen
    from_memory: bool = False                    # parsed via a remembered layout
    # packing lists: the document's OWN printed totals, {key: (value_raw, unit_raw)}
    printed_totals: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)              # audit, shown to the reviewer

    def confirmed_row_count(self) -> int:
        return sum(len(pp.rows) for pp in self.pages.values() if pp.confirmed)


# a remembered layout must prove itself on this many arithmetic-verified rows
# before the parser trusts a headerless document to it
MIN_MEMORY_ROWS = 3


def _cells(line: str) -> list[str]:
    return [c.strip().strip("*").strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r"-{2,}|", c) for c in cells)


# A cell that is just a number — a header never carries one, a data row almost
# always does.  The gate that stops a goods row from being mistaken for a
# header: "| 6-10 | LEATHER GOODS | 50 | CTNS | 30.000 | 33.000 |" matched
# `desc` (the word GOODS) and `ctn` (CTNS), replaced the working column map
# mid-page, and silently deleted itself from a page the parser still owned.
_DATA_CELL = re.compile(r"\d[\d.,]*")
# A header naming a PER-UNIT rate ("UNITS PER CARTON", "PCS/CTN", "N.W./CTN",
# "KG/PC") is a rate column: mapping it to qty/ctn/weight declares a per-carton
# figure as the row's total.  Such a column is skipped for those keys entirely.
#
# Two shapes, because the qty key needs a NARROWER one: "Qty./ Unit" is a
# routine combined quantity+UOM header, and guarding qty against "/UNIT"
# unmapped it — which unmapped the whole page.  A quantity column is only a
# rate when the denominator is a PACKAGE ("PCS/CTN"); a weight or carton
# column is a rate against any unit at all ("KG/PC", "N.W./CTN").
_PER_UNIT_HDR = re.compile(
    r"(?:\bPER\s+(?:CTN|CARTON|CASE|BOX|PC|PCS|PIECE|UNIT|PACK|PKG|BAG|PALLET|DZN?|DOZEN)\b"
    r"|/\s*(?:CTN|CARTON|CASE|BOX|PC|PCS|PIECE|UNIT|EA|PACK|PKG|BAG|PALLET"
    r"|KARTON|COLIS|COLLO|CAJA|BULTO|KOLI|KOLLO|STUCK|PZ)\b"
    # "hmotnost NA karton", "Gewicht PRO Karton", "poids PAR colis", "peso POR caja"
    r"|\b(?:NA|PRO|PAR|POR|JE)\s+(?:KARTON|KOLLO|COLIS|COLLO|CAJA|BULTO|STUCK|KUS|PZ)\b"
    # "Poids brut UNITAIRE" beside "Poids brut TOTAL" — the per-unit adjective
    # carries the whole meaning and there is no "/" or "PER" to key on
    r"|\bUNITAIR(?:E|IO)\b|\bUNITARI[AO]\b|EINZELGEWICHT|\bUNIT\s*(?:WT|WEIGHT)\b)", re.I)
_PER_PKG_HDR = re.compile(
    r"(?:\bPER\s+(?:CTN|CARTON|CASE|BOX|PACK|PKG|BAG|PALLET|DZN?|DOZEN)\b"
    r"|/\s*(?:CTN|CARTON|CASE|BOX|PACK|PKG|BAG|PALLET)\b)", re.I)
# keys whose value must be the ROW's own figure, never a per-unit rate
_RATE_SENSITIVE = ("ctn", "gross_wt", "net_wt", "wt_any")
# "QTY CTN", "QTY/CTN", "PCS PER CARTON" — quantity PER package.  It is neither
# the row's quantity nor its package count, and it sits between the two on the
# page so first-match-wins hands it to whichever key is tested first.
_QTY_PER_PKG_HDR = re.compile(
    r"(?i)\b(?:QTY|QUANTITY|PCS|PIECES|NOS|PRS|PAIRS|UNITS)\s*"
    r"(?:/|PER\s+|\s)\s*(?:CTNS?|CARTONS?|CASES?|BOXES|BOX|PACKS?|PKGS?|BAGS?|PALLETS?)\b")
# A money header naming the PRE-TAX figure — the customs value — and one naming
# the tax-INCLUSIVE figure.  Both are routinely printed side by side.
_NET_MONEY_HDR = re.compile(
    r"(?i)\b(?:NET(?:TO|T)?|EXCL(?:UDING|\.)?|EXKL|EKS\.?|BEZ|VEROTON|HT|"
    r"FOB|CIF|CFR|CNF|EXW|FARA\s*TVA|PRE-?TAX)\b")
_GROSS_MONEY_HDR = re.compile(
    r"(?i)\b(?:GROSS|BRUTTO|BRUT|INCL(?:UDING|\.)?|INKL|TTC|CON\s*IVA|"
    r"MED\s*MOMS|INCL\.?\s*(?:VAT|BTW|MWST))\b")
# A price header naming the LIST/pre-discount figure: the row is billed at the
# discounted one, so this is not the rate that multiplies the quantity.
_DISCOUNTABLE_HDR = re.compile(
    r"(?i)\b(?:LIST|GROSS|BRUT(?:TO)?|CATALOGUE|CATALOG|RRP|MSRP|"
    r"BEFORE\s*DISCOUNT|PRE-?DISCOUNT)\b")
_BILINGUAL_SLASH = re.compile(r"\S\s+/\s+\S")
# An invoice may print BOTH what was ordered and what actually shipped.  The
# shipped figure is the one the consignment contains.
_ORDERED_QTY_HDR = re.compile(r"(?i)\b(?:ORDER(?:ED)?|BESTELLT|COMMANDE|PEDIDO)\b")
_SHIPPED_QTY_HDR = re.compile(
    r"(?i)\b(?:SHIP(?:PED)?|DELIVER(?:ED|Y)?|DISPATCH(?:ED)?|SUPPLIED|"
    r"GELIEFERT|LIVRE|ENVIADO)\b")


def _header_map(cells: list[str], need_qty: bool = True) -> dict | None:
    """Column map from a header row, or None when this is not a header.

    ``need_qty=False`` (packing lists) accepts a header that maps a description
    plus any of quantity / cartons / gross / net / weight: real packing lists
    routinely print no quantity column at all, and requiring one is what kept
    the parser from ever owning a packing page.
    """
    # A row carrying DATA can never be a header, however many header words its
    # cells happen to contain.
    if any(_DATA_CELL.fullmatch((c or "").strip()) for c in cells):
        return None
    if qty_uom_cell_at(cells) is not None:
        return None
    mapping: dict = {}
    # accents folded once: every pattern in _HDR_KEYS is written in plain ASCII
    folded = [fold_header(c) for c in cells]
    # Bilingual halves.  When BOTH languages use the Latin alphabet, folding
    # drops nothing and the cell stays "U. Med. / Unit" — where every ^-anchored
    # pattern misses the half it could have read.  Split only on a SPACED slash,
    # which is the bilingual signature: "Qty./ Unit" and "HSN/SAC" are single
    # headers and keep their existing single-cell reading.  The RAW cell is
    # split, not the folded one: folding drops the bare "/" as a token carrying
    # no letters, which erased the very separator this needs.
    halves = [[fold_header(h) for h in re.split(r"\s+/\s+", c or "") if h.strip()]
              if _BILINGUAL_SLASH.search(c or "") else [] for c in cells]
    for i, c in enumerate(folded):
        for key, pat in _HDR_KEYS.items():
            if c and ((key in _RATE_SENSITIVE and _PER_UNIT_HDR.search(c))
                      or (key == "qty" and _PER_PKG_HDR.search(c))):
                continue
            if key in mapping or not c:
                continue
            if pat.search(c) or any(pat.search(h) for h in halves[i]):
                mapping[key] = i
    # "Qty (UOM)" style: ONE cell matched both qty and uom, so that column
    # holds unit words (PCS), not numbers.  When another column is a pure
    # quantity header ("Unit quantity"), the numbers live there — remap qty.
    if "qty" in mapping and mapping.get("uom") == mapping["qty"]:
        for i, c in enumerate(folded):
            if (i != mapping["qty"] and c and _HDR_KEYS["qty"].search(c)
                    and not _HDR_KEYS["uom"].search(c)):
                mapping["qty"] = i
                break
    # A declaration describes the CONSIGNMENT, not the purchase order.  With
    # "Qty Ordered" printed before "Qty Shipped", first-match-wins took the
    # ordered figure, the row's arithmetic then failed against its own money
    # and the whole page went to the LLM.
    qi = mapping.get("qty")
    if isinstance(qi, int) and _ORDERED_QTY_HDR.search(folded[qi] or ""):
        for i, c in enumerate(folded):
            if (i != qi and c and _HDR_KEYS["qty"].search(c)
                    and _SHIPPED_QTY_HDR.search(c)):
                mapping["qty"] = i
                break
    # The customs value is the PRE-TAX figure.  An EU invoice prints "Net
    # amount | VAT % | VAT amount | Gross amount" and whichever matched first
    # won — declaring the VAT-inclusive figure as the goods value wherever the
    # gross column happened to print first.
    #
    # The same rule binds the UNIT PRICE.  "P.U. brut" beside "P.U. net" with a
    # "Remise %" between them is the routine French/Belgian layout, and taking
    # the gross one does something worse than mis-state the rate: the row's
    # arithmetic then fails against its own printed total, and the quantity
    # repair "corrects" a perfectly legible printed quantity to fit
    # (900 -> 810 on a 10 % discount, exactly).
    for key in ("total", "price"):
        ci = mapping.get(key)
        if not (isinstance(ci, int) and 0 <= ci < len(folded)):
            continue
        if not (_GROSS_MONEY_HDR.search(folded[ci]) or _DISCOUNTABLE_HDR.search(folded[ci])):
            continue
        for i, c in enumerate(folded):
            if (i != ci and c and _HDR_KEYS[key].search(c)
                    and _NET_MONEY_HDR.search(c) and not _GROSS_MONEY_HDR.search(c)):
                mapping[key] = i
                break
    # Two money columns, no rate column: invoices print the RATE first and the
    # AMOUNT second, always.  "Value for Customs Purposes Only | Total" mapped
    # the total onto the first of them and reported a unit value as the line
    # value — 2.00 declared where the row totals 50.00.
    # Also fires when ONE cell matched both keys ("Valor Unitario" beside
    # "Valor Total": `^VALOR` claims the first for the total as well).
    if "total" in mapping and mapping.get("price") in (None, mapping["total"]):
        later = [i for i, c in enumerate(folded)
                 if i > mapping["total"] and c and _HDR_KEYS["total"].search(c)
                 and not _GROSS_MONEY_HDR.search(c)]
        if later:
            mapping["price"], mapping["total"] = mapping["total"], later[0]
    # A column that reads as a QUANTITY is a quantity, never the money total.
    if mapping.get("total") is not None and mapping.get("total") == mapping.get("qty"):
        mapping.pop("total")
    # "Product Description" / "Item Name" match BOTH `desc` and `model`, and a
    # column that reads as a description is a description — mapping it as the
    # model as well made the row's model the first WORD of its goods name
    # ("CERAMIC" out of "CERAMIC FLOOR TILE 600X600").
    for key in ("model", "line_no", "size", "brand"):
        if mapping.get(key) is not None and mapping.get(key) == mapping.get("desc"):
            mapping.pop(key)
    # A MONEY column is money, never a weight.  "Einzelpreis netto EUR" and
    # "Betrag netto" trip the net-weight vocabulary on the word "netto", and a
    # weight key pointing at the price column feeds allocation a currency
    # amount as kilograms.
    for wkey in ("net_wt", "gross_wt", "wt_any"):
        if mapping.get(wkey) is not None and mapping[wkey] in (
                mapping.get("price"), mapping.get("total"), mapping.get("taxable")):
            mapping.pop(wkey)
    # …and the converse: a column whose header NAMES A MASS UNIT is a weight,
    # whatever else its wording matched.  "TOTAL NET (KGS)" and "GROSS (KGS)"
    # on a packing list carry no WT/WEIGHT word at all, so the money `total`
    # key claimed the first and nothing claimed the second — a chest-tea
    # packing list lost both its weights to one header nobody had spelled out.
    ti = mapping.get("total")
    if isinstance(ti, int) and 0 <= ti < len(cells) and _header_unit(cells[ti] or ""):
        f = folded[ti]
        wkey = ("net_wt" if re.search(r"(?i)\bNET{1,2}O?\b", f)
                else "gross_wt" if re.search(r"(?i)\bGROSS\b|\bBRUT", f) else None)
        if wkey and wkey not in mapping:
            mapping[wkey] = ti
            mapping.pop("total")
            units = mapping.setdefault("units", {})
            units[wkey] = _header_unit(cells[ti] or "") or units.get(wkey)
    # "Qty UoM (Size)" style: one cell matched qty (and/or uom) AND size — that
    # column holds the QUANTITY; reading it as size emitted the quantity cell
    # ("25 EA (1/EA)") as the item's SIZE.  The quantity reading wins and the
    # size mapping is dropped (the description parser still mines a real size).
    for key in ("qty", "uom"):
        if "size" in mapping and mapping.get(key) == mapping["size"]:
            mapping.pop("size")
            break
    # One cell, two readings: "CARTON NO" and "PACKAGE TYPE" both contain a
    # carton word, so the loop above assigns `ctn` (a COUNT) the same column.
    # A numbered or typed column is never a count — the specific key wins.
    # Likewise "PACK SIZE" / "CARTON SIZE" (a size), and any weight/qty header
    # that merely mentions a carton: a cell that reads as a weight or a
    # quantity is that, not a carton count.
    # "Colli nr." and "Aantal colli" both contain the package word, so `ctn`
    # takes whichever prints first — and when that is the NUMBER column the
    # count is popped and the document loses its package basis entirely.  Look
    # for another package-word column before giving up on the count.
    # "CTN NO | QTY CTN | CTN" — three carton-ish headers meaning three different
    # things: WHICH cartons, quantity PER carton, and HOW MANY cartons.  First
    # match wins gave `ctn` the number column, the collision rules then moved it
    # to the RATE column and popped it against `qty`, and the real count column
    # was never read at all: a 37-row packing list whose CTN column sums to the
    # printed 492 produced 385 from counting carton-number ranges, and `qty`
    # declared 30 keyboards where the row totals 3000.
    #
    # Only ever applies when the document ALSO prints a pure package-count
    # column, so a list whose only carton header is "QTY CTNS" (meaning the
    # count) keeps reading it as the count.
    rate_cols = {i for i, c in enumerate(folded) if c and _QTY_PER_PKG_HDR.search(c)}
    if rate_cols:
        pure_ctn = [i for i, c in enumerate(folded)
                    if c and i not in rate_cols and _HDR_KEYS["ctn"].search(c)
                    and not _HDR_KEYS["ctn_no"].search(c)]
        pure_qty = [i for i, c in enumerate(folded)
                    if c and i not in rate_cols and _HDR_KEYS["qty"].search(c)
                    and not _HDR_KEYS["uom"].search(c)]
        # Kept whether or not a count column exists — and it matters MOST when
        # one does not.  `rate x cartons == quantity` is an identity the row
        # proves about itself: it realigns a row whose OCR dropped a cell
        # (_realign_short_row), and where no carton column is printed at all it
        # gives the carton count EXACTLY (quantity / units-per-carton) instead
        # of measuring a carton-number range or apportioning by weight.
        mapping["qty_per_pkg"] = sorted(rate_cols)[0]
        if pure_ctn:
            mapping["ctn"] = pure_ctn[0]
            if mapping.get("qty") in rate_cols and pure_qty:
                mapping["qty"] = pure_qty[0]
            elif mapping.get("qty") in rate_cols:
                mapping.pop("qty")           # a rate is never the row's quantity
    if mapping.get("ctn") is not None and mapping["ctn"] == mapping.get("ctn_no"):
        for i, c in enumerate(folded):
            if (i != mapping["ctn"] and c and _HDR_KEYS["ctn"].search(c)
                    and not _HDR_KEYS["ctn_no"].search(c)):
                mapping["ctn"] = i
                break
    for specific in ("ctn_no", "pkg_type", "uom", "size", "qty",
                     "gross_wt", "net_wt", "wt_any", "price", "total", "hs"):
        if "ctn" in mapping and mapping.get(specific) == mapping["ctn"]:
            mapping.pop("ctn")
    # An explicit gross/net column always beats the generic "WEIGHT" reading of
    # the same cell.
    for key in ("gross_wt", "net_wt"):
        if mapping.get("wt_any") == mapping.get(key):
            mapping.pop("wt_any", None)
    # Unit printed in the header ("GROSS WT (KGS)") applies to every value in
    # that column — only when it really is a mass unit, so "(SEE NOTE)" is
    # ignored rather than stored as a unit.
    units: dict[str, str] = {}
    for key in _WEIGHT_KEYS:
        i = mapping.get(key)
        if not isinstance(i, int) or not (0 <= i < len(cells)):
            continue
        units[key] = _header_unit(cells[i] or "") or units.get(key)
    units = {k: v for k, v in units.items() if v}
    if units:
        mapping["units"] = units
    # A quantity header that NAMES the unit ("PCS", "Qty (PRS)", "Quantity in
    # MTR") states it once for the whole column instead of on every row.  Only
    # read when there is no separate UOM column to contradict it, and only from
    # the document's own words — never a default.
    qi = mapping.get("qty")
    if isinstance(qi, int) and 0 <= qi < len(cells) and "uom" not in mapping:
        for token in re.findall(r"[A-Za-z]+", folded[qi] or ""):
            if _UOM_CELL.fullmatch(token):
                mapping["header_uom"] = token.upper()
                break
    has_body = "qty" in mapping or (
        not need_qty and any(k in mapping for k in ("ctn", "gross_wt", "net_wt", "wt_any")))
    if has_body and "desc" in mapping:
        mapping["n_cols"] = len(cells)
        return mapping
    return None


def page_prints_goods_rows(page_text: str) -> bool:
    """Deterministic "this page provably prints goods rows" signal for
    completeness checks.  Works on CELLS (bold ``**`` markers stripped, banner
    and header lines skipped), so OCR emphasis like ``| **42021900000** | PCS |``
    — which defeats plain raw-text regexes — still registers (2026-07-19
    incident: the last invoice page was silently dropped because the raw-text
    qty|UOM regex missed its bold cells)."""
    for line in (page_text or "").splitlines():
        if line.count("|") < 4:
            continue
        cells = _cells(line)
        if _is_separator(cells) or not any(cells):
            continue
        first = next((c for c in cells if c), "")
        if _BANNER.match(first) or _header_map(cells):
            continue
        found = qty_uom_cell_at(cells)
        if found is not None and found[0] != 0:
            return True
    return False


def _suspicious(cells: list[str]) -> bool:
    """Could this line be (part of) a goods row? Identity token or a qty/UOM
    cell (pair or merged single cell)."""
    joined = " ".join(cells).upper()
    if _GTIN.search(joined) or _PART.search(joined) or _PART_DIGIT_LED.search(joined):
        return True
    return qty_uom_cell_at(cells) is not None


# columns a continuation fragment may legitimately fill: the identity and
# annotation columns.  Everything else — quantity, UOM, money, weight, carton —
# must be EMPTY for a line to be excused as a fragment.
_FRAGMENT_ANNOT_KEYS = ("desc", "model", "line_no", "batch", "expiry",
                        "coo", "hs", "brand", "size")
# the mapped columns that carry a row's VALUES (invoice money and packing
# weight/carton columns) — the ones a provable fragment prints empty
_FRAGMENT_VALUE_KEYS = ("qty", "uom", "price", "total", "taxable",
                        "gross_wt", "net_wt", "wt_any", "ctn")


def _valueless_fragment(cells: list[str], mapping: dict | None) -> bool:
    """A cross-page continuation fragment: the batch/COO/serial breakdown of the
    PREVIOUS page's last row, printed as its own table line carrying an identity
    token but NOTHING in any quantity/money/weight/carton column.

    Such a line can never be a goods row — a real row always prints a quantity
    or a value (free-of-charge rows print an explicit 0.00; the same contract
    ingest's ROW_NO_VALUE_SKIPPED gate relies on) — so it must not count as a
    suspicious leftover that disowns the whole page to the LLM.  Disowning is
    exactly what let a fragment become a phantom declaration item (2026-07-31
    live job: given the page, the LLM emitted the fragment as a goods row and
    INVENTED its value cells, declaring 77 items on a 76-item invoice).

    Deliberately strict in two ways.  Content in ANY cell outside the
    identity/annotation columns — including a value that OCR drifted out of
    its mapped column — disqualifies the line.  And the mapped value columns
    must actually EXIST in the line as (empty) cells: a line truncated before
    them is a SPLIT row whose values may continue on the next OCR line, not a
    provable fragment.  Either way the page is disowned to the LLM exactly as
    before, so a mangled real row is never silently swallowed."""
    if mapping is None or qty_uom_cell_at(cells) is not None:
        return False
    allowed = {mapping.get(k) for k in _FRAGMENT_ANNOT_KEYS} | {0}
    value_cols = [mapping[k] for k in _FRAGMENT_VALUE_KEYS
                  if isinstance(mapping.get(k), int)]
    if not value_cols or len(cells) <= max(value_cols):
        return False
    return all(not c for i, c in enumerate(cells) if i not in allowed)


def _split_merged_money(cell: str, qty, locale: str | None):
    """Recover ``| 35,00 700,00 |`` (unit price + line total merged into one
    OCR cell).  Returns ``(price_raw, total_raw, derived_qty_or_None)``.

    Accepted when the arithmetic proves the split against the printed
    quantity — or, when the printed quantity contradicts it, when total /
    price is a clean positive integer: OCR sometimes misreads the QUANTITY
    digit while price and total survive (live job 2026-07-30: qty read 28
    against ``225,00 5.850,00``; 5850/225 = 26 exactly), and one such row
    used to disown the whole page to the LLM.  The derived quantity is
    returned so the caller can report the correction; both parts must carry a
    decimal/thousands separator so two bare integers (a batch/year fragment)
    can never fabricate a priced row.
    """
    parts = cell.split()
    if len(parts) != 2:
        return None
    a, b = parse_decimal(parts[0], locale=locale), parse_decimal(parts[1], locale=locale)
    if a is None or b is None:
        return None
    if qty and q2(qty * a) == q2(b):
        return parts[0], parts[1], None
    if (a > 0 and b > 0 and all(re.search(r"[.,]", p) for p in parts)):
        derived = (b / a).to_integral_value(rounding=ROUND_HALF_UP)
        # EXACT only — a near-miss (99.99 / 5.00 -> 19.998) is bad math, not a
        # misread quantity, and must still disown the page
        if derived >= 1 and derived != qty and q2(derived * a) == q2(b):
            return parts[0], parts[1], derived
    return None


def _printed_qty_reconciles(cells, qty, price, amount, locale, tol) -> bool:
    """Does some OTHER money cell on the row make the PRINTED quantity exact?

    Either direction counts: a different AMOUNT that equals qty x the mapped
    rate, or a different RATE that multiplies the printed quantity into the
    mapped amount.  When one exists, the document supports the quantity it
    printed and the mapped money column is the thing in the wrong place.
    """
    product = q2(qty * price)
    for cell in cells:
        v = parse_decimal(cell, locale=locale) if cell else None
        if v is None or v <= 0:
            continue
        if v != amount and abs(product - q2(v)) <= tol:
            return True                       # another amount fits qty x rate
        if v != price and abs(q2(qty * v) - q2(amount)) <= tol:
            return True                       # another rate fits qty -> amount
    return False


def _confirm_invoice_row(cells: list[str], mapping: dict[str, int], page_no: int,
                         row_index: int, line: str, locale: str | None,
                         notes: list[str] | None = None,
                         money_audit: list | None = None,
                         strict_arithmetic: bool = False) -> InvoiceLineRaw | None:
    found = qty_uom_cell_at(cells)                   # pair OR merged "25 EA (1/EA)" cell
    if found is not None:
        # Never read the mapped HS/tariff column (or any 8+-digit code cell)
        # as the quantity of a "| qty | UOM |" pair: "| 42021900000 | PCS |"
        # is an HS code followed by the unit column, not billions of pieces
        # (2026-07-19 incident vendor).  Fall through to the mapped qty column.
        if (found[0] == 0 or found[0] == mapping.get("hs")
                or len(re.sub(r"\D", "", found[2])) >= 8):
            found = None
    # The header-mapped quantity column outranks a positional qty/UOM match in
    # a DIFFERENT column: "| 16 CM |" in a size/box-number cell reads as a
    # merged quantity ("16 CM" — the spaced form accepts length units), and it
    # sits BEFORE the real qty column, so the positional scan found it first
    # (2026-08-01 live job: 'Corrugated Box No.' cells became the quantities).
    # When the mapped column holds its own qty(+UOM), use it; when it does not,
    # trust nothing positional — the bare-qty path below still requires a real
    # number in the mapped column, and an unconfirmed line disowns the page to
    # the LLM rather than confirming a misread.
    qi = mapping.get("qty")
    if found is not None and qi is not None and found[0] != qi:
        cand = (cells[qi] or "").strip() if 0 <= qi < len(cells) else ""
        m = _QTY_UOM_MERGED.fullmatch(cand) or _QTY_UOM_NOSPACE.fullmatch(cand)
        found = (qi, qi + 1, m.group(1), m.group(2)) if m else None
    if found is not None:
        qty_at, after_uom, qty_raw, uom_raw = found
    else:
        # Option A (user 2026-07-19): a unit-less quantity — a bare number in
        # the header-mapped qty column with no attached UOM — is still a goods
        # row when a printed total backs it. The quantity is never invented: it
        # must be a real number already in the mapped qty cell, and the
        # printed-total check below still gates the row.  The UOM comes from
        # the mapped UOM column when it holds a unit word, else stays NULL.
        #
        # Null, never "PCS": defaulting made an unreadable unit indistinguishable
        # from a printed one, so a wrong column map reported 15 rows of "PCS"
        # against an invoice printing KGM/PRS/MTR and nothing downstream could
        # tell.  An absent unit is now an empty field the reviewer fills in
        # (ITEM_UOM_MISSING), which is a question, not a silent assertion.
        qi = mapping.get("qty")
        if (qi is None or not 0 <= qi < len(cells) or not (cells[qi] or "").strip()
                or not _BARE_QTY.fullmatch(cells[qi].strip())):
            return None
        ui = mapping.get("uom")
        uom_raw = mapping.get("header_uom")
        if ui is not None and ui != qi and 0 <= ui < len(cells):
            cand = (cells[ui] or "").strip()
            # the vendor's own spelling, not the folded one: "m²" is what the
            # document printed and what the reviewer should see
            if cand and (_UOM_CELL.fullmatch(fold_header(cand)) or _printed_unit(cand)):
                uom_raw = cand
        qty_at, after_uom, qty_raw = qi, qi + 1, cells[qi].strip()
    qty = parse_decimal(qty_raw, locale=locale)
    if qty is None or qty <= 0:
        return None

    def _mapped_cell(key: str) -> str | None:
        i = mapping.get(key)
        return cells[i] if i is not None and 0 <= i < len(cells) and cells[i] else None

    # Money columns.  An invoice can print TWO distinct per-line figures: the
    # "amount" (quantity x rate) and a separate "taxable / assessable value".
    # The assessable value is the figure the invoice's own grand total sums and
    # the customs-declared goods value, so it is the reported line total; the
    # amount + rate are used only to arithmetically prove the row is real.
    amount_raw = _mapped_cell("total")
    price_raw = _mapped_cell("price")
    value_raw = _mapped_cell("taxable")

    def _repair_qty(derived) -> None:
        """The printed quantity contradicts price x total while total / price
        is a clean integer — the quantity digit was misread by OCR.  The row
        stays arithmetic-gated (qty is DERIVED, never invented) and the
        correction is reported to the reviewer via the parser notes."""
        nonlocal qty, qty_raw
        if notes is not None:
            notes.append(f"p{page_no} row {row_index}: printed qty {qty_raw!r} contradicts "
                         f"price x total; quantity {derived} derived from total / price")
        qty, qty_raw = derived, str(derived)

    if amount_raw is None and value_raw is None:
        # no mapped value column — positional heuristic (first money cells after
        # the UOM, with merged "35,00 700,00" recovery)
        money = [c for c in cells[after_uom:] if c]
        if len(money) >= 2:
            price_raw, amount_raw = money[0], money[1]
        elif len(money) == 1:
            split = _split_merged_money(money[0], qty, locale)
            if split:
                price_raw, amount_raw, derived = split
                if derived is not None:
                    _repair_qty(derived)
    total_raw = value_raw or amount_raw           # assessable value preferred
    if total_raw is None:
        return None
    total = parse_decimal(total_raw, locale=locale)
    if total is None or total <= 0:
        # require a real non-zero value so a blank/parse-miss can never confirm
        # a 0-priced goods row
        return None
    # Arithmetic sanity: when BOTH a per-unit rate and the qty x rate amount are
    # printed they must agree — proving the row is a real goods line even though
    # the reported value may be the higher assessable figure. A row with no
    # usable rate (amount-only) is trusted on its positive printed value.
    # Tolerance: vendors that derive the printed rate FROM the total round it
    # to 2dp (total 7.77 / qty 5 -> rate 1.55), so qty x rate can be off by up
    # to half a cent per unit (+1 cent for the total's own rounding). The
    # reported value is always the printed total, never the product.
    price = parse_decimal(price_raw, locale=locale) if price_raw else None
    amount = parse_decimal(amount_raw, locale=locale) if amount_raw else None
    value = parse_decimal(value_raw, locale=locale) if value_raw else None
    tol = qty * Decimal("0.005") + Decimal("0.01")
    if price is not None and price > 0 and amount is not None and amount > 0:
        # The row is proven when qty x rate matches EITHER printed money figure.
        # An invoice that prints both an "Amount" and a "Taxable value" may have
        # the arithmetic close against the assessable one, and checking only the
        # other made a correct row look broken.
        if any(v is not None and v > 0 and abs(q2(qty * price) - q2(v)) <= tol
               for v in (amount, value)):
            pass
        elif _printed_qty_reconciles(cells, qty, price, amount, locale, tol):
            # Some OTHER money cell on this row makes the PRINTED quantity come
            # out exact — so the quantity is not what is wrong here, the column
            # map is, and "repairing" the quantity would replace a legible
            # printed figure with a fabricated one.  Live shapes that do this:
            # a "P.U. brut / Remise % / P.U. net" trio (900 -> 810 on a 10 %
            # discount) and a tax-inclusive total beside a taxable value
            # (500 -> 590 on 18 % GST).  Both divide to an EXACT integer, which
            # is precisely the condition the repair reads as proof.  Stand the
            # page down instead and let the LLM path read it.
            if notes is not None:
                notes.append(
                    f"p{page_no} row {row_index}: printed qty {qty_raw!r} is exact against a "
                    f"different money column on the row — the column map is wrong, so the "
                    f"page was not confirmed (the quantity was NOT rewritten)")
            return None
        else:
            # separate-cells variant of the merged-money quantity repair: a
            # misread qty digit no longer disowns the page when total / price
            # is a clean integer — EXACTLY (same gate as _split_merged_money;
            # a near-miss is bad math and must still disown the page)
            derived = (amount / price).to_integral_value(rounding=ROUND_HALF_UP)
            if (derived >= 1 and derived != qty
                    and q2(derived * price) == q2(amount)
                    and any(re.search(r"[.,]", p or "") for p in (price_raw, amount_raw))):
                _repair_qty(derived)
            else:
                return None
    elif strict_arithmetic:
        # A REMEMBERED layout is only ever applied to a document whose own
        # header could not be read, so nothing in the document itself proves
        # the columns line up — qty x rate == amount is the only evidence there
        # is, and a row that cannot produce it is not confirmed.  The leniency
        # below (amount-only rows trusted on a positive printed value) is what
        # let a 6-column map run over a 7-column table: the rate column landed
        # on a unit word, parsed to None, the gate never ran, and every row
        # confirmed with an HS code as its quantity.
        return None
    else:
        price_raw = None

    ident_cells = [c for c in cells[:qty_at] if c]
    if not ident_cells and mapping.get("desc") is None:
        # Nothing identifies this row and no description column was mapped —
        # there is no goods name to declare.  When the header DID map a
        # description the position no longer matters: "QTY | UNIT | ARTICLE"
        # is a normal Philippine/US layout, and requiring an identity cell
        # BEFORE the quantity refused every row of it.
        return None

    def mapped(key: str) -> str | None:
        """A mapped identity column (before the quantity)."""
        i = mapping.get(key)
        return cells[i] if i is not None and i < qty_at and cells[i] else None

    def mapped_any(key: str) -> str | None:
        """A mapped column anywhere on the row (HS / origin often print after
        the money columns)."""
        i = mapping.get(key)
        return cells[i] if i is not None and 0 <= i < len(cells) and cells[i] else None

    # `mapped_any`, not `mapped`: the description column is not always BEFORE
    # the quantity ("QTY | UNIT | ARTICLE / DESCRIPTION").
    desc = mapped_any("desc") or (ident_cells[-1] if ident_cells else None)
    if not desc:
        return None
    # positional model fallback must never pick up the serial-number column,
    # and never the DESCRIPTION either: on a vendor that prints no code column
    # at all the description is the first identity cell, and taking it made the
    # model the first WORD of the goods name ("CERAMIC" out of "CERAMIC FLOOR
    # TILE 600X600").
    fallback_model = ident_cells[0] if len(ident_cells) > 1 else None
    if fallback_model is not None and fallback_model in (mapped("line_no"), desc):
        fallback_model = None
    model = mapped("model") or fallback_model
    # A bare small integer is a LINE COUNTER, whatever the header called it:
    # "Réf." matches the model vocabulary but numbers its rows 1, 2, 3.
    if model and re.fullmatch(r"\d{1,3}", model.strip()):
        model = None
    if model and " " in model:
        # material cells may embed batch/COO/tariff annotations after the part
        # number ("01E3120 (Qty) Batch/Expiry ... Tariff Code 38220090"), and
        # some vendors print the catalogue code AND its GTIN barcode in one
        # cell ("RSINT25012X 00763000478896") — the first NON-BARCODE token is
        # the model; a 13-14 digit GTIN is never a model/part code.
        tokens = model.split()
        model = next((t for t in tokens if not _GTIN.fullmatch(t)), tokens[0])
    # HS / COO: a dedicated column wins; otherwise recover a labelled code
    # embedded in the identity cell. Both are optional deterministic bonuses.
    row_blob = " ".join(c for c in cells if c)
    hs_cell = mapped_any("hs")
    hs_code = None
    if hs_cell:
        # Try the WIDEST shape first — a tariff code is printed with dots, with
        # spaces ("4407 11 100 0", CIS), with a hyphen ("8708.30-1000", Korea)
        # or with nothing at all, and the narrow digits-and-dots pattern used to
        # win the race and truncate the rest ("4015.12" out of "4015.12 000").
        # Only read from a column the HEADER already named as the tariff column,
        # and only when the digit count is really code-length.
        for pat in (r"\d[\d.\s\-]{5,19}\d", r"\d[\d.]{5,17}\d"):
            m = re.search(pat, hs_cell)
            if m and 6 <= len(re.sub(r"\D", "", m.group(0))) <= 14:
                hs_code = m.group(0).strip()
                break
    if hs_code is None and (m := _TARIFF_IN_CELL.search(row_blob)):
        hs_code = m.group(1)
    coo_cell = mapped_any("coo")
    coo_code = None
    if coo_cell:
        if (m := re.fullmatch(r"\s*([A-Za-z]{2})\s*", coo_cell)):
            coo_code = m.group(1).upper()
        else:
            # a mapped Origin column printing a full name ("CHINA", "Ireland")
            # used to be silently dropped here (2-letter codes only) while the
            # packing path took the same cell verbatim — now both paths carry
            # the raw value and rules.coo normalize-gates it downstream.
            coo_code = coo_cell.strip() or None
    if coo_code is None and (m := _COO_IN_CELL.search(row_blob)):
        raw = m.group(1).strip()
        coo_code = raw.upper() if len(raw) == 2 else raw
    # brand / size are optional export-only enrichment columns (anywhere on the
    # row); the resolver falls back to the exporter / description when absent.
    brand_cell = mapped_any("brand")
    size_cell = mapped_any("size")
    # Invoice-printed item weight — the HIGHEST non-reviewer net-weight
    # authority (spec section 5, rank 2), and the parser never emitted it, so
    # an invoice stating every item's weight fell through to lower-ranked
    # sources.  A NET column wins; a bare "Weight (KG)" column is taken as the
    # item's weight; a GROSS column is deliberately NOT — a gross is not a net.
    weight_raw = weight_unit = None
    units = mapping.get("units") or {}
    for wkey in ("net_wt", "wt_any"):
        wi = mapping.get(wkey)
        if isinstance(wi, int) and 0 <= wi < len(cells) and cells[wi]:
            value, unit = _split_value_unit(cells[wi])
            if value is not None and parse_decimal(value, locale=locale) is not None:
                weight_raw, weight_unit = value, (unit or units.get(wkey))
                break
    row = InvoiceLineRaw(
        source_page_no=page_no, source_row_index=row_index,
        line_no_raw=mapped("line_no"),
        description_raw=desc, model_raw=model,
        brand_raw=brand_cell, size_raw=size_cell,
        quantity_raw=qty_raw, uom_raw=uom_raw,
        unit_price_raw=price_raw, line_total_raw=total_raw,
        item_weight_raw=weight_raw, item_weight_unit_raw=weight_unit,
        hs_code_raw=hs_code,
        country_of_origin_raw=coo_code,
        evidence=[Evidence(page_no=page_no, label=PARSER_EVIDENCE_LABEL, quote=line.strip()[:500])],
    )
    if money_audit is not None:
        # both money readings, for the document-level taxable-column sanity
        # pass (_resolve_taxable_column) — the per-row preference cannot see
        # that the whole column is junk
        money_audit.append((row, amount_raw, value_raw,
                            parse_decimal(amount_raw, locale=locale) if amount_raw else None,
                            parse_decimal(value_raw, locale=locale) if value_raw else None))
    return row


def _resolve_taxable_column(money_audit: list, notes: list[str]) -> None:
    """PER-PAGE sanity on the taxable/assessable-value preference.

    ``_confirm_invoice_row`` prefers a mapped "Taxable/Assessable" column as
    the declared line value.  That is right when the column really holds the
    assessable value — and silently wrong when the cell under that header
    prints a tax RATE (2026-08-01 live job: a skewed scan's OCR dropped
    columns on page 4 only, shifting the IGST-rate column — a constant 5.00 —
    under the "Taxable AMT." header while the real values sat under "Amount";
    20 rows shipped with a 5.00 line total each).

    Per row that is undecidable, and per DOCUMENT it is unprovable — on the
    same job the untouched pages printed a perfectly legitimate, varying
    taxable column, so a document-wide vote is diluted by the healthy pages.
    Per page it is provable: when every confirmed row on a page printing BOTH
    figures shows a single constant taxable while the amounts vary — or a
    taxable that is a small fraction of its amount on every row — that page's
    column is a rate/junk column, and its rows are re-based on the printed
    amount column (which the qty x rate arithmetic gate already vetted where
    a rate exists).  The correction is reported per page."""
    by_page: dict[int, list] = {}
    for entry in money_audit:
        by_page.setdefault(entry[0].source_page_no, []).append(entry)
    for page_no in sorted(by_page):
        entries = by_page[page_no]
        pairs = [(a, t) for (_row, _ar, _tr, a, t) in entries
                 if a is not None and a > 0 and t is not None and t > 0]
        if len(pairs) < 3:
            continue
        amounts = {a for a, _ in pairs}
        taxables = {t for _, t in pairs}
        constant_junk = len(taxables) == 1 and len(amounts) > 1
        fraction_junk = all(t < a * Decimal("0.25") for a, t in pairs)
        if not (constant_junk or fraction_junk):
            continue
        rebased = 0
        for row, amount_raw, taxable_raw, a, _t in entries:
            if (taxable_raw is not None and row.line_total_raw == taxable_raw
                    and amount_raw and a is not None and a > 0):
                row.line_total_raw = amount_raw
                rebased += 1
        if rebased:
            reason = ("a constant figure on every row while the amounts vary"
                      if constant_junk else "a small fraction of the amount on every row")
            notes.append(f"p{page_no}: taxable/assessable column rejected as the line value "
                         f"({reason} — a tax-rate or junk column, not an assessable value); "
                         f"{rebased} row(s) re-based on the printed amount column")


def _confirm_packing_row(cells: list[str], mapping: dict, page_no: int,
                         row_index: int, line: str, locale: str | None,
                         notes: list[str] | None = None) -> PackingRowRaw | None:
    """One packing-list goods row, INCLUDING its weight and carton columns.

    This used to emit only line_no / description / quantity / UOM.  Every gross
    weight, net weight and carton count printed on a parser-owned page was
    therefore discarded — and a parser-owned page never reaches the LLM, so the
    values were gone for good.  Allocation then saw no packing evidence at all
    and fell back to splitting the authorised gross by invoice VALUE, on a
    shipment whose packing list stated every weight.  That is the bug this
    function existed to cause.

    Two rules keep the added columns safe:

    * **header-mapped only, never positional** — reading a net-weight column as
      gross is a silent declaration error no downstream check can see;
    * **a carton NUMBER is not a carton COUNT** — ``C/NO 1-5`` is five cartons
      with the identifier ``1-5``, and rows sharing that identifier are marked
      as a shared group so the count is divided, never duplicated.

    The old GTIN/part-number requirement is gone: real packing lists are often
    description-only, and demanding an identity token is what stopped the parser
    from ever owning such a page.  What replaces it is stricter in the way that
    matters — a header-derived column map, a real description, at least one
    numeric body value, and (in ``parse_pages``) a document-level cross-check of
    the parsed sums against the packing list's own printed totals.
    """
    if _is_totals_line(cells, mapping):
        return None

    def cell(key: str) -> str | None:
        i = mapping.get(key)
        if isinstance(i, int) and 0 <= i < len(cells):
            return (cells[i] or "").strip() or None
        return None

    desc = cell("desc")
    if not desc or not re.search(r"[A-Za-z]{2}", desc):
        return None                                  # continuation/spacer line
    units = mapping.get("units") or {}

    def number(key: str) -> RawNumber | None:
        raw = cell(key)
        if not raw:
            return None
        value, unit = _split_value_unit(raw)
        if value is None or parse_decimal(value, locale=locale) is None:
            return None
        return RawNumber(value_raw=value, unit_raw=unit or units.get(key))

    # quantity: the mapped column ("25", "25 EA", "25EA"), else a qty|UOM pair
    qty_raw = uom_raw = None
    qcell = cell("qty")
    if qcell:
        m = _QTY_UOM_MERGED.fullmatch(qcell) or _QTY_UOM_NOSPACE.fullmatch(qcell)
        if m:
            qty_raw, uom_raw = m.group(1), m.group(2)
        elif _BARE_QTY.fullmatch(qcell):
            qty_raw = qcell
    if qty_raw is None:
        found = qty_uom_cell_at(cells)
        # …but NEVER out of a package column.  "| 40 | DOZEN |" is forty boxes,
        # and the positional scan reads it as a perfectly good "40 DOZEN"
        # quantity — declaring the package count as the goods quantity on a
        # document that prints no quantity at all.
        blocked = {0, mapping.get("hs"), mapping.get("ctn"), mapping.get("ctn_no"),
                   mapping.get("pkg_type")}
        if found is not None and found[0] not in blocked:
            _, _, qty_raw, uom_raw = found
    if qty_raw is not None and parse_decimal(qty_raw, locale=locale) is None:
        qty_raw = uom_raw = None
    if uom_raw is None:
        cand = cell("uom")
        if cand and _UOM_CELL.fullmatch(cand):
            uom_raw = cand

    gross, net = number("gross_wt"), number("net_wt")
    # an unlabelled weight column is only ever the fallback, and says so
    declared = number("wt_any") if (gross is None and net is None) else None

    ctn_no = cell("ctn_no")
    carton = number("ctn")
    if carton is None:
        # EXACT, not proportional: a row printing "20 PRS per carton" and a
        # total of 600 PRS packs thirty cartons, and the document said so.
        # This outranks measuring the carton-number range, which is an
        # identifier the shipper chose and only sometimes a span.
        rate = number("qty_per_pkg")
        per = parse_decimal(rate.value_raw, locale=locale) if rate else None
        total = parse_decimal(qty_raw, locale=locale) if qty_raw else None
        if per and total and per > 0 and total > 0:
            exact = total / per
            if exact == exact.to_integral_value() and exact >= 1:
                carton = RawNumber(value_raw=str(int(exact)), unit_raw="CTN")
    if carton is None and ctn_no:
        derived = carton_count_from_no(ctn_no)
        if derived:
            carton = RawNumber(value_raw=str(derived), unit_raw="CTN")

    if all(v is None for v in (qty_raw, gross, net, declared, carton)):
        return None                                  # a description is not a row

    return PackingRowRaw(
        source_page_no=page_no, source_row_index=row_index,
        line_no_raw=cell("line_no") or (cells[0] or None),
        description_raw=desc,
        brand_raw=cell("brand"), model_raw=cell("model"), size_raw=cell("size"),
        item_code_raw=cell("model"),
        quantity_raw=qty_raw, uom_raw=uom_raw,
        hs_code_raw=cell("hs"), country_of_origin_raw=cell("coo"),
        gross_weight=gross, net_weight=net,
        declared_weight=declared,
        weight_type_raw=("UNKNOWN" if declared is not None else None),
        carton_count=carton, carton_no_raw=ctn_no,
        package_type_raw=cell("pkg_type"),
        batch_no_raw=cell("batch"), expiry_date_raw=cell("expiry"),
        evidence=[Evidence(page_no=page_no, label=PARSER_EVIDENCE_LABEL,
                           quote=line.strip()[:_QUOTE_CHARS])],
    )


def _absorb_totals_row(cells: list[str], mapping: dict, totals: dict, page_no: int) -> None:
    """Read a packing list's own printed TOTALS row through the column map.

    The totals row is the document stating what its rows must add up to — the
    cheapest, strongest check available on a deterministic parse, and it comes
    free with the header map that parsed the rows themselves.  The page number
    rides along so the total is later parsed under the SAME numeric locale as
    the rows it gates — an EU "12,000" parsed locale-blind as twelve thousand
    stood a perfectly correct parse down on a phantom mismatch.
    """
    units = mapping.get("units") or {}
    for key in ("gross_wt", "net_wt", "ctn", "qty"):
        i = mapping.get(key)
        if not isinstance(i, int) or not (0 <= i < len(cells)):
            continue
        value, unit = _split_value_unit(cells[i] or "")
        if value is not None:
            totals[key] = (value, unit or units.get(key), page_no)


def _absorb_inline_totals(ocr_pages: dict[int, str], totals: dict) -> None:
    """Totals printed as free text ("TOTAL GROSS WEIGHT: 12.00 KGS") rather than
    as a table row.  Only ever fills gaps — a mapped totals row wins, because it
    is unambiguous about which column it states."""
    for page_no, text in ocr_pages.items():
        for line in (text or "").splitlines():
            if "total" not in line.lower():
                continue                      # a per-row value is not a total
            for key, pat in _INLINE_TOTALS.items():
                if key in totals:
                    continue
                m = pat.search(line)
                if m:
                    unit = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                    totals[key] = (m.group(1), unit, page_no)


def _demote_serial_cartons(rows: list, res: ParseResult) -> None:
    """A column of 1, 2, 3, 4 … is a carton NUMBER, not a carton count.

    Only applied when the document prints no carton total to check against —
    with a total, the cross-check below already catches a serial column, and it
    does so on evidence rather than on a pattern.  A genuine count column that
    happens to read 1,2,3 is rare; a serial column read as a count silently
    multiplies the shipment's carton basis by its own row number, which is not.
    """
    if res.printed_totals.get("ctn") or len(rows) < 3:
        return
    # A count DERIVED from a carton range can never be a serial: "1-2 / 3-5 /
    # 6-9" gives 2, 3, 4 — a legitimate, range-corroborated count column that
    # this heuristic destroyed precisely because ranges step by one.  Any row
    # carrying a carton NUMBER means the numbers already have a home.
    if any(getattr(r, "carton_no_raw", None) for r in rows):
        return
    values = []
    for r in rows:
        raw = getattr(r, "carton_count", None)
        val = parse_decimal(raw.value_raw) if raw and raw.value_raw else None
        if val is None:
            return
        values.append(val)
    if all(values[i + 1] - values[i] == 1 for i in range(len(values) - 1)):
        for r in rows:
            if r.carton_no_raw is None and r.carton_count is not None:
                r.carton_no_raw = r.carton_count.value_raw
            r.carton_count = None
        res.notes.append("carton column reads as consecutive serial numbers — treated as carton "
                         "NUMBERS, not counts")


def _mark_shared_cartons(pages: dict[int, PageParse]) -> None:
    """Rows printed against the SAME carton number share that carton.

    Spec section 4, Condition 2: the shared carton quantity is divided among the
    rows that share it and the group total never changes.  ``packing_match``
    implements the division; all it needs is for the rows to carry the same
    group id, which the printed carton number already is.
    """
    seen: dict[str, list] = {}
    for pp in pages.values():
        for row in pp.rows:
            # Collapse WHITESPACE and dash variants only.  Deleting every
            # non-alphanumeric erased the one thing that distinguishes a span
            # from an identifier: "1-25" (a range of 25) and "125" (carton one
            # hundred twenty-five) normalized to the same key, and the single
            # carton was then declared as the whole range.
            raw = (row.carton_no_raw or "").lower().translate({0x2013: "-", 0x2014: "-"})
            key = re.sub(r"\s+", "", raw)
            if key:
                seen.setdefault(key, []).append(row)
    # Rows sharing a carton id must also be ADJACENT to share a physical carton.
    # Carton numbering restarts per shipping mark within one document, so the
    # same id appears more than once meaning different boxes: live 2026-08-04,
    # SMALL LAMINATION MACHINE (marka LIANABIN-25) and SPEAKER (marka SKAN-25)
    # both printed "1-25" twenty-six rows apart, were merged into one group
    # whose total was taken as 25 instead of 50, and 25 of the shipment's 492
    # cartons vanished.
    for key, rows in list(seen.items()):
        rows.sort(key=lambda r: (r.source_page_no or 0, r.source_row_index or 0))
        runs, run = [], [rows[0]]
        for prev, cur in zip(rows, rows[1:]):
            adjacent = (prev.source_page_no == cur.source_page_no
                        and (cur.source_row_index or 0) - (prev.source_row_index or 0) <= 1)
            if adjacent:
                run.append(cur)
            else:
                runs.append(run)
                run = [cur]
        runs.append(run)
        seen[key] = max(runs, key=len) if len(runs) == 1 else []
        if len(runs) > 1:
            for r in runs:
                if len(r) > 1:
                    seen[f"{key}#{r[0].source_row_index}"] = r
    for rows in seen.values():
        if len(rows) > 1:
            # ONE canonical id for the whole group.  Grouping is decided on the
            # normalized key, but downstream (packing_match.shared_members)
            # buckets on the raw string — publishing each row's own spelling
            # ("1-5" vs "1 - 5", a routine OCR artifact) split the group back
            # apart, and each fragment then claimed the FULL carton range.
            canon = rows[0].carton_no_raw
            for row in rows:
                row.shared_carton_group_raw = canon


def packing_column_candidates(ocr_pages: dict[int, str]) -> dict | None:
    """The header cells and two sample rows of the first mappable table, for a
    caller that wants to ask a model which column is which.

    Returns None when the page has no header row at all — there is nothing to
    be confused ABOUT, and guessing column roles off bare data is exactly what
    this codebase never does.
    """
    for page_no in sorted(ocr_pages):
        lines = [l for l in (ocr_pages[page_no] or "").splitlines() if l.count("|") >= 3]
        for i, line in enumerate(lines):
            cells = _cells(line)
            if _is_separator(cells) or not any(cells):
                continue
            if _header_map(cells, need_qty=False) is None and not any(
                    _HDR_KEYS[k].search(fold_header(c)) for c in cells if c
                    for k in ("desc", "gross_wt", "net_wt", "ctn")):
                continue
            samples = []
            for nxt in lines[i + 1:]:
                sc = _cells(nxt)
                if _is_separator(sc) or not any(sc) or _is_totals_line(sc):
                    continue
                samples.append(sc)
                if len(samples) == 2:
                    break
            if samples:
                return {"page_no": page_no, "header": cells, "rows": samples}
    return None


def parse_pages(role: DeclaredRole, ocr_pages: dict[int, str],
                locales: dict[int, str | None],
                fallback_mappings: list[dict] | tuple = (),
                column_roles: dict | None = None) -> ParseResult:
    """Parse every page; a PageParse with ``confirmed=True`` is parser-owned.

    Column maps come from header rows found in the document.  When NO header
    is readable anywhere, each ``fallback_mapping`` (vendor layout memory) is
    tried and the one confirming the most arithmetic-verified rows wins —
    but only past MIN_MEMORY_ROWS; otherwise the result has empty ``pages``
    and the caller must use the historical LLM path unchanged."""
    res = _parse_with(role, ocr_pages, locales, None, column_roles)
    if res.header_signature is not None:
        return _gated(role, res, locales, ocr_pages)
    best: ParseResult | None = None
    for fb in fallback_mappings:
        cand = _parse_with(role, ocr_pages, locales, dict(fb))
        n = cand.confirmed_row_count()
        if n >= MIN_MEMORY_ROWS and (best is None or n > best.confirmed_row_count()):
            cand.mapping, cand.from_memory = dict(fb), True
            best = cand
    return _gated(role, best, locales, ocr_pages) if best is not None else ParseResult()


# Per-column tolerance when checking parsed sums against the printed totals:
# rows carry 2-3 dp and totals are rounded, so an exact match is not required —
# but anything past this is a column read wrong, not rounding.
_TOTAL_TOLERANCE = Decimal("0.02")               # 2 %


_CURRENCY_WORD = (r"USD|EUR|GBP|INR|NPR|CNY|RMB|JPY|AED|HKD|SGD|AUD|CAD|CHF|KRW|THB|MYR|"
                  r"IDR|VND|BDT|PKR|LKR|SAR|QAR|TRY|ZAR|NZD|SEK|NOK|DKK|PLN|RUB|BRL|MXN|"
                  r"TWD|PHP|DOLLARS?|RUPEES?")
# Deliberately LOOSE where `_TOTALS_CELL` is tight, because the two carry
# opposite risks.  `_TOTALS_CELL` decides whether to SKIP a line, so a false
# positive silently drops a goods row and it must enumerate its tail.  This one
# only nominates a figure for the goods-sum cross-check, where an extra
# candidate can make the gate more lenient but can never make a parse wrong —
# so the label may be followed by anything at all.  Enumerating the tail here
# is what made "I alt ekskl. moms", "Summe netto EUR" and "Veroton yhteensä"
# invisible, and the gate then rejected three correct parses for missing a
# total the document had printed in plain sight.
_MONEY_TOTAL_LABEL = re.compile(
    rf"^(?:GRAND\s+|SUB[\s-]*|NET\s+)?(?:{_TOTAL_WORD})\b"
    rf"|^(?:INVOICE|GOODS)\s+(?:VALUE|AMOUNT|TOTAL)\b"
    # the assessable base is the goods sum under another name, and on a
    # bilingual invoice it is often the ONLY line that equals it
    rf"|^(?:TAXABLE|ASSESSABLE|DUTIABLE)\b|\bTAX(?:ABLE)?\s*BASE\b", re.I)
# Charges printed as their own labelled row.  Plenty of invoices print ONLY a
# freight-inclusive grand total, so the goods rows legitimately sum to that
# total minus these — without them the cross-check would stand the parser down
# on a perfectly good parse and hand the document back to the LLM.
_MONEY_CHARGE_LABEL = re.compile(
    r"^(?:TOTAL\s+|TRADE\s+|CASH\s+)?(?:FREIGHT|INSURANCE|DISCOUNT|REBATE|PACKING|PACKAGING|"
    r"HANDLING|SHIPPING|CARRIAGE|V\.?A\.?T|GST|IGST|CGST|SGST|TAX|DUTY|SURCHARGE|"
    r"COMMISSION|FRACHT|VERSANDKOSTEN|PORT|SEGURO|FLETE)\b"
    # A multi-page invoice opens later pages with the balance its earlier pages
    # carried.  It is not this page's goods and not a total — but the grand
    # total DOES include it, so it has to be subtractable or the gate rejects a
    # correct parse for being short by exactly the carried amount.
    r"|^(?:BALANCE\s*)?(?:BROUGHT|CARR(?:IED|Y))\s*FORWARD\b|^B/?F\b", re.I)


def _printed_money_totals(ocr_pages: dict[int, str],
                          locales: dict[int, str | None]) -> tuple[list[Decimal], list[Decimal]]:
    """``(totals, charges)`` — the money figures the document prints on its own
    TOTAL-labelled and charge-labelled table rows.

    Lower bounds, not authorities: several totals are normal on one invoice (a
    goods subtotal, then the same figure net of a discount), so the caller
    accepts a match against ANY of them rather than picking one.
    """
    totals: list[Decimal] = []
    charges: list[Decimal] = []
    for page_no in sorted(ocr_pages):
        locale = locales.get(page_no)
        for line in (ocr_pages[page_no] or "").splitlines():
            if line.count("|") < 3:
                continue
            cells = _cells(line)
            if _is_separator(cells) or not any(cells):
                continue
            for i, c in enumerate(cells):
                if not c:
                    continue
                label = fold_header(c)
                # CHARGES are tested first: now that the totals label accepts
                # any trailing text, "TOTAL FREIGHT" and "Total insurance"
                # match both, and bucketing them as totals loses the charge
                # adjustment that lets a freight-inclusive grand total
                # reconcile against the goods rows.
                bucket = (charges if _MONEY_CHARGE_LABEL.search(label)
                          else totals if _MONEY_TOTAL_LABEL.match(label) else None)
                if bucket is None:
                    continue
                for rest in cells[i + 1:]:
                    value = parse_decimal(rest, locale=locale) if rest else None
                    if value is not None and value > 0:
                        bucket.append(value)
                break
    return totals, charges


def _gate_invoice_totals(res: ParseResult, ocr_pages: dict[int, str],
                         locales: dict[int, str | None]) -> ParseResult:
    """Cross-check the parsed line values against the invoice's OWN printed
    total, and abandon the parse wholesale when they disagree.

    The invoice equivalent of the packing-list check below, and the structural
    answer to a wrong column map: the 2026-08-03 job read the RATE column as the
    line total, so 15 rows summed to 53.95 against a printed 36,058.40 — every
    row individually plausible, the whole document off by three orders of
    magnitude.  Only the sum can see that, and the invoice states its own sum.

    Accepts a match against any single printed total, any SUBSET of them, or
    any of those adjusted by the charge rows the document prints alongside.
    No printed total at all -> no check, and the parse stands.
    """
    rows = [r for pp in res.pages.values() if pp.confirmed for r in pp.rows]
    if not rows:
        return res
    got = Decimal("0")
    for r in rows:
        value = parse_decimal(r.line_total_raw, locale=locales.get(r.source_page_no))
        if value is None:
            return res
        got += value
    candidates, charges = _printed_money_totals(ocr_pages, locales)
    if not candidates:
        res.notes.append("the invoice prints no totals row — parsed line values could not be "
                         "cross-checked against a document total")
        return res
    targets = set(candidates)
    # Any SUBSET of the printed totals, not just their whole sum.  One invoice
    # routinely prints several partial totals that the goods rows add up to
    # across: a per-currency subtotal each, a taxable base per VAT rate, a
    # subtotal per section, or one total per bundled printed invoice.  Extra
    # targets can only make this gate more lenient — it never turns a wrong
    # parse into an accepted one — so the powerset is the honest reading.
    distinct = sorted(set(candidates))[:8]
    for bits in range(1, 1 << len(distinct)):
        subset = [distinct[i] for i in range(len(distinct)) if bits >> i & 1]
        if len(subset) > 1:
            targets.add(sum(subset, Decimal("0")))
    charge_set = set(charges)
    if charge_set:
        every = sum(charge_set, Decimal("0"))
        for base in list(targets):
            targets.update({base - c for c in charge_set} | {base + c for c in charge_set})
            targets.update({base - every, base + every})
    # closest first, so the note names the figure the reviewer can actually find
    # on the page rather than whichever charge-adjusted variant sorted first
    for target in sorted((t for t in targets if t > 0), key=lambda t: abs(got - t)):
        if abs(got - target) <= max(target * _TOTAL_TOLERANCE, Decimal("0.05")):
            res.notes.append(f"parsed line values sum {got} matches the printed invoice "
                             f"total {target}")
            return res
    printed = ", ".join(str(t) for t in sorted(set(candidates)))
    res.notes.append(f"parsed line values sum {got} matches none of the printed invoice "
                     f"totals ({printed}) — the column map is wrong")
    return ParseResult(notes=res.notes)               # stand down: the map is wrong


def _gated(role: DeclaredRole, res: ParseResult, locales: dict[int, str | None],
           ocr_pages: dict[int, str] | None = None) -> ParseResult:
    """Cross-check a parse against the document's OWN printed totals.

    A deterministic parse cannot invent a row, but it CAN read the wrong column
    — a net-weight column taken as gross produces a full, plausible, silently
    wrong declaration.  The document already states what its rows add up to,
    so when the sums disagree the parse is abandoned wholesale and the LLM path
    runs: a mismatch means the column map is wrong, and a wrong map is wrong for
    every row on every page.

    The check only runs when the parser owns ALL the pages that hold content —
    otherwise the sums are partial by construction and could not match anyway.
    """
    if not res.pages:
        return res
    partial = [n for n, pp in res.pages.items()
               if not pp.confirmed and (pp.rows or pp.suspicious_leftover)]
    if not partial and role != DeclaredRole.PACKING_LIST:
        return _gate_invoice_totals(res, ocr_pages or {}, locales)
    if role != DeclaredRole.PACKING_LIST:
        res.notes.append(f"parsed line values not cross-checked: pages {sorted(partial)} are "
                         f"not parser-owned, so the parsed rows are only part of the document")
        return res
    if partial:
        res.notes.append(f"parsed sums not cross-checked: pages {sorted(partial)} are not "
                         f"parser-owned, so the parsed rows are only part of the document")
        return res
    rows = [r for pp in res.pages.values() if pp.confirmed for r in pp.rows]
    # Runs BEFORE the no-totals early return: a serial column is exactly the
    # case where there is no printed total to catch it.
    _demote_serial_cartons(rows, res)
    if not res.printed_totals:
        res.notes.append("the packing list prints no totals row — parsed rows could not be "
                         "cross-checked against a document total")
        return res

    for key, attr in (("gross_wt", "gross_weight"), ("net_wt", "net_weight"),
                      ("ctn", "carton_count")):
        printed = res.printed_totals.get(key)
        if not printed:
            continue
        # SAME locale as the rows: the totals row prints in the same numeric
        # convention as the data above it, and parsing it locale-blind read the
        # EU "12,000" as twelve thousand and stood the parser down for nothing.
        total = parse_decimal(printed[0],
                              locale=locales.get(printed[2]) if len(printed) > 2 else None)
        if total is None or total <= 0:
            continue
        got = Decimal("0")
        seen = False
        for r in rows:
            raw = getattr(r, attr, None)
            if raw is None or not raw.value_raw:
                continue
            val = parse_decimal(raw.value_raw, locale=locales.get(r.source_page_no))
            if val is not None:
                got += val
                seen = True
        if not seen:
            continue
        if abs(got - total) > max(total * _TOTAL_TOLERANCE, Decimal("0.05")):
            res.notes.append(f"parsed {key} sum {got} does not match the printed total {total}")
            return ParseResult(notes=res.notes)      # stand down: the map is wrong
        res.notes.append(f"parsed {key} sum {got} matches the printed total {total}")
    return res


def _realign_short_row(cells: list[str], mapping: dict, n_cols: int,
                       locale: str | None) -> list[str] | None:
    """Put a row's cells back under the right headers, PROVED by its own numbers.

    A packing row often prints a blank where a carton range or a shipping mark
    repeats from the line above, and OCR drops the empty cell rather than
    emitting it — so six cells arrive against an eight-column header and every
    column past the gap reads its neighbour.  Left alone that is silent: a
    quantity becomes a carton count (200 cartons on a row that packs 10).
    Disowning the page to the LLM is safe but expensive — one ragged row sends
    all 37 rows of a packing list through model calls that take orders of
    magnitude longer than the 13 ms the deterministic parse costs.

    So the gap is located instead of guessed.  Exactly one blank is inserted,
    at each interior position in turn, and a placement is accepted ONLY when
    the row's own arithmetic proves it: ``qty per package x packages ==
    quantity``.  If no placement satisfies it, or more than one does, the
    answer is None and the ordinary suspicious-leftover path takes over — a
    realignment that is not proved is a guess, and a guess is what this whole
    module exists to avoid.
    """
    rate_i, ctn_i, qty_i = (mapping.get("qty_per_pkg"), mapping.get("ctn"),
                            mapping.get("qty"))
    if not all(isinstance(v, int) for v in (rate_i, ctn_i, qty_i)):
        return None                          # no identity to prove anything with
    if not 0 < n_cols - len(cells) <= 2:
        return None                          # more than a dropped cell or two

    def proves(cand: list[str]) -> bool:
        # The arithmetic alone does not pick a unique placement: inserting the
        # blank before or after the description leaves every NUMBER in the same
        # column.  The text columns are what break the tie — the description
        # must still hold words, and a mapped unit column must still hold a
        # unit — so all three signals have to agree before a row is realigned.
        di = mapping.get("desc")
        if isinstance(di, int):
            if di >= len(cand) or not re.search(r"[A-Za-z]{2}", cand[di] or ""):
                return False
        ui = mapping.get("uom")
        if isinstance(ui, int) and ui < len(cand) and (cand[ui] or "").strip():
            if not _UOM_CELL.fullmatch(fold_header(cand[ui].strip())):
                return False
        try:
            rate = parse_decimal(cand[rate_i], locale=locale)
            ctn = parse_decimal(cand[ctn_i], locale=locale)
            qty = parse_decimal(cand[qty_i], locale=locale)
        except IndexError:
            return False
        if None in (rate, ctn, qty) or rate <= 0 or ctn <= 0 or qty <= 0:
            return False
        return q2(rate * ctn) == q2(qty)

    hits = []
    for at in range(1, len(cells) + 1):       # never before the identity cell
        cand = cells[:at] + [""] + cells[at:]
        cand += [""] * (n_cols - len(cand))
        if len(cand) == n_cols and proves(cand):
            hits.append(cand)
    return hits[0] if len(hits) == 1 else None


def _shifts_a_value_column(cells: list[str], mapping: dict, n_cols: int) -> bool:
    """Would a SHORT row misread one of the columns that carry values?

    A row can be short two ways.  Trailing empties trimmed by OCR leave every
    remaining column correctly aligned — harmless.  A cell dropped from the
    MIDDLE shifts everything after it, and on a packing list that silently
    turns a quantity into a carton count.

    They are told apart by what lands where: if a column mapped to carry a
    NUMBER (cartons, quantity, weight) holds a non-numeric cell, or the unit
    column has fallen off the end while a value column still reads, the row is
    shifted rather than trimmed.
    """
    value_keys = ("ctn", "qty", "gross_wt", "net_wt", "wt_any")
    present = [k for k in value_keys
               if isinstance(mapping.get(k), int) and mapping[k] < len(cells)]
    if not present:
        return True                         # nothing readable lines up at all
    for k in present:
        cell = (cells[mapping[k]] or "").strip()
        if cell and not re.match(r"^[\d]", cell):
            return True                     # a value column holding a word
    ui = mapping.get("uom")
    if isinstance(ui, int) and ui >= len(cells):
        # the unit fell off the end while value columns still read — the row
        # is short by a cell somewhere before it, not merely trimmed
        return any(isinstance(mapping.get(k), int) and mapping[k] < len(cells)
                   and (cells[mapping[k]] or "").strip() for k in value_keys)
    return False


def _parse_with(role: DeclaredRole, ocr_pages: dict[int, str],
                locales: dict[int, str | None],
                initial_mapping: dict | None,
                column_roles: dict | None = None) -> ParseResult:
    packing = role == DeclaredRole.PACKING_LIST
    confirm = _confirm_packing_row if packing else _confirm_invoice_row
    mapping: dict | None = initial_mapping
    # A remembered layout carries the column COUNT of the table it was learned
    # from, and may only be applied to rows of that width.  `n_cols` was
    # recorded from the first day and never once read, so a 6-column Medtronic
    # map ran over a 7-column table and every column after the description was
    # off by one.  A width mismatch does not skip the line — the row simply
    # fails to confirm and the ordinary suspicious-leftover path disowns the
    # page to the LLM, which is the safe outcome.
    memory_n_cols = (initial_mapping or {}).get("n_cols")
    if not isinstance(memory_n_cols, int) or memory_n_cols <= 0:
        memory_n_cols = None
    out: dict[int, PageParse] = {}
    first_mapping: dict | None = None
    signature: str | None = None
    totals: dict = {}
    notes: list[str] = []
    money_audit: list = []                # invoice rows' (row, amount, taxable)

    for page_no in sorted(ocr_pages):
        text = ocr_pages[page_no] or ""
        locale = locales.get(page_no)
        pp = PageParse(page_no=page_no)
        lines = text.splitlines()
        # Where this page's OWN first table header prints, if it reprints one.
        # Lines ABOVE it are page furniture (order/terms/address blocks), never
        # rows of a table whose header starts below them — but with a column
        # map persisting from the previous page they used to reach the
        # suspicious check, and one codey furniture token ("…@60days", live
        # Medtronic job) then disowned every page after the first to the LLM.
        own_header_at = None
        for j, line in enumerate(lines):
            if line.count("|") < 3:
                continue
            jcells = _cells(line)
            if (not _is_separator(jcells) and any(jcells)
                    and _header_map(jcells, need_qty=not packing)):
                own_header_at = j
                break
        for j, line in enumerate(lines):
            if line.count("|") < 3:
                continue
            cells = _cells(line)
            if _is_separator(cells) or not any(cells):
                continue
            hdr = _header_map(cells, need_qty=not packing)
            if hdr and column_roles:
                # A caller-supplied column role OVERRIDES the header reading for
                # the keys it names.  Only ever reached after the printed-totals
                # gate has already rejected the header's own reading, and the
                # result is put through that same gate again — so an override
                # that does not reconcile is discarded, whatever proposed it.
                hdr = dict(hdr)
                for key, i in column_roles.items():
                    if isinstance(i, int) and 0 <= i < len(cells):
                        for other in [k for k, v in hdr.items() if v == i and k != key]:
                            hdr.pop(other, None)
                        hdr[key] = i
                    elif i is None:
                        hdr.pop(key, None)
            if hdr:
                mapping = hdr
                if first_mapping is None:
                    first_mapping = hdr
                    signature = "|".join(re.sub(r"[^A-Z0-9/]", "", c.upper()) for c in cells if c)
                continue
            # A totals line is the document summarizing itself.  Skipping it
            # before the suspicious-leftover count matters: a totals row carries
            # numbers and a UOM, so counting it as an unexplained leftover sent
            # otherwise perfectly parsed pages to the LLM.
            if _is_totals_line(cells, mapping):
                if packing and mapping is not None:
                    _absorb_totals_row(cells, mapping, totals, page_no)
                continue
            first = next((c for c in cells if c), "")
            if _BANNER.match(first):
                continue
            if mapping is None:
                continue
            hdr_n_cols = (mapping or {}).get("n_cols")
            if memory_n_cols is not None and len(cells) != memory_n_cols:
                row = None                 # remembered map, wrong table width
            elif (packing and isinstance(hdr_n_cols, int) and len(cells) < hdr_n_cols
                  and _shifts_a_value_column(cells, mapping, hdr_n_cols)
                  and (fixed := _realign_short_row(cells, mapping, hdr_n_cols, locale))):
                # the gap was located and PROVED by the row's own arithmetic
                notes.append(f"p{page_no} row {len(pp.rows) + 1}: a dropped cell shifted this "
                             f"row's columns; realigned and proved by "
                             f"qty-per-package x packages == quantity")
                row = confirm(fixed, mapping, page_no, len(pp.rows) + 1, line, locale,
                              notes=notes)
            elif (packing and isinstance(hdr_n_cols, int) and len(cells) < hdr_n_cols
                  and _shifts_a_value_column(cells, mapping, hdr_n_cols)):
                # A packing row NARROWER than its header has lost a cell, and
                # every column after the gap is reading its neighbour.  Live
                # 2026-08-04: four rows whose blank CTN NO cell the OCR dropped
                # read the TOTAL QTY column as the carton count — 200 cartons
                # where the row packs 10, and 630 against a printed 60.  A
                # packing row has no arithmetic to catch that, so the row does
                # not confirm and the page goes to the LLM.
                row = None
            elif packing:
                row = confirm(cells, mapping, page_no, len(pp.rows) + 1, line, locale,
                              notes=notes)
            else:
                row = confirm(cells, mapping, page_no, len(pp.rows) + 1, line, locale,
                              notes=notes, money_audit=money_audit,
                              strict_arithmetic=initial_mapping is not None)
            if row is not None:
                pp.rows.append(row)
            elif _suspicious(cells):
                # pre-header furniture is only ever excused when it could not
                # possibly be a goods row: no qty|UOM pair. A real row above
                # the header (mixed layouts) still disowns the page.
                if (own_header_at is not None and j < own_header_at
                        and qty_uom_cell_at(cells) is None):
                    continue
                if _valueless_fragment(cells, mapping):
                    notes.append(f"p{page_no}: value-less continuation fragment skipped "
                                 f"(no quantity or amount printed): {line.strip()[:90]!r}")
                    continue
                pp.suspicious_leftover += 1
        pp.confirmed = bool(pp.rows) and pp.suspicious_leftover == 0
        out[page_no] = pp

    if packing:
        _absorb_inline_totals(ocr_pages, totals)
        _mark_shared_cartons(out)
    else:
        _resolve_taxable_column(money_audit, notes)
    if signature is None and initial_mapping is None:
        return ParseResult()                       # no map at all — stand down
    return ParseResult(pages=out, mapping=first_mapping or initial_mapping,
                       header_signature=signature, printed_totals=totals, notes=notes)
