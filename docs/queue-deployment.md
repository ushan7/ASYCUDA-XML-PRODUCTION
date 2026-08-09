# Queue mode: infrastructure, deployment and verification

Queue mode splits extraction in two. `POST /extract` stops running OCR and the
LLM inside the request — it *claims* the document (status `EXTRACTING`) and puts
a reference on an SQS queue; `backend/worker.py`, a separate long-running
process, does the slow part. The API answers in milliseconds, the work survives
a deploy, and capacity becomes "run more workers".

It is **off by default**. `EASYCUSTOMS_QUEUE_PROVIDER=off` (the default) keeps
the original synchronous behaviour, unchanged, and nothing in this document
applies.

The design rules live where they are enforced — read `backend/worker.py`'s
module docstring before changing anything here. The one that explains the rest:
**a message dies with its attempt.** Success, refusal, skip *and failure* all
delete the message. Retrying a failure is the reviewer pressing Continue (a
fresh claim, a fresh message), never an SQS redelivery. So a redelivery means
one thing only — the worker died mid-run — which is what makes redelivery the
crash recovery and the DLQ a genuine poison-message queue.

---

## 1. Infrastructure

Two queues. Create them with the script rather than the console, and audit them
with it afterwards:

```bash
python scripts/provision_sqs.py            # read-only audit (default)
```

```bash
python scripts/provision_sqs.py --apply    # create what's missing, fix drift
```

Run it from `backend/`, with the same `.env` the API uses. The audit is the
point: a queue edited by hand during an incident has no record of what it was
supposed to be, and this reads the live attributes back and names every
difference.

| Queue | Attribute | Value | Why |
|---|---|---|---|
| `customs-processing-dlq` | Message retention | 14 days (max) | A poison document arrives exactly when nobody is watching. A shorter window deletes the evidence before the operator reads the alert. |
| | SSE-SQS encryption | on | Bodies name a job, a document and the operator who pressed Continue. |
| `customs-processing-queue` | Visibility timeout | 5 min | A floor, not the real window. Every receive names its own (`EASYCUSTOMS_SQS_VISIBILITY_EXTEND_SECONDS`, 10 min) and a heartbeat thread re-extends it for the length of the run. This value only covers a consumer that forgets to ask. |
| | Receive wait time | 20 s | Long polling. The worker passes `WaitTimeSeconds=20` too; setting it on the queue means an idle worker costs ~3 API calls a minute instead of thousands. |
| | Redrive policy | 3 receives → DLQ | See below — not optional. |
| | SSE-SQS encryption | on | |

Access policy: a queue created with **no** policy is already owner-only, which
is stricter than anything worth writing down. The script does not write one; it
reads an existing one back and complains if a statement opens the queue wider
than the owner (`Principal: "*"` with no condition) — the failure mode that
actually happens when a policy is pasted in from a tutorial.

### The redrive policy is load-bearing

There is exactly one message the worker deliberately never deletes: a
well-formed body carrying a `v` or `kind` it does not understand — a newer
producer during a rolling upgrade. Deleting it would strand that producer's
claim as `EXTRACTING` forever, so the worker leaves it and logs. With no
redrive policy that message redelivers until retention expires, waking a worker
to reject it every time. **The DLQ is the only drain in the design.** Verify it
end to end with `python producer.py --probe unsupported`.

### IAM

