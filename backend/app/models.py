"""ORM models for the operational store.

Deliberately boring: server-owned IDs, immutable upload metadata, versioned
OCR / extraction / declaration payloads stored as JSON.  Nothing here trusts
model output — the LLM never writes a job_id, storage_key or role.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, ForeignKey, Index, Integer, LargeBinary, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "customs_job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(40), default="UPLOADING")
    version: Mapped[int] = mapped_column(Integer, default=1)
    rule_set_version: Mapped[str] = mapped_column(String(40), default="")
    exchange_rate: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[str] = mapped_column(String(120), default="")
    # WHO MAY SEE THIS JOB.  Deliberately not `created_by`: that column is
    # audit ("who made it"), and overloading one field for both audit and
    # authorization is how a later ownership transfer silently rewrites
    # history, or a history fix silently grants access.
    #
    # Holds the signed-in principal.  Today that is the single configured
    # operator's username; when accounts move to Supabase it becomes that
    # user's id, which is also a string — so the seam does not have to be cut
    # twice.  Empty means a job created before ownership existed; see
    # services.job_visible_to for the one place that decides what that means.
    owner_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Latest computed artefacts (JSON) — see pipeline.
    critical_review: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    declaration: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Reviewer add/delete overlay (review.item_mutations) — evidence stays immutable.
    item_mutations: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Durable per-job regime/office/transport selections
    # ({"revision": n, "values": {...}}) — seed every Critical Review recompute.
    review_selections: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Content-keyed HS history (user rule 2026-08-02): normalized item name ->
    # previously reviewer-confirmed HS selection, folded out of the positional
    # overlay whenever invoice evidence changes.  Feeds the resolver's HISTORY
    # cascade as a low-confidence proposal — never re-applied blindly as final.
    hs_history: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    documents: Mapped[list["Document"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    events: Mapped[list["AuditEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "uploaded_document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declared_role: Mapped[str] = mapped_column(String(40))
    upload_index_within_role: Mapped[int] = mapped_column(Integer, default=0)
    original_file_name: Mapped[str] = mapped_column(String(400))
    content_type: Mapped[str] = mapped_column(String(120), default="application/pdf")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_key: Mapped[str] = mapped_column(String(400), default="")
    status: Mapped[str] = mapped_column(String(40), default="UPLOADED")
    # Where raw_extraction came from (domain.enums.ExtractionProvenance).  The
    # row is the only place that can answer it afterwards: a fixture-seeded
    # extraction and an OCR'd one are the same JSON in the same column, and
    # "these values were never read off the document" is exactly what an audit
    # of a declaration needs to be able to establish.  Defaults to OCR, which
    # is what every row written before this column existed was.
    extraction_provenance: Mapped[str] = mapped_column(String(24), default="OCR",
                                                       server_default="OCR")

    ocr: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw_extraction: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    role_match: Mapped[bool | None] = mapped_column(nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="documents")


class XmlArtifact(Base):
    __tablename__ = "xml_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declaration_version: Mapped[int] = mapped_column(Integer, default=1)
    template_version: Mapped[str] = mapped_column(String(40), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    xml_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class BmsArtifact(Base):
    """The per-item brand/model/size ``.xls`` produced alongside each XML build
    (same declaration version).  Export-only sibling of :class:`XmlArtifact`."""

    __tablename__ = "bms_artifact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    declaration_version: Mapped[int] = mapped_column(Integer, default=1)
    template_version: Mapped[str] = mapped_column(String(40), default="")
    checksum: Mapped[str] = mapped_column(String(64), default="")
    xls_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageEvent(Base):
    """One billable vendor call, attributed to the user who caused it.

    Every extraction spends real money at Mistral and OpenAI, and nothing
    recorded who spent it.  The token counts existed — the extractor has always
    accumulated them — but only to be written to a log line and thrown away, so
    the questions that matter at 1500 users ("what does a job cost", "which
    account is burning the budget", "is anyone abusing this") had no answer.

    UNITS ARE AUTHORITATIVE, COST IS AN ESTIMATE.  The token and page counts
    come from the vendor's own response and are always recorded.  The money
    column is computed from a rate table this deployment configures, is NULL
    when no rate is known for that model, and is stamped with the rate version
    that produced it — so a later price change cannot silently rewrite what
    last month appeared to cost.

    Deliberately NOT a ledger.  It records what was spent, never what is owed;
    a credits/billing ledger is a separate thing with different integrity rules
    (it has to be append-only and reconcilable against payments), and conflating
    measurement with money owed is how the two end up disagreeing.
    """

    __tablename__ = "usage_event"
    __table_args__ = (
        # Every read is "this owner, this period" — the rollup and any future
        # quota check both scan exactly this.
        Index("ix_usage_event_owner_created", "owner_key", "created_at"),
        Index("ix_usage_event_job", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # WHOSE spend.  Job.owner_key, copied at the time of the call rather than
    # joined on demand: reassigning a job later must not move last month's cost
    # onto a different account's total.
    owner_key: Mapped[str] = mapped_column(String(160), default="")
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(40))          # openai | mistral
    operation: Mapped[str] = mapped_column(String(40))         # extraction | ocr
    model: Mapped[str] = mapped_column(String(80), default="")
    # How many vendor calls this row aggregates (an extraction is many windows).
    calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Cached prompt tokens are billed at a lower rate and are the single biggest
    # lever on extraction cost, so they are recorded separately rather than
    # folded into prompt_tokens.
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int] = mapped_column(Integer, default=0)     # OCR
    # Decimal-as-string, like Job.exchange_rate: money never touches a float in
    # this codebase.  NULL means "no rate configured for this model" — which is
    # a different fact from zero and must not be reported as free.
    estimated_cost_usd: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rate_version: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, index=True)


class AccountQuota(Base):
    """One account's OWN monthly document cap, when someone has set it.

    Until this existed the cap was a single deployment-wide number
    (``Settings.usage_monthly_document_cap``), which has two consequences the
    moment more than one account can exist: a new account's limit is "the same
    as everybody else's", recorded nowhere, and there is no way to raise ONE
    account's limit without raising everyone's — even though the sentence
    ``metering.quota_exceeded`` already shows the reviewer promises exactly that
    ("or when an administrator raises the limit").  That sentence was untrue.

    ABSENCE IS THE NORMAL STATE, and is deliberate.  A brand-new account gets no
    row and therefore the deployment default.  Writing a row at signup would
    freeze the default at whatever it happened to be on the day each user
    registered, so it could never be changed for existing accounts again; and it
    would make signup depend on a second write that can fail AFTER the identity
    provider has already created the account — a partial state this app cannot
    roll back, because it does not own the account.  A row appears only when
    somebody sets a specific cap.

    ``monthly_document_cap`` is nullable for the same reason the row is
    optional: NULL means "follow the deployment default", which is a different
    fact from 0, and 0 means unlimited.  A row that exists only to carry a
    ``note`` must not silently become a cap of zero.

    Deliberately NOT a ledger and NOT usage.  It records what an account is
    ALLOWED, never what it has spent — ``UsageEvent`` is the measurement, and
    conflating an allowance with a count is how the two end up disagreeing.
    """

    __tablename__ = "account_quota"

    # The same key Job.owner_key holds: the Supabase auth.users.id, or the
    # configured username under the `local` provider.  Primary key outright —
    # an account has one allowance, not a history of them.
    owner_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    # NULL = follow the deployment default.  0 = unlimited, at this level as at
    # the deployment one.
    monthly_document_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Why, for the next administrator who reads it.  A cap with no reason
    # recorded is one nobody can safely change back.
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=_now, onupdate=_now)
    # NULL means no admin route wrote this — it was set out of band, by hand.
    # Which is the only way a row can exist today: the route that sets a cap and
    # names its setter is a later step.  Empty-string-as-unknown would read as a
    # setter whose name was lost; NULL says nobody recorded one.
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AccountRole(Base):
    """One account's PLATFORM role.  A row means ``admin``; absence means member.

    ``docs/ADR-001-identity-and-tenancy.md`` decides what the role is for, and it
    is narrow: an admin manages accounts, quotas and usage METADATA.  An admin
    does not read declarations, documents, extractions, XML artifacts or job
    audit trails, and ``services.job_visible_to`` is deliberately not aware that
    this table exists — a ``role == "admin"`` arm in that predicate is the
    "unowned job is visible to everyone" branch with a different condition, in
    the one function whose entire value is not having a bypass.

    ``admin`` is a PLATFORM role — the service operator — never a firm role.  It
    does not mean "senior broker" or "manages this brokerage's staff"; the system
    has no concept of a brokerage at all.

    WHY HERE AND NOT IN SUPABASE ``app_metadata``.  Writing that requires the
    service-role key, which ``app/auth_supabase.py`` keeps deliberately out of
    this process — so an admin console that grants a role could not exist without
    granting this process control of the whole auth database.  It also splits
    authorization across two systems when ``owner_key`` (the value a role has to
    line up with) is a local column, and the `local` provider has no
    ``app_metadata`` at all, so the local mechanism gets built either way.

    The one genuine advantage of ``app_metadata`` — a user cannot self-promote —
    is kept by the obvious means: this table is read SERVER-SIDE and a role is
    never accepted from a request body, a header or a client-supplied claim.  It
    is also deliberately NOT copied into the session token: a stateless 24h token
    cannot be revoked, and late revocation of a privilege is the direction that
    matters.  One primary-key lookup, on the admin routes only.

    ABSENCE IS THE NORMAL STATE.  No row is written for an ordinary user, so
    there is no signup-time write and no default to migrate.  The first admin is
    inserted out of band (``scripts/grant_admin.py``) — bootstrapping a
    privileged role through a route that must itself be privileged has no
    non-silly answer.
    """

    __tablename__ = "account_role"

    # The same key Job.owner_key holds: the Supabase auth.users.id, or the
    # configured username under the `local` provider.
    owner_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    # Only "admin" is meaningful.  Any other value is read as no role at all
    # (accounts.role_of), so a typo cannot silently grant something.
    role: Mapped[str] = mapped_column(String(20))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # WHO granted it, as a display name — same convention as AuditEvent.actor,
    # because this column exists to be read by the next administrator.  NULL
    # means nobody recorded a grantor, which is what the bootstrap row is when
    # it is inserted by hand.
    granted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class AccountDisabled(Base):
    """The local deny-list: a row means this account may not sign in.

    Local, and not a Supabase user-disable, for the same reason the role is
    local — disabling a Supabase user needs the service-role key this process
    deliberately does not hold.

    WHAT THIS DOES NOT DO, stated here because the gap is the interesting part.
    Sessions are stateless HMAC tokens with a 24h lifetime, so a row written here
    stops the NEXT sign-in, not the session already in someone's browser.  Two
    places therefore consult it besides login: the extraction route, which is the
    one route that is about to spend real money at Mistral and OpenAI, and
    ``require_admin``.  Every other route keeps working until the token expires.
    Closing that window completely would mean a database read on every request
    for all 1500 reviewers, which is the cost the role deliberately avoids; the
    remedy for an urgent revocation remains rotating ``EASYCUSTOMS_AUTH_SECRET``,
    which ends every open session at once.
    """

    __tablename__ = "account_disabled"

    owner_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    # Why, for the administrator who has to decide whether to lift it.
    reason: Mapped[str] = mapped_column(Text, default="")
    disabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    disabled_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class LoginAttempt(Base):
    """One FAILED sign-in, kept only long enough to rate-limit the next one.

    The throttle was a process-local dict, which is two different bugs at once
    on a real deployment.  With N API processes it allowed N times the intended
    guess rate, because each kept its own count.  And behind a load balancer
    every request appears to come from the balancer, so all callers shared ONE
    bucket: ten failures from anywhere locked out every user of the system, and
    an unauthenticated attacker could do that deliberately.  A shared store
    fixes the first; ``auth.client_key`` (trusted_proxy_hops) fixes the second,
    and neither is any use without the other.

    Successful sign-ins delete the caller's rows, and anything older than the
    window is pruned on write — this table is a short sliding window, never a
    login history.  It is deliberately NOT an audit record: `AuditEvent` is
    where anything durable belongs.
    """

    __tablename__ = "login_attempt"
    __table_args__ = (Index("ix_login_attempt_client_created", "client_key", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # An IP address — 45 characters covers IPv6, including a v4-mapped form.
    client_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SignupAttempt(Base):
    """One ACCEPTED self-service signup, kept long enough to rate-limit the next.

    A SEPARATE TABLE FROM ``LoginAttempt``, and that separation is the control
    rather than tidiness.  The login throttle counts FAILURES and a correct
    password deletes that caller's rows (``auth.clear_failures``); signup abuse is
    a stream of SUCCESSES.  Sharing one table — even with a ``kind`` column —
    means one of two things happens: a successful signup clears the caller's
    password-guessing budget, so an attacker interleaves a registration between
    guesses and guesses for ever; or the two windows share a pruning delete and a
    24h signup window is silently truncated to the 5-minute failure window.  The
    suite's own ``auth.reset_throttle`` is a third: it empties the login window
    between every test, and a shared table would empty this one with it.

    WHAT IT COUNTS is a signup this app ACCEPTED, which is a superset of accounts
    created and never fewer.  With email confirmation on, Supabase deliberately
    answers an already-registered address exactly as it answers a new one — that
    is its anti-enumeration behaviour and this app keeps it — so "an account was
    created" is not knowable here.  Counting the accepted request is the closest
    honest thing, and it errs towards limiting more, not less.

    Deliberately NOT an audit trail of who registered: no email, no user id, no
    outcome.  It holds a throttle key and a timestamp, is pruned to its longest
    window on every write, and answers exactly one question — may this caller
    register again yet.
    """

    __tablename__ = "signup_attempt"
    __table_args__ = (Index("ix_signup_attempt_client_created", "client_key", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # An IP address, from `auth.client_key` — the same key the login throttle
    # uses, so EASYCUSTOMS_TRUSTED_PROXY_HOPS governs both and cannot be right
    # for one and wrong for the other.
    client_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AccountSeen(Base):
    """An account that has SIGNED IN to this deployment at least once.

    Written on a successful sign-in, and this is the only column in the app that
    holds an email address.  It exists because step 2's admin listing is the union
    of the ``owner_key`` values in the app's own tables, which answers "who is
    spending the budget" and cannot answer either of the two questions self-service
    registration creates: *did this person get in?* and *which UUID is
    alice@broker.np, so I can raise her cap?*  Neither is answerable from
    ``AuditEvent.actor``, which only names an account that has already created a
    job and done something to it.

    NOT WRITTEN AT SIGNUP, for a reason that survives being tempting.  With email
    confirmation on, Supabase answers an already-registered address with an
    obfuscated user object carrying an id that belongs to nobody; a row written
    from that response would key on a fabricated id, and a stranger could fill
    this table with them.  A sign-in is proof: the password verified and the id
    came from a real session.  The cost is stated rather than hidden — an account
    that registered and never confirmed its email is NOT here, which is the same
    fact as "the confirmation link was never followed".

    METADATA, never content.  ``docs/ADR-001-identity-and-tenancy.md`` lets an
    admin see accounts, quotas and usage and nothing else; an email is account
    metadata and is already in this database on every audit row a human action
    wrote.  What changes is that it becomes ANSWERABLE per account instead of
    being derived best-effort from an audit trail.  ``services.job_visible_to``
    does not know this table exists.
    """

    __tablename__ = "account_seen"

    # The same key Job.owner_key holds: the Supabase auth.users.id, or the
    # configured username under the `local` provider.
    owner_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    # What they signed in AS — an email under supabase.  A display label, never
    # an identity: ownership is the owner_key, and this column changes when the
    # account's email does.  Empty rather than NULL: a sign-in always knows the
    # name it was given, so "nothing recorded" is not a state this can be in.
    display: Mapped[str] = mapped_column(String(160), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorLayout(Base):
    """A remembered table column map, keyed by role + header signature.

    Was ``storage/vendor_layouts.json``, and the file is why this table exists.
    Both stores were whole-file read-modify-write: two processes that recorded a
    layout at the same time each loaded the document, each edited its own copy,
    and the second `os.replace` discarded everything the first had learned.
    Atomic on ONE writer, lossy on two — so the file was a reason the app could
    not run more than one process, not merely a slower way to store this.

    A row per key makes concurrent writers independent, and the read-modify-write
    that remains (counters, `max` of confirmed_rows) is done under a row lock.
    """

    __tablename__ = "vendor_layout"
    __table_args__ = (UniqueConstraint("role", "header_signature",
                                       name="uq_vendor_layout_role_signature"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    role: Mapped[str] = mapped_column(String(40), index=True)
    # Never "POSITIONAL" and never empty: layout_memory.record_layout refuses to
    # write a layout that did not come from the document's own header, because
    # such an entry matches every headerless document of the role and confirms
    # itself.  The column is not nullable so that rule cannot be bypassed here.
    header_signature: Mapped[str] = mapped_column(String(255))
    mapping: Mapped[dict] = mapped_column(JSON)
    vendor_hint: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_rows: Mapped[int] = mapped_column(Integer, default=0)
    docs: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class VendorFieldProfile(Base):
    """Learned per-vendor field defaults — today only the default COO.

    Was ``storage/vendor_field_profiles.json``; see :class:`VendorLayout` for why
    a file could not survive a second process.  ``vendor`` is the normalised key
    (``field_profiles._norm_vendor``), so it is the primary key outright.
    """

    __tablename__ = "vendor_field_profile"

    vendor: Mapped[str] = mapped_column(String(160), primary_key=True)
    display: Mapped[str] = mapped_column(String(120), default="")
    coo_default: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # REVIEWER (a deliberate decision) outranks OBSERVED (agreeing finalized
    # jobs) — the state machine that depends on this lives in field_profiles.
    coo_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coo_docs: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("customs_job.id"))
    actor: Mapped[str] = mapped_column(String(120), default="system")
    event_code: Mapped[str] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    job: Mapped[Job] = relationship(back_populates="events")
