"""FastAPI application — Easy Customs XML Generator.

Role-specific uploads -> OCR -> extraction -> deterministic resolution ->
critical review -> finalize -> ASYCUDA XML.  Serves the built React frontend
from ``/`` when present.
"""
from __future__ import annotations

import json
import logging
import time
from functools import partial
from pathlib import Path

from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import auth, queueing, services
from .config import get_settings
from .database import SessionLocal, get_session, init_db
from .demo import seed_demo_job
from .domain.enums import DeclaredRole, DocumentStatus, ExtractionProvenance
from .domain.errors import BlockingValidationError
from .reference.store import get_reference, hs_query_error
from .review.item_mutations import ItemMutationError

log = logging.getLogger("easycustoms.api")

app = FastAPI(title="Easy Customs XML Generator", version="1.0.0")


# --------------------------------------------------------------------------- #
# Authentication gate — DENY BY DEFAULT.
#
# Registered as middleware rather than as a per-route dependency so that the
# next endpoint someone adds is protected by the fact that they added it.  A
# forgotten `Depends(require_login)` on one route is the whole job: every route
# here reads or writes an importer's invoices, party details and declarations.
#
# The only unauthenticated surfaces are the four below.  In particular the SPA
# SHELL is public — it has to load to draw the login form — while every byte of
# data it renders comes from /api/*, which is not.
# --------------------------------------------------------------------------- #
_PUBLIC_API_PATHS = frozenset({
    "/api/health",       # liveness for docker/compose healthchecks; no data
    "/api/auth/login",   # the way in
})
# Interactive API docs enumerate every endpoint and can call them: same gate.
_PROTECTED_NON_API = ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")


def _needs_login(request: Request) -> bool:
    path = request.url.path
    if request.method == "OPTIONS":
        return False                      # CORS preflight carries no credentials
    if path in _PUBLIC_API_PATHS:
        return False
    if path.startswith("/api/") or path.startswith(_PROTECTED_NON_API):
        return True
    return False                          # static SPA shell (index.html, assets)


# Fetch Metadata resource isolation — the layer SameSite cannot be.
#
# SameSite=Lax stops a cross-site POST from carrying the session cookie, but it
# deliberately does NOT stop a top-level GET navigation, and this API has a GET
# that recomputes and commits (/api/jobs/{id}/critical-review), so a plain link
# was enough to make a signed-in reviewer's browser mutate a job.  Lax is also
# no help against a same-site subdomain.  Browsers that send Sec-Fetch-* let us
# state the policy directly: nothing off this origin has any business calling
# /api.  Same-ORIGIN navigation stays allowed — the evidence iframe and the XML
# and .xls download links are exactly that, and they are why the cookie exists.
#
# Requests with no Sec-Fetch-Site header (older browsers, curl, server-to-server
# clients) fall through to SameSite as before: this tightens the ceiling, it
# does not become the only lock.  Skipped entirely when CORS_ORIGINS is
# configured, since that is the operator explicitly inviting a browser client
# from somewhere else.
_SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})


def _cross_origin_api_call(request: Request) -> bool:
    if _cors_origins:                     # cross-origin browser access is intended here
        return False
    site = request.headers.get("sec-fetch-site")
    return bool(site) and site not in _SAME_ORIGIN_FETCH_SITES


@app.middleware("http")
async def _require_login(request: Request, call_next):
    request.state.session = None
    if _needs_login(request):
        if _cross_origin_api_call(request):
            log.warning("refused a %s %s arriving with Sec-Fetch-Site: %s",
                        request.method, request.url.path,
                        request.headers.get("sec-fetch-site"))
            return JSONResponse(status_code=403, content={
                "status": "REJECTED", "code": "CROSS_ORIGIN_REFUSED",
                "detail": "This request did not come from the application and was refused."})
        session = auth.authenticate_request(request)
        if not session:
            # 401 + a sentence the SPA shows on its login screen.  `code` is
            # what the client branches on — never the message text.
            return JSONResponse(status_code=401, content={
                "status": "UNAUTHENTICATED", "code": "AUTH_REQUIRED",
                "detail": "Your session has ended. Sign in to continue."})
        request.state.session = session
    return await call_next(request)


# --------------------------------------------------------------------------- #
# Request body ceiling — the only layer that can refuse bytes before they land.
#
# `max_upload_mb` (services.validate_upload) and `max_photos_per_document`
# (images.photos_to_pdf) are the right caps in the wrong place to bound memory:
# Starlette spools EVERY multipart part to a temporary file while it builds the
# form, and that happens before the route function is called at all.  By the
# time `await file.read()` ran, the bytes had already arrived and been written
# down — the cap could report the mis-pick, but never prevent it.  Those checks
# stay (they are what produce the reviewer-facing message, and they still guard
# the demo/test paths that never touch HTTP); this one is what makes them
# affordable.
# --------------------------------------------------------------------------- #
class _BodyTooLarge(Exception):
    """Raised out of the receive channel when a body crosses the ceiling."""