The worker's instance role needs four actions on the main queue, and nothing
on the DLQ (SQS moves messages there itself):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes"
    ],
    "Resource": "arn:aws:sqs:<region>:<account>:customs-processing-queue"
  }]
}
```

The API (producer) needs only `sqs:SendMessage` and `sqs:GetQueueAttributes` on
the same ARN. `scripts/provision_sqs.py` needs considerably more
(`CreateQueue`, `SetQueueAttributes`, `TagQueue`) — run it from a dev machine
under an admin identity, never from the worker's role.

---

## 2. Deployment

Background workers serve no HTTP traffic, so they need no domain, no
certificate and no reverse-proxy entry. An EC2 instance running the process
under `systemd` is the whole production story.

Units are in `backend/deploy/`:

* `easycustoms-worker.service` — the SQS consumer.
* `easycustoms-api.service` + `Caddyfile.example` — the API and its front end,
  for the machine that *does* serve traffic.

```bash
sudo cp backend/deploy/easycustoms-worker.service /etc/systemd/system/
```

```bash
sudo systemctl enable --now easycustoms-worker && journalctl -u easycustoms-worker -f
```

Prerequisites on the box are listed in the unit file itself. Three that bite:

* **Postgres, not SQLite,** unless the API and the worker are on one machine
  sharing one file. Two machines on SQLite means the worker extracts into a
  database the API never reads. The worker warns about this at startup; it
  cannot refuse, because the single-machine case is legitimate.
  On Postgres the schema is migrated explicitly — `cd backend && alembic upgrade
  head`, with both units stopped — and **both** the API and the worker refuse to
  start until the database is at head.
* **An instance role, not keys in `.env`.** `boto3` finds the role by itself.
  The `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` pair in `backend/.env` is
  the dev-machine fallback, read via Settings and handed to the client
  explicitly (a `.env` line never reaches `os.environ`, so `boto3` alone would
  not see it).
* **`TimeoutStopSec=900`.** SIGTERM starts a drain: no new receives, in-flight
  extractions finish. Give a slow packing list time to land before systemd
  escalates to SIGKILL.

Scaling out is running more workers — on more boxes, or as
`easycustoms-worker@N` template units. Every worker long-polls the same queue
and the document claim keeps them off each other's work.

### Changing the flag

Stop the API and every worker **first**, flip
`EASYCUSTOMS_QUEUE_PROVIDER`, start again. A document left `EXTRACTING` by a
crash while the queue was off has no message behind it, so no worker will ever
release it, and queue mode disables the startup sweep that used to clear such
claims. If one is wedged, with everything stopped:

```bash
python scripts/release_stuck_extractions.py --release
```

---

## 3. Local verification

Three terminals, all in `backend/`, all sharing one `.env`.

**1 — the API**

```bash
uvicorn app.main:app --reload --port 8000
```

**2 — the worker**

```bash
python worker.py
```

It refuses to start unless `EASYCUSTOMS_QUEUE_PROVIDER=sqs`, and logs the queue
URL, concurrency and heartbeat settings when it comes up.

**3 — work to do**

```bash
python producer.py --list
```

```bash
python producer.py --document-id <id>
```

`producer.py` enqueues through the real producer (`app/queueing.py`), claim and
all — a bare `send_message` would reach a worker that correctly finds nothing to
do and deletes it, giving you a green log line for a pipeline that never ran.

What a pass looks like in terminal 2: `extracting job … doc … (delivery 1)`,
then `done: … -> EXTRACTED`, and the message deleted. Terminal 1's `GET
/jobs/<id>` shows the document leaving `EXTRACTING` without the SPA touching
anything — its existing claim-watcher already knows how to wait on that status,
which is why queue mode needed no frontend change.

Probes test the wire instead of the pipeline, and are honest that they run no
extraction:

| Command | What the worker does | Proves |
|---|---|---|
| `producer.py --probe` | deletes it ("no longer exists") | credentials, region, queue URL, long polling |
| `producer.py --probe malformed` | logs the body, deletes it | the unintelligible-message path |
| `producer.py --probe unsupported` | **leaves it**, once per delivery | the redrive policy — it should reach the DLQ after 3 deliveries |

The automated equivalent, which needs no AWS account at all (the queue is
faked), is `pytest tests/test_queue_mode.py` — 17 tests covering the claim, the
double-submit refusal, the released claim on a failed send, every worker
disposal branch, and the heartbeat.

---

## 4. Status

**Done**

- [x] SQS producer (`app/queueing.py`) — claim-before-send, versioned message
      schema, claim released on a failed send
- [x] SQS consumer (`worker.py`) — long polling, bounded concurrency,
      visibility heartbeat, SIGTERM drain, one disposal rule per outcome
- [x] Config validation that refuses queue mode without a queue URL, or with a
      visibility window too short for the heartbeat
- [x] `POST /extract` wired to both modes behind the flag; no frontend change
- [x] Queue provisioning **and drift audit** (`scripts/provision_sqs.py`)
- [x] Both queues provisioned in `ap-south-1` and auditing clean (2026-08-09).
      The flag is still `off`, so they exist but carry no traffic.
- [x] Manual producer / wire probes (`producer.py`)
- [x] Stuck-claim recovery (`scripts/release_stuck_extractions.py`)
- [x] systemd units for worker and API, Caddy front end
- [x] Test coverage without an AWS account (`tests/test_queue_mode.py`)
- [x] PDF OCR via Mistral (`app/ocr/mistral.py`, with an offline provider)
- [x] LLM extraction pipeline (`app/extraction/`)
- [x] ASYCUDA XML mapping (`app/xml/composer.py`) and the brand/model/size
      export

**Not done**

- [x] Postgres is now *supported* properly: the driver ships, and the schema is
      owned by alembic with the app refusing to serve a database that is not at
      head (`alembic upgrade head`, or `stamp` for one that predates migrations).
- [ ] Postgres actually *deployed*. The default is still SQLite, so queue mode
      remains single-machine until `EASYCUSTOMS_DATABASE_URL` points at Postgres.
- [ ] Supabase auth cutover. `supabase/migrations/` and the commented
      `SUPABASE_*` block in `.env.example` are scaffolding — **nothing in the
      backend reads them.** Jobs are owned by `app/auth.py`'s operator, and the
      worker writes through SQLAlchemy, not the Supabase client. Wiring auth is
      a separate piece of work; it is not a missing part of the queue.
- [ ] Queue depth / DLQ alarms. Nothing watches `ApproximateNumberOfMessages`
      or tells anyone when a message lands in the DLQ.
- [ ] Worker metrics beyond the log stream.
