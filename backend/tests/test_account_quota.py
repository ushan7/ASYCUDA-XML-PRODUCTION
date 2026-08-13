"""An account's document cap is its own, and a multi-account deployment has one.

`docs/plan-signup-and-roles.md` step 1.  Two things had to become true before
accounts can create themselves, and neither was:

  * **A cap could not be set for ONE account.**  `usage_monthly_document_cap`
    was a single deployment-wide number, so raising one user's limit raised
    everyone's — while the sentence `metering.quota_exceeded` shows the reviewer
    has always promised the opposite ("or when an administrator raises the
    limit").  `account_quota` is what makes that sentence true.

  * **The default was unlimited.**  Correct for the single-operator install and
    an uncapped vendor bill the moment more than one account can exist.  The two
    providers therefore resolve differently, and `supabase` + an explicit 0 is
    refused at boot rather than written down somewhere and hoped for.

The assertions worth naming, because they are the ones that would fail silently:
NULL and 0 on a row are DIFFERENT answers (follow the default / unlimited), and
one account's row and one account's usage move nothing about another's.
"""
from __future__ import annotations

import pytest

from app import metering, services
from app.config import DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP, Settings, get_settings
from app.database import SessionLocal, init_db
from app.models import AccountQuota, UsageEvent

# Distinct from every other module's principals: the suite shares one database
# for the whole run, so a reused key would make these totals depend on which
# module ran first.
ANA = "a9a00000-0000-4000-8000-0000000000a1"
RAJ = "4a100000-0000-4000-8000-0000000000b2"


@pytest.fixture(autouse=True)
def _schema():
    """`init_db` is idempotent (create_all, then a stamp/upgrade that no-ops once
    at head), so the first test through here pays for it and the rest do not."""
    init_db()


@pytest.fixture()
def db(_schema):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _clean_slate(_schema):
    """No quota rows and no spend for this module's principals, before or after.

    Both halves are load-bearing, and each was a test passing for the wrong
    reason before it was added.  A quota row left behind is invisible to the
    next test's assertion — absence is the state most of these are about.  And
    the suite shares ONE database for the whole run, so `usage_event` rows
    accumulate down the module: a later test asserting "this account is not
    blocked" would be reading a total four earlier tests contributed to.

    Scoped to ANA and RAJ rather than truncating the tables, so this cannot
    disturb another module's totals in the same run.

    Depends on `_schema` EXPLICITLY: two autouse fixtures at the same scope are
    not ordered by where they appear in the file, and this one deletes from
    tables the other creates.
    """
    def _clear() -> None:
        session = SessionLocal()
        try:
            session.query(AccountQuota).delete()
            session.query(UsageEvent).filter(
                UsageEvent.owner_key.in_([ANA, RAJ])).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    _clear()
    yield
    _clear()


def _set_cap(owner_key: str, cap: int | None, *, note: str = "") -> None:
    """Insert the row an admin route will write in step 2.  By hand until then,
    which is why `updated_by` is nullable."""
    session = SessionLocal()
    try:
        session.merge(AccountQuota(owner_key=owner_key, monthly_document_cap=cap,
                                   note=note))
        session.commit()
    finally:
        session.close()


def _spend(owner_key: str, documents: int) -> None:
    """`documents` OCR events, written through `metering.record` — the same call
    the extractor makes, so the rows are shaped exactly as real ones."""
    session = SessionLocal()
    try:
        for _ in range(documents):
            metering.record(session, owner_key=owner_key, provider="mistral",
                            operation="ocr", model="mistral-ocr-latest",
                            calls=1, pages=3)
        session.commit()
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Resolution order: the row, then the deployment, and <= 0 is unlimited at both
# --------------------------------------------------------------------------- #
def test_the_accounts_own_row_wins_over_the_deployment_default(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 5)
    _set_cap(ANA, 50, note="paid plan")
    assert metering.resolved_cap(db, ANA) == 50


def test_an_account_with_no_row_takes_the_deployment_default(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 5)
    assert metering.resolved_cap(db, ANA) == 5


def test_a_row_with_no_number_follows_the_deployment_default(db, monkeypatch):
    """NULL is "follow the default", 0 is "unlimited", and they are different
    answers.  A row added only to carry a note must not become a ban."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 5)
    _set_cap(ANA, None, note="under discussion with the customer")
    assert metering.resolved_cap(db, ANA) == 5
    assert metering.quota_exceeded(db, ANA) is None


def test_zero_on_the_row_is_unlimited_for_that_account(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 1)
    _spend(ANA, 3)
    assert metering.quota_exceeded(db, ANA) is not None, "the deployment cap applies"

    _set_cap(ANA, 0, note="internal account")
    assert metering.resolved_cap(db, ANA) == 0
    assert metering.quota_exceeded(db, ANA) is None


def test_zero_at_the_deployment_is_unlimited(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 0)
    _spend(ANA, 3)
    assert metering.resolved_cap(db, ANA) == 0
    assert metering.quota_exceeded(db, ANA) is None


def test_the_row_can_lower_a_cap_as_well_as_raise_it(db, monkeypatch):
    """Both directions, because a resolver that only ever took the larger of the
    two would pass every test above."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 100)
    _set_cap(ANA, 2)
    _spend(ANA, 2)
    reason = metering.quota_exceeded(db, ANA)
    assert reason and "limit of 2" in reason, reason