def _content_length(scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class RequestBodyLimit:
    """Bound the request body at the ASGI boundary.

    Content-Length is checked first, so an honest client is refused before it
    sends a byte of body.  A client that understates it, or uses chunked
    transfer-encoding and declares nothing at all, is caught by the running
    total — which is the case the header check alone would miss.
    """

    _METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in self._METHODS:
            await self.app(scope, receive, send)
            return

        limit = get_settings().request_body_limit_bytes()
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await self._refuse(send, limit)
            return

        seen = 0
        started = False

        async def counting_receive():
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _BodyTooLarge:
            # Only safe to write our own answer if the app has not begun one.
            # It has not, in practice: the parser hits the ceiling while it is
            # still reading, long before a handler decides a status.
            if started:
                raise
            await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        body = json.dumps({
            "status": "REJECTED",
            "code": "REQUEST_TOO_LARGE",
            "detail": f"This upload is over the server's {limit // (1024 * 1024)} MB request "
                      f"limit and was refused while it was still arriving. Attach a smaller "
                      f"file, or split a photo set across two uploads.",
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
            (b"connection", b"close"),
        ]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestBodyLimit)


# CORS is NOT wildcard.  The SPA is served from this very app (frontend/index.html
# uses `const API = ""`), so no legitimate caller is cross-origin.  With
# allow_origins=["*"] any page on the internet could POST cross-origin guesses at
# the public login route and READ the reply — and could hold the office IP at the
# 429 throttle indefinitely, denying the operator the only way in.  Configure
# EASYCUSTOMS_CORS_ORIGINS only if an API client really is served from elsewhere.
_cors_origins = [o.strip() for o in (get_settings().cors_origins or "").split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                       allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(BlockingValidationError)
async def _blocking_error(request, exc: BlockingValidationError):
    # Deterministic rules refusing to produce a value is a client-state
    # conflict, not a server fault — surface the machine-readable message.
    return JSONResponse(status_code=409, content={
        "status": "BLOCKED",
        "blocking_errors": [exc.message.model_dump(mode="json")],
    })


@app.exception_handler(ItemMutationError)
async def _item_mutation_error(request, exc: ItemMutationError):
    # Actionable 404/409/422 for reviewer add/delete; nothing was persisted.
    return JSONResponse(status_code=exc.status_code, content={
        "status": "REJECTED", "code": exc.code, "message": exc.message})


@app.middleware("http")
async def _no_cache_html(request, call_next):
    # The SPA is served without Cache-Control, so browsers reuse stale copies
    # heuristically after frontend edits. no-cache = always revalidate (ETag
    # still yields cheap 304s when unchanged).
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    _security_headers(resp)
    return resp


def _security_headers(resp) -> None:
    """The second layer under the upload/serving gates.

    This app renders reviewer-uploaded documents on its own origin and its
    finalize button produces a legally binding artifact, so the two that earn
    their keep here are `nosniff` (a stored file can never be re-interpreted as
    markup, whatever it claims) and `frame-ancestors 'none'` (nobody frames the
    declaration workspace and clickjacks finalize).

    The CSP allows the CDN the zero-build SPA loads React and Babel from, and
    'unsafe-eval' because Babel standalone COMPILES the application in the
    browser.  Both are concessions to the current frontend, not endorsements —
    vendoring those bundles locally is what lets this tighten to 'self'.

    The framing rule is split by content type, because this app is BOTH the
    thing that must never be framed and the thing that frames.  The workspace
    is HTML and carries the clickjackable finalize button, so it stays at
    `DENY`/`frame-ancestors 'none'`.  Everything else is evidence and payload —
    above all the stored PDF the reviewer's document panel loads in an
    `<iframe>` FROM THIS ORIGIN.  Sending `DENY` there too blanked that panel
    in every browser (`X-Frame-Options: DENY` refuses same-origin framing as
    firmly as cross-origin), which is the whole reason a reviewer could not see
    the document behind a 📄 link.  `SAMEORIGIN`/'self' keeps a third-party
    page from embedding an importer's invoice while letting the app frame its
    own; a PDF or JSON response has no control to clickjack in the first place.
    """
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", "; ".join((
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )))
    else:
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # Modern browsers prefer frame-ancestors over X-Frame-Options; both are
        # sent so the rule holds either way it is read.
        resp.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")


def _widen_threadpool() -> None:
    """Lift Starlette's 40-thread default to the configured ceiling.

    Every `def` route runs there, and so does every explicit run_in_threadpool
    call — which is to say uploads, extraction and finalize, the three slowest
    things this app does.  40 is therefore the per-process concurrency limit on
    exactly the routes that hold a thread for seconds to minutes, and it is
    reached long before CPU is: 41 reviewers pressing Finalize means the 41st
    waits for a thread before it waits for anything real.

    Never fatal.  A failure to widen the pool is a slower app, not a broken one,
    and refusing to boot over it would trade a throughput ceiling for an outage.
    """
    target = get_settings().threadpool_max_threads
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        if limiter.total_tokens < target:
            limiter.total_tokens = target
            log.info("threadpool limiter raised to %s threads", target)
    except Exception as e:                    # pragma: no cover - defensive
        log.warning("could not raise the threadpool limiter to %s (%s: %s) — the "
                    "process keeps AnyIO's default of 40 concurrent threadpool "
                    "requests", target, type(e).__name__, e)


