# ADR-001 — The user is the tenant

- **Status**: Accepted
- **Date**: 2026-08-12
- **Supersedes**: nothing. This records a decision the code already implements.
- **Related**: `app/services.py` (`job_visible_to`, `list_jobs`), `app/metering.py`
  (`quota_exceeded`), `app/models.py` (`Job.owner_key`), `backend/tests/test_tenant_isolation.py`

## Decision

**The account is the tenant. There is no layer above it.**

One signed-in user owns their declarations, carries their own monthly document
quota, and pays their own bill. There are no organizations, no firms, no
memberships and no shared visibility. Two brokers at the same brokerage are two
tenants who cannot see each other's work, exactly as two brokers at competing
brokerages are.

Roles are `admin` and `member`, and **`admin` is a platform role — the service
operator — not a firm role.** It does not mean "senior broker" or "manages this
brokerage's staff". Nobody inside a customer's business has any elevated
standing over anybody else inside it, because the system has no concept of
"inside a business" at all.

`Job.owner_key` holds the Supabase `auth.users.id` (the configured username
under the `local` provider), and it is the *only* access key. It is deliberately
not `Job.created_by`, which is the audit label: conflating "who did this" with
"who may see this" means a later ownership transfer silently rewrites history,
or a history correction silently grants access.

## What was rejected, and why

**Firm-level tenancy**: an `organization` row, users as members of it, jobs owned
by the organization, shared visibility within it, and one pooled quota the firm
buys and its staff draw down.

Rejected for four reasons, in descending order of weight:

1. **It is the wrong default for the data.** A declaration is built from a
   commercial invoice belonging to a *third* party — the importer, who is the
   broker's client and is not a user of this system at all. Their supplier
   relationships, unit prices and party details are commercially sensitive to
   *them*. Sharing that by default across everyone who happens to share an
   employer is a disclosure decision made on the importer's behalf by people who
   never asked them. Per-user is the setting from which sharing can be granted;
   firm-wide is a setting from which it cannot be un-granted.

2. **The failure mode is silent and unbounded.** Per-user isolation fails closed:
   the worst outcome is a broker who cannot see their own colleague's job and
   says so out loud. Firm-level visibility fails open: a mis-set `organization_id`
   on signup, or a membership row that outlives an employment, exposes a whole
   firm's book of business and nothing in the product surfaces it. We have
   already had one instance of the open-failing shape here — `job_visible_to`
   once returned `True` for an unowned job, which was correct with exactly one
   account and became "everyone sees every unowned job" the moment a second could
   exist.

3. **Nothing has asked for it.** No sharing, hand-off, review-queue or
   supervisor-approval workflow exists in the product. Building the membership
   model before the workflow that needs it means guessing at the shape of
   sharing — per-job? per-client-importer? read vs. write? — and being wrong in
   schema, which is the expensive place to be wrong.

4. **It is additive later.** `owner_key` is a single string column read through a
   single predicate. An `organization` concept arrives as a *widening* of that
   predicate (`owner_key == me OR owner_key IN (my org's members)`) in one
   function, plus a migration that backfills. Going the other way — retracting
   firm-wide visibility once brokers rely on it — is a product regression that
   breaks live workflows.

**A pooled firm quota** was rejected with it, and separately: metering attributes
every vendor call to the `owner_key` of the job that caused it
(`metering.record`, via `services._owner_of_job`). A pooled quota needs an
aggregation key that does not exist and a billing relationship with an entity
that has no row. Per-user metering is what is measured today, so it is what can
honestly be billed today.

## What already implements this

| Requirement | Implementation |
| --- | --- |
| A job belongs to exactly one account | `Job.owner_key`, set only from a named principal in `services.create_job`; never defaulted from `actor` |
| One place decides visibility | `services.job_visible_to` — `bool(owner_key) and owner_key == principal` |
| A job you may not see is indistinguishable from one that does not exist | `services.get_job` returns `None`, routes answer 404 rather than 403 |
| The dashboard listing agrees with the per-job check | `services.list_jobs` mirrors the predicate in SQL; the blank-owner arm was removed from both in the same commit |
| Internal callers cannot accidentally inherit "see everything" | `services.principal_of` returns `None` rather than defaulting; `SYSTEM_PRINCIPAL` must be named explicitly |
| Ownership survives an email change | The token subject is `auth.users.id`, not the display name (`auth.Session.user_id` vs `username`) |
| Quota is per account | `metering.quota_exceeded(db, owner_key)` over `UsageEvent.owner_key` |
| Isolation is Python, not database policy | The backend connects to Postgres as a privileged role and bypasses Supabase RLS entirely — see "Supabase is the identity provider, and only that" in `CLAUDE.md` |

**Known gap at the time of writing.** Two routes —
`GET /api/jobs/{job_id}/xml` and `GET /api/jobs/{job_id}/brand-model-size.xls` —
resolve their artifact by `job_id` alone and never consult `job_visible_to`.
This decision is not what failed; the per-route discipline that carries it is.
`tests/test_tenant_isolation.py` asserts the property across every job-scoped
route, and those two assertions currently fail. Fixing them is a separate change.