def test_the_message_names_the_accounts_own_limit(db, monkeypatch):
    """"You are over your limit" is only actionable if it says what the limit
    was — and after this change the limit it must name is the account's."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 999)
    _set_cap(ANA, 3)
    _spend(ANA, 3)
    reason = metering.quota_exceeded(db, ANA)
    assert reason and "limit of 3" in reason and "999" not in reason, reason


# --------------------------------------------------------------------------- #
# One account's row, and one account's usage, move nothing about another's
# --------------------------------------------------------------------------- #
def test_one_accounts_row_does_not_move_anothers_cap(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 5)
    _set_cap(ANA, 500)
    assert metering.resolved_cap(db, ANA) == 500
    assert metering.resolved_cap(db, RAJ) == 5, (
        "raising one account's limit must not raise a stranger's")


def test_one_accounts_usage_never_counts_against_anothers(db, monkeypatch):
    """The property the whole per-account quota exists for.  A counter keyed on
    the wrong column would block every account the moment any one of them
    reached its cap, and would let a heavy user exhaust a stranger's plan."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 2)
    _set_cap(ANA, 1)
    _spend(ANA, 4)

    assert metering.documents_this_month(db, ANA) >= 4
    assert metering.documents_this_month(db, RAJ) == 0
    assert metering.quota_exceeded(db, ANA) is not None
    assert metering.quota_exceeded(db, RAJ) is None, (
        "RAJ has extracted nothing and holds no row — ANA's spend is not theirs")


