Redesign the UI of Easy Customs using the ui-ux-pro-max skill. Read
frontend/index.html first — the entire frontend is that one 4,932-line file.

PRODUCT
Easy Customs turns shipment documents (commercial invoice, packing list,
air waybill, banking/LC) into a Nepal ASYCUDA World SAD import declaration
XML. Users are licensed customs brokers and clearing agents doing
high-volume, high-stakes, repetitive document review under time pressure.
This is a professional operator console / document-review workspace — NOT
a landing page, NOT a marketing site. Product type: internal tool +
data-dense dashboard.

SCREENS TO COVER
1. Login (single account, 24h session)
2. Jobs dashboard — table of every declaration job, status pills, 8s poll
3. The 4-step workspace, one card at a time behind a fixed 216px dark left
   rail: (1) Upload role-specific documents, (2) Critical Review of
   declaration control values across 8 sections, (3) Detailed Review of up
   to ~200 item rows in an editable grid with HS-code search, (4) Finalize
   & generate XML
4. Decision queue — open questions the reviewer answers in place
5. Split-pane document viewer with page-level evidence links
6. Audit trail

ENTRY FLOW — the one behaviour change I want
Today the app lands on the jobs dashboard: `view` defaults to "jobs"
(around line 2405, `if(id) openJob(id); else setView("jobs")`). Change it
so a broker ALWAYS lands on a new empty job, ready to upload — step 1 of
the workspace, not the jobs list. Signing in, or opening the app with no
job id, should put the upload box in front of them immediately.

Two things this must not cost me:
- Do NOT flood the server with empty jobs. `newJob()` (line 2320) POSTs a
  real row to /api/jobs. Landing on it unconditionally would create a
  throwaway job on every sign-in, reload and stray tab, and the jobs table
  would fill with empties. Either defer creation until the first file is
  actually picked, or reuse the most recent untouched empty job for this
  user instead of minting another. Tell me which you chose and why.
- The jobs dashboard must stay one obvious click away, and reopening a
  specific job by id must keep working exactly as it does now.

HARD CONSTRAINTS — do not violate any of these
- NO build step, NO npm, NO bundler, NO Tailwind, NO CSS framework, NO new
  CDN dependency. Everything must stay: React 18 UMD + Babel-standalone +
  hand-written CSS in the single <style> block, driven by :root custom
  properties. Deliver the redesign as changes to those CSS variables and
  rules, plus className changes where genuinely needed.
- The script block at the end of <body> is presentation-only and reads the
  rendered DOM to build the rail, facts strip and Next bar. It must never
  mutate nodes inside #root; navigation stays data-ec-goto delegation.
  Keep that contract.
- Semantic status colours are regulatory, not decorative: --block (a
  blocking validation failure), --warn, --ok. Never let a theme override
  their meaning, and never encode status by colour alone — every state
  needs a text or shape cue too.
- Apart from the ENTRY FLOW change above, change nothing about behaviour:
  no React handler, request, field, validation rule or declaration box may
  be renamed, reordered or removed. Everything else is presentation only.
- Keep Alt+1…4 step jumping and full keyboard operability.
- Base font is currently 13px because the grids are dense. Do not inflate
  to 16px body if it breaks the item grid — solve density properly instead.

DESIGN DIRECTION
Run --design-system with --density 9 (dense operator console), --motion 2
(subtle micro-interactions only; this is a compliance tool, no
choreography), --variance 3 (restrained, structured, not brutalist).

The --design-system generator is landing-page biased — tested on this
product it returned a hero/CTA "Operations Landing" pattern and an
"Exaggerated Minimalism" style whose own best-for field reads fashion,
portfolios and luxury brands. Ignore any hero, CTA, testimonial, pricing
or trial-signup section it proposes: this app has no landing page. Treat
its PATTERN block as not applicable and take only the colour, typography,
spacing and effects guidance. Cross-check everything against --domain web
(that is where the app-interface rule set lives — "app-interface" is not a
valid --domain value) and --domain ux, which are the relevant databases
for a dense internal console.

Do not invert the app to a dark shell — the generator proposed #020617 as
a background. The light workspace (#eef0f4) with a dark rail (#14181f) is
deliberate; propose changes within that structure unless you can argue
otherwise.

Evaluate one specific conflict: --brand and --block are currently the same
value (#b91c1c), so the brand colour and the blocking-error colour are
indistinguishable. Recommend a fix.

WORKFLOW I WANT
Phase 1 (now): generate the design system with --design-system --persist
-p "Easy Customs" --output-dir "F:\cld-easy-customs-xml-v2.0.1", plus a
page override for the item grid. Then audit the current UI against the
skill's priority 1-10 rule categories — especially accessibility, forms &
feedback, and charts/data — and give me a findings list ranked by impact,
each tied to a concrete line in frontend/index.html. Include your
recommendation for the ENTRY FLOW change.
STOP THERE. Show me the design system and the audit. Write no UI code
until I approve.

Phase 2 (after I approve): apply it screen by screen, smallest blast
radius first, verifying each in the browser preview before moving on.
