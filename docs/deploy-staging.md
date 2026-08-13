# Deploying a staging environment — one Linux box, Caddy, Supabase identity

This is a runbook for someone who has never deployed this application. It brings
up **one** staging environment on **one** Linux box: the API and the extraction
worker as two systemd units, Caddy in front for TLS, Postgres and identity from a
Supabase project, documents in S3, extraction jobs over SQS.

Read it in order. Each section states what it creates, what breaks if it is
skipped, and how to tell it worked.

**What this is not.** It is not the production shape and it does not pretend to
be. It is one box with no redundancy: a reboot is an outage, and the box itself
is the backup story. It exists to exercise the things a laptop cannot — TLS, a
real reverse proxy, a real object store, a real queue, and above all **the email
loop**, which is the one part of this app that cannot be tested any other way
(see [What cannot be verified until it is deployed](#13-what-cannot-be-verified-until-it-is-deployed)).

**Never put customer documents on it.** `backend/scripts/generate_sample_documents.py`
produces synthetic paperwork; that is what the smoke test uses.

---

## 0. Decisions this runbook makes for you

| Decision | Choice here | Where it is argued |
| --- | --- | --- |
| Identity provider | Supabase | given |
| Supabase project | **A separate project for staging**, not production's | §9 |
| Postgres | **That staging project's own Postgres** | §2 |
| Pooling mode | **Session mode or direct — never transaction mode** | §2 |
| Documents | S3, not the local disk | §3 |
| Extraction | Queue mode on (`sqs`), both units on this one box | §4 |
| Self-service signup | On — it is what the smoke test exercises | §5 |
| Monthly document cap | Left unset → 200/account/month | §5 |

---

## 1. The box and its security group

### The instance

* **OS**: any current long-term-support Linux with systemd. The units in
  `backend/deploy/` assume a `systemd` box and nothing else.
* **Python**: 3.10–3.14. `backend/requirements.txt` is version-ranged on purpose
  so a new interpreter can still find wheels.
* **Size**: 2 vCPU / 4 GB is enough for staging. The work this box does is
  waiting on Mistral and OpenAI, not computing — extraction is I/O bound. Disk
  matters less than you would think once documents live in S3; 20 GB covers the
  OS, the venv and the logs.
* **A dedicated service user.** Both units run as `User=easycustoms`. Create it
  with no login shell and no password.

```bash
sudo useradd --system --home-dir /opt/easycustoms --shell /usr/sbin/nologin easycustoms
```

* **The repo at `/opt/easycustoms`, as a git clone — never a folder copy.** The
  rollback in §14 is `git checkout` of the previous tag, and a copied folder has
  nothing to check out.

```bash
sudo git clone <your-repo-url> /opt/easycustoms && sudo chown -R easycustoms:easycustoms /opt/easycustoms
```

```bash
sudo -u easycustoms python3 -m venv /opt/easycustoms/backend/.venv
```

```bash
sudo -u easycustoms /opt/easycustoms/backend/.venv/bin/pip install -r /opt/easycustoms/backend/requirements.txt
```

* **An instance role, not access keys.** Attach an IAM role to the instance; §3
  and §4 give it its two policies. `boto3` finds an instance role by itself.
  `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in `backend/.env` exist for a
  developer's laptop and are the wrong answer on a server — a long-lived key in a
  file survives the box, and a role's credentials do not.

### The security group

| Direction | Port | Source | Why |
| --- | --- | --- | --- |
| Inbound | 443 | `0.0.0.0/0` | the application |
| Inbound | 80 | `0.0.0.0/0` | **not optional** — Caddy's ACME HTTP challenge renews the certificate here. Closing it works right up until the certificate expires and takes the whole app offline |
| Inbound | 22 | your own address only | administration |
| Inbound | 8000 | **nowhere** | the API binds `127.0.0.1` and must not be reachable. It speaks plain HTTP |
| Outbound | 443 | anywhere | Supabase, Mistral, OpenAI, S3, SQS, Let's Encrypt |
| Outbound | 5432 (or the pooler's port) | the database | see §2 |

The API listening on loopback is what makes Caddy the entire public surface. If
you can reach `http://<box>:8000` from your laptop, stop and fix that before
going further — that address serves an importer's documents with no TLS.

---

## 2. Postgres

### The recommendation: use the staging Supabase project's own Postgres

**For staging, yes — put the application's tables in the same Postgres that
Supabase project runs.** Not a separate RDS instance.

The argument, and the counter-argument, both turn on the same fact: **the app
connects as a privileged role and bypasses row-level security entirely.** That is
stated in `CLAUDE.md` and it is true wherever the database lives. Job isolation is
Python — `services.job_visible_to` — and is tested as Python. So:

* **A separate instance buys no isolation the app does not already have to
  enforce itself.** The instinct that says "keep the app's data away from the auth
  data" is really an instinct about RLS, and RLS is not in the picture. Moving the
  tables to another host does not add a boundary; it adds a host.
* **It removes a whole component from a staging environment whose purpose is to
  test something else.** No second instance to size, patch, back up, monitor,
  connect a security group to, or pay for. The failure you are trying to find on
  staging is a broken confirmation email, not a Postgres one.
* **`customs_job.owner_key` holds the Supabase `auth.users.id`.** In one database
  "who owns this job?" is a join. Across two it is a manual lookup in a dashboard,
  every time — which on staging you will do constantly.
* **No collision.** Alembic's tables (`customs_job`, `uploaded_document`,
  `usage_event`, `account_quota`, `account_role`, `account_disabled`,
  `login_attempt`, `signup_attempt`, `password_reset_attempt`, `account_seen`,
  `vendor_layout`, `vendor_field_profile`, `audit_event`, `xml_artifact`,
  `bms_artifact`) land in `public`. Supabase's own live in `auth`, `storage` and
  friends. `supabase/migrations/` also creates `public.users`,
  `public.transactions` and `public.document_generations` — different names, and
  nothing in the backend reads them.

**And the honest counter-argument, which is why this is a staging
recommendation and not a production one.** Because the app connects privileged,
a bug or a compromise in this process has the run of the same database that
holds `auth.users`. On staging, against a project with synthetic accounts and
synthetic documents, that is an acceptable trade for the operational
simplification. **On production it is not**, and this is a place where staging
and production are allowed to differ deliberately: production should get its own
Postgres, and that difference should be written down when it is made rather than
discovered.

### Getting the connection string right

Take it from the Supabase dashboard (Project Settings → Database). You will be
offered more than one, and the choice matters:

* **Direct connection** — fine, and simplest. Bounded by the project's
  `max_connections`, which on small tiers is low.
* **Session-mode pooler** — the recommendation if the direct connection's
  ceiling is tight. Each client gets a dedicated server connection for the life of
  the session, which is what SQLAlchemy's pool expects.
* **Transaction-mode pooler — do not use it.** Not because of locking: the job
  lock is deliberately `pg_advisory_xact_lock` (transaction-scoped) precisely so
  it survives a transaction pooler. The problem is prepared statements. `psycopg`
  prepares a statement after it has seen it a few times, and under transaction
  pooling the next execution can land on a different server connection that has
  never heard of it. Turning that off needs a `prepare_threshold=0` connect
  argument, and `app/database.py` builds `connect_args` empty for Postgres with no
  setting to supply one. **Nothing in this repo has been tested against
  transaction mode. Treat it as unsupported until someone adds that knob and a
  test.**

The URL goes in `.env` as:

```
EASYCUSTOMS_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<database>
```

`postgresql+psycopg` — psycopg **3**, which is what `requirements.txt` ships.
`postgresql://` alone makes SQLAlchemy look for psycopg2, which is not installed.

### Sizing the pool, because it is enforced

Three settings move together and **the app refuses to boot if they contradict**:
`EASYCUSTOMS_THREADPOOL_MAX_THREADS` must not exceed
`EASYCUSTOMS_DB_POOL_SIZE + EASYCUSTOMS_DB_MAX_OVERFLOW`.

They are **per process**. At the defaults (10 + 30, threadpool 40), this box asks
for `2 API workers × 40 + 1 worker process × 40 = 120` connections, which a small
Supabase tier will refuse — as `FATAL: sorry, too many clients already`, on an
arbitrary request, first under load.

For staging, set all three down together:

```
EASYCUSTOMS_DB_POOL_SIZE=5
EASYCUSTOMS_DB_MAX_OVERFLOW=10
EASYCUSTOMS_THREADPOOL_MAX_THREADS=15
```

That is `2 × 15 + 15 = 45` connections at full stretch, leaving headroom for
`alembic` and a `psql` session. Check it against the number the dashboard reports
before you assume it fits.

---

## 3. S3, and the IAM the instance role needs

### The bucket

One private bucket. On a single box you could leave `storage_backend=local` and
share the filesystem between the two units — but then staging is not exercising
the code production runs, and the S3 path is exactly the sort of thing that fails
on a permission rather than in a test.

* **Block all public access.** On.
* **Default encryption**: SSE-S3 is enough for staging; SSE-KMS if the
  organisation requires it. Encryption belongs to the bucket, not to this app.
* **Versioning**: on. It costs nothing at staging volume and it is what makes an
  accidental `delete_stored_document` recoverable.
* **A lifecycle rule** expiring objects under the staging prefix after 30 days.
  Staging documents are synthetic and worthless; the rule is what stops the
  bucket from growing forever unattended.
* **A prefix**, so one bucket can hold more than one environment:
  `EASYCUSTOMS_S3_PREFIX=staging/`. Documents are written under
  `<prefix>jobs/<job>/…`.

Switching to S3 is safe in one direction only. `app/storage.py` dispatches on the
**key**, not on the setting, so documents already written locally stay readable
after you turn S3 on. Turning it back off does not make S3 documents local.

### The IAM policy

Grant only what the code calls. `app/storage.py` uses `put_object`, `get_object`,
`head_object` and `delete_object` — and nothing lists, so there is no
`s3:ListBucket` here.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::<bucket>/staging/*"
  }]
}
```

`head_object` is authorised by `s3:GetObject`; there is no separate action for it.

On this box the API and the worker share one instance role, so this is the whole
policy. If you later split them: the **API** needs all three (it stores the
upload, serves it back to the evidence panel, and deletes it with the job); the
**worker** needs `s3:GetObject` only — it reads the document and writes its
findings to the database, never back to the bucket.

---

## 4. SQS

### Who owns the queue: the script

**`scripts/provision_sqs.py` owns it. Not the console.** The queues can be
*created* either way and the script is happy to audit ones it did not make — but
the reason to run it is what happens afterwards. A queue edited by hand during an
incident has no record of what it was supposed to be, and a visibility timeout
nudged or a redrive policy lost to a console mis-click goes unnoticed until the
day it matters. The script reads the live attributes back and names every
difference from the spec in its own docstring.

Run it from a **developer machine under an admin identity**, not from this box's
role: it needs `sqs:CreateQueue`, `sqs:GetQueueUrl`, `sqs:GetQueueAttributes`,
`sqs:ListQueueTags`, `sqs:SetQueueAttributes` and `sqs:TagQueue`, which is far
more than either unit should ever hold.

```bash
cd backend && python scripts/provision_sqs.py
```

```bash
cd backend && python scripts/provision_sqs.py --apply
```

Audit mode is the default and read-only. Re-run it after any incident.

It creates two queues, `customs-processing-queue` and `customs-processing-dlq`.
**The redrive policy is load-bearing, not decoration.** In this design a message
dies with its attempt — success, refusal, skip and failure all delete it — so a
redelivery means one thing only: the worker died mid-run. There is exactly one
message the worker deliberately never deletes (a well-formed body carrying a
version it does not understand, i.e. a newer producer during a rolling upgrade),
and the DLQ is its only exit. With no redrive policy it redelivers until
retention expires, waking a worker to reject it every time.
`docs/queue-deployment.md` is the full treatment.

### The IAM policy

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility"
    ],
    "Resource": "arn:aws:sqs:<region>:<account-id>:customs-processing-queue"
  }]
}
```

Nothing on the DLQ — SQS moves messages there itself. Split by unit if you later
split the boxes: the API sends, the worker receives/deletes/extends.

`docs/queue-deployment.md` also lists `sqs:GetQueueAttributes` for both. `worker.py`
does not call it; it belongs to the provisioning identity above.

---

## 5. `backend/.env`

### How this file is read — which is not how you would guess

**systemd does not read it.** Neither unit has an `EnvironmentFile=` line.
`app/config.py` loads it through pydantic-settings from an **absolute** path
compiled into the module (`BACKEND_ROOT/".env"`), which is why it works whatever
directory the process was started from. Consequences:

* it must be readable by `easycustoms` — `chmod 600`, owned by that user;
* a variable that is in neither this file nor the unit is simply absent;
* **an unknown line is inert, not an error** (`extra="ignore"`). A typo does not
  fail; it silently does nothing. This is the single most likely way to
  misconfigure this deployment, and §12's smoke test is what catches it.

### Naming rule

Every setting binds `EASYCUSTOMS_<FIELD_NAME>`. Many *also* accept a bare
industry-standard name (`OPENAI_API_KEY`, `AWS_REGION`, `SUPABASE_URL`, …). When
both are present, **the `EASYCUSTOMS_`-prefixed one wins.** The table in §6 gives
every alternative name.

### The file

```bash
sudo -u easycustoms install -m 600 /dev/null /opt/easycustoms/backend/.env
```

```ini
# ============================================================================
# Easy Customs — STAGING
# ============================================================================

# ---- Identity ---------------------------------------------------------------
# supabase is the ONLY multi-account provider. Under it, AUTH_USERNAME and
# AUTH_PASSWORD are NEVER read: sign-in is proxied server-side to Supabase's
# password grant. They are not set below because setting them would be a lie.
EASYCUSTOMS_AUTH_PROVIDER=supabase
EASYCUSTOMS_SUPABASE_URL=https://<staging-project-ref>.supabase.co
# The ANON / publishable key. NOT the service-role key — nothing in the request
# path acts as an administrator, and a service key here would turn any RCE into
# full control of the auth database.
EASYCUSTOMS_SUPABASE_ANON_KEY=<anon publishable key>

# Signs THIS app's own session token, under either provider. Generate with:
#   python -c "import secrets; print(secrets.token_hex(32))"
# The app REFUSES TO BOOT without it on this provider. Unset, each uvicorn worker
# signs with its own random key and a session minted by one is rejected by the
# other — a browser signed in every other request, which looks like a broken
# proxy rather than a missing line, so it is stopped rather than warned about.
EASYCUSTOMS_AUTH_SECRET=<64 hex characters>
EASYCUSTOMS_AUTH_TOKEN_TTL_HOURS=24

# TLS terminates at Caddy, so the app sees plain http and `auto` would ship the
# session cookie WITHOUT Secure to a browser that is on https.
EASYCUSTOMS_SESSION_COOKIE_SECURE=always

# One proxy of ours (Caddy) in front. Load-bearing THREE times: the login
# throttle, the signup limiter and the reset limiter all key on it. At 0 every
# caller shares one bucket and the third registration of the day locks out
# everybody.
EASYCUSTOMS_TRUSTED_PROXY_HOPS=1

# Strangers may register. This is what the smoke test exercises; see §9 for the
# four dashboard settings that make it safe, none of which this app can check.
EASYCUSTOMS_ALLOW_SELF_SIGNUP=true

# ---- Persistence ------------------------------------------------------------
EASYCUSTOMS_DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:<port>/<database>
EASYCUSTOMS_DB_POOL_SIZE=5
EASYCUSTOMS_DB_MAX_OVERFLOW=10
EASYCUSTOMS_THREADPOOL_MAX_THREADS=15
# Wait forever and wedged look identical from a browser. A finite value turns
# that into a 409 the reviewer can act on. Above your slowest finalize.
EASYCUSTOMS_JOB_LOCK_TIMEOUT_SECONDS=120

# ---- Documents --------------------------------------------------------------
EASYCUSTOMS_STORAGE_BACKEND=s3
EASYCUSTOMS_S3_BUCKET=<bucket>
EASYCUSTOMS_S3_PREFIX=staging/
EASYCUSTOMS_S3_REGION=<region>

# ---- Queue ------------------------------------------------------------------
EASYCUSTOMS_QUEUE_PROVIDER=sqs
SQS_QUEUE_URL=https://sqs.<region>.amazonaws.com/<account-id>/customs-processing-queue
AWS_REGION=<region>
# No AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY. The instance role is the
# credential source; a long-lived key in this file outlives the box.

# ---- Extraction -------------------------------------------------------------
# BOTH ARE REQUIRED HERE. This file sets AUTH_PROVIDER=supabase and leaves the
# providers at their live defaults (mistral / openai), and that combination with
# a missing key is a BOOT REFUSAL naming the variable — because the fallback is
# to the OFFLINE reader, so the box would otherwise come up clean and serve
# declarations built from facts nothing read off the document.
# A placeholder ('***', 'your_..._here') counts as missing. To run this staging
# box deliberately without vendor keys, say so outright:
#   EASYCUSTOMS_OCR_PROVIDER=offline
#   EASYCUSTOMS_EXTRACTION_PROVIDER=offline
# A key that is present but EXPIRED or out of quota is not covered by that
# refusal and does not need to be: it fails the extraction loudly (document
# FAILED, EXTRACTION_FAILED in the audit trail). §12 step 5 still checks the
# provider that actually ran.
EASYCUSTOMS_MISTRAL_API_KEY=<mistral key>
EASYCUSTOMS_OPENAI_API_KEY=<openai key>

# ---- Logging ----------------------------------------------------------------
EASYCUSTOMS_LOG_FORMAT=json
EASYCUSTOMS_LOG_LEVEL=INFO
# EASYCUSTOMS_LOG_THIRD_PARTY_DEBUG stays OFF. At DEBUG, sqlalchemy prints every
# statement with its bound parameters (party names, invoice values) and httpx
# prints request bodies, which on the login route is a password.
```

Everything not listed above takes its default. §6 says what each default is and
what happens if you change it.

### The metering price file

Optional, and separate from the `.env`. Token and page counts are recorded either
way — they come from the vendor's own response and are facts. **Cost stays NULL
until rates are configured**, because this app cannot know what your account is
charged and a guessed price would look exactly as authoritative as a verified one.

```bash
cd backend && cp vendor_prices.example.json vendor_prices.json
```

Fill it in from your invoice and bump its `version` when rates change; every row
records the version that priced it. **In queue mode this file must exist on the
box running the worker**, because extraction — and therefore `metering.record` —
runs there.

---

## 6. Every setting, and what happens if it is missing or wrong

Derived from `app/config.py` (91 settings). "Required" means *for this staging
shape*. Where a bare alias exists it is listed after `|`; the `EASYCUSTOMS_`-prefixed
form always works and always wins.

### Identity and session

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_AUTH_PROVIDER` \| `AUTH_PROVIDER` | **yes** (`supabase`) | `local` | Left at `local`, the deployment is single-account, signup is refused **at boot**, and password reset answers 501. Any value but `local`/`supabase` fails validation |
| `EASYCUSTOMS_SUPABASE_URL` \| `SUPABASE_URL` \| `NEXT_PUBLIC_SUPABASE_URL` | **yes** | *(empty)* | **Boot refusal** under `supabase`. Deliberate — an unconfigured provider would 502 every login and read as a credential problem |
| `EASYCUSTOMS_SUPABASE_ANON_KEY` \| `SUPABASE_ANON_KEY` \| `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` \| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | **yes** | *(empty)* | Same boot refusal. A **service-role** key here would work and must never be used — it is not needed and it turns any RCE into control of the auth database |
| `EASYCUSTOMS_SUPABASE_TIMEOUT_SECONDS` \| `SUPABASE_TIMEOUT_SECONDS` | no | `15.0` | Too low: slow sign-ins surface as "could not check credentials" (503), not as wrong passwords |
| `EASYCUSTOMS_AUTH_USERNAME` \| `AUTH_USERNAME` | **no** under supabase | unset | **Never read at sign-in under `supabase`.** Under `local` its absence is fail-closed: no token can be issued and every route 401s. Startup logs a "no login account is configured" error even under supabase — see §13 |
| `EASYCUSTOMS_AUTH_PASSWORD` \| `AUTH_PASSWORD` | **no** under supabase | unset | As above. Placeholder values (`your_…`, anything containing `*`) count as unset |
| `EASYCUSTOMS_AUTH_SECRET` \| `AUTH_SECRET` | **yes** | unset → random per process | **Boot refusal under `supabase`.** It used to be a warning, and the warning was not enough: unset, the key is generated per process, so `--workers 2` signs with two different keys and each worker treats the other's session as a forgery — a browser signed in on roughly every other request, at random, which reads as a broken proxy or a broken Supabase rather than a missing line. Under `local` it stays a warning (one process, one operator, "sign in again after a restart"). Changing it signs everyone out |
| `EASYCUSTOMS_AUTH_TOKEN_TTL_HOURS` \| `AUTH_TOKEN_TTL_HOURS` | no | `24.0` | `<= 0` is refused at boot — it would issue already-expired tokens |
| `EASYCUSTOMS_SESSION_COOKIE_SECURE` \| `SESSION_COOKIE_SECURE` | **yes** (`always`) | `auto` | At `auto` behind Caddy the app sees `http` and the session cookie goes out **without `Secure`**. One downgraded request puts the token on the wire in clear. Values: `auto`/`always`/`never` |
| `EASYCUSTOMS_TRUSTED_PROXY_HOPS` \| `TRUSTED_PROXY_HOPS` | **yes** (`1`) | `0` | At 0 all three limiters key on Caddy's address: everyone shares one bucket, and an unauthenticated attacker can lock out every user. Set too **high** and the key comes from a position the caller controls, so nothing is limited |
| `EASYCUSTOMS_ALLOW_SELF_SIGNUP` \| `ALLOW_SELF_SIGNUP` | decision | `false` | Off, `POST /api/auth/signup` answers 403 and nothing reaches Supabase. **`true` under `local` is a boot refusal** — that provider has nowhere to put a second account |
| `EASYCUSTOMS_PASSWORD_RESET_REDIRECT_URL` \| `PASSWORD_RESET_REDIRECT_URL` | conditional | *(empty)* | Leave **unset** when staging has its own Supabase project — the link then follows Site URL. **Set it if staging shares production's project**, or staging users are mailed links to production. Must be an absolute `http(s)://` origin (boot refusal otherwise) *and* be in the dashboard's Redirect URLs, or Supabase silently falls back to Site URL |
| `EASYCUSTOMS_CORS_ORIGINS` \| `CORS_ORIGINS` | no | *(empty)* | Empty disables CORS entirely, which is correct — the SPA is same-origin. Setting it also **disables the Sec-Fetch-Site cross-origin refusal**. A wildcard would let any page on the internet drive the login route |
| `EASYCUSTOMS_ALLOW_FIXTURE_UPLOADS` \| `ALLOW_FIXTURE_UPLOADS` | **must stay `false`** | `false` | `true` lets an upload carry a `fixture` blob that **becomes** the extraction, skipping OCR, the model and the evidence validators — unverified facts in a legally binding declaration, with nothing on the row saying so |

### Persistence and concurrency

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_DATABASE_URL` | **yes** | a local SQLite file | Left at the default, this box runs on SQLite: the two units then need a shared filesystem, and under `ProtectSystem=strict` the default path is not writable at all (see §8). Wrong driver (`postgresql://` instead of `postgresql+psycopg://`) fails at import looking for psycopg2 |
| `EASYCUSTOMS_DB_POOL_SIZE` \| `DB_POOL_SIZE` | no | `10` | Per process. Too high across the fleet: `FATAL: sorry, too many clients already` on a random request, first under load |
| `EASYCUSTOMS_DB_MAX_OVERFLOW` \| `DB_MAX_OVERFLOW` | no | `30` | As above; these are burst connections opened under load and closed again |
| `EASYCUSTOMS_DB_POOL_RECYCLE_SECONDS` \| `DB_POOL_RECYCLE_SECONDS` | no | `1800` | Longer than an upstream idle-reaper's window: a reaped-but-pooled connection surfaces as a random `OperationalError` on a healthy request |
| `EASYCUSTOMS_DB_POOL_PRE_PING` \| `DB_POOL_PRE_PING` | no | `true` | `false` saves a round-trip per checkout and hands handlers connections the server has already closed |
| `EASYCUSTOMS_THREADPOOL_MAX_THREADS` \| `THREADPOOL_MAX_THREADS` | no | `40` | **Boot refusal** if it exceeds pool + overflow. Uploads, extraction and finalize all run on this limiter, so it is the real per-process concurrency ceiling |
| `EASYCUSTOMS_JOB_LOCK_TIMEOUT_SECONDS` \| `JOB_LOCK_TIMEOUT_SECONDS` | no | `0.0` (wait forever) | Postgres only. At 0 a request holding the lock behind a hung upstream call queues every later request on that job with no error anywhere. Set **above** your slowest legitimate finalize or you will 409 honest work |

### Documents

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_STORAGE_BACKEND` \| `STORAGE_BACKEND` | **yes** (`s3`) | `local` | `local` on more than one machine means the worker has nothing to OCR and the evidence panel 410s. Values: `local`/`s3` |
| `EASYCUSTOMS_S3_BUCKET` \| `S3_BUCKET` | **yes** with s3 | *(empty)* | **Boot refusal** with `storage_backend=s3` — a bucket-less s3 backend would accept a document, fail to store it, and lose it |
| `EASYCUSTOMS_S3_PREFIX` \| `S3_PREFIX` | no | *(empty)* | Only matters when one bucket holds two environments. Changing it later orphans existing keys (they record where they live) |
| `EASYCUSTOMS_S3_REGION` \| `S3_REGION` | no | *(empty)* | Empty uses boto3's chain, which on EC2 is the instance's region. Wrong region: every document operation fails |
| `EASYCUSTOMS_STORAGE_DIR` | no | `backend/storage` | Created at import **whatever the backend**, so it must be writable even with s3. Under `ProtectSystem=strict` it is the only writable path (§8) |

### Queue

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_QUEUE_PROVIDER` \| `QUEUE_PROVIDER` | **yes** (`sqs`) | `off` | `off` runs OCR + LLM inside the request (minutes), and the worker unit should then not be enabled at all. Values: `off`/`sqs` |
| `EASYCUSTOMS_SQS_QUEUE_URL` \| `SQS_QUEUE_URL` | **yes** with sqs | *(empty)* | **Boot refusal** — queue mode without a queue would 502 every extraction |
| `EASYCUSTOMS_AWS_REGION` \| `AWS_REGION` | no | *(empty)* | Empty uses boto3's chain (instance role/region), which is the right answer on EC2 |
| `EASYCUSTOMS_AWS_ACCESS_KEY_ID` \| `AWS_ACCESS_KEY_ID` | **no — omit** | *(empty)* | Dev-machine fallback only. Set on a server it defeats the instance role and outlives the box. Blank on either one falls back to boto3's chain |
| `EASYCUSTOMS_AWS_SECRET_ACCESS_KEY` \| `AWS_SECRET_ACCESS_KEY` | **no — omit** | *(empty)* | As above |
| `EASYCUSTOMS_WORKER_CONCURRENCY` \| `WORKER_CONCURRENCY` | no | `4` (1–32) | Documents one worker extracts at once. I/O-bound, but every slot multiplies vendor-API and DB load |
| `EASYCUSTOMS_SQS_HEARTBEAT_SECONDS` \| `SQS_HEARTBEAT_SECONDS` | no | `120` (10–3600) | How often an in-flight message's invisibility is re-extended |
| `EASYCUSTOMS_SQS_VISIBILITY_EXTEND_SECONDS` \| `SQS_VISIBILITY_EXTEND_SECONDS` | no | `600` (60–43199) | **Boot refusal** if it is not at least 60s above the heartbeat: one slow beat would hand the message to a second worker mid-extraction — a duplicate paid run |

### Extraction and vendors

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_OCR_PROVIDER` \| `OCR_PROVIDER` | no | `mistral` | `offline` reads PDFs with pypdf and finds nothing in a scan — but it is an *explicit* choice and boots anywhere. `mistral` with no key does not, on this provider |
| `EASYCUSTOMS_EXTRACTION_PROVIDER` | no | `openai` | `openai` \| `langroid` \| `offline`. `langroid` is not installed by default — and with a key but no library it still falls back to offline with only a warning, the one gap the refusal below cannot close |
| `EASYCUSTOMS_MISTRAL_API_KEY` \| `MISTRAL_API_KEY` | **yes** | unset | **Boot refusal** under `auth_provider=supabase` with `ocr_provider=mistral`: unset, OCR silently downgrades to offline and the deployment serves invented facts. Placeholder values count as unset. Under `local` it still only warns |
| `EASYCUSTOMS_OPENAI_API_KEY` \| `OPENAI_API_KEY` \| `EASYCUSTOMS_LLM_API_KEY` | **yes** | unset | **Boot refusal** on the same terms — this was the failure that looked like success. A key that is present but *expired or out of quota* is not covered and does not need to be: it fails the extraction loudly |
| `EASYCUSTOMS_LLM_MODEL` | no | `gpt-4o-mini` | Must be structured-output capable |
| `EASYCUSTOMS_MISTRAL_OCR_MODEL` \| `MISTRAL_OCR_MODEL` | no | `mistral-ocr-latest` | A name Mistral does not know fails every OCR call |
| `EASYCUSTOMS_EXTRACTION_MAX_REPAIR_ROUNDS` | no | `2` | Evidence/schema repair attempts. 0 means a single malformed response fails the document |
| `EASYCUSTOMS_LLM_CONCURRENCY` \| `LLM_CONCURRENCY` | no | `4` | Global cap across all documents — one TPM budget. Too high: 429s (retried with backoff) |
| `EASYCUSTOMS_LLM_TIMEOUT_SECONDS` \| `LLM_TIMEOUT_SECONDS` | no | `180.0` | SDK retries are disabled, so this is the whole budget per call |
| `EASYCUSTOMS_MISTRAL_OCR_TIMEOUT_SECONDS` \| `MISTRAL_OCR_TIMEOUT_SECONDS` | no | `120.0` | Per OCR HTTP call (upload / signed-url / process) |
| `EASYCUSTOMS_PACKING_EXTRACTION_BUDGET_SECONDS` \| `PACKING_EXTRACTION_BUDGET_SECONDS` | no | `240.0` | Exceeded, the job proceeds but weight/carton allocation falls back to quantity-proportional **and the reviewer is warned**. Too low quietly degrades every packing list |
| `EASYCUSTOMS_EXTRACTION_MAX_PAGES` \| `EXTRACTION_MAX_PAGES` | no | `150` | Hard ceiling on pages sent to the LLM per document; 0 disables. Without it a 500-page PDF is 125 model calls per `/extract`, repeated on every retry |
| `EASYCUSTOMS_EXTRACTION_CHUNK_PAGE_THRESHOLD` | no | `6` | Chunk when pages exceed this |
| `EASYCUSTOMS_EXTRACTION_CHUNK_PAGE_SIZE` | no | `4` | Pages per window. Larger risks output truncation, and one repair round re-requests everything |
| `EASYCUSTOMS_EXTRACTION_CHUNK_PAGE_SIZE_PACKING` | no | `2` | Smaller on purpose — dense tables whose rows rarely straddle a page break |
| `EASYCUSTOMS_DETERMINISTIC_TABLE_PARSER_ENABLED` | **leave `true`** | `true` | Off, every goods row goes to the LLM instead of being parsed and arithmetic-verified in code. Slower, dearer, and less certain |
| `EASYCUSTOMS_VENDOR_LAYOUT_MEMORY_ENABLED` | no | `true` | Off, headerless/garbled documents stop parsing deterministically. Rows are still arithmetic-verified either way |
| `EASYCUSTOMS_VENDOR_FIELD_PROFILES_ENABLED` | no | `true` | Off, a corrected vendor COO is not remembered and the exporter-country fallback returns |
| `EASYCUSTOMS_MISTRAL_OCR_INCLUDE_IMAGE_BASE64` \| `MISTRAL_OCR_INCLUDE_IMAGE_BASE64` | no | `false` | `true` inflates every OCR response with page images |
| `EASYCUSTOMS_OPENAI_REASONING_ENABLED` \| `OPENAI_REASONING_ENABLED` | no | `false` | With a reasoning model set, switches the primary chat model |
| `EASYCUSTOMS_OPENAI_REASONING_MODEL` \| `OPENAI_REASONING_MODEL` | no | unset | Only consulted when the flag above is on |
| `EASYCUSTOMS_OPENAI_REASONING_FALLBACK_MODEL` \| `OPENAI_REASONING_FALLBACK_MODEL` | no | unset | The **fast tier** for mechanical row windows; repair rounds escalate to the primary. Unset = single-model mode. Never used for whole-document judgement |

### Upload bounds

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_MAX_UPLOAD_MB` \| `MAX_UPLOAD_MB` | no | `25` | Per document. Raising it raises the derived request ceiling with it — **and Caddy's `max_size` must move too** (§10) |
| `EASYCUSTOMS_MIN_PHOTO_PX` \| `MIN_PHOTO_PX` | no | `1000` | Short-edge minimum for photos. Below it OCR reads digits unreliably, so the upload is refused with a "retake" message rather than extracting confidently wrong numbers |
| `EASYCUSTOMS_MAX_PHOTO_PIXELS` \| `MAX_PHOTO_PIXELS` | no | `80000000` | Checked from the header before any decode; 0 disables. Without it a crafted image decodes ~500 MB of raster |
| `EASYCUSTOMS_MAX_PHOTOS_PER_DOCUMENT` \| `MAX_PHOTOS_PER_DOCUMENT` | no | `40` | Each is read whole into memory before validation; 0 disables |
| `EASYCUSTOMS_MAX_REQUEST_MB` \| `MAX_REQUEST_MB` | no | `0` → derived | **When above 0 this IS the ceiling and the derivation is not consulted.** 0 does not disable the check — it takes `max(64, MAX_UPLOAD_MB × 2)` MB |

### Metering

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_USAGE_PRICE_PATH` \| `USAGE_PRICE_PATH` | no | `backend/vendor_prices.json` | Absent, token and page counts are still recorded and **cost stays NULL** — reported as unknown, never as free. In queue mode this is read on the **worker's** box |
| `EASYCUSTOMS_USAGE_MONTHLY_DOCUMENT_CAP` \| `USAGE_MONTHLY_DOCUMENT_CAP` | no | unset → 200 under supabase | Unset takes the provider default (unlimited under `local`, 200 under `supabase`). An **explicit `0` is refused at boot under supabase** — uncapped multi-account is an unbounded vendor bill. Negative is refused. Raise it for one account with an `account_quota` row, not for everyone |

### Logging

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_LOG_FORMAT` \| `LOG_FORMAT` | no | `auto` | `auto` picks json off a terminal. Under systemd that is already json; naming it makes it explicit. `auto`/`json`/`text` |
| `EASYCUSTOMS_LOG_LEVEL` \| `LOG_LEVEL` | no | `INFO` | `DEBUG` is safe — it deliberately does **not** raise the libraries |
| `EASYCUSTOMS_LOG_THIRD_PARTY_DEBUG` \| `LOG_THIRD_PARTY_DEBUG` | **keep `false`** | `false` | `true` makes `sqlalchemy.engine` print every statement **with bound parameters** (party names, invoice values, the declaration) and `httpx` print request bodies — on the login route, a password. Test data only |

### Reference data paths

| Setting | Req. | Default | Missing / wrong |
| --- | --- | --- | --- |
| `EASYCUSTOMS_BANK_CSV_PATH` \| `BANK_CSV_PATH` | no | `reference_data/bank_code_and_bank_names_with_swift_code.csv` | The only bank/SWIFT authority. A missing or edited file changes declared bank codes — never hand-maintain a parallel list |
| `EASYCUSTOMS_TERMS_CSV_PATH` \| `TERMS_CSV_PATH` | no | `reference_data/terms_of_payments_and_codes.csv` | Same, for payment terms |
| `EASYCUSTOMS_HS_EXCEL_PATH` \| `HS_EXCEL_PATH` | no | `reference_data/HS_with_ALL_descriptions_AND_UNITS.xlsx` | The official HS database. LLM-supplied HS11 is rejected outright and completed from **this** file; a wrong one silently changes commodity codes |

### Declaration defaults — **do not set these on staging**

These are per-job **review defaults** seeded into Critical Review and selectable
per job. They describe the deployment's home office and standard regime. They are
listed for completeness; setting one in a staging `.env` changes what reviewers
are offered and makes staging stop resembling production.

| Setting | Default |
| --- | --- |
| `EASYCUSTOMS_CUSTOMS_OFFICE_CODE` | `TIA00` |
| `EASYCUSTOMS_CUSTOMS_OFFICE_NAME` | `TIA Customs Office` |
| `EASYCUSTOMS_DECLARATION_TYPE` | `IM` |
| `EASYCUSTOMS_DECLARATION_GEN_PROCEDURE_CODE` | `4` |
| `EASYCUSTOMS_EXTENDED_CUSTOMS_PROCEDURE` | `4000` |
| `EASYCUSTOMS_NATIONAL_CUSTOMS_PROCEDURE` | `000` |
| `EASYCUSTOMS_PLACE_OF_LOADING_CODE` | `NPKTM` |
| `EASYCUSTOMS_PLACE_OF_LOADING_NAME` | `KATHMANDU` |
| `EASYCUSTOMS_LOCATION_OF_GOODS` | `TIA...IM/GODOWN` |
| `EASYCUSTOMS_BORDER_NATIONALITY` | `NP` |
| `EASYCUSTOMS_PACKAGE_TYPE_DEFAULT` | `CT` |
| `EASYCUSTOMS_NATIONAL_CURRENCY` | `NPR` |
| `EASYCUSTOMS_DEFAULT_EXCHANGE_RATE` | `145.76` (NPR per 1 unit foreign; overridable per job) |
| `EASYCUSTOMS_DEFAULT_EXCHANGE_RATE_CURRENCY` | `USD` — which currency the rate is quoted *for*, so the plausibility check can skip a JPY invoice instead of crying wolf |
| `EASYCUSTOMS_XML_STRICT_BLOCKING` | `false` — **warn mode is the default**: blocking cases warn and still produce XML so a reviewer can test it in real ASYCUDA. Do not assume a blocker stops generation |

### Rule-set flags — **frozen; changing one is not a deploy decision**

Every one of these is an authoritative business requirement recorded in the ADR
table in `README.md` and in `docs/allocation-spec.md`. A `.env` line here silently
outranks the code default and changes declared values with nothing on screen
saying so — which is exactly how the net-to-gross ratio drifted between 0.3 and
0.7 for months. Changing one takes explicit approval and updates the spec in the
same commit. **Set none of them on staging.**

| Setting | Default | What it decides |
| --- | --- | --- |
| `EASYCUSTOMS_DEFAULT_NET_TO_GROSS_RATIO` | `0.7` (ADR-003) | Net weight of every item with no packing net, no invoice weight and no convertible description. Refused at boot unless strictly between 0 and 1; any value other than 0.7 is written into the audit trail |
| `EASYCUSTOMS_PAIR_DIVIDE_BY_TWO` | `true` (ADR-004) | PCS ÷ 2 for PR/NPR supplementary units |
| `EASYCUSTOMS_COST_ALLOCATION_BASIS` | `value` | Freight/insurance allocation basis — `value` (sample) or `gross_weight` (rule text) |
| `EASYCUSTOMS_DEFAULT_UNKNOWN_PAYMENT_TERMS_TO_LC` | `false` (ADR-006) | Never silently default unknown payment terms to LC |
| `EASYCUSTOMS_RULE_SET_VERSION` | `2026.07.15` | Recorded on every declaration so it says which rule set produced it |
| `EASYCUSTOMS_PROMPT_VERSION` | `extract-v2` | Recorded per extraction |
| `EASYCUSTOMS_EXTRACTION_SCHEMA_VERSION` | `raw-v1` | Recorded per extraction |

---

## 7. Migrations

### Run them by hand, with both units stopped

```bash
sudo systemctl stop easycustoms-api easycustoms-worker
```

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/alembic upgrade head
```

```bash
sudo systemctl start easycustoms-api easycustoms-worker
```

`alembic` reads `EASYCUSTOMS_DATABASE_URL` through the app's own settings —
`alembic.ini` leaves `sqlalchemy.url` deliberately empty — so running it from
`backend/` with this `.env` targets the right database with no extra argument.

### Why no unit does this to itself on restart

`app/database.py` **refuses to serve a Postgres database whose schema is not at
head**, and refuses in three distinguishable ways because three different things
are wrong and "run the migrations" is useless advice to two of them:

| State | Message | Fix |
| --- | --- | --- |
| Empty database | "…is empty" | `alembic upgrade head` |
| Tables, no migration history (predates alembic) | "…has tables but no migration history" | `alembic stamp fa843d18c61e && alembic upgrade head` — **`upgrade` alone would try to create tables that already exist** |
| At revision X, code expects Y | "…is at migration X, but this code expects Y" | `alembic upgrade head` |

A fresh staging database is the first case. Both the API **and** the worker call
`init_db()` at startup, so **both** refuse — a forgotten migration is a loud
startup failure in the journal, not a column-missing error on a reviewer's screen
an hour later.

`create_all` is not used on Postgres because it creates missing *tables* and never
`ALTER`s an existing one, so a column added since the database was built would be
silently absent.

There is no `ExecStartPre=` running the upgrade, on purpose, and it is worth being
explicit about why:

* **An API restart during an incident must not reshape the database.** Restarting
  a service is the first thing anyone tries; it should never be a schema change.
* **Two units restarting together would race each other** applying the same DDL.
* **`Restart=always` would retry it.** A migration that failed halfway would be
  re-attempted every 5 seconds, against a database already in a partial state.
* Migrations are the one deploy step that should happen in a chosen window with
  someone watching.

SQLite behaves differently — it builds *and* migrates itself at startup, because
that file is local, single-tenant, and the app's own. That path is not in use
here.

---

## 8. The two units

```bash
sudo cp /opt/easycustoms/backend/deploy/easycustoms-api.service /etc/systemd/system/
```

```bash
sudo cp /opt/easycustoms/backend/deploy/easycustoms-worker.service /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now easycustoms-api easycustoms-worker
```

```bash
journalctl -u easycustoms-api -f
```

Read the comment headers in both files — they are the reference and this section
does not repeat them. What matters at bring-up:

* **The API binds `127.0.0.1:8000`.** Caddy owns TLS and the public port.
* **`--workers 2`** is now safe: the job lock is a Postgres advisory lock, the
  vendor stores are tables, and the login throttle is a table. It is *not* safe
  without `EASYCUSTOMS_AUTH_SECRET`, which is why this provider refuses to boot
  without one (§6) — the unit will fail to start and say so.
* **`ProtectSystem=strict`** makes the whole filesystem read-only except
  `backend/storage` and `/tmp`. Fine here — Postgres and S3 hold everything. It is
  why a SQLite deployment under these units cannot write its database unless the
  URL is moved inside `storage/`.
* **The worker enforces no quota.** `POST /extract` checks the monthly document
  cap before it claims and enqueues, so a message on the queue is already paid
  for. More workers means more throughput, never more spend allowance.
* **Config validation is process-wide.** The worker shares this `.env`, so it also
  refuses to boot without `SUPABASE_URL`/`SUPABASE_ANON_KEY` — despite never
  authenticating anyone. It says so in the message.

Verify before moving on:

```bash
systemctl is-active easycustoms-api easycustoms-worker
```

```bash
curl -sS http://127.0.0.1:8000/api/health
```

`{"status":"ok"}`. The worker's journal should name the queue URL, its
concurrency and its heartbeat at startup.

---

## 9. Caddy and DNS

### DNS first

Point an `A` (and `AAAA` if the box has one) record at the box's public address
and wait for it to resolve. Caddy's ACME challenge needs the name to reach this
machine; starting Caddy before DNS propagates burns Let's Encrypt rate limit on
failures.

```bash
dig +short <staging-host>
```

### Caddy

```bash
sudo cp /opt/easycustoms/backend/deploy/Caddyfile.example /etc/caddy/Caddyfile
```

```bash
sudo $EDITOR /etc/caddy/Caddyfile     # set the domain and the email
```

```bash
sudo systemctl reload caddy && journalctl -u caddy -f
```

Set the `email` in the global block. Renewal-failure notices go there, and a
silently expired certificate takes the whole app offline.

Two things about that file worth knowing rather than discovering:

* **The body cap is deliberately *above* the app's.** `max_size 80MB` sits over
  the app's 64 MB so an oversized upload is refused by the **app**, which names
  the real limit in a message the SPA can show, rather than by the proxy with a
  bare 413. If you raise `EASYCUSTOMS_MAX_UPLOAD_MB` — or set
  `EASYCUSTOMS_MAX_REQUEST_MB`, which overrides the derivation entirely — raise
  this with it. `GET /api/config` reports the number the app is actually using.
* **The security-header block is deliberately short.** Caddy's `header Name value`
  *replaces* what the app sent, and `app/main.py` sets `X-Frame-Options`,
  `Referrer-Policy`, `X-Content-Type-Options` and a CSP on every response — with
  `X-Frame-Options` split by content type (`DENY` on HTML so nobody clickjacks
  finalize, `SAMEORIGIN` on documents so the evidence `<iframe>` works). Adding
  those names back here overrides a decision made with more information. Caddy
  keeps HSTS, which the app cannot set because it never sees the TLS connection.

Confirm from your laptop, not from the box:

```bash
curl -sSI https://<staging-host>/api/health
```

Expect `200`, `strict-transport-security`, and an `x-request-id` — that last one
is the string to quote when reporting a failure. Then confirm the app is *not*
reachable without TLS:

```bash
curl -sS --max-time 5 http://<box-public-ip>:8000/api/health ; echo "exit=$?"
```

A timeout is the pass.

---

## 10. Supabase dashboard

**Give staging its own Supabase project.** Site URL is a single value per
project, so a project shared with production mails staging's users links to
production's origin — recoverable only by setting
`EASYCUSTOMS_PASSWORD_RESET_REDIRECT_URL` and maintaining a second allow-list
entry, which is more moving parts than a second free project.

None of the following can be checked by this application. Every one of them is a
control the app depends on and cannot see.

| Where | Setting | Value | Why it is not optional |
| --- | --- | --- | --- |
| Auth → Providers → Email | **Enable Sign Ups** | ON | While it is off, `POST /auth/v1/signup` returns 422 and registration cannot work whatever `ALLOW_SELF_SIGNUP` says |
| Auth → Providers → Email | **Confirm email** | **ON** | **The primary anti-abuse control.** An unconfirmed account cannot sign in, so it never reaches a paid vendor call. With it off a signup returns a usable account immediately — the app detects that from the response and logs a WARNING naming this setting |
| Auth → URL Configuration | **Site URL** | `https://<staging-host>` exactly | Where confirmation and recovery links land |
| Auth → URL Configuration | **Redirect URLs** | the same origin, **no wildcard** | The open-redirect control. A wildcard mails recovery tokens wherever an attacker asks. Covers **both** flows — confirmation and reset land on the same origin (the token arrives in the URL *fragment*, which never reaches a server) |
| Auth → Emails / SMTP | **A real SMTP provider** | SES, Postmark, … | Supabase's built-in sender is a few messages an hour and is explicitly not for production. Left alone, **signup appears to work and the emails silently do not arrive** — and that is the exact failure staging exists to catch |
| Auth → Rate limits | **signup** and **email**, set explicitly | a deliberate number | These bound the *refused* attempts this app deliberately does not count, and the per-address mailbombing its caller-keyed limiter cannot see |
| Auth → Policies | **Minimum password length**, **leaked-password protection** | ON | Password policy belongs to the provider — a second minimum in this app would be a second source of truth that drifts |
| Project Settings → API | **Service-role key** | stays out of this deployment | Nothing in the request path needs it |

Do not point the `.env` at production's project "just to test the login". Every
signup in the smoke test creates a real account.

### Check the project answers before starting the app

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/check_supabase_connection.py
```

Read-only — one `GET /auth/v1/settings` with the anon key. It creates no account
and sends no email. It reads the URL and key **through the app's own settings**,
so it checks the values `app/auth_supabase.py` will actually use rather than
whatever a second reading of `.env` would find, and a wrong key comes back as a
401 here instead of as a failed sign-in in step 3 of the smoke test.

It also prints the project's `disable_signup` and `mailer_autoconfirm` flags,
which are two of the dashboard settings in the table above — the only ones
visible from this side. **It reports them; it enforces nothing.** SMTP, Site URL
and the Redirect URLs allow-list remain invisible to it, and to every other
check on this box.

It needs no service-role key. If a command anywhere asks you to put one in
`backend/.env`, that is the defect this check was rewritten to remove — the key
bypasses row-level security, and the process reading that file parses uploaded
PDFs.

### The first admin

No route grants a role — the first admin cannot be created by an endpoint that
requires an admin, and any endpoint that could would be able to create the second
one too. It is out of band, once, from a shell on this box:

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/grant_admin.py --list
```

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/grant_admin.py --grant <auth.users.id> --apply
```

The key is the Supabase `auth.users.id` UUID (Authentication → Users), not the
email. Without `--apply` it rehearses. Revocation takes effect on that account's
next request — the role is read per request and never carried in the session token.

**An admin still cannot read anyone else's declarations.** The role manages
accounts, quotas and usage metadata; `services.job_visible_to` does not know it
exists. `docs/ADR-001-identity-and-tenancy.md` is the record.

---

## 11. Smoke test

Everything before this proves a component. This proves the deployment. Run it
from your laptop against `https://<staging-host>`, in a **fresh browser profile**,
with `journalctl -u easycustoms-api -u easycustoms-worker -f` open on the box.

Use a real mailbox you control — the email steps are the point.

### 1. Sign up

Open `https://<staging-host>`, choose "Create an account", and register
`<you>+stg1@<your-mail-domain>`.

**Expect: 202, and a message that says an email is on its way — for every
outcome.** New address, already-registered address, and confirmation-disabled
project all return the same body, on purpose: a public endpoint that
distinguishes them is an account-existence oracle for any address a stranger
cares to test.

**So a 202 is not evidence the account was created.** The journal is. Look for
the signup line; if it carries a WARNING naming **Confirm email**, that dashboard
setting is off — fix it before continuing, because with it off an unconfirmed
account can sign straight in and the anti-abuse control does not exist.

Repeat the same address twice more: **the fourth attempt in an hour is refused**
(3/hour, 10/day, counted in `signup_attempt`). That the limiter fires at all is
the proof `TRUSTED_PROXY_HOPS=1` is right — at 0 you would be sharing a bucket
with the world.

### 2. Confirmation email

**The one step nothing on the box can fake.** Wait for it.

* **No email at all** → SMTP is unconfigured (§10) or the address bounced. Check
  the Supabase project's Auth logs; nothing appears in this app's journal, because
  the app never saw a send.
* **An email whose link points at the wrong host** → Site URL. If staging shares
  production's project, this is where you discover it.

Click the link. It lands on `https://<staging-host>/#access_token=…&type=signup`,
the SPA strips the fragment from the address bar before the first render, and you
get "you can sign in now". **Confirm the address bar no longer shows the token.**

### 3. Sign in

Sign in with the address and password from step 1.

Then check the session survives a restart, which is what proves
`EASYCUSTOMS_AUTH_SECRET` is really set:

```bash
sudo systemctl restart easycustoms-api
```

Reload the page. **Still signed in.** Signed out — or signed in on some reloads
and out on others — means the secret is unset and each worker minted its own.

Confirm the cookie is right: in devtools, the session cookie must be `HttpOnly`,
`Secure` and `SameSite=Lax`. No `Secure` means `SESSION_COOKIE_SECURE` is not
`always`.

### 4. Upload

Generate synthetic paperwork on the box first — never put a customer document on
staging:

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/generate_sample_documents.py
```

Copy `backend/sample_data/sample_invoice.pdf` and `sample_packing_list.pdf` to
your laptop, create a job in the SPA, and upload the invoice as **INVOICE** and
the packing list as **PACKING_LIST**.

Then confirm the bytes went where you think:

```bash
aws s3 ls s3://<bucket>/staging/jobs/ --recursive | head
```

Objects here means S3 is wired. Nothing here, with a working upload, means
`STORAGE_BACKEND` is still `local` and the file is on the box's disk.

Open the 📄 evidence link. **The PDF must render in the panel.** A blank panel is
the `X-Frame-Options` regression (§9) — the app sends `SAMEORIGIN` for documents
and a proxy that overwrites it with `DENY` blanks exactly this.

### 5. Extract — and check *which engine ran*

Press Continue. In queue mode the API answers in milliseconds and the document
sits at `EXTRACTING`; the **worker's** journal should show
`extracting job … doc … (delivery 1)` and then `done: … -> EXTRACTED`.

Nothing in the worker journal means the API enqueued nowhere: check
`SQS_QUEUE_URL`, the region, and the instance role's `sqs:SendMessage`.

**Now the failure that looks like success.** A key that is *missing* no longer
gets this far: on `auth_provider=supabase` the app refuses to boot naming the
variable, so §7's `systemctl status` would already have caught it. This step is
what remains — a key that is present but **wrong**, and the `offline` providers
if somebody named them.

A wrong key fails loudly (the document goes to `FAILED` with
`EXTRACTION_FAILED (…)`), but check the provider that actually ran anyway, in the
journal or the job's audit trail: it must say `mistral` and `openai`, not
`offline`. That check is still worth doing precisely because it does not depend
on the refusal being right. If you want to isolate the keys before involving the
pipeline:

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/live_smoke_test.py
```

Then check metering recorded the spend — this is what proves `usage_event` writes
from the *worker's* process:

```bash
curl -sS https://<staging-host>/api/usage -H "Cookie: ec_session=<token>"
```

Token and page counts must be non-zero. Cost is `null` unless
`vendor_prices.json` is on the worker's box; that is correct, not a fault.

### 6. Review and XML download

Work through Critical Review, then Detailed Review, then Finalize. Download the
XML.

**Remember warn mode is the default** (`xml_strict_blocking=false`): blocking
cases warn and still produce XML, so a downloaded file is not by itself evidence
that nothing was flagged. Read the warnings.

Then test the isolation that matters, because it is the one thing a single-account
test can never show. Sign up a **second** account, sign in as it in a private
window, and fetch the first account's job by id:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<staging-host>/api/jobs/<job-id-of-account-1> -H "Cookie: ec_session=<account 2 token>"
```

**404 is the pass.** Repeat for `/xml` and `/brand-model-size.xls` on the same
job id — those two once returned another user's finished declaration to any
authenticated caller, and 404 on both is the regression check.

### 7. Password reset

Sign out. Choose "Forgot your password?" and submit the address from step 1.

**Expect 202 — for everything**, including an address that does not exist, a
Supabase rate limit, and a delivery failure. Supabase applies a per-address send
cooldown and only sends for an address that exists, so surfacing either one would
be an existence oracle two requests deep.

Check the mailbox. The link lands on `https://<staging-host>/#access_token=…&type=recovery`;
the SPA shows a reset screen and, again, strips the token from the address bar.
Set a new password.

**Then confirm two things that are easy to miss:**

* **You are not signed in afterwards.** The confirm route issues no session on
  purpose — a recovery token proves control of a mailbox, and this app's session
  is minted from a password grant. You should land on the sign-in screen.
* Signing in with the **new** password works and the **old** one does not.

Finally, submit reset five more times to trip the limiter (5/hour, 20/day in
`password_reset_attempt`). It counts every request that reached the provider,
whatever the provider answered — counting only the ones that produced an email
would put the oracle back in the remaining-tries.

### Pass criteria

| # | Proves |
| --- | --- |
| 1 | signup route, `allow_self_signup`, the signup limiter, `TRUSTED_PROXY_HOPS` |
| 2 | **SMTP, Site URL, Confirm email** — the whole email loop |
| 3 | Supabase password grant, `AUTH_SECRET` across workers, cookie flags, TLS |
| 4 | S3 + IAM, the upload gate, the evidence iframe and its headers |
| 5 | SQS both ways, the worker unit, **live vendor keys**, metering |
| 6 | the deterministic pipeline end to end, and tenant isolation |
| 7 | reset route, redirect URLs, the reset limiter, no-session-on-confirm |

---

## 12. Day-two operations

```bash
journalctl -u easycustoms-api -u easycustoms-worker -f
```

Every line carries a request id, echoed to the client as `X-Request-ID` and
carried into the SQS message — one id selects an upload and the queued extraction
it caused. That is the string to ask a reviewer for.

**Do not raise `EASYCUSTOMS_LOG_THIRD_PARTY_DEBUG` on this box even for
debugging.** It makes SQLAlchemy print statements with bound parameters and
`httpx` print request bodies. `EASYCUSTOMS_LOG_LEVEL=DEBUG` is the safe knob and
deliberately does not raise the libraries. The same rule applies to Caddy's own
`level` — request bodies there carry invoice contents and the login form.

**A wedged document.** With everything stopped:

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/python scripts/release_stuck_extractions.py --release
```

**Changing `EASYCUSTOMS_QUEUE_PROVIDER`.** Stop the API and every worker
**first**, flip it, start again. A document left `EXTRACTING` by a crash while the
queue was off has no message behind it, and queue mode disables the startup sweep
that used to clear such claims.

**Nothing watches the DLQ.** No alarm exists on `ApproximateNumberOfMessages` or
on a message landing in the dead-letter queue. Until one does, check it by hand
after any incident.

---

## 13. What cannot be verified until it is deployed

Listed so these are *expected*, not discovered. Nothing in CI, no test, and no
local run covers any of them.

1. **The email loop — above all.** Whether the confirmation and recovery emails
   are actually *sent*, actually *arrive*, and land on the right origin depends
   entirely on the Supabase project's SMTP, Site URL and Redirect URLs. The app
   never sees a send; it sees Supabase's acknowledgement, and it deliberately
   returns the same 202 whether an email went out or not. **A completely broken
   mail configuration is indistinguishable from a working one from this side.**
   Smoke-test steps 2 and 7 are the only test that exists, and they need a human
   with a mailbox.
2. **Whether "Confirm email" is on.** The app infers it from the signup response
   and logs a WARNING when it looks off — an inference, not a check.
3. **The redirect allow-list.** Supabase silently falls back to Site URL for a
   `redirect_to` it does not like. Nothing fails; users are quietly mailed links
   to the wrong origin.
4. **TLS and certificate renewal.** Issuance can only be tested against a real
   domain and open port 80. Renewal cannot be tested at all until ~60 days in —
   which is why the `email` in the Caddyfile is not decoration.
5. **Cookie `Secure` end to end.** Locally the app is on http, where `auto` and
   `always` are indistinguishable. Only a browser on real TLS shows it.
6. **`TRUSTED_PROXY_HOPS` being correct.** A wrong value does not error. Too low
   and everyone shares one throttle bucket; too high and nobody is throttled. The
   only proof is triggering a limiter from two different addresses.
7. **The live vendor keys, and the silent offline fallback.** The keys are not
   validated at boot. A deployment with no keys boots clean, extracts, and
   produces a complete-looking declaration from the offline extractor.
8. **The instance role's IAM.** Every S3 and SQS permission is tested by using
   it. A missing `s3:DeleteObject` only appears when someone deletes a job.
9. **The connection budget.** The pool arithmetic only fails under real
   concurrency, and it fails as `too many clients already` on an arbitrary
   request.
10. **`ProtectSystem=strict` against a path nobody exercised.** A write to
    somewhere unlisted fails only on the code path that writes it.
11. **Postgres at scale.** CI runs SQLite. The advisory-lock path, the pool, and
    the schema-at-head refusal all behave differently there.
12. **Transaction-mode pooling.** Untested, and expected to fail on prepared
    statements (§2). Do not find out on staging by accident.

---

## 14. Rollback

Decide the trigger before you deploy, not during. **Roll back if** `/api/health`
is not 200 five minutes after start, if sign-in fails for an account that worked
before, if extraction fails on a document that previously succeeded, or if the
journal shows a schema or config refusal.

### Application only — no migration in this deploy

The fast path, and the reason the repo is a git clone.

```bash
sudo systemctl stop easycustoms-api easycustoms-worker
```

```bash
cd /opt/easycustoms && sudo -u easycustoms git checkout <previous-tag-or-sha>
```

```bash
sudo -u easycustoms /opt/easycustoms/backend/.venv/bin/pip install -r /opt/easycustoms/backend/requirements.txt
```

```bash
sudo systemctl start easycustoms-api easycustoms-worker
```

Reinstall requirements even when they look unchanged: a rollback across a
dependency bump otherwise leaves the new library under the old code.

### The deploy included a migration

**This is the case to think about before you are in it.** Old code against a
newer schema is not automatically safe: additive changes (a new nullable column)
are usually fine, but anything that dropped or renamed will fail on the old
code's first query.

1. **Try code-only first.** Stop, check out the previous revision, start. If the
   journal shows the schema-at-head refusal, the old code will not run against
   the new schema and you need step 2.
2. **Downgrade one revision**, with both units stopped:

```bash
cd /opt/easycustoms/backend && sudo -u easycustoms .venv/bin/alembic downgrade -1
```

   Only if that revision's `downgrade()` is real. Read it first — a `downgrade`
   that drops a column drops the data in it.
3. **If it is not reversible, restore the database.** Supabase takes automatic
   backups; a point-in-time restore to just before the migration is the honest
   answer, and it loses everything written since. On staging that is acceptable,
   which is exactly why the migration rehearsal belongs here rather than in
   production.

**Take a snapshot immediately before every migration**, so step 3 is a decision
rather than an ordeal:

```bash
pg_dump "$EASYCUSTOMS_DATABASE_URL" -Fc -f ~/staging-before-<revision>.dump
```

### Configuration only

A bad `.env` needs no checkout. Fix the line and restart. Both units refuse to
boot on a contradictory configuration and say which setting, so the journal names
the fix:

```bash
journalctl -u easycustoms-api -n 50 --no-pager
```

### What rollback does not undo

* **Documents already in S3** stay there. Keys record where they live, so old
  code reads them fine.
* **Messages already on the queue.** A rolled-back worker may receive a message
  from a newer producer with a version it does not understand — it leaves it, by
  design, and after three deliveries the DLQ takes it. That is the design working.
* **Accounts created in Supabase.** Rolling back this app does not delete them.
* **A completed rollback is not a fix.** Write down what failed while it is fresh.

---

## See also

* `backend/deploy/` — the two units and the Caddyfile, each with the reasoning in
  its header
* `docs/queue-deployment.md` — queue infrastructure, the redrive policy, probes
* `docs/ADR-001-identity-and-tenancy.md` — who may see what, and why the admin
  role is not a data privilege
* `docs/plan-signup-and-roles.md` — the signup / reset / role work this deploys
* `.env.example` — the annotated settings file
* `CLAUDE.md` — the architectural rule, and which rules are frozen