def test_the_reverse_direction_too(db, monkeypatch):
    """Asserted separately: a resolver reading the wrong key is symmetric and
    would pass whichever direction happened to be written."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 2)
    _set_cap(RAJ, 1)
    _spend(RAJ, 4)

    assert metering.quota_exceeded(db, RAJ) is not None
    assert metering.quota_exceeded(db, ANA) is None


def test_a_row_belonging_to_nobody_in_particular_is_not_everyones(db, monkeypatch):
    """The empty owner_key.  `job_visible_to` once returned True for an unowned
    job, which was correct with one account and a disclosure with two; a quota
    row keyed on "" must not become the fallback for every account."""
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 7)
    _set_cap("", 1)
    assert metering.resolved_cap(db, ANA) == 7
    assert metering.resolved_cap(db, RAJ) == 7


# --------------------------------------------------------------------------- #
# The deployment default is per PROVIDER
#
# A supabase deployment naming a LIVE extraction/OCR provider without its key is
# refused at boot too (tests/test_live_provider_keys.py), so the constructions
# below say the vendor keys are present.  These tests are about the CAP: a
# supabase shape that trips a different refusal would assert nothing about it.
# --------------------------------------------------------------------------- #
_LIVE_KEYS = {"MISTRAL_API_KEY": "sk-mistral", "OPENAI_API_KEY": "sk-openai"}
def test_a_single_operator_deployment_is_unlimited_by_default():
    """`local` holds exactly one account — auth.verify_token checks the token
    subject against the configured username, so a second cannot exist.  There is
    no stranger to bound, and a cap would only ever fire on the operator who
    paid for the deployment."""
    s = Settings(_env_file=None, AUTH_PROVIDER="local")
    assert s.usage_monthly_document_cap is None
    assert s.resolved_monthly_document_cap() == 0


def test_a_multi_account_deployment_gets_a_real_cap_by_default():
    s = Settings(_env_file=None, AUTH_PROVIDER="supabase",
                 SUPABASE_URL="https://project.supabase.co", SUPABASE_ANON_KEY="anon", **_LIVE_KEYS)
    assert s.usage_monthly_document_cap is None
    assert s.resolved_monthly_document_cap() == DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP


def test_an_explicit_value_overrides_the_provider_default():
    s = Settings(_env_file=None, AUTH_PROVIDER="local",
                 USAGE_MONTHLY_DOCUMENT_CAP="25")
    assert s.resolved_monthly_document_cap() == 25


def test_an_explicit_zero_is_still_unlimited_for_a_single_operator():
    """The upgrade path this default change had to protect: an install that
    deliberately wants no cap says so, and is not stopped."""
    s = Settings(_env_file=None, AUTH_PROVIDER="local",
                 USAGE_MONTHLY_DOCUMENT_CAP="0")
    assert s.resolved_monthly_document_cap() == 0


def test_a_negative_cap_is_refused_where_it_enters():
    with pytest.raises(Exception) as e:
        Settings(_env_file=None, AUTH_PROVIDER="local",
                 USAGE_MONTHLY_DOCUMENT_CAP="-1")
    assert "USAGE_MONTHLY_DOCUMENT_CAP" in str(e.value)


# --------------------------------------------------------------------------- #
# The boot refusal
# --------------------------------------------------------------------------- #
def test_boot_refuses_an_uncapped_multi_account_deployment():
    """Enforced rather than documented.  Every extraction buys Mistral OCR and
    OpenAI tokens, so an unlimited cap on the provider that holds many accounts
    is an unbounded vendor bill — and it arrives as an invoice weeks later, not
    as an error anyone can act on."""
    with pytest.raises(Exception) as e:
        Settings(_env_file=None, AUTH_PROVIDER="supabase",
                 SUPABASE_URL="https://project.supabase.co", SUPABASE_ANON_KEY="anon",
                 **_LIVE_KEYS, USAGE_MONTHLY_DOCUMENT_CAP="0")
    message = str(e.value)
    assert "USAGE_MONTHLY_DOCUMENT_CAP" in message
    assert str(DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP) in message, (
        "the refusal has to say what to do instead")


def test_boot_does_not_refuse_a_multi_account_deployment_that_named_a_cap():
    s = Settings(_env_file=None, AUTH_PROVIDER="supabase",
                 SUPABASE_URL="https://project.supabase.co", SUPABASE_ANON_KEY="anon",
                 **_LIVE_KEYS, USAGE_MONTHLY_DOCUMENT_CAP="10")
    assert s.resolved_monthly_document_cap() == 10


def test_boot_does_not_refuse_a_multi_account_deployment_that_named_nothing():
    """The upgrade case.  Only an EXPLICIT 0 is refused: a deployment that never
    mentioned the setting boots with a real cap rather than being stopped by a
    change it did not make."""
    s = Settings(_env_file=None, AUTH_PROVIDER="supabase",
                 SUPABASE_URL="https://project.supabase.co", SUPABASE_ANON_KEY="anon", **_LIVE_KEYS)
    assert s.resolved_monthly_document_cap() == DEFAULT_MULTI_ACCOUNT_DOCUMENT_CAP


def test_boot_never_refuses_a_single_operator_deployment():
    """The existing install this change must not break: `local`, no cap line in
    its .env, and no way for a second account to exist."""
    s = Settings(_env_file=None, AUTH_PROVIDER="local",
                 USAGE_MONTHLY_DOCUMENT_CAP="0")
    assert s.resolved_monthly_document_cap() == 0
    assert Settings(_env_file=None, AUTH_PROVIDER="local"
                    ).resolved_monthly_document_cap() == 0


# --------------------------------------------------------------------------- #
# End to end — the route the cap actually guards, and what /api/usage reports
# --------------------------------------------------------------------------- #
def test_extraction_is_refused_at_the_accounts_own_cap(monkeypatch):
    """`quota_exceeded` is called one line BEFORE the claim (main.py), which is
    the last point at which refusing costs nothing — past it the OCR is paid for
    whether or not the account was over its limit."""
    from fastapi.testclient import TestClient

    from app.domain.enums import DocumentStatus
    from app.main import app
    from app.models import Document

    # Generous deployment-wide; the account's own row is what must bite.
    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 10_000)

    with TestClient(app) as client:
        job_id = client.post("/api/jobs/demo").json()["job_id"]
        doc_id = client.get(f"/api/jobs/{job_id}").json()["documents"][0]["document_id"]

        session = SessionLocal()
        try:
            owner = services._owner_of_job(session, job_id)
            # The demo seeds documents already extracted; put one back so the
            # route reaches the quota gate rather than "already processed".
            session.get(Document, doc_id).status = DocumentStatus.UPLOADED.value
            session.commit()
        finally:
            session.close()

        _set_cap(owner, 1)
        _spend(owner, 1)

        r = client.post(f"/api/jobs/{job_id}/documents/{doc_id}/extract")

    assert r.status_code == 429, r.text
    assert r.json()["code"] == "USAGE_LIMIT_REACHED"
    assert "limit of 1" in r.json()["detail"]


def test_the_usage_endpoint_reports_the_accounts_own_cap(monkeypatch):
    """Not the deployment's.  Showing a number the caller is not subject to is
    wrong in both directions — it either promises headroom they do not have or
    hides headroom they paid for."""
    from fastapi.testclient import TestClient

    from app.auth import configured_username
    from app.main import app

    monkeypatch.setattr(get_settings(), "usage_monthly_document_cap", 10)
    with TestClient(app) as client:
        assert client.get("/api/usage").json()["monthly_document_cap"] == 10

        _set_cap(configured_username(), 42)
        assert client.get("/api/usage").json()["monthly_document_cap"] == 42

        _set_cap(configured_username(), 0)
        assert client.get("/api/usage").json()["monthly_document_cap"] is None, (
            "unlimited is reported as null, not as 0")