## What this costs

These are real costs, accepted knowingly:

- **A broker cannot hand a declaration to a colleague.** If someone goes on
  leave mid-shipment, or a senior reviewer should check a junior's work before
  finalize, the system offers no path. The documents must be re-uploaded under
  the other account and the work redone. There is no transfer, no share, no
  read-only grant.
- **A brokerage cannot buy one bundle for its staff.** Every broker needs their
  own subscription and carries their own cap. A ten-person firm buys ten plans
  and gets ten separate quotas, several of which will be idle while another
  broker is blocked at their limit. This is a commercial disadvantage against
  any competitor selling seats.
- **There is no firm-level view.** A brokerage owner cannot see what their
  business filed this month, or what it spent. Only each individual can, for
  themselves.
- **Offboarding is manual.** When a broker leaves, their declarations leave with
  their account. There is no administrative path to recover them (see the admin
  question below — this is a direct consequence of the position taken there).

The mitigating fact: the XML is a **downloadable artifact**. A broker who needs
to hand work over downloads the XML and the `.xls` and sends them. That is not a
workflow, but it means the data is never trapped.

## Open question — what may a platform admin see?

**Decision: an admin manages accounts and quotas. An admin cannot read customer
declarations, documents, extractions, XML artifacts or audit trails.**

I argued this against the alternative and it holds. The reasoning:

**The data is not ours.** A declaration is assembled from a commercial invoice
belonging to an importer who is not a user of this platform, who has no account,
was never shown a privacy notice by us, and cannot consent to or object to
anything. It carries their supplier, their unit prices and their shipment
values — the material a competitor would most want. The broker was trusted with
it under a professional relationship. We were trusted with it as a *processor*,
incidentally, because the broker chose a tool. A role bit granting the service
operator read access converts an incidental processing relationship into
standing access to every importer's commercial terms in the country. Nobody in
that chain agreed to that.

**It reintroduces the exact bug the code already documents.**
`services.job_visible_to`'s docstring records that the "unowned job is visible to
everyone" branch was a real disclosure. An `if role == "admin": return True`
arm is that branch with a different condition — a single predicate returning
`True` for a class of callers, guarded by one boolean whose correctness now
depends on how that boolean is set, migrated, defaulted, and cached in a token.
The function exists to have exactly one hard-to-get-wrong rule. Adding a bypass
to the one function whose entire value is not having a bypass is a bad trade for
a capability nobody has yet needed.

**"Read every job" is not needed for the jobs an admin actually has.** Enumerated
honestly, the platform-admin tasks are: create/disable an account, reset or
raise a quota, read usage and spend, investigate abuse, and answer "why did my
extraction fail". Every one of those is served by account and usage metadata —
`UsageEvent` rows, `Job.status`, `AuditEvent` codes and timestamps, structured
logs. None requires the *content*: not the invoice, not the extraction, not the
XML. Support can see that job `abc` failed extraction on document 2 at 14:03
with an OCR timeout, which is what actually diagnoses the ticket.

**The counter-argument, and why it loses.** The honest case for admin read
access is support: a user says "the XML is wrong", and diagnosing it without
seeing the declaration is slower. That is true. But it is *slower*, not
impossible — and the price of the faster path is standing access to every
customer's commercial data, held permanently, by a role that will eventually be
held by more than one person and eventually be phished. Trading a permanent
worst-case disclosure for a routine convenience is the wrong direction. The
support case also has a better answer: the user can export and send the XML
themselves, which requires no privilege at all and leaves them in control of
their own disclosure.

**If support access is ever genuinely required**, it must be:

- **Explicit** — the *account holder* grants it, per incident. Not a role bit,
  not a config flag, not a checkbox in an admin console.
- **Time-boxed** — it expires by itself. A grant that must be revoked will not be.
- **Scoped** — to one job, not to an account and not to the platform.
- **Audited on the read** — an `AuditEvent` written when the declaration is
  opened, visible *to the customer*, not only in our logs. An audit trail the
  audited party cannot see is not a control.
- **Implemented as a grant row, not as a predicate branch** — so
  `job_visible_to` gains at most a lookup against an explicit, expiring,
  customer-created record, and never a role test.

Until such a grant exists, the answer is that an admin sees accounts, quotas and
usage, and does not see declarations. `job_visible_to` is not modified by the
admin/member role work.

## Consequences

- The admin/member role gates **account and usage** routes only. It must not
  appear in `job_visible_to`, `get_job` or `list_jobs`.
- Any future sharing feature is a *widening* of `job_visible_to` backed by an
  explicit grant record, and updates this ADR in the same commit.
- Per-user quota is the billing unit. A firm bundle would require pooled
  metering, which does not exist and is not planned.
- Support tooling is built against usage and status metadata. If it is ever
  found insufficient, the answer is better metadata before it is broader access.