@app.on_event("startup")
def _startup() -> None:
    _widen_threadpool()
    if not auth.credentials_configured():
        # Loud, because the symptom otherwise reads as a broken app: every
        # screen is a login form that rejects every password.
        log.error("no login account is configured — set EASYCUSTOMS_AUTH_USERNAME and "
                  "EASYCUSTOMS_AUTH_PASSWORD in backend/.env. Until then nobody can sign in "
                  "and every API route answers 401 (the app fails CLOSED, never open).")
    init_db()
    get_reference()  # warm the reference cache
    if get_settings().queue_provider == "sqs":
        # Queue mode: EXTRACTING claims are held by WORKER processes, so this
        # API restarting proves nothing about them — sweeping them here would
        # reset documents that are mid-extraction on another machine, and the
        # worker's later commit would then overwrite whatever the reviewer did
        # with the "recovered" document in between.  Crash recovery belongs to
        # SQS itself: a dead worker stops heartbeating its message, SQS
        # re-delivers it, and the next worker takes the claim over
        # (run_extraction's claim_from).
        log.info("queue mode (sqs): startup leaves EXTRACTING claims alone — "
                 "redelivery is the crash recovery")
    else:
        # A previous process may have died mid-extraction — `uvicorn --reload`
        # restarts on every file edit — leaving documents claimed by a run that
        # no longer exists.  Nothing releases that claim except this.
        db = SessionLocal()
        try:
            for doc in services.recover_interrupted_extractions(db):
                log.warning("extraction interrupted by a restart: %s #%s (%s) returned to the "
                            "queue; press Continue to resume%s", doc.declared_role,
                            doc.upload_index_within_role, doc.original_file_name,
                            " (stored OCR reused)" if doc.ocr else "")
        finally:
            db.close()


def db_dep():
    yield from get_session()


def principal_dep(request: Request) -> str:
    """WHO is asking, as the access key job rows are scoped by.

    Declared per route rather than read implicitly from a context variable, so
    that a new job-scoped endpoint cannot be written without stating whose data
    it serves — the same reason the login gate is middleware. The auth
    middleware has already verified the session by the time this runs.
    """
    principal = services.principal_of(getattr(request.state, "session", None))
    if principal is None:
        # Unreachable through the middleware, which is the point: if a route is
        # ever added outside the gate, it fails closed here instead of
        # inheriting whatever a missing session happened to evaluate to.
        raise HTTPException(401, "Your session has ended. Sign in to continue.")
    return principal


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


def _client_id(request: Request) -> str:
    """Throttle key — see auth.client_key.

    X-Forwarded-For is read only as far as EASYCUSTOMS_TRUSTED_PROXY_HOPS says
    the proxies in front of this app are ours; at the default of 0 it is ignored
    entirely and the socket peer is used, because a caller that can set the
    header would otherwise pick its own key and reset its failure count.
    """
    return auth.client_key(request)


def _actor(request: Request) -> str:
    """WHO the audit trail should name for this request.

    The auth middleware puts the verified session on request.state; falling back
    to the system actor keeps the trail honest rather than inventing a name if a
    route is ever reached without one.
    """
    session = getattr(request.state, "session", None)
    return getattr(session, "username", None) or services.SYSTEM_ACTOR


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookie goes out with `Secure`.

    `auto` asks the request, which is the honest answer only when nothing sits
    in front of this process: a reverse proxy that terminates TLS and forwards
    plain http makes the scheme "http" for a browser that is very much on https,
    and the cookie then travels without `Secure`.  uvicorn will believe
    X-Forwarded-Proto, but only from an address listed in FORWARDED_ALLOW_IPS
    (default 127.0.0.1) — which a proxy in a sibling container is not.  A TLS
    deployment should set EASYCUSTOMS_SESSION_COOKIE_SECURE=always and stop
    depending on that inference.
    """
    choice = get_settings().session_cookie_secure
    if choice == "always":
        return True
    if choice == "never":
        return False
    return request.url.scheme == "https"


def _session_payload(session: auth.Session, token: str | None = None) -> dict:
    body = {"username": session.username,
            "issued_at": session.issued_at,
            "expires_at": session.expires_at,
            "expires_in_seconds": max(0, session.expires_at - int(time.time()))}
    if token:
        body["token"] = token
    return body


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(db_dep)):
    """Exchange the configured username + password for a token valid for
    ``auth_token_ttl_hours`` (24h).  The token comes back in the body (for API
    clients and the SPA's fetches) AND as an HttpOnly cookie (for the evidence
    iframe and the XML/.xls download links, which are plain navigations).

    The session itself stays stateless; the database is here only for the
    failure counter, which cannot live in this process (see app/auth.py).
    """
    if not auth.credentials_configured():
        raise HTTPException(503, "This server has no login account configured. Set "
                                 "EASYCUSTOMS_AUTH_USERNAME and EASYCUSTOMS_AUTH_PASSWORD in "
                                 "backend/.env and restart it.")
    client = _client_id(request)
    try:
        retry_after = auth.throttle_retry_after(db, client)
    except auth.ThrottleUnavailable as e:
        # FAIL CLOSED. Letting the attempt through because the counter is
        # unreadable would turn a database fault into unlimited password
        # guessing against a public endpoint — and a signed-in user could not do
        # anything without that database anyway, so nothing is gained by it.
        log.error("refusing sign-in: the failure counter is unreadable (%s)", e)
        raise HTTPException(503, "This server cannot verify sign-in attempts right now. "
                                 "Try again shortly.")
    if retry_after:
        return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={
            "status": "THROTTLED", "code": "TOO_MANY_ATTEMPTS",
            "detail": f"Too many failed sign-in attempts. Try again in {retry_after} second(s)."})
    if not auth.verify_credentials(body.username, body.password):
        auth.record_failure(db, client)
        log.warning("failed sign-in attempt from %s", client)
        # One message for both halves: which of the two was wrong is not the
        # caller's business.
        return JSONResponse(status_code=401, content={
            "status": "UNAUTHENTICATED", "code": "BAD_CREDENTIALS",
            "detail": "Incorrect username or password."})
    auth.clear_failures(db, client)
    # The token names the CONFIGURED username, not the typed one: the login
    # comparison is case-insensitive, and a token has to keep matching the
    # account it was issued for (verify_token re-checks that).
    token, session = auth.issue_token(auth.configured_username() or body.username.strip())
    resp = JSONResponse(_session_payload(session, token))
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.token_ttl_seconds(),
                    httponly=True, samesite="lax",
                    secure=_cookie_secure(request), path="/")
    return resp


@app.get("/api/auth/session")
def whoami(request: Request):
    """Who is signed in and until when.  Protected like everything else, so a
    401 here is exactly the SPA's "show the login form" signal."""
    return _session_payload(request.state.session)


