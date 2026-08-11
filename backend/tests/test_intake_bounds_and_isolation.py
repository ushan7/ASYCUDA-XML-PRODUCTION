"""Round-2 hardening: bound the intake, and refuse what did not come from here.

Four separate holes, all of the same shape — a control that existed but ran
somewhere it could not do its job:

  * `max_upload_mb` and `max_photos_per_document` were checked after Starlette
    had already spooled the whole multipart body, so they could report an
    oversized upload but never prevent one;
  * `extraction_max_pages` was checked after the OCR bill was paid, and only on
    the OpenAI provider path;
  * the fixture replay gate was on the upload ROUTE, while `POST /api/jobs/demo`
    reached the same service function without passing it;
  * SameSite=Lax does not cover a top-level GET, and this API has a GET that
    recomputes and commits.
"""
import io

import pytest

from fastapi.testclient import TestClient

from app import services
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.domain.enums import DeclaredRole, ExtractionProvenance
from app.domain.errors import BlockingValidationError
from app.main import app


@pytest.fixture()
def client():
    init_db()
    with TestClient(app) as c:
        yield c


def _job(client) -> str:
    return client.post("/api/jobs").json()["job_id"]


def _pdf(pages: int = 1) -> bytes:
    """A minimal but genuinely parseable multi-page PDF."""
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Request body ceiling — refused at the boundary, before anything parses it
# --------------------------------------------------------------------------- #
def test_oversized_declared_body_is_refused_before_parsing(client):
    """An honest Content-Length is refused without reading the body at all."""
    limit = get_settings().request_body_limit_bytes()
    job = _job(client)
    r = client.post(
        f"/api/jobs/{job}/documents/INVOICE",
        headers={"content-length": str(limit + 1)},
        content=b"x" * 32,                       # never reaches the parser
    )
    assert r.status_code == 413
    assert r.json()["code"] == "REQUEST_TOO_LARGE"


def test_body_over_the_ceiling_is_refused_even_when_content_length_lies(client):
    """The running total is what catches a client that understates the size."""
    limit = get_settings().request_body_limit_bytes()
    job = _job(client)
    oversized = b"%PDF-1.4\n" + b"A" * (limit + 1024)
    r = client.post(f"/api/jobs/{job}/documents/INVOICE",
                    files={"file": ("huge.pdf", oversized, "application/pdf")})
    assert r.status_code == 413, r.text


def test_a_normal_upload_still_passes_the_ceiling(client):
    """The guard must not be in the way of the thing it protects."""
    job = _job(client)
    r = client.post(f"/api/jobs/{job}/documents/INVOICE",
                    files={"file": ("invoice.pdf", _pdf(), "application/pdf")})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Page ceiling — before the paid OCR call, on every provider path
# --------------------------------------------------------------------------- #
def test_page_ceiling_refuses_at_upload_not_after_ocr(monkeypatch):
    """`validate_upload` now knows the page count, so nothing is spent first."""
    monkeypatch.setattr(get_settings(), "extraction_max_pages", 3, raising=False)
    with pytest.raises(BlockingValidationError) as excinfo:
        services.validate_upload("fat.pdf", _pdf(pages=5))
    assert excinfo.value.message.code == "DOCUMENT_TOO_MANY_PAGES"


def test_page_ceiling_lets_a_normal_document_through(monkeypatch):
    monkeypatch.setattr(get_settings(), "extraction_max_pages", 150, raising=False)
    assert services.validate_upload("fine.pdf", _pdf(pages=3)) == "pdf"


def test_page_count_of_an_unreadable_pdf_does_not_raise():
    """The ceiling is not a validity check — a broken file fails further in,
    with a message about what is actually wrong with it."""
    assert services.pdf_page_count(io.BytesIO(b"not a pdf at all")) is None
    services.enforce_page_ceiling(None, filename="mystery.pdf")   # must not raise


# --------------------------------------------------------------------------- #
# Fixture replay gate — enforced at the service boundary, not one route
#
# The gate is on the PROVENANCE of the supplied values, not on the fact that
# some were supplied.  It once refused both, which meant the bundled demo was
# refused too: POST /api/jobs/demo answered 409 on every deployment that had
# not opted into a flag it has no reason to set.  See
# tests/test_extraction_provenance.py for the split in full; what belongs here
# is that the service boundary — not just the upload route — is where a
# CLIENT-supplied extraction is stopped.
# --------------------------------------------------------------------------- #
def test_client_supplied_extraction_is_stopped_at_the_service_boundary(client, monkeypatch):
    """The upload route checks too, but the route is not the only way in, so
    the refusal has to live under it."""
    monkeypatch.setattr(get_settings(), "allow_fixture_uploads", False, raising=False)
    job_id = client.post("/api/jobs").json()["job_id"]
    db = SessionLocal()
    try:
        job = services.get_job(db, job_id, principal=services.SYSTEM_PRINCIPAL)
        with pytest.raises(BlockingValidationError) as e:
            services.add_document(db, job, DeclaredRole.INVOICE, "invoice.pdf",
                                  b"%PDF-1.4\ninvoice\n", {"invoice_number": "MADE-UP"},
                                  provenance=ExtractionProvenance.CLIENT_FIXTURE)
        assert e.value.message.code == "FIXTURE_UPLOADS_DISABLED"
    finally:
        db.rollback()
        db.close()


