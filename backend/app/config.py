"""Application configuration.

Everything is environment driven so the same code runs against SQLite (zero
setup) or Postgres (production).  Rule-version identifiers live here too so a
declaration can record *which* rule set produced it (see the ADR notes in the
README).
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_DIR = BACKEND_ROOT / "reference_data"
SAMPLE_DIR = BACKEND_ROOT / "sample_data"

# ADR-003, resolved 2026-07-21 against the reference declaration.  Kept as a
# named constant so the value the rules were decided on is greppable from
# anywhere that reports or checks it (see `Settings.net_to_gross_ratio_note`).
ADR_003_NET_TO_GROSS_RATIO = Decimal("0.7")


def _real_key(value: str | None) -> str | None:
    """Treat blanks and obvious placeholders ('***', 'your_..._here') as unset
    so the offline fallback still kicks in with a template .env."""
    if not value:
        return None
    v = value.strip()
    if not v or "*" in v or v.lower().startswith("your_"):
        return None
    return v


def _alias(name: str) -> AliasChoices:
    """Accept both the EASYCUSTOMS_-prefixed and the bare env name (as used in
    backend/.env); the prefixed form wins when both are set."""
    return AliasChoices(f"EASYCUSTOMS_{name}", name)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EASYCUSTOMS_",
        # Absolute path so backend/.env loads regardless of the cwd uvicorn
        # was started from ("." kept for the documented cd-backend flow).
        env_file=(str(BACKEND_ROOT / ".env"), ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    # ---- Persistence -------------------------------------------------------
    # Default: local SQLite file.  For Postgres set e.g.
    #   EASYCUSTOMS_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/easycustoms
    database_url: str = f"sqlite:///{BACKEND_ROOT / 'easy_customs.db'}"

    # Connection pool (Postgres only — SQLAlchemy pools a SQLite FILE too, but
    # the knobs that matter there are the WAL pragmas in database.py, not these).
    #
    # These exist because the fleet's total connection count is a number someone
    # has to CHOOSE.  Left at the SQLAlchemy defaults (5 + 10) they are invisible
    # per process and catastrophic in aggregate: N API tasks and M extraction
    # workers each open up to 15, so a 10 + 75 fleet asks a Postgres whose
    # max_connections is 500 for more than 1200.  The failure is not a slow
    # query, it is `FATAL: sorry, too many clients already` on an arbitrary
    # request, and it arrives the first time the worker fleet scales up under
    # load — i.e. exactly when the queue is deepest.
    #
    # Size them as:  tasks x (pool_size + max_overflow) <= the server's ceiling,
    # minus headroom for migrations and psql.  Behind PgBouncer/RDS Proxy the
    # ceiling that matters is the POOLER's, not the database's.
    #
    # The defaults are 10 + 30 rather than SQLAlchemy's 5 + 10 so that capacity
    # MATCHES `threadpool_max_threads` below.  That relationship is the point:
    # Starlette already lets 40 threadpool requests run at once, so a 15
    # connection pool was always the tighter of the two limits and the surplus
    # 25 queued on it silently.  Persistent cost stays 10; the other 30 are
    # burst connections the pool opens under load and closes again.
    db_pool_size: int = Field(default=10, ge=1, validation_alias=_alias("DB_POOL_SIZE"))
    db_max_overflow: int = Field(default=30, ge=0, validation_alias=_alias("DB_MAX_OVERFLOW"))
    # Recycle before any upstream idle-reaper (RDS Proxy, PgBouncer, an NLB) can
    # drop a pooled connection underneath us: a reaped-but-pooled connection
    # surfaces as a random OperationalError on a healthy request.
    db_pool_recycle_seconds: int = Field(default=1800,
                                         validation_alias=_alias("DB_POOL_RECYCLE_SECONDS"))
    # One round-trip per checkout, in exchange for never handing a handler a
    # connection the server has already closed.  Worth it on any deployment
    # where something sits between this process and Postgres.
    db_pool_pre_ping: bool = Field(default=True, validation_alias=_alias("DB_POOL_PRE_PING"))

    # ---- Concurrency -------------------------------------------------------
    # Starlette runs every `def` route (and every run_in_threadpool call) on an
    # AnyIO thread limiter whose default is 40 PER PROCESS.  Uploads, extraction
    # and finalize all go through it, so 40 is the real per-task concurrency
    # ceiling on precisely the slowest routes — reached long before CPU is.
    #
    # Raising it is only safe up to the connection pool: a thread that gets past
    # the limiter and then waits on an exhausted pool has just moved the queue
    # somewhere with a 30s timeout and a worse error message.  The validator
    # below enforces that relationship rather than trusting two numbers in a
    # .env to stay in step.
    threadpool_max_threads: int = Field(default=40, ge=1,
                                        validation_alias=_alias("THREADPOOL_MAX_THREADS"))

    # How long a review/finalize/mutation waits for another one to let go of the
    # SAME job (services.job_lock).  Postgres deployments only — the SQLite path
    # uses an in-process lock that has no timeout to give.
    #
    # 0 (default) = wait indefinitely, which is what the in-process lock has
    # always done.  The reason to set it on a real deployment is that "wait
    # forever" and "wedged" look identical from a browser: a request that has
    # been holding the lock since a hung upstream call keeps every later request
    # on that job queued behind it with no error anywhere.  A finite value turns
    # that into a 409 the reviewer can act on.  Size it above your slowest
    # legitimate finalize, not below.
    job_lock_timeout_seconds: float = Field(default=0.0, ge=0,
                                            validation_alias=_alias("JOB_LOCK_TIMEOUT_SECONDS"))

    @model_validator(mode="after")
    def _threadpool_fits_the_pool(self) -> "Settings":
        if self.database_url.startswith("sqlite"):
            return self                       # single writer; the pool is not the bound
        capacity = self.db_pool_size + self.db_max_overflow
        if self.threadpool_max_threads > capacity:
            raise ValueError(
                f"EASYCUSTOMS_THREADPOOL_MAX_THREADS={self.threadpool_max_threads} exceeds the "
                f"connection pool ({self.db_pool_size} + {self.db_max_overflow} = {capacity}). "
                f"Every one of those threads runs a request that wants a connection, so the "
                f"surplus would queue on the pool and time out instead of being refused. "
                f"Raise EASYCUSTOMS_DB_POOL_SIZE / _DB_MAX_OVERFLOW with it.")
        return self

    # ---- Queue (async extraction) -------------------------------------------
    # "off" (default): POST /extract runs OCR + LLM inside the API request,
    # exactly as before.  "sqs": the endpoint only CLAIMS the document
    # (status -> EXTRACTING) and enqueues a message; backend/worker.py does the
    # slow work.  The SPA needs no change — it already treats EXTRACTING as
    # "poll until the claim is released, then continue by itself".
    #
    # Queue mode requires a database BOTH processes can reach.  SQLite only
    # qualifies when the API and the worker share one machine (WAL mode); for
    # separate machines use Postgres, or the worker will extract into a
    # database the API never reads.
    queue_provider: str = Field(default="off", validation_alias=_alias("QUEUE_PROVIDER"))
    sqs_queue_url: str = Field(default="", validation_alias=_alias("SQS_QUEUE_URL"))
    # Region for the SQS client.  Empty = boto3's own chain (env, shared
    # config, or the EC2/ECS instance role — the right credential source in
    # production; long-lived keys in a server's .env are the fallback, not the
    # goal).
    sqs_region: str = Field(default="", validation_alias=_alias("AWS_REGION"))
    # Dev-machine credential fallback.  A line in backend/.env is read by
    # pydantic into THIS object only — it never reaches os.environ, so boto3's
    # own chain cannot see it.  When both are set they are handed to the
    # client explicitly (app/queueing.make_sqs_client); when either is blank
    # boto3 falls back to its chain, which on EC2/ECS is the instance role.
    aws_access_key_id: str = Field(default="", validation_alias=_alias("AWS_ACCESS_KEY_ID"))
    aws_secret_access_key: str = Field(default="", validation_alias=_alias("AWS_SECRET_ACCESS_KEY"))
    # Worker only: documents one worker process extracts concurrently.  The
    # work is I/O-bound (OCR + LLM calls), so this is an HTTP-connection
    # count, not a CPU count — but every slot multiplies the vendor-API and
    # DB load, so raise it deliberately.
    worker_concurrency: int = Field(default=4, ge=1, le=32,
                                    validation_alias=_alias("WORKER_CONCURRENCY"))
    # Worker only: while a message is being processed its invisibility is
    # re-extended to `extend` every `heartbeat` seconds, so a message returns
    # to the queue ONLY when its worker died — redelivery is the crash
    # recovery, replacing the single-process startup sweep
    # (services.recover_interrupted_extractions), which queue mode disables.
    sqs_heartbeat_seconds: int = Field(default=120, ge=10, le=3600,
                                       validation_alias=_alias("SQS_HEARTBEAT_SECONDS"))
    sqs_visibility_extend_seconds: int = Field(default=600, ge=60, le=43199,
                                               validation_alias=_alias("SQS_VISIBILITY_EXTEND_SECONDS"))

    @field_validator("queue_provider")
    @classmethod
    def _check_queue_provider(cls, v: str) -> str:
        choice = (v or "off").strip().lower()
        if choice not in ("off", "sqs"):
            raise ValueError("EASYCUSTOMS_QUEUE_PROVIDER must be off or sqs")
        return choice

    @model_validator(mode="after")
    def _check_queue_config(self) -> "Settings":
        if self.queue_provider == "sqs" and not self.sqs_queue_url.strip():
            # Fail at boot, not at the first upload: a queue mode without a
            # queue URL would answer every extraction with a 502.
            raise ValueError("EASYCUSTOMS_QUEUE_PROVIDER=sqs requires SQS_QUEUE_URL")
        if (self.queue_provider == "sqs"
                and self.sqs_visibility_extend_seconds < self.sqs_heartbeat_seconds + 60):
            # The extension must comfortably outlive the beat interval, or a
            # single slow heartbeat hands the message to a second worker while
            # the first is mid-extraction — the exact double-spend the
            # heartbeat exists to prevent.  Gated on queue mode: these fields
            # also bind BARE env names (SQS_HEARTBEAT_SECONDS), and a stale or
            # unrelated line in someone's environment must not stop a
            # deployment that is not using the queue at all.
            raise ValueError("SQS_VISIBILITY_EXTEND_SECONDS must exceed "
                             "SQS_HEARTBEAT_SECONDS by at least 60")
        return self

    # ---- Authentication (see app/auth.py) ----------------------------------
    # One operator account, from the environment.  Unset = FAIL CLOSED: no
    # token can be issued and every protected route answers 401, so a
    # deployment that forgot to configure the account is visibly broken rather
    # than quietly serving an importer's invoices to the whole network.
    auth_username: str | None = Field(default=None, validation_alias=_alias("AUTH_USERNAME"))
    auth_password: str | None = Field(default=None, validation_alias=_alias("AUTH_PASSWORD"))
    # HMAC key for login tokens.  Unset = a random per-process key, which signs
    # every session out on restart (including each uvicorn --reload).
    auth_secret: str | None = Field(default=None, validation_alias=_alias("AUTH_SECRET"))
    # Token lifetime.  24h = "log in once a working day" (user rule 2026-08-02).
    auth_token_ttl_hours: float = Field(default=24.0, validation_alias=_alias("AUTH_TOKEN_TTL_HOURS"))
    # Whether the session cookie carries `Secure`.
    #
    #   auto   (default) — set it when THIS request arrived over TLS.
    #   always           — set it unconditionally.  Correct for any deployment
    #                      reached over https, and the only setting that is safe
    #                      when TLS is terminated upstream.
    #   never            — never set it.  Plain-http LAN only.
    #
    # "auto" reads request.url.scheme, and behind a reverse proxy that speaks
    # plain http to this app that scheme is "http" even though the browser is on
    # https — so the cookie went out without `Secure` and a single downgraded
    # request would put the session token on the wire in clear.  uvicorn only
    # believes X-Forwarded-Proto from an address in FORWARDED_ALLOW_IPS (default
    # 127.0.0.1), which a proxy in another container is not.  Rather than make
    # session security depend on getting that list right, a TLS deployment
    # should say so outright.
    session_cookie_secure: str = Field(default="auto",
                                       validation_alias=_alias("SESSION_COOKIE_SECURE"))
    # How many proxies of YOURS sit in front of this app.  Decides where the
    # caller's real IP is read from for the login throttle (auth.client_key).
    #
    #   0 (default) — nothing trusted in front. The socket peer is the key and
    #                 X-Forwarded-For is ignored entirely, because a caller that
    #                 can set that header would otherwise choose its own throttle
    #                 key and reset its failure count at will.
    #   1           — one reverse proxy / load balancer (Caddy, nginx, an ALB).
    #   2           — e.g. CloudFront in front of an ALB.
    #
    # This is NOT optional behind a proxy, and getting it wrong is not a subtle
    # degradation.  Left at 0, every request appears to come from the proxy, so
    # all callers share ONE throttle bucket: ten failed sign-ins from anywhere
    # lock out every user of the system, and an unauthenticated attacker can
    # trigger that deliberately.  Set too HIGH, the key is read from a position
    # the caller controls, and the throttle stops limiting anyone.
    #
    # Count only hops you operate. Each appends the address it received the
    # request from, so the real client is the hops-th entry from the right.
    trusted_proxy_hops: int = Field(default=0, ge=0,
                                    validation_alias=_alias("TRUSTED_PROXY_HOPS"))

    @field_validator("session_cookie_secure")
    @classmethod
    def _check_cookie_secure(cls, v: str) -> str:
        choice = (v or "auto").strip().lower()
        if choice not in ("auto", "always", "never"):
            raise ValueError("EASYCUSTOMS_SESSION_COOKIE_SECURE must be auto, always or never")
        return choice
    # Comma-separated origins allowed to call the API cross-origin.  EMPTY BY
    # DEFAULT, which disables CORS entirely: the SPA is served by this same app,
    # so nothing legitimate is cross-origin.  A wildcard here would let any page
    # on the internet drive the public login route — guessing credentials and
    # pinning the operator's IP at the failure throttle.
    cors_origins: str = Field(default="", validation_alias=_alias("CORS_ORIGINS"))
    # Replay hook: allow POST /documents/{role} to carry a `fixture` JSON blob
    # that BECOMES the document's extraction, skipping OCR, the model and the
    # evidence/anchor validators.  OFF unless explicitly enabled, because on a
    # real deployment it is a way to put unverified facts into a legally binding
    # declaration with nothing on the row recording that they were supplied
    # rather than extracted.  The test suite and the offline demo turn it on.
    allow_fixture_uploads: bool = Field(default=False,
                                        validation_alias=_alias("ALLOW_FIXTURE_UPLOADS"))

    # ---- Object storage ----------------------------------------------------
    # WHERE uploaded documents live.
    #
    #   "local" (default) — a directory on this machine.  Correct for a broker's
    #                       laptop or a single server, and the reason the app
    #                       needs no cloud account to run.
    #   "s3"              — an S3 bucket.
    #
    # This is the difference between one API instance and several.  A local
    # directory is not shared: the instance that did NOT receive the upload has
    # no such file, so the reviewer's evidence panel answers 410 depending on
    # which instance the load balancer picked.  Queue mode makes it worse — the
    # worker that OCRs a document is a different machine from the API that
    # stored it, so with local storage it has nothing to read.
    #
    # Switching is safe in one direction and only one: keys already written are
    # read by the backend that WROTE them (app/storage.py dispatches on the key,
    # not on this setting), so turning s3 on leaves existing local documents
    # readable.  Turning it back off does not make S3 documents local.
    storage_backend: str = Field(default="local", validation_alias=_alias("STORAGE_BACKEND"))
    storage_dir: Path = BACKEND_ROOT / "storage"
    s3_bucket: str = Field(default="", validation_alias=_alias("S3_BUCKET"))
    # Key prefix inside the bucket, for sharing one bucket between environments
    # ("staging/", "prod/").  Documents are written under <prefix>jobs/<job>/...
    s3_prefix: str = Field(default="", validation_alias=_alias("S3_PREFIX"))
    # Empty = boto3's own chain (env, shared config, or the EC2/ECS task role —
    # the right credential source in production), same rule as sqs_region.
    s3_region: str = Field(default="", validation_alias=_alias("S3_REGION"))

    @field_validator("storage_backend")
    @classmethod
    def _check_storage_backend(cls, v: str) -> str:
        choice = (v or "local").strip().lower()
        if choice not in ("local", "s3"):
            raise ValueError("EASYCUSTOMS_STORAGE_BACKEND must be local or s3")
        return choice

    @model_validator(mode="after")
    def _check_storage_config(self) -> "Settings":
        if self.storage_backend == "s3" and not self.s3_bucket.strip():
            # At boot, not at the first upload: a bucket-less s3 backend would
            # accept a document, fail to store it, and lose it.
            raise ValueError("EASYCUSTOMS_STORAGE_BACKEND=s3 requires EASYCUSTOMS_S3_BUCKET")
        return self
    # Upload ceiling in megabytes.  Real customs paperwork tops out around a
    # few MB; anything far beyond that is a mis-pick (a video, an installer)
    # that would sit in "uploading…" for minutes and then cost a full OCR run.
    max_upload_mb: int = Field(default=25, validation_alias=_alias("MAX_UPLOAD_MB"))
    # Minimum short-edge resolution for photo uploads.  Below this, OCR reads
    # digits unreliably — refused at upload with a "retake" message rather
    # than extracting confidently wrong numbers into a legal declaration.
    # (Any phone from the last decade shoots 3000px+; what this catches is
    # thumbnails, screenshots and heavy crops.)
    min_photo_px: int = Field(default=1000, validation_alias=_alias("MIN_PHOTO_PX"))
    # Upper bound on a photo's PIXEL COUNT, checked from the header before any
    # decode.  Pillow only raises above ~179M pixels and merely warns between
    # ~89M and there, so the band in between decodes ~500 MB of raster (tripled
    # again by the alpha-flatten path).  80M ~= a 10000x8000 shot, far above any
    # phone page photo.  0 disables the check.
    max_photo_pixels: int = Field(default=80_000_000, validation_alias=_alias("MAX_PHOTO_PIXELS"))
    # How many photos may be merged into ONE document.  Each is read whole into
    # memory before any validation runs, so an unbounded list is an unbounded
    # allocation.  0 disables the check.
    max_photos_per_document: int = Field(default=40,
                                         validation_alias=_alias("MAX_PHOTOS_PER_DOCUMENT"))
    # Ceiling on a whole REQUEST BODY, enforced at the ASGI boundary before
    # anything parses it.  The two caps above cannot do this job: Starlette
    # spools every multipart part to a temporary file while it builds the form,
    # which happens BEFORE the route function runs, so `max_upload_mb` was only
    # ever a cap on bytes that had already arrived and been written down.
    #
    # 0 uses the derived default below rather than disabling the check — an
    # unbounded request body is not a configuration anyone should reach for by
    # accident.  Set it explicitly if a deployment really needs a bigger one.
    max_request_mb: int = Field(default=0, validation_alias=_alias("MAX_REQUEST_MB"))

    def request_body_limit_bytes(self) -> int:
        """The whole-body ceiling in bytes.

        Derived from ``max_upload_mb`` so raising the document limit raises this
        with it.  The largest legitimate body is one photo set, and img2pdf is
        lossless, so the parts sum to roughly the merged PDF that must itself
        clear ``max_upload_mb`` — double that, with a 64 MB floor, leaves room
        for multipart framing and for a set that merges smaller than its parts.
        """
        if self.max_request_mb > 0:
            return self.max_request_mb * 1024 * 1024
        return max(64, self.max_upload_mb * 2) * 1024 * 1024

    # ---- Extraction provider ----------------------------------------------
    # OCR provider:        "mistral" (default) | "offline" (pypdf, no keys)
    # Extraction provider: "openai"  (default, direct structured output)
    #                    | "langroid" (report's ChatAgent + ToolMessage design)
    #                    | "offline"  (fixtures / heuristics, no keys)
    # When a live provider is selected but its key is missing, the code falls
    # back to the offline path with a warning so the app still boots and the
    # bundled demo / test-suite keep working without keys.
    ocr_provider: str = Field(default="mistral", validation_alias=_alias("OCR_PROVIDER"))
    extraction_provider: str = "openai"
    mistral_api_key: str | None = Field(default=None, validation_alias=_alias("MISTRAL_API_KEY"))
    llm_api_key: str | None = Field(default=None, validation_alias=_alias("OPENAI_API_KEY"))
    llm_model: str = "gpt-4o-mini"          # any structured-output-capable model
    mistral_ocr_model: str = Field(default="mistral-ocr-latest", validation_alias=_alias("MISTRAL_OCR_MODEL"))
    extraction_max_repair_rounds: int = 2
    # Global cap on concurrent LLM calls (shared across all documents and
    # windows — one OpenAI tokens-per-minute budget). Raise to 3-4 only if the
    # account's TPM limit allows; the 429 backoff self-protects either way.
    # 4 rather than 2: a document under a time budget spends that budget queued
    # behind undeadlined work, and the 429 backoff self-protects if the
    # account's TPM cannot take it.  A deadlined extraction also gets one
    # RESERVED slot on top of this (openai_extractor._llm_slot).
    llm_concurrency: int = Field(default=4, validation_alias=_alias("LLM_CONCURRENCY"))
    # Per-request timeout on LLM calls. Our own retry loop handles transients,
    # so the OpenAI SDK's internal retries are disabled — without this a hung
    # request looks like "extracting…" for the SDK default 600s x 3 attempts.
    llm_timeout_seconds: float = Field(default=180.0, validation_alias=_alias("LLM_TIMEOUT_SECONDS"))
    # End-to-end timeout on Mistral OCR HTTP calls (upload / signed-url / process).
    mistral_ocr_timeout_seconds: float = Field(default=120.0, validation_alias=_alias("MISTRAL_OCR_TIMEOUT_SECONDS"))
    # Packing-list extraction time budget (user rule 2026-07-17): when OCR +
    # LLM reasoning exceed this, the job proceeds but weight/carton allocation
    # uses the quantity-proportional fallback and the reviewer is warned.
    packing_extraction_budget_seconds: float = Field(
        default=240.0, validation_alias=_alias("PACKING_EXTRACTION_BUDGET_SECONDS"))
    # Long INVOICE / PACKING_LIST documents are extracted in page windows so a
    # single LLM response stays small: a 21-page invoice in one shot risks
    # output truncation, and one repair round then re-requests everything.
    # Hard ceiling on the pages of ONE document that will be sent to the LLM.
    # Nothing bounded the fan-out before: at the 4-page window a 500-page PDF is
    # 125 model calls for a single /extract, repeatable on every retry.  Real
    # customs paperwork does not come close; 0 disables the check.
    extraction_max_pages: int = Field(default=150, validation_alias=_alias("EXTRACTION_MAX_PAGES"))
    extraction_chunk_page_threshold: int = 6   # chunk when pages > threshold
    extraction_chunk_page_size: int = 4        # pages per window
    # Packing lists get SMALLER windows.  Their pages are dense tables whose
    # rows rarely straddle a page break, so the 4+2 shape mostly bought
    # re-sending each page up to three times as context — on the one document
    # that has a time budget to lose.
    extraction_chunk_page_size_packing: int = 2
    # Deterministic-first extraction: parse goods rows from OCR markdown
    # tables in code (arithmetic-verified) and call the LLM only for pages
    # the parser could not fully own, plus one header/totals call.
    deterministic_table_parser_enabled: bool = True
    # Remember proven table layouts per vendor (the `vendor_layout` table) so
    # headerless/garbled-header documents still parse deterministically;
    # arithmetic verification still gates every row.
    vendor_layout_memory_enabled: bool = True
    # Remember per-vendor field-allocation defaults learned from finalized /
    # reviewer-corrected jobs (the `vendor_field_profile` table) — today the
    # vendor's default COO, proposed (with a warning) before the exporter-
    # country fallback.  Read by extraction/field_profiles.py.
    #
    # Both were JSON files under storage/ until 2026-08-11.  They are tables
    # because a whole-file read-modify-write loses one writer's entries the
    # moment a second process records at the same time, and a local file is
    # invisible to a second container at all.
    vendor_field_profiles_enabled: bool = True

    # ---- Mistral OCR request options ---------------------------------------
    mistral_ocr_include_image_base64: bool = Field(default=False, validation_alias=_alias("MISTRAL_OCR_INCLUDE_IMAGE_BASE64"))

    # NOTE — settings that describe behaviour must be READ by that behaviour.
    # An OCR fallback provider, a `quality_gate` mode and an
    # `ocr_min_confidence` threshold used to be declared, documented, validated
    # and published on /api/config, and no code path consumed any of them.
    # `backend/.env` carried OCR_FALLBACK_ENABLED=true and
    # OCR_MIN_CONFIDENCE=0.80, which reads as "a quality gate is checking my
    # OCR" — a claim of protection that did not exist.  Removed, along with the
    # unread Mistral header/footer/table-format flags and the OpenAI PDF
    # fallback, audit-mode and repair-candidate flags.  `extra="ignore"` means a
    # stale .env line is inert rather than fatal.  Re-add a setting in the same
    # commit as the code that obeys it.

    # ---- OpenAI extraction options ------------------------------------------
    # reasoning_enabled + reasoning_model pick the chat model via
    # resolved_llm_model(). reasoning_fallback_model is the FAST tier: row
    # windows (chunked + parser-residue) try it first and escalate to the
    # primary model when a repair round fires (resolved_fast_llm_model()).
    openai_reasoning_enabled: bool = Field(default=False, validation_alias=_alias("OPENAI_REASONING_ENABLED"))
    openai_reasoning_model: str | None = Field(default=None, validation_alias=_alias("OPENAI_REASONING_MODEL"))
    openai_reasoning_fallback_model: str | None = Field(default=None, validation_alias=_alias("OPENAI_REASONING_FALLBACK_MODEL"))

    # ---- Reference data paths (consumed by reference/store.py) -------------
    bank_csv_path: Path = Field(default=REFERENCE_DIR / "bank_code_and_bank_names_with_swift_code.csv",
                                validation_alias=_alias("BANK_CSV_PATH"))
    terms_csv_path: Path = Field(default=REFERENCE_DIR / "terms_of_payments_and_codes.csv",
                                 validation_alias=_alias("TERMS_CSV_PATH"))
    hs_excel_path: Path = Field(default=REFERENCE_DIR / "HS_with_ALL_descriptions_AND_UNITS.xlsx",
                                validation_alias=_alias("HS_EXCEL_PATH"))

    # ---- Declaration defaults (Nepal ASYCUDA) ------------------------------
    # Per-job REVIEW DEFAULTS, not constants: since 2026-08-01 every one of
    # these seeds the Critical Review and is reviewer-selectable per job,
    # validated against the reference tables (customs_offices.csv,
    # declaration_models.csv, extended/national_procedures.csv).  The values
    # here are the deployment's home office / standard regime.
    customs_office_code: str = "TIA00"
    customs_office_name: str = "TIA Customs Office"
    declaration_type: str = "IM"
    declaration_gen_procedure_code: str = "4"
    # Box 37 defaults when the reviewer selects nothing (user rule 2026-08-01)
    extended_customs_procedure: str = "4000"
    national_customs_procedure: str = "000"
    place_of_loading_code: str = "NPKTM"
    place_of_loading_name: str = "KATHMANDU"
    # Box 30 default for the home office+regime; reviewer-editable per job.
    location_of_goods: str = "TIA...IM/GODOWN"
    # Border_information nationality default and the reviewed package-kind
    # default.  The border and
    # inland MODES of transport (Box 25/26) deliberately have NO defaults
    # (user rule 2026-08-01: never silently emit 01/09 — the reviewer must
    # choose them per job; finalize blocks until they are selected).
    border_nationality: str = "NP"
    package_type_default: str = "CT"

    # False (default, user rule 2026-07-18): blocking cases WARN but never
    # stop XML generation — the reviewer tests the XML in real ASYCUDA and
    # sees a pop-up listing every unresolved case.  True restores hard blocks.
    xml_strict_blocking: bool = False

    # NRB mid exchange rate (NPR per 1 unit foreign).  Overridable per job.
    default_exchange_rate: Decimal = Decimal("145.76")
    national_currency: str = "NPR"
    # Which foreign currency `default_exchange_rate` is quoted FOR.  Needed to
    # know when the rate a reviewer enters is comparable to the default: a JPY
    # or KRW invoice legitimately sits orders of magnitude away from a USD
    # rate, so the plausibility check must skip it rather than cry wolf.
    default_exchange_rate_currency: str = "USD"

    # ---- Rule-set versioning (see ADR table in README) --------------------
    rule_set_version: str = "2026.07.15"
    # ADR-003 RESOLVED (user decision 2026-07-21) in favour of 0.7: every item
    # in the reference declaration satisfies Net_weight_itm = 0.7 x
    # Gross_weight_itm (1.064 = 0.7 x 1.52, 0.105 = 0.7 x 0.15, ...), and
    # README.md has always published 0.7.  Only this default and the older rule
    # text said 0.3.  Applies to every item with no packing net, no invoice
    # weight and no convertible description.
    default_net_to_gross_ratio: Decimal = ADR_003_NET_TO_GROSS_RATIO
    # ADR-004: divide PCS by 2 for PR/NPR per newer rule, or keep as-is (sample).
    pair_divide_by_two: bool = True
    # Freight/insurance item allocation basis: "value" (matches sample XML) or
    # "gross_weight" (matches freight_rules.txt preferred basis).
    cost_allocation_basis: str = "value"
    # ADR-006: never silently default unknown payment terms to LC.
    default_unknown_payment_terms_to_lc: bool = False

    # v2 (2026-07-30): rules 11/11c/14 define description/COO/brand/model/size
    # scope + the schema gained per-field descriptions and invoice-row
    # batch/lot/serial/expiry homes (live-job field-misallocation root cause).
    prompt_version: str = "extract-v2"
    extraction_schema_version: str = "raw-v1"

    # ---- Auth guards -------------------------------------------------------
    @field_validator("auth_token_ttl_hours")
    @classmethod
    def _ttl_is_positive(cls, v: float) -> float:
        """A non-positive lifetime issues tokens that are already expired: the
        operator would type the right password and be told, every time, that
        their session had ended.  Refuse it where it enters."""
        if not (v > 0):
            raise ValueError(f"EASYCUSTOMS_AUTH_TOKEN_TTL_HOURS={v} must be greater than 0 — "
                             f"a login token has to outlive the request that issued it.")
        return v

    # ---- Rule-set guards ---------------------------------------------------
    @field_validator("default_net_to_gross_ratio")
    @classmethod
    def _ratio_is_a_ratio(cls, v: Decimal) -> Decimal:
        """`net = r x gross` is only below gross when 0 < r < 1.

        This value multiplies the FINAL reconciled gross of every item with no
        packing net, no invoice weight and no convertible description — on the
        reference shipment, all 119 of them.  An out-of-range value does not
        fail loudly: at r >= 1 every ratio item asserts a net at or above its
        own gross and the declaration is rejected item by item; at r <= 0 it
        declares a zero or negative net weight.  Refuse it where it enters.
        """
        if not v.is_finite() or not (Decimal("0") < v < Decimal("1")):
            raise ValueError(
                f"EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO={v} is not a ratio strictly "
                f"between 0 and 1. Net weight is derived as ratio x gross weight, so it "
                f"must sit below gross for every item. ADR-003 resolved this to "
                f"{ADR_003_NET_TO_GROSS_RATIO} (see docs/allocation-spec.md section 5).")
        return v

    def net_to_gross_ratio_note(self) -> str | None:
        """Audit line when the configured ratio is not the ADR-003 value.

        The ratio drifted between 0.3 and 0.7 for months because it lived in
        documents with no connection to the code, and a stale `.env` line
        outranks the code default silently — every ratio-derived net weight
        changes and nothing on screen says so.  Returns None when they agree.
        """
        if self.default_net_to_gross_ratio == ADR_003_NET_TO_GROSS_RATIO:
            return None
        return (f"net-to-gross ratio {self.default_net_to_gross_ratio} overrides the "
                f"ADR-003 resolved value {ADR_003_NET_TO_GROSS_RATIO}; every item with no "
                f"packing net, no invoice weight and no convertible description takes its "
                f"net weight from this number")

    # ---- Resolved keys (placeholder values like '***' count as unset) ------
    def resolved_mistral_key(self) -> str | None:
        return _real_key(self.mistral_api_key)

    def resolved_openai_key(self) -> str | None:
        return _real_key(self.llm_api_key)

    def resolved_llm_model(self) -> str:
        if self.openai_reasoning_enabled and self.openai_reasoning_model:
            return self.openai_reasoning_model
        return self.llm_model

    def resolved_fast_llm_model(self) -> str | None:
        """Fast tier for mechanical row windows; None = single-model mode.
        Never used for whole-document judgement calls (role validation, AWB
        form splitting, banking) or the header/totals call."""
        m = (self.openai_reasoning_fallback_model or "").strip()
        return m if m and not m.lower().startswith("your_") else None

    @property
    def ocr_live_ready(self) -> bool:
        return self.ocr_provider == "mistral" and bool(self.resolved_mistral_key())

    @property
    def extraction_live_ready(self) -> bool:
        return self.extraction_provider in ("openai", "langroid") and bool(self.resolved_openai_key())


@lru_cache
def get_settings() -> Settings:
    s = get_settings_uncached()
    s.storage_dir.mkdir(parents=True, exist_ok=True)
    return s


def get_settings_uncached() -> Settings:
    return Settings()