@app.post("/api/auth/logout")
def logout(request: Request):
    """Drop the cookie.  The token itself stays valid until it expires (it is
    stateless by design) — a client that kept a copy must discard it, which is
    what the SPA does."""
    resp = JSONResponse({"status": "SIGNED_OUT"})
    # Same attributes the cookie was SET with.  A browser matches the deletion
    # on name/path/domain, but a mismatched SameSite or Secure makes some of
    # them ignore the Set-Cookie that carries it, leaving the session cookie in
    # place after a sign-out that reported success.
    resp.delete_cookie(auth.COOKIE_NAME, path="/", httponly=True, samesite="lax",
                       secure=_cookie_secure(request))
    return resp


@app.get("/api/reference/hs")
def search_hs(q: str = "", limit: int = 30):
    """Deterministic, local, network-free search of the official HS database.
    Numeric prefix or official-description text; the display-only explanation
    column is returned but never searched or ranked."""
    err = hs_query_error(q)
    if err:
        raise HTTPException(400, err)
    ref = get_reference()                        # warm singleton — no re-read
    return [{"code": rec.hs11, "description": rec.description, "unit": rec.unit,
             "explanation": rec.explanation, "score": score}
            for rec, score in ref.search_hs(q, limit)]


@app.get("/api/reference/banks")
def list_banks():
    """The full bank reference (code + name + SWIFT) for the review dropdown —
    deterministic, local, network-free; small enough to ship whole."""
    ref = get_reference()
    return [{"code": b.code, "name": b.name, "swift": b.swift}
            for b in sorted(ref.banks, key=lambda b: (b.name or "").upper())]


@app.get("/api/reference/payment-terms")
def list_payment_terms():
    """The ASYCUDA terms-of-payment table (code + description) for the review
    dropdown — numeric codes first in numeric order, then any others."""
    ref = get_reference()

    def _key(code: str):
        return (0, int(code)) if code.isdigit() else (1, code)

    return [{"code": code, "description": desc}
            for code, desc in sorted(ref.terms_by_code.items(), key=lambda kv: _key(kv[0]))]


@app.get("/api/reference/customs-offices")
def list_customs_offices():
    """The 71 NECAS customs-office codes (NNSW codification DB) — Box A /
    Border_office pickers."""
    ref = get_reference()
    return [{"code": code, "name": name}
            for code, name in sorted(ref.office_by_code.items())]


@app.get("/api/reference/declaration-models")
def list_declaration_models():
    """The 17 Box-1 declaration-model LINES (type+code+description).  The
    reviewer picks one line — IM 4 exists, IM 1 does not."""
    ref = get_reference()
    return [{"type": m.type, "code": m.code, "description": m.description}
            for m in ref.declaration_models]


@app.get("/api/reference/extended-procedures")
def list_extended_procedures():
    """NECAS ANNEX 1 in annex order (55 rows; 9100 twice in the source — both
    kept).  Filter client-side by the Box-1 general-procedure digit."""
    ref = get_reference()
    return [{"code": code, "description": desc} for code, desc in ref.extended_procedures]


@app.get("/api/reference/national-procedures")
def list_national_procedures():
    """NECAS ANNEX 3 in annex order (182 rows; numbering gaps are in the
    source)."""
    ref = get_reference()
    return [{"code": code, "description": desc} for code, desc in ref.national_procedures]


@app.get("/api/reference/transport-modes")
def list_transport_modes():
    """Box 25/26 modes of transport (01–09).  No default is ever applied —
    the reviewer must choose."""
    ref = get_reference()
    return [{"code": code, "description": desc}
            for code, desc in sorted(ref.transport_mode_by_code.items())]


@app.get("/api/reference/incoterms")
def list_incoterms():
    """Incoterms 2020 for the delivery-terms dropdown (legacy values like
    "C&F" remain accepted as typed)."""
    ref = get_reference()
    return [{"code": code, "description": desc}
            for code, desc in ref.incoterm_by_code.items()]