def test_demo_seeding_does_not_need_the_flag(client, monkeypatch):
    """Bundled sample data is server-side and unreachable from a request, so
    the demo runs on a default deployment — marked, not gated."""
    monkeypatch.setattr(get_settings(), "allow_fixture_uploads", False, raising=False)
    r = client.post("/api/jobs/demo")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/jobs/{r.json()['job_id']}").json()["is_demo"] is True


# --------------------------------------------------------------------------- #
# Fetch Metadata isolation — same-origin navigation stays, cross-site goes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_origin_api_calls_are_refused(client, site):
    r = client.get("/api/jobs", headers={"sec-fetch-site": site})
    assert r.status_code == 403
    assert r.json()["code"] == "CROSS_ORIGIN_REFUSED"


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_navigation_is_untouched(client, site):
    """The evidence iframe and the XML/.xls download links are same-origin
    navigations — the whole reason the cookie exists."""
    assert client.get("/api/jobs", headers={"sec-fetch-site": site}).status_code == 200


def test_a_client_that_sends_no_fetch_metadata_still_works(client):
    """Older browsers and API clients fall through to SameSite, as before."""
    assert client.get("/api/jobs").status_code == 200


# --------------------------------------------------------------------------- #
# Session cookie `Secure` no longer depends on inferring the scheme
# --------------------------------------------------------------------------- #
def test_cookie_secure_can_be_forced_on_for_a_tls_deployment(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "session_cookie_secure", "always", raising=False)
    client.headers.pop("Authorization", None)
    r = client.post("/api/auth/login",
                    json={"username": "pytest-operator", "password": "pytest-password"})
    assert r.status_code == 200
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_secure_auto_stays_off_on_plain_http(client, monkeypatch):
    """A `Secure` cookie on a plain-http LAN deployment is set and then never
    sent back — an unusable login, which is why `auto` is still the default."""
    monkeypatch.setattr(get_settings(), "session_cookie_secure", "auto", raising=False)
    client.headers.pop("Authorization", None)
    r = client.post("/api/auth/login",
                    json={"username": "pytest-operator", "password": "pytest-password"})
    assert r.status_code == 200
    assert "secure" not in r.headers["set-cookie"].lower()


# --------------------------------------------------------------------------- #
# Ownership seam (EC2-02)
#
# The app still has exactly one account, so none of this changes what today's
# operator sees. The point is that the enforcement POINT now exists and is
# required: `get_job` and `list_jobs` cannot be called without saying whose
# data is being asked for, so a second account is a configuration change rather
# than an audit finding.
# --------------------------------------------------------------------------- #
def test_get_job_requires_a_principal():
    """Not defaultable: an optional owner is how every route ends up unscoped."""
    import inspect
    sig = inspect.signature(services.get_job)
    assert sig.parameters["principal"].default is inspect.Parameter.empty
    assert sig.parameters["principal"].kind is inspect.Parameter.KEYWORD_ONLY
    list_sig = inspect.signature(services.list_jobs)
    assert list_sig.parameters["principal"].default is inspect.Parameter.empty


def test_a_job_is_owned_by_the_operator_who_created_it(client):
    from app.database import SessionLocal
    from app.models import Job
    job_id = _job(client)
    db = SessionLocal()
    try:
        assert db.get(Job, job_id).owner_key == "pytest-operator"
    finally:
        db.close()


def test_another_principal_cannot_read_or_enumerate_the_job(client):
    from app.database import SessionLocal
    job_id = _job(client)
    client.post(f"/api/jobs/{job_id}/documents/INVOICE",
                files={"file": ("invoice.pdf", _pdf(), "application/pdf")})
    db = SessionLocal()
    try:
        assert services.get_job(db, job_id, principal="pytest-operator") is not None
        # A different signed-in principal gets None -> the route answers 404,
        # so an id that exists but is someone else's is indistinguishable from
        # one that never existed. A 403 would confirm the job is real.
        assert services.get_job(db, job_id, principal="someone-else") is None
        mine = services.list_jobs(db, principal="pytest-operator")
        theirs = services.list_jobs(db, principal="someone-else")
        assert any(j["job_id"] == job_id for j in mine["jobs"])
        assert not any(j["job_id"] == job_id for j in theirs["jobs"])
    finally:
        db.close()


