# Sample data — synthetic, and kept that way on purpose

Everything in this directory describes a shipment that does not exist.

It did not always. Until the round-2 security review, these files were a real
Nepali import declaration: a named importer with its government EXIM/VAT
registration, a named exporter, a named customs broker with a home address and a
personal PAN, the issuing bank and its LC reference, both waybill numbers, and
the full 119-line goods breakdown with prices. All of it was tracked in git and
pushed to the remote — and the five PDFs were not pictures, they carried
extractable text layers, so `pypdf.extract_text()` returned the party details in
one call.

## What replaced it

| Field | Value now |
|---|---|
| Exporter | `NORTHWIND TRADING LTD., UNIT 12 EXAMPLE INDUSTRIAL PARK, CHINA` |
| Consignee | `Demo Importers Pvt.Ltd, Lalitpur` |
| Consignee EXIM/VAT | `1000000000001NP` |
| Declarant | `Demo Declarant`, PAN `900000001` |
| Issuing bank (name) | `Example Bank International` |
| LC reference | `LCDEMO0000000001` |
| MAWB / HAWB | `999-00000000` / `DEMOHAWB0057` |
| Invoice / packing list no. | `DEMO-209-1`, `DEMO-209-2` |
| Carrier / forwarder | `Demo Air Cargo` / `Example Cargo Logistics` |
| Insurer | `Example Insurance Company Limited` |

## Two things that are deliberately NOT synthetic

**`CTZNNPKAXXX`** — the sender BIC in `fixtures/banking.json` is a real, publicly
published bank identifier, kept because the critical review resolves the bank
code against the official reference table in `backend/reference_data` and an
invented code would not resolve. A BIC on its own names a bank, not a customer,
and every party and reference number around it is now invented, so it links to
nobody.

**The line-item quantities, weights and prices** are the original numbers. They
are what `test_apportion_and_golden_weights.py` asserts against
(`net = 0.7 × gross`, 119 items, 199.00 kg total), and re-basing them would mean
regenerating the golden reference the allocation rewrite is measured by. With
every party, invoice number and LC reference invented, the figures are an
unattributable price list rather than a named importer's landed cost. Re-basing
them is still worth doing; it is a separate change with the golden test as its
acceptance check.

## Regenerating the PDFs

The five PDFs are generated from the JSON fixtures beside them, so the input is
reviewable text rather than an opaque binary somebody has to trust:

```bash
python backend/scripts/generate_sample_documents.py
```

Every page is stamped `SYNTHETIC DEMO DOCUMENT, NOT A REAL SHIPMENT`, and
`tests/test_repo_hygiene.py::test_sample_documents_are_generated_not_real`
fails if a PDF in here is missing that stamp. That test is the reason a real
scan cannot quietly be copied back in for convenience.

## If you need a real document to reproduce a bug

Put it in `backend/storage/` (already git-ignored) and reference it from a local
scratch test. Never add it here, and never commit it: the repository history
keeps a blob even after a later delete, and removing one takes a history
rewrite plus a force-push of every branch.