@app.get("/api/config")
def config():
    s = get_settings()
    ref = get_reference()
    return {
        "ocr_provider": s.ocr_provider,
        "extraction_provider": s.extraction_provider,
        "ocr_live": s.ocr_live_ready,
        "extraction_live": s.extraction_live_ready,
        "llm_model": s.resolved_llm_model(),
        "rule_set_version": s.rule_set_version,
        "exchange_rate": str(s.default_exchange_rate),
        "adr_flags": {
            "default_net_to_gross_ratio": str(s.default_net_to_gross_ratio),
            # non-null only when a config override moved the ratio off ADR-003
            "net_to_gross_ratio_override": s.net_to_gross_ratio_note(),
            "pair_divide_by_two": s.pair_divide_by_two,
            "cost_allocation_basis": s.cost_allocation_basis,
            "default_unknown_payment_terms_to_lc": s.default_unknown_payment_terms_to_lc,
        },
        "reference_counts": {
            "hs_codes": len(ref.hs_by_11),
            "countries": len(ref.valid_alpha2),
            "banks": len(ref.banks),
            "payment_terms": len(ref.terms_by_code),
            "customs_offices": len(ref.office_by_code),
            "declaration_models": len(ref.declaration_models),
            "extended_procedures": len(ref.extended_procedures),
            "national_procedures": len(ref.national_procedures),
            "transport_modes": len(ref.transport_mode_by_code),
            "incoterms": len(ref.incoterm_by_code),
        },
        # A default that steers XML output is published, never silent (same
        # philosophy as adr_flags).  Border/inland transport modes have NO
        # default by design (user rule 2026-08-01).
        "declaration_defaults": {
            "customs_office_code": s.customs_office_code,
            "declaration_type": s.declaration_type,
            "gen_procedure_code": s.declaration_gen_procedure_code,
            "extended_customs_procedure": s.extended_customs_procedure,
            "national_customs_procedure": s.national_customs_procedure,
            "place_of_loading_code": s.place_of_loading_code,
            "location_of_goods": s.location_of_goods,
            "border_nationality": s.border_nationality,
        },
        "roles": [r.value for r in DeclaredRole],
    }


# --------------------------------------------------------------------------- #
# Jobs & documents
# --------------------------------------------------------------------------- #
@app.get("/api/jobs")
def list_jobs(limit: int = 50, offset: int = 0, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Newest-first job listing for the dashboard: status, document roles and
    the stored review summary (invoices, parties, totals, office/type, XML).
    Jobs that never received an upload are not listed; a job whose documents
    were later removed still is (its audit trail proves the work)."""
    return services.list_jobs(db, limit=limit, offset=offset, principal=principal)


@app.post("/api/jobs")
def create_job(request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.create_job(db, actor=_actor(request), owner_key=principal)
    return {"job_id": job.id, "status": job.status}


@app.post("/api/jobs/demo")
def create_demo(request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = seed_demo_job(db, actor=_actor(request), owner_key=principal)
    return {"job_id": job.id, "status": job.status, "documents": len(job.documents)}


@app.post("/api/jobs/{job_id}/documents/{role}")
async def upload_document(job_id: str, role: str, request: Request, file: UploadFile = File(...),
                          fixture: str | None = Form(default=None),
                          db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Attach a document to a job.  Its extracted facts come from OCR and the
    extractor, and — unless the replay hook below is explicitly enabled — from
    nowhere else.

    `fixture` is a JSON blob that BECOMES the document's extraction verbatim,
    skipping OCR, the model and the evidence/anchor validators, and leaving
    nothing on the row to record that the facts were supplied rather than
    extracted.  That is exactly the shape of an unverified value reaching a
    legally binding declaration, so it now requires
    EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS=true and is refused otherwise.  The test
    suite and the offline demo set it; a real deployment must not.
    """
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    try:
        declared = DeclaredRole(role.upper())
    except ValueError:
        raise HTTPException(400, f"unknown role {role!r}")
    if fixture is not None and not get_settings().allow_fixture_uploads:
        raise HTTPException(403, "fixture uploads are disabled on this server — a document's "
                                 "extracted values must come from its own OCR, not from the "
                                 "request. Set EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS=true only on a "
                                 "test or demo deployment.")
    data = await file.read()
    try:
        fixture_obj = json.loads(fixture) if fixture else None
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"fixture is not valid JSON: {e}")
    # file.content_type is NOT forwarded: it is the browser's guess from the
    # file extension, i.e. attacker-influenced, and storing it once meant the
    # evidence viewer served it back as the response media type.
    doc = await run_in_threadpool(partial(services.add_document, db, job, declared,
                                          file.filename or "upload.pdf", data, fixture_obj,
                                          actor=_actor(request),
                                          # Anything arriving on THIS route came from the
                                          # request, whatever it claims — the one provenance
                                          # that stays behind allow_fixture_uploads.
                                          provenance=ExtractionProvenance.CLIENT_FIXTURE))
    return {"document_id": doc.id, "role": doc.declared_role, "role_match": doc.role_match,
            "status": doc.status, "warnings": doc.warnings}


