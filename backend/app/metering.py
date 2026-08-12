"""What each extraction spent, and whose spend it was.

Every job buys Mistral OCR and OpenAI tokens.  The counts already existed —
``OpenAIExtractor`` has always accumulated them — but only to be logged and
discarded, so nothing could answer what a job costs, which account is burning
the budget, or whether anyone is abusing the service.  This module records them.

THE ONE RULE HERE: **units are measured, money is estimated, and the two are
never confused.**

Token and page counts come from the vendor's own response and are always
recorded.  A cost is only computed for a model this deployment has configured a
rate for; otherwise the money column stays NULL — which means "not known", not
"free", and every reader has to say so.  A price this code invented would look
exactly as authoritative as one the operator verified against an invoice, and
would be wrong the first time a vendor changed a rate or the account moved to a
volume tier.  So the shipped rate table is EMPTY on purpose.

Recording never breaks a job.  A declaration is not worth losing to a failed
INSERT into a cost report.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache

from sqlalchemy import func, select

from .config import get_settings
from .models import AccountQuota, UsageEvent

log = logging.getLogger("easycustoms.metering")

# Rates are quoted per MILLION tokens (how both vendors publish them), so the
# arithmetic below divides by this rather than carrying a per-token price with
# ten leading zeros.
TOKENS_PER_UNIT = Decimal("1000000")


@dataclass(frozen=True)
class ModelRate:
    input_per_1m: Decimal | None = None
    cached_input_per_1m: Decimal | None = None
    output_per_1m: Decimal | None = None
    per_page: Decimal | None = None


@dataclass(frozen=True)
class RateTable:
    version: str
    rates: dict[str, ModelRate]

    def for_model(self, model: str) -> ModelRate | None:
        return self.rates.get((model or "").strip().lower())


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d >= 0 else None


@lru_cache
def rate_table() -> RateTable:
    """The configured prices, or an empty table.

    Cached: this is read on every extraction and the file does not change under
    a running process.  ``reset_rate_cache`` exists for tests and for an
    operator who edits the file and restarts nothing.
    """
    path = get_settings().usage_price_path
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.info("no vendor price table at %s — usage is recorded in tokens and "
                 "pages, and cost is left unknown rather than guessed", path)
        return RateTable(version="none", rates={})
    except Exception as e:
        # A malformed table must not silently become "everything is free".
        log.error("vendor price table %s is unreadable (%s) — costs will be "
                  "recorded as unknown", path, e)
        return RateTable(version="unreadable", rates={})

    rates: dict[str, ModelRate] = {}
    for model, spec in (data.get("models") or {}).items():
        if not isinstance(spec, dict):
            continue
        rates[str(model).strip().lower()] = ModelRate(
            input_per_1m=_decimal(spec.get("input_per_1m_tokens")),
            cached_input_per_1m=_decimal(spec.get("cached_input_per_1m_tokens")),
            output_per_1m=_decimal(spec.get("output_per_1m_tokens")),
            per_page=_decimal(spec.get("per_page")),
        )
    version = str(data.get("version") or "unversioned")
    log.info("vendor price table %s loaded: %d model(s) priced", version, len(rates))
    return RateTable(version=version, rates=rates)


def reset_rate_cache() -> None:
    rate_table.cache_clear()


def estimate_cost(model: str, *, prompt_tokens: int = 0, cached_tokens: int = 0,
                  completion_tokens: int = 0, pages: int = 0) -> tuple[Decimal | None, str]:
    """``(cost, rate_version)``; cost is None when this model has no rate.

    Cached prompt tokens are billed at their own (lower) rate when one is
    configured, and are SUBTRACTED from the uncached count rather than charged
    twice — the vendor reports them as a subset of prompt_tokens, not in
    addition to it.  Getting that wrong overstates every extraction, which for a
    number that will eventually set a price is the more damaging direction.
    """
    table = rate_table()
    rate = table.for_model(model)
    if rate is None:
        return None, table.version

    total = Decimal("0")
    priced = False
    if pages and rate.per_page is not None:
        total += rate.per_page * Decimal(pages)
        priced = True
    if prompt_tokens and rate.input_per_1m is not None:
        billable_cached = min(cached_tokens, prompt_tokens) if cached_tokens else 0
        uncached = Decimal(prompt_tokens - billable_cached)
        total += rate.input_per_1m * uncached / TOKENS_PER_UNIT
        cached_rate = (rate.cached_input_per_1m if rate.cached_input_per_1m is not None
                       else rate.input_per_1m)
        total += cached_rate * Decimal(billable_cached) / TOKENS_PER_UNIT
        priced = True
    if completion_tokens and rate.output_per_1m is not None:
        total += rate.output_per_1m * Decimal(completion_tokens) / TOKENS_PER_UNIT
        priced = True
    if not priced:
        return None, table.version
    return total, table.version


def record(db, *, owner_key: str, provider: str, operation: str, model: str,
           job_id: str | None = None, document_id: str | None = None,
           calls: int = 0, prompt_tokens: int = 0, cached_tokens: int = 0,
           completion_tokens: int = 0, pages: int = 0) -> UsageEvent | None:
    """Write one usage row.  Never raises on its own account.

    Uses the CALLER's session and does not commit, so the row lands atomically
    with the work that caused it: a rolled-back extraction cannot leave a charge
    for an extraction that did not happen, and a recorded charge always has a
    real extraction behind it.  That is the trade worth having — the price is
    that a database-level insert failure surfaces at the caller's commit like
    any other write, rather than being swallowed here.  Nothing about this row
    can cause that (no foreign keys, no unique constraints); a database that
    cannot insert it is one the surrounding commit was going to fail on anyway.

    What IS swallowed is everything this function does by itself — pricing,
    coercion, constructing the row — because none of it is worth failing a
    declaration for.
    """
    if not (calls or prompt_tokens or completion_tokens or pages):
        return None                                   # nothing was spent
    try:
        cost, version = estimate_cost(
            model, prompt_tokens=prompt_tokens, cached_tokens=cached_tokens,
            completion_tokens=completion_tokens, pages=pages)
        event = UsageEvent(
            owner_key=owner_key or "", job_id=job_id, document_id=document_id,
            provider=provider, operation=operation, model=model or "",
            calls=calls, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, cached_tokens=cached_tokens,
            pages=pages,
            estimated_cost_usd=(str(cost) if cost is not None else None),
            rate_version=version)
        db.add(event)
        return event
    except Exception as e:
        # A declaration is not worth losing to a failed insert into a cost
        # report. Loud, because a meter that has stopped counting is exactly the
        # thing nobody notices until the invoice arrives.
        log.error("could not record usage for %s/%s (%s): %s",
                  provider, operation, owner_key, e)
        return None


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def documents_this_month(db, owner_key: str, *, now: datetime | None = None) -> int:
    """How many documents this owner has had extracted in the calendar month.

    The quota is counted in DOCUMENTS rather than dollars on purpose: it is
    derived from what actually happened, needs no price table to be configured,
    and therefore cannot be defeated by a missing rate. Money is for reporting;
    a spend limit belongs with the credits ledger, where a balance is the
    mechanism.
    """
    return int(db.scalar(
        select(func.count(UsageEvent.id))
        .where(UsageEvent.owner_key == owner_key,
               UsageEvent.operation == "ocr",
               UsageEvent.created_at >= month_start(now))) or 0)


def summary(db, owner_key: str, *, since: datetime | None = None) -> dict:
    """Totals for one owner, with the honest caveat attached.

    ``unpriced_events`` is not decoration: a cost total computed from rows where
    some models had no rate is an UNDERSTATEMENT, and reporting it without
    saying so invites someone to budget against it.
    """
    since = since or month_start()
    rows = db.execute(
        select(UsageEvent.provider, UsageEvent.model,
               func.count(UsageEvent.id), func.sum(UsageEvent.calls),
               func.sum(UsageEvent.prompt_tokens), func.sum(UsageEvent.completion_tokens),
               func.sum(UsageEvent.cached_tokens), func.sum(UsageEvent.pages))
        .where(UsageEvent.owner_key == owner_key, UsageEvent.created_at >= since)
        .group_by(UsageEvent.provider, UsageEvent.model)).all()

    costs = db.scalars(
        select(UsageEvent.estimated_cost_usd)
        .where(UsageEvent.owner_key == owner_key, UsageEvent.created_at >= since)).all()
    known = [Decimal(c) for c in costs if c]
    unpriced = sum(1 for c in costs if not c)

    return {
        "since": since.isoformat(),
        "documents_this_month": documents_this_month(db, owner_key),
        "estimated_cost_usd": str(sum(known, Decimal("0"))) if known else None,
        # How many recorded calls had no rate to price them. A non-zero value
        # means the cost above is a floor, not a total.
        "unpriced_events": unpriced,
        "rate_version": rate_table().version,
        "by_model": [
            {"provider": p, "model": m, "events": n, "calls": int(c or 0),
             "prompt_tokens": int(pt or 0), "completion_tokens": int(ct or 0),
             "cached_tokens": int(cc or 0), "pages": int(pg or 0)}
            for p, m, n, c, pt, ct, cc, pg in rows
        ],
    }


def resolved_cap(db, owner_key: str) -> int:
    """How many documents THIS account may extract this month.  0 = unlimited.

    Resolution order, and nothing else decides it:

      1. the account's own ``account_quota`` row, when it has one carrying a
         number.  A row whose ``monthly_document_cap`` is NULL exists to hold a
         note and defers to the deployment — NULL is "follow the default", 0 is
         "unlimited", and collapsing the two would turn every note into a ban;
      2. otherwise the deployment default
         (``Settings.resolved_monthly_document_cap``, which is itself
         per-provider);
      3. ``<= 0`` means unlimited, at either level.

    A per-account row is what makes the sentence below true.  It has always
    promised the reviewer that "an administrator raises the limit" is a thing
    that can happen; until this row existed, raising one account's limit meant
    raising everyone's.

    Deliberately NOT wrapped in a try/except, unlike ``record``.  That one
    swallows because a declaration is not worth losing to a failed insert into a
    cost report; this one is the check that stands between an account and this
    deployment's vendor budget, so a database that cannot answer it must stop the
    extraction loudly rather than wave it through.  Fail closed, like the login
    throttle it sits beside.
    """
    if owner_key:
        row = db.get(AccountQuota, owner_key)
        if row is not None and row.monthly_document_cap is not None:
            return row.monthly_document_cap
    return get_settings().resolved_monthly_document_cap()


def quota_exceeded(db, owner_key: str) -> str | None:
    """The reason this owner may not start another extraction, or None.

    Returns a sentence for the reviewer rather than a bool: "you are over your
    limit" is only actionable if it says what the limit was.
    """
    cap = resolved_cap(db, owner_key)
    if cap <= 0:
        return None                                   # unlimited
    used = documents_this_month(db, owner_key)
    if used < cap:
        return None
    return (f"This account has extracted {used} documents this calendar month, which "
            f"is its limit of {cap}. Extraction resumes at the start of next month, "
            f"or when an administrator raises the limit.")
