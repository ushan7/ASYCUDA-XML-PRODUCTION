"""Accounts as the PLATFORM sees them: role, deny-list, quota, usage metadata.

This is the whole surface of the admin/member role, and the boundary is the
point.  ``docs/ADR-001-identity-and-tenancy.md`` decides it: **an admin manages
accounts and quotas and cannot read customer declarations.**  Nothing in this
module loads a ``Job``'s content, a ``Document``, an extraction, an XML artifact
or a job's audit trail.  What it reads is counts, statuses, usage rows and the
two tables it owns.

``services.job_visible_to`` is deliberately unaware that any of this exists.  An
``if role == "admin": return True`` arm there is the same shape as the "unowned
job is visible to everybody" branch that function's own docstring records as a
live disclosure — one predicate returning True for a class of callers, guarded by
one boolean whose correctness then depends on how it is set, defaulted, migrated
and cached.  So the role gates account routes and never a job route.

WHAT THIS MODULE CANNOT DO, and it is worth stating before someone tries.  This
app has no user table: identity lives in Supabase and enumerating it needs the
admin API, i.e. the service-role key that ``app/auth_supabase.py`` keeps out of
this process on purpose.  So there is no such thing here as "every account" —
only every account THIS DEPLOYMENT HAS SEEN, which is the union of the
``owner_key`` values in its own tables.

That set grew a member in step 3, and the reason is worth recording.  Until then
it was jobs, usage and the three tables above, so an account that registered and
never uploaded anything was invisible — which was tolerable while accounts were
created by hand and became a support hole the day strangers could register: "did
X sign up?" and "which UUID is alice@broker.np, so I can raise her cap?" were
both unanswerable.  ``AccountSeen`` closes them by recording a SIGN-IN (never a
signup — see the model for why a signup response cannot be trusted to name a real
account).  It is still not "every account": one that registered and never
confirmed its email has never signed in, and is not here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import metering
from .models import (AccountDisabled, AccountQuota, AccountRole, AccountSeen, AuditEvent,
                     Job, UsageEvent)

log = logging.getLogger("easycustoms.accounts")

# The only role that means anything.  Absence of a row — and equally a row
# carrying any other string — is a member, so a typo in the column cannot
# silently grant a privilege.
ADMIN = "admin"
MEMBER = "member"

# Audit actors that are not people, so not display labels either.  `services`
# writes the first for anything no human triggered; `demo` is the seeded job.
_NOT_A_PERSON = ("system", "demo")


# --------------------------------------------------------------------------- #
# Role
# --------------------------------------------------------------------------- #
def role_of(db, owner_key: str) -> str:
    """``ADMIN`` or ``MEMBER``, from the local table and from nowhere else.

    One primary-key lookup.  Never read from a request body, a header, or a
    claim in this app's session token: the token is a stateless HMAC valid for
    24h, so a role stamped into it could not be revoked before it expired — and
    late revocation is the direction that matters for a privilege.  A table read
    is immediate, which is the entire argument for paying for it.

    Deliberately NOT wrapped in a try/except.  A database that cannot answer
    "may this caller administer accounts" must refuse the request, and it does
    that by raising: an except arm here would have to choose between granting on
    a fault and denying on one, and the honest answer is that the question was
    not answered at all.
    """
    if not owner_key:
        # An empty principal is not an account.  `job_visible_to` has the
        # equivalent guard for the same reason: "" is what a caller with no
        # identity looks like, and it must never match a stored row.
        return MEMBER
    row = db.get(AccountRole, owner_key)
    return ADMIN if row is not None and (row.role or "").strip().lower() == ADMIN else MEMBER


def is_admin(db, owner_key: str) -> bool:
    return role_of(db, owner_key) == ADMIN


def grant_admin(db, owner_key: str, *, granted_by: str | None = None) -> AccountRole:
    """Make an account an administrator.

    Reachable from ``scripts/grant_admin.py`` and from no HTTP route, which is
    deliberate: the first admin cannot be granted through a route that itself
    requires an admin, and once that is true out of band there is no reason for
    the privileged path to exist over HTTP at all.  Revoking is the same script.
    """
    owner_key = (owner_key or "").strip()
    if not owner_key:
        raise ValueError("an owner_key is required — a role for nobody is a role for anybody")
    row = db.get(AccountRole, owner_key)
    if row is None:
        row = AccountRole(owner_key=owner_key)
        db.add(row)
    row.role = ADMIN
    row.granted_by = granted_by or row.granted_by
    db.flush()
    return row


def revoke_admin(db, owner_key: str) -> bool:
    """Drop the role row.  True when one was there to drop.

    Takes effect on the account's NEXT request — which is the property the token
    claim was rejected for not having.
    """
    row = db.get(AccountRole, (owner_key or "").strip())
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


# --------------------------------------------------------------------------- #
# Who has actually signed in
# --------------------------------------------------------------------------- #
def record_sign_in(db, owner_key: str, *, display: str = "") -> None:
    """Note that this account signed in, and what it signed in as.

    BEST EFFORT, and it must stay that way: the caller has already proved their
    password, and a sign-in is not worth failing because a metadata upsert lost a
    race with a second tab.  Every failure mode here — a duplicate insert from two
    simultaneous logins, a read-only replica, a missing table on a database
    somebody upgraded halfway — ends in a log line and a successful sign-in.

    Called at SIGN-IN and never at signup.  ``models.AccountSeen`` has the whole
    argument; the short version is that a signup response cannot be trusted to
    name a real account, because the anti-enumeration answer for an
    already-registered address carries an id that belongs to nobody.
    """
    if not owner_key:
        return
    try:
        row = db.get(AccountSeen, owner_key)
        if row is None:
            row = AccountSeen(owner_key=owner_key)
            db.add(row)
        # Truncated to the column rather than trusted: the display name comes from
        # the identity provider, and a value longer than the column is a write
        # that fails on Postgres and silently truncates elsewhere.
        name = (display or "").strip()[:160]
        if name:
            row.display = name
        row.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:
        db.rollback()
        log.warning("could not record a sign-in for %s: %s", owner_key, e)


def sign_ins_of(db, owner_keys) -> dict[str, AccountSeen]:
    """``{owner_key: row}`` for the accounts among those named that have signed
    in — one query, for the same reason :func:`roles_of` exists."""
    keys = [k for k in (owner_keys or []) if k]
    if not keys:
        return {}
    rows = db.scalars(select(AccountSeen).where(AccountSeen.owner_key.in_(keys))).all()
    return {row.owner_key: row for row in rows}


# --------------------------------------------------------------------------- #
# The deny-list
# --------------------------------------------------------------------------- #
def disabled_reason(db, owner_key: str) -> str | None:
    """Why this account is barred, or None.  Empty string reason -> a sentence.

    Consulted at sign-in, on the extraction route (the one route about to spend
    real money at Mistral and OpenAI) and in ``require_admin``.  NOT on every
    request: that would be a database read per request for every reviewer, which
    is the cost the role deliberately avoids, and a session already issued keeps
    working on the other routes until its 24h token expires.  See
    :class:`app.models.AccountDisabled` for that window and the remedy.
    """
    if not owner_key:
        return None
    row = db.get(AccountDisabled, owner_key)
    if row is None:
        return None
    return (row.reason or "").strip() or "This account has been disabled by the operator."


def disable(db, owner_key: str, *, reason: str = "", actor: str | None = None) -> AccountDisabled:
    owner_key = (owner_key or "").strip()
    if not owner_key:
        raise ValueError("an owner_key is required")
    row = db.get(AccountDisabled, owner_key)
    if row is None:
        row = AccountDisabled(owner_key=owner_key)
        db.add(row)
    row.reason = reason or ""
    row.disabled_by = actor or row.disabled_by
    db.flush()
    return row


def enable(db, owner_key: str) -> bool:
    """Lift the bar.  True when the account was actually disabled.

    The inverse exists because a deny-list that can only be added to turns one
    mis-click into a permanently barred paying customer, fixable only with SQL
    against a live database.
    """
    row = db.get(AccountDisabled, (owner_key or "").strip())
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


# --------------------------------------------------------------------------- #
# Quota — the writer `account_quota.updated_by` shipped without
# --------------------------------------------------------------------------- #
def set_quota(db, owner_key: str, cap: int | None, *, note: str = "",
              actor: str | None = None) -> AccountQuota:
    """Set (or clear) one account's monthly document cap, naming who set it.

    ``cap`` follows the column's meaning exactly, and the three values are three
    different answers — collapsing any two of them is the bug this signature
    exists to prevent:

      * ``None``  — follow the deployment default.  The row stays, carrying its
        note, which is why the column is nullable rather than defaulted to 0;
      * ``0``     — unlimited for this account;
      * ``n > 0`` — n documents per calendar month.

    ``updated_by`` is the SETTER'S DISPLAY NAME, matching ``AuditEvent.actor``,
    because the column exists to be read by the next administrator.  The stable
    account id of whoever called this goes on the structured log line instead
    (``logging_setup`` attaches the principal to every record), so both facts are
    recorded in the place each is useful.

    This is the writer step 1 shipped without: until it existed a row could only
    be inserted by hand, which is why ``updated_by`` is nullable — NULL says
    nobody recorded a setter rather than that a setter's name was lost.
    """
    owner_key = (owner_key or "").strip()
    if not owner_key:
        raise ValueError("an owner_key is required — a cap for nobody is a cap for nobody")
    if cap is not None and cap < 0:
        raise ValueError("a cap must be 0 or more (0 means unlimited)")
    row = db.get(AccountQuota, owner_key)
    if row is None:
        row = AccountQuota(owner_key=owner_key)
        db.add(row)
    row.monthly_document_cap = cap
    row.note = note or ""
    row.updated_by = actor
    db.flush()
    return row


# --------------------------------------------------------------------------- #
# The listing.  Metadata only — counts, statuses, usage, allowances.
# --------------------------------------------------------------------------- #
def known_owner_keys(db) -> set[str]:
    """Every account this deployment has SEEN.

    Not "every account": see the module docstring.  The union of the app's own
    tables is the honest set, and it is what quota and abuse work actually needs
    — an account with no jobs and no usage has spent nothing and cannot be over
    a limit.
    """
    stmt = (select(Job.owner_key.label("owner_key")).where(Job.owner_key != "")
            .union(select(UsageEvent.owner_key).where(UsageEvent.owner_key != ""),
                   select(AccountQuota.owner_key).where(AccountQuota.owner_key != ""),
                   select(AccountRole.owner_key).where(AccountRole.owner_key != ""),
                   select(AccountDisabled.owner_key).where(AccountDisabled.owner_key != ""),
                   # Added in step 3: an account that has signed in and done
                   # nothing else is now listed, which is what makes "did X get
                   # in?" answerable once strangers can register.
                   select(AccountSeen.owner_key).where(AccountSeen.owner_key != "")))
    return {key for key in db.execute(stmt).scalars() if key}


def _audit_labels(db, owner_keys: list[str]) -> dict[str, str]:
    """Best-effort human label per account, from the audit trail.

    THE FALLBACK, since step 3.  ``AccountSeen.display`` records what an account
    signed in as and is the better answer where it exists; this one covers every
    account that has not signed in since that table arrived, which on an upgraded
    deployment is all of them until they next sign in.

    ``Job.created_by`` is empty for every job the API creates, and before
    ``AccountSeen`` the display name reached the database in exactly one place:
    ``AuditEvent.actor``, written from the session's display name.  So this label
    is derived, is reported as a label rather than as an identity, and is absent
    for an account whose only rows are usage or a quota.

    Grouped by (owner, actor) so the row count is bounded by the distinct actors
    of the accounts on ONE page — normally one each — rather than by their audit
    history.  Restricted to the page for the same reason.
    """
    if not owner_keys:
        return {}
    rows = db.execute(
        select(Job.owner_key, AuditEvent.actor, func.max(AuditEvent.created_at))
        .join(AuditEvent, AuditEvent.job_id == Job.id)
        .where(Job.owner_key.in_(owner_keys),
               AuditEvent.actor.notin_(_NOT_A_PERSON),
               AuditEvent.actor != "")
        .group_by(Job.owner_key, AuditEvent.actor)).all()
    latest: dict[str, tuple[datetime, str]] = {}
    for owner, actor, seen_at in rows:
        if seen_at is None:
            continue                  # cannot be ordered, so cannot be "the latest"
        best = latest.get(owner)
        if best is None or seen_at > best[0]:
            latest[owner] = (seen_at, actor)
    return {owner: actor for owner, (_, actor) in latest.items()}


def _label_of(owner_key: str, sign_ins: dict, audit_labels: dict) -> tuple[str | None, str | None]:
    """``(label, source)`` for one account, sign-in record first.

    The source travels with the label because the two are not equally good and a
    reader has to be able to tell: ``sign-in`` is what the identity provider
    itself said this account is called, ``audit`` is inferred from who last acted
    on one of its jobs, and ``None`` means nothing has ever named a person.
    Collapsing them would present a guess with the same authority as a fact.
    """
    row = sign_ins.get(owner_key)
    if row is not None and (row.display or "").strip():
        return row.display.strip(), "sign-in"
    label = audit_labels.get(owner_key)
    return (label, "audit") if label else (None, None)


def _job_counts(db, owner_keys: list[str]) -> dict[str, dict[str, int]]:
    """How many jobs each account holds, by status.

    A COUNT, from its own aggregate, and no ``Job`` row is loaded: not the review
    summary, not the parties, not the totals.  The residual disclosure, stated
    rather than waved away — an admin learns how many declarations an account has
    and roughly when it was active.  That is the minimum quota and abuse work
    needs, and it is not nothing.
    """
    if not owner_keys:
        return {}
    rows = db.execute(
        select(Job.owner_key, Job.status, func.count(Job.id))
        .where(Job.owner_key.in_(owner_keys))
        .group_by(Job.owner_key, Job.status)).all()
    counts: dict[str, dict[str, int]] = {}
    for owner, status, n in rows:
        counts.setdefault(owner, {})[status or ""] = int(n or 0)
    return counts


def roles_of(db, owner_keys) -> dict[str, str]:
    """``{owner_key: role}`` for several accounts in ONE query.

    The bulk sibling of :func:`role_of`, and it exists because a listing that
    called the single-key version per row would read ``account_role`` once per
    account on the page.  That is the shape of cost this design refused to put on
    every request in the first place, so it does not get to reappear per row.
    Same rule as the single-key read: only ``admin`` counts.
    """
    keys = [k for k in (owner_keys or []) if k]
    if not keys:
        return {}
    rows = db.execute(
        select(AccountRole.owner_key, AccountRole.role)
        .where(AccountRole.owner_key.in_(keys))).all()
    return {owner: (ADMIN if (role or "").strip().lower() == ADMIN else MEMBER)
            for owner, role in rows}


def disabled_reasons(db, owner_keys) -> dict[str, str]:
    """``{owner_key: reason}`` for the disabled accounts among those named — one
    query, for the same reason :func:`roles_of` exists.  Absent key = not
    disabled."""
    keys = [k for k in (owner_keys or []) if k]
    if not keys:
        return {}
    rows = db.execute(
        select(AccountDisabled.owner_key, AccountDisabled.reason)
        .where(AccountDisabled.owner_key.in_(keys))).all()
    return {owner: (reason or "").strip() or "This account has been disabled by the operator."
            for owner, reason in rows}


def account_row(db, owner_key: str, *, display_label: str | None = None,
                label_source: str | None = None,
                jobs_by_status: dict[str, int] | None = None,
                usage: dict | None = None,
                role: str | None = None,
                disabled: str | None = None,
                last_sign_in: datetime | None = None) -> dict:
    """One account's metadata, in the shape both admin routes report it.

    ``monthly_document_cap`` is the RESOLVED number from
    ``metering.resolved_cap`` — the account's row when it has one, else the
    deployment default — reported as null for unlimited exactly as ``/api/usage``
    reports it to the account itself.  ``quota`` carries the row underneath it,
    or null when there is none, because "no row, following the deployment
    default" and "a row that happens to say the same number" are different facts
    and only the second one has an owner.

    ``role`` and ``disabled`` are supplied by a caller that read them in bulk, and
    looked up here only when it did not.  A listing that read those two tables
    once per row would put back, per account, exactly the cost this design refused
    to put on every request — so a bulk caller passes ``""`` for an account it has
    established is NOT disabled, and ``None`` is reserved for "nobody has looked".
    """
    quota = db.get(AccountQuota, owner_key)
    jobs_by_status = jobs_by_status or {}
    usage = usage or {}
    if role is None:
        role = role_of(db, owner_key)
    if disabled is None:
        disabled = disabled_reason(db, owner_key)
    # Resolution order stays in metering.resolved_cap and is not reimplemented
    # here.  The row above is already in this session's identity map, so this is
    # not a second query — but if it ever became one, one query is the right
    # price for having exactly one implementation of the order.
    cap = metering.resolved_cap(db, owner_key)
    return {
        "owner_key": owner_key,
        # What this account signed in as, or — for one that has not signed in
        # since `account_seen` existed — a name inferred from its audit trail.
        # `label_source` says which, because they are not equally good.  Never
        # presented as an identity: ownership is the owner_key.
        "display_label": display_label,
        "label_source": label_source,
        # When this account last signed in, or null for one this deployment has
        # only ever seen through its jobs and usage.  This is the field that
        # answers "did they get in", which the union of owner_keys could not.
        "last_sign_in": last_sign_in.isoformat() if last_sign_in else None,
        "role": role,
        "disabled": bool(disabled),
        "jobs": sum(jobs_by_status.values()),
        "jobs_by_status": jobs_by_status,
        "documents_this_month": int(usage.get("documents_this_month") or 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        # Non-zero means the cost above is a FLOOR, not a total — some of this
        # account's calls used a model with no configured rate.
        "unpriced_events": int(usage.get("unpriced_events") or 0),
        "monthly_document_cap": cap or None,
        "quota": None if quota is None else {
            "monthly_document_cap": quota.monthly_document_cap,
            "note": quota.note or "",
            "updated_at": quota.updated_at.isoformat() if quota.updated_at else None,
            "updated_by": quota.updated_by,
        },
    }


def has_activity(db, owner_key: str) -> bool:
    """Whether this deployment has ever seen this key do anything.

    The only check available on an ``owner_key`` an admin has typed.  There is no
    user table to validate it against and no way to ask the identity provider
    without the service-role key, so a mistyped Supabase id would otherwise
    silently accept a quota row that governs nobody, for ever.  Reported rather
    than enforced: pre-provisioning an account that has not signed in yet is a
    legitimate thing to want.

    A SIGN-IN COUNTS, since step 3.  It has to: the case this check exists to
    catch is a typo, and the commonest legitimate reason to set a cap on an
    account with no jobs is that somebody has just registered and asked for one.
    Reporting them as never seen would train an admin to ignore the flag.
    """
    if not owner_key:
        return False
    return bool(
        db.get(AccountSeen, owner_key) is not None
        or db.scalar(select(func.count(Job.id)).where(Job.owner_key == owner_key))
        or db.scalar(select(func.count(UsageEvent.id)).where(UsageEvent.owner_key == owner_key)))


def account_detail(db, owner_key: str) -> dict:
    """One account's metadata, in the same shape the listing reports.

    Same fields, so a client that renders a row can render this, plus ``seen`` —
    which is how a typo in an ``owner_key`` becomes visible immediately instead of
    becoming a quota row that governs nobody.
    """
    sign_ins = sign_ins_of(db, [owner_key])
    label, source = _label_of(owner_key, sign_ins, _audit_labels(db, [owner_key]))
    costs = metering.estimated_cost_this_month(db, [owner_key])
    barred = disabled_reason(db, owner_key)
    row = account_row(
        db, owner_key,
        display_label=label, label_source=source,
        jobs_by_status=_job_counts(db, [owner_key]).get(owner_key),
        usage={"documents_this_month": metering.documents_this_month(db, owner_key),
               **costs.get(owner_key, {})},
        role=role_of(db, owner_key),
        disabled=barred or "",
        last_sign_in=getattr(sign_ins.get(owner_key), "last_seen_at", None))
    row["seen"] = has_activity(db, owner_key)
    # WHY it is barred, on the single-account view only: the listing reports the
    # fact, and the reason is what an administrator deciding whether to lift it
    # actually needs.
    row["disabled_reason"] = barred
    return row


def list_accounts(db, *, limit: int = 50, offset: int = 0) -> dict:
    """One page of accounts, busiest month first.

    Ordered by documents extracted this month, descending, and NOT by owner_key:
    the question this route exists to answer is "who is at their limit" and "who
    is burning the budget", and a page of UUIDs in lexical order answers neither.
    The account a reviewer's quota message is complaining about is therefore on
    the first page.
    """
    limit = max(1, min(int(limit or 50), 200))
    offset = max(0, int(offset or 0))

    keys = known_owner_keys(db)
    # One grouped query for every active account, because the ORDER depends on
    # it — the page cannot be chosen before the counts are known.  Bounded by the
    # number of accounts with usage this month, not by their usage rows.
    documents = metering.documents_this_month_by_owner(db)
    ordered = sorted(keys, key=lambda k: (-documents.get(k, 0), k))
    page = ordered[offset:offset + limit]

    # Everything below is restricted to the page, and every one of them is ONE
    # query for the whole page rather than one per account.
    sign_ins = sign_ins_of(db, page)
    labels = _audit_labels(db, page)
    jobs = _job_counts(db, page)
    costs = metering.estimated_cost_this_month(db, page)
    roles = roles_of(db, page)
    barred = disabled_reasons(db, page)
    shown = {key: _label_of(key, sign_ins, labels) for key in page}
    return {
        "total": len(ordered),
        "limit": limit,
        "offset": offset,
        # Said in the payload, not only in a docstring: a client that shows this
        # list as "all accounts" is showing something else.
        "scope": "accounts this deployment has seen (identity lives with the auth "
                 "provider; an account that has never signed in and owns nothing is "
                 "not listed)",
        "rate_version": metering.rate_table().version,
        "accounts": [
            account_row(db, key,
                        display_label=shown[key][0], label_source=shown[key][1],
                        jobs_by_status=jobs.get(key),
                        usage={"documents_this_month": documents.get(key, 0),
                               **costs.get(key, {})},
                        role=roles.get(key, MEMBER),
                        # "" and not None: None would send account_row back to the
                        # table for every account that is not on the deny-list,
                        # which is all of them.
                        disabled=barred.get(key, ""),
                        last_sign_in=getattr(sign_ins.get(key), "last_seen_at", None))
            for key in page
        ],
    }