@app.post("/api/jobs/{job_id}/documents/{role}/photos")
async def upload_photo_document(job_id: str, role: str, request: Request,
                                files: list[UploadFile] = File(...),
                                db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """ONE document from several phone photos, merged in the order given —
    each photo is one page of the SAME document (a 3-page invoice photographed
    page by page).  Photos of DIFFERENT documents belong in separate uploads:
    merged here they would extract as one document's rows."""
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    try:
        declared = DeclaredRole(role.upper())
    except ValueError:
        raise HTTPException(400, f"unknown role {role!r}")
    photos = [(f.filename or f"photo-{i + 1}.jpg", await f.read())
              for i, f in enumerate(files)]
    doc = await run_in_threadpool(partial(services.add_photo_document, db, job, declared, photos,
                                          actor=_actor(request)))
    return {"document_id": doc.id, "role": doc.declared_role, "role_match": doc.role_match,
            "status": doc.status, "warnings": doc.warnings}


@app.api_route("/api/jobs/{job_id}/documents/{document_id}/file", methods=["GET", "HEAD"])
def get_document_file(job_id: str, document_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Serve the stored upload inline — the evidence the review's document
    viewer shows next to the extracted values.  Read-only in every job state:
    a finalized declaration's documents stay viewable (post-clearance audits
    arrive months later), they just can't be changed.

    HEAD is answered as well as GET, and is not decoration: the viewer panel
    frames this URL, and a frame reports nothing back — its load event fires
    for a refusal exactly as for a PDF.  The panel therefore asks HEAD first
    and renders the reason instead of an empty grey rectangle.  FileResponse
    sends headers only for HEAD, so the probe costs no bytes; without the
    method here it answered 404 (the SPA mount at "/" swallows the 405) and
    every document would have been reported unavailable."""
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    doc = next((d for d in job.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "document not found")
    if not doc.storage_key or not Path(doc.storage_key).is_file():
        raise HTTPException(410, f"the stored file for {doc.original_file_name!r} is no longer "
                                 f"on this server (its extracted content is still available)")
    # The media type is a SERVER fact, never the stored row's: every upload is
    # PDF bytes by the time it is written (services.add_document), and this
    # response is rendered INLINE on the app's own origin.  Reflecting a
    # client-supplied type here turned an uploaded file into script running as
    # the signed-in operator.  nosniff stops the browser second-guessing it.
    return FileResponse(doc.storage_key, media_type="application/pdf",
                        filename=doc.original_file_name or "document.pdf",
                        content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff"})


@app.delete("/api/jobs/{job_id}/documents/{document_id}")
def remove_document(job_id: str, document_id: str, request: Request,
                    db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Detach a document from the job (wrong file, wrong box, or the replace
    step the duplicate-upload refusal instructs).  Derived state is
    invalidated; the audit trail keeps the removal."""
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    doc = next((d for d in job.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "document not found")
    return services.remove_document(db, job, doc, actor=_actor(request))


@app.post("/api/jobs/{job_id}/documents/{document_id}/extract")
async def extract_uploaded_document(job_id: str, document_id: str, request: Request,
                                    db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    doc = next((d for d in job.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "document not found")
    if doc.status == DocumentStatus.EXTRACTING.value:
        raise HTTPException(409, "an extraction is already running for this document — "
                                 "wait for it to finish rather than starting a second one")
    if doc.status not in (DocumentStatus.UPLOADED.value, DocumentStatus.FAILED.value):
        raise HTTPException(409, f"document already processed (status {doc.status})")
    if get_settings().queue_provider == "sqs":
        # Queue mode: claim + enqueue, return immediately.  backend/worker.py
        # does the slow work; the SPA already polls any document it sees in
        # EXTRACTING and continues by itself when the claim is released, so
        # this response only has to avoid looking like a failure.
        try:
            queueing.enqueue_extraction(db, doc, actor=_actor(request))
        except queueing.QueueSendError as e:
            # The claim has been released; the document is retriable.  502:
            # the fault is the queue, an upstream of ours — same shape the
            # synchronous path uses for an OCR/LLM fault, so the SPA shows it.
            return JSONResponse(status_code=502, content={
                "document_id": document_id, "role": doc.declared_role,
                "status": "FAILED", "error": str(e)[:300]})
        # BlockingValidationError (a lost claim race) propagates to the 409
        # handler exactly as the synchronous path's does.
        return {"document_id": doc.id, "role": doc.declared_role,
                "role_match": None, "status": doc.status, "queued": True,
                "warnings": []}
    # Threadpool: OCR + LLM extraction block for tens of seconds; running them
    # on the event loop would freeze every other request (incl. parallel extracts).
    try:
        doc = await run_in_threadpool(partial(services.run_extraction, db, doc,
                                              actor=_actor(request)))
    except BlockingValidationError:
        # A refused claim (the check above lost a race) is a conflict, not a
        # failure: the 409 handler says so.  Reporting it as FAILED below would
        # tell the reviewer their document broke while it is in fact extracting.
        raise
    except Exception as e:
        # run_extraction already persisted FAILED + the reason; tell the client
        # what happened instead of an opaque 500 (502: upstream OCR/LLM fault).
        return JSONResponse(status_code=502, content={
            "document_id": document_id, "role": doc.declared_role, "status": "FAILED",
            "error": f"{type(e).__name__}: {str(e)[:300]}"})
    return {"document_id": doc.id, "role": doc.declared_role, "role_match": doc.role_match,
            "status": doc.status, "warnings": doc.warnings}


class RoleDecisionRequest(BaseModel):
    """Reviewer's answer to an extractor role-mismatch verdict."""
    model_config = ConfigDict(extra="forbid")
    accept: bool
    reason: str = ""


@app.post("/api/jobs/{job_id}/documents/{document_id}/role-decision")
def decide_document_role(job_id: str, document_id: str, body: RoleDecisionRequest,
                         request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Confirm or reject a document whose extracted content does not match the
    role its upload box declared.  Accepting uses it as declared; rejecting
    excludes it from the declaration (the evidence itself is kept)."""
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    doc = next((d for d in job.documents if d.id == document_id), None)
    if not doc:
        raise HTTPException(404, "document not found")
    return services.resolve_document_role(db, job, doc, accept=body.accept,
                                          reason=body.reason, actor=_actor(request))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return {
        "job_id": job.id, "status": job.status, "exchange_rate": job.exchange_rate,
        "rule_set_version": job.rule_set_version,
        # Whether this job's facts were seeded from the bundled samples rather
        # than read off the attached documents. The SPA marks it everywhere the
        # job appears: a demo declaration is a real, downloadable XML, so the
        # one thing that must not happen is mistaking it for a shipment.
        "is_demo": services.is_demo_job(job),
        "documents": [
            {"document_id": d.id, "role": d.declared_role, "file": d.original_file_name,
             "status": d.status, "role_match": d.role_match, "warnings": d.warnings or [],
             "provenance": d.extraction_provenance}
            for d in sorted(job.documents, key=lambda x: x.declared_role)
        ],
        "critical_review": job.critical_review,
        "has_declaration": job.declaration is not None,
    }


# GET *and* POST: this route recomputes and commits, so POST is what it should
# have been.  The GET stays because the SPA and the whole test suite call it
# that way, and churning both to rename a verb is not worth the regression risk
# on a declaration pipeline — the cross-site route to it is closed above.
@app.api_route("/api/jobs/{job_id}/critical-review", methods=["GET", "POST"])
def critical_review(job_id: str, request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.critical_review(db, job, actor=_actor(request))


# --------------------------------------------------------------------------- #
# Reviewer item mutations (Detailed Review add / delete)
# --------------------------------------------------------------------------- #
def _finite_nonneg(value, field_name: str):
    """Quantities and prices must be finite, non-negative numbers."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} is not a number")
    if not d.is_finite() or d < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return value


class ManualItemSeed(BaseModel):
    # extra="forbid": client-supplied item_id / derived fields are rejected —
    # identity, allocation, supplementary and XML values are server-owned.
    model_config = ConfigDict(extra="forbid")
    description: str = ""
    quantity: float | int | str = 0
    uom: str = "PCS"
    total_price: float | int | str = 0
    country_of_origin: str = ""
    final_hs_code: str = ""

    @field_validator("quantity")
    @classmethod
    def _qty(cls, v):
        return _finite_nonneg(v, "quantity")

    @field_validator("total_price")
    @classmethod
    def _price(cls, v):
        return _finite_nonneg(v, "total_price")


class AddItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    insertion_sn: int
    invoice_id: str = ""
    manual_review_addition: bool = False
    reason: str = ""
    item: ManualItemSeed = Field(default_factory=ManualItemSeed)


class DeleteItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_sn: str
    reason: str = ""


class HsReviewRequest(BaseModel):
    # exactly the review-save shape: only identity + code + allowlisted source.
    # Units/descriptions/derived values are NEVER accepted from the client.
    model_config = ConfigDict(extra="forbid")
    item_id: str
    final_hs_code: str
    hs_review_source: str


class HsReviewRangeRequest(BaseModel):
    # bulk apply: one code + allowlisted source + a printer-style SN range
    # ("1-15, 19, 80" or "all").  Derived values are never accepted from the client.
    model_config = ConfigDict(extra="forbid")
    final_hs_code: str
    hs_review_source: str = "detailed_review_hs_search"
    sn_range: str


class ShipmentTotalsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gross_weight: float | int | str
    weight_unit: str = "KGM"
    total_packages: float | int | str


class AllCooRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country_of_origin: str


class EditableItemFields(BaseModel):
    # item_id / HS are rejected (HS goes through hs-review, and the supplementary
    # unit CODE follows that HS's tariff unit).  gross_weight / net_weight (kg)
    # and carton_count (CTN) PIN the item: allocation keeps them exact and
    # adjusts the other items to hold the shipment authority.
    # supplementary_quantity is a plain override of the derived number — nothing
    # is redistributed and no derivation rule changes; the entered value is what
    # the XML carries.  An EMPTY STRING clears a pin or the override.
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    quantity: float | int | str | None = None
    uom: str | None = None
    total_price: float | int | str | None = None
    country_of_origin: str | None = None
    gross_weight: float | int | str | None = None
    net_weight: float | int | str | None = None
    carton_count: float | int | str | None = None
    supplementary_quantity: float | int | str | None = None


class EditItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: EditableItemFields


class BmsEdit(BaseModel):
    """One row's export-only brand/model/size override. An empty string CLEARS
    that cell's override so the deterministic value returns; an omitted field is
    left untouched."""
    model_config = ConfigDict(extra="forbid")
    item_id: str
    brand: str | None = None
    model: str | None = None
    size: str | None = None


class BmsEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edits: list[BmsEdit]


@app.post("/api/jobs/{job_id}/items")
def add_item(job_id: str, body: AddItemRequest, request: Request,
             db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.add_job_item(db, job, body.model_dump(), actor=_actor(request))


@app.delete("/api/jobs/{job_id}/items/{item_id}")
def delete_item(job_id: str, item_id: str, body: DeleteItemRequest,
                request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.delete_job_item(db, job, item_id, body.model_dump(), actor=_actor(request))


@app.post("/api/jobs/{job_id}/items/hs-review")
def review_item_hs(job_id: str, body: HsReviewRequest, request: Request,
                   db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.review_item_hs(db, job, body.model_dump(), actor=_actor(request))


@app.post("/api/jobs/{job_id}/items/hs-review-range")
def review_item_hs_range(job_id: str, body: HsReviewRangeRequest,
                         request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.review_item_hs_range(db, job, body.model_dump(), actor=_actor(request))


@app.post("/api/jobs/{job_id}/items/brand-model-size")
def edit_items_bms(job_id: str, body: BmsEditRequest, request: Request,
                   db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Bulk reviewer edit of the export-only BRAND/MODEL/SIZE columns.

    Accepts one cell, a pasted block or a filled range in a single call.  Never
    invalidates the declaration or a generated XML — only the .xls is rebuilt.
    """
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.edit_item_bms(db, job, body.model_dump(exclude_none=True),
                                 actor=_actor(request))


@app.patch("/api/jobs/{job_id}/items/{item_id}")
def edit_item(job_id: str, item_id: str, body: EditItemRequest,
              request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.edit_job_item(db, job, item_id,
                                  {"fields": body.fields.model_dump()}, actor=_actor(request))


@app.post("/api/jobs/{job_id}/shipment-totals")
def review_shipment_totals(job_id: str, body: ShipmentTotalsRequest,
                           request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.review_shipment_totals(db, job, body.model_dump(), actor=_actor(request))


@app.post("/api/jobs/{job_id}/regime")
def review_regime(job_id: str, body: dict, request: Request, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Durable per-job regime/office/transport selections (Boxes 1, 25/26, 27,
    30, 37, A) — reference-gated at write time, seeds every review recompute;
    ``null`` reverts a field to the deployment default."""
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.review_regime_selections(db, job, body or {}, actor=_actor(request))


@app.post("/api/jobs/{job_id}/items/coo-all")
def set_all_item_coo(job_id: str, body: AllCooRequest, request: Request,
                     db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return services.set_all_item_coo(db, job, body.model_dump(), actor=_actor(request))


@app.post("/api/jobs/{job_id}/finalize")
async def finalize(job_id: str, request: Request, body: dict | None = None,
                   db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    # Threadpool: finalize runs the whole declaration build — pipeline, lxml XML
    # serialization, the xlwt workbook — plus a blocking job lock.  On the event
    # loop that froze every other request for its duration, including the
    # reviewer's own parallel extractions.
    result = await run_in_threadpool(partial(services.finalize_job, db, job, body or {},
                                             actor=_actor(request)))
    # warn-mode finalize succeeds (200) whenever an XML was produced, even
    # with blocking cases — the payload carries them for the pop-up warning
    blocked_without_xml = (result.get("blocking_errors")
                           and not result.get("xml_built_with_blockers"))
    # REVIEW_STALE and ITEM_COUNT_MISMATCH say the same thing to a client —
    # "not finalized, go re-review" — so they must not differ in status code.
    # REVIEW_STALE used to return 200, telling any non-SPA caller the finalize
    # had succeeded.  (The SPA is unaffected either way: api() branches on the
    # body's `status`, not the HTTP code.)
    refused = result.get("status") in ("REVIEW_STALE", "ITEM_COUNT_MISMATCH")
    status = 409 if blocked_without_xml or refused else 200
    return JSONResponse(status_code=status, content=result)


@app.get("/api/jobs/{job_id}/declaration")
def declaration(job_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job or not job.declaration:
        raise HTTPException(404, "declaration not built")
    return job.declaration


@app.get("/api/jobs/{job_id}/xml")
def download_xml(job_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    art = services.latest_xml(db, job_id)
    if not art:
        raise HTTPException(404, "xml not built")
    return Response(content=art.xml_bytes, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="ASYCUDA_{job_id[:8]}.xml"'})


@app.get("/api/jobs/{job_id}/brand-model-size.xls")
def download_brand_model_size(job_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    """Per-item BRAND / MODEL / SIZE workbook — built alongside the XML."""
    art = services.latest_bms(db, job_id)
    if not art:
        raise HTTPException(404, "brand-model-size not built")
    return Response(content=art.xls_bytes, media_type="application/vnd.ms-excel",
                    headers={"Content-Disposition":
                             f'attachment; filename="brand-model-size_{job_id[:8]}.xls"'})


@app.get("/api/jobs/{job_id}/audit")
def audit(job_id: str, db: Session = Depends(db_dep),
                      principal: str = Depends(principal_dep)):
    job = services.get_job(db, job_id, principal=principal)
    if not job:
        raise HTTPException(404, "job not found")
    return [{"code": e.event_code, "actor": e.actor, "detail": e.detail,
             "at": e.created_at.isoformat(), "payload": e.payload}
            for e in sorted(job.events, key=lambda x: x.created_at)]


# --------------------------------------------------------------------------- #
# Static frontend — mounted last so /api/* wins.
# Serves the built Vite app (frontend/dist) if present, else the zero-build
# single-file SPA in frontend/.
# --------------------------------------------------------------------------- #
_FRONTEND_ROOT = Path(__file__).resolve().parent.parent.parent / "frontend"
_FRONTEND_DIR = _FRONTEND_ROOT / "dist" if (_FRONTEND_ROOT / "dist").exists() else _FRONTEND_ROOT
if (_FRONTEND_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    def _root():
        return "<h1>Easy Customs XML Generator API</h1><p>Frontend not found. See /docs for the API.</p>"