def test_system_callers_still_see_everything(client):
    """Startup recovery and cascade invalidation have no signed-in principal."""
    from app.database import SessionLocal
    job_id = _job(client)
    db = SessionLocal()
    try:
        assert services.get_job(db, job_id, principal=services.SYSTEM_PRINCIPAL) is not None
    finally:
        db.close()


def test_an_unowned_job_is_visible_to_nobody(client):
    """The branch that used to make these readable by anyone is gone.

    It was correct while exactly one account could exist: hiding a broker's
    whole history behind a check only one person could ever match was the worse
    of the two failures. A second account can now exist, so the same branch
    means "every user sees every unowned job" — the disclosure the check exists
    to prevent. Pre-ownership rows are BACKFILLED by the migration that removed
    it, not shared.
    """
    from app.database import SessionLocal
    from app.models import Job
    job_id = _job(client)
    db = SessionLocal()
    try:
        db.get(Job, job_id).owner_key = ""
        db.commit()
        assert services.get_job(db, job_id, principal="anyone-at-all") is None
        assert services.get_job(db, job_id, principal="pytest-operator") is None
        # ...and it is still reachable by the internal principal, so a cascade
        # invalidation or a startup sweep is not locked out of its own rows.
        assert services.get_job(db, job_id,
                                principal=services.SYSTEM_PRINCIPAL) is not None
    finally:
        db.close()


def test_one_users_job_is_invisible_to_another(client):
    """The property the whole ownership column exists for, stated directly.

    Isolation is PYTHON, not Postgres row-level security: this backend connects
    as a privileged role and bypasses RLS entirely, so a policy in the database
    would not have caught a regression here.
    """
    from app.database import SessionLocal
    from app.models import Job
    job_id = _job(client)
    db = SessionLocal()
    try:
        db.get(Job, job_id).owner_key = "user-a"
        db.commit()
        assert services.get_job(db, job_id, principal="user-a") is not None
        assert services.get_job(db, job_id, principal="user-b") is None
        # ...and the dashboard listing agrees with the per-job check. They are
        # separate pieces of SQL, and a listing that disagreed would show one
        # user another's shipment totals and party names in the summary.
        assert services.list_jobs(db, principal="user-b")["total"] == 0
    finally:
        db.close()


def test_a_route_reached_without_a_session_fails_closed():
    """`principal_of` must not turn "nobody is signed in" into system access."""
    assert services.principal_of(None) is None
    assert services.principal_of(object()) is None


def test_every_job_scoped_route_declares_whose_data_it_serves():
    """The structural half of the ownership seam.

    `services.get_job` refusing to run without a principal catches the mistake
    at the call; this catches it at the signature, which is where a new route
    is written. Same reasoning as the login gate living in middleware: the next
    endpoint someone adds should be scoped by the act of adding it.
    """
    import inspect
    from app.main import app as fastapi_app

    unscoped = sorted(
        route.path for route in fastapi_app.routes
        if "{job_id}" in getattr(route, "path", "")
        and "principal" not in inspect.signature(route.endpoint).parameters)
    assert not unscoped, (
        "These job-scoped routes do not say whose data they serve:\n  "
        + "\n  ".join(unscoped) +
        "\n\nAdd `principal: str = Depends(principal_dep)` and pass it to "
        "services.get_job — a job id in the path is not an authorization check.")


def test_ownership_is_never_inferred_from_the_audit_actor(client):
    """`actor` is who did it; `owner_key` is who may see it. Not the same thing.

    Deriving one from the other made seed_demo_job mint jobs owned by the
    literal string "demo" — which the operator who pressed the button could
    then not read. A caller that names no principal creates an unowned job.
    """
    from app.database import SessionLocal
    from app.models import Job
    from app import services as svc

    db = SessionLocal()
    try:
        job = svc.create_job(db, actor="some-audit-label")
        db.commit()
        assert db.get(Job, job.id).owner_key == ""
        # ...and naming no principal means the job belongs to no user, so no
        # user can read it. The audit label must not become an access grant by
        # the back door.
        assert svc.get_job(db, job.id, principal="some-audit-label") is None
        assert svc.get_job(db, job.id, principal="pytest-operator") is None
    finally:
        db.close()
