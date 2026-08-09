"""Create — or audit — the two SQS queues queue mode runs on.

    python scripts/provision_sqs.py            # audit only (default, read-only)
    python scripts/provision_sqs.py --apply    # create what is missing, fix drift

The queues are usually made once by hand in the console.  That is fine, and
this script exists for what happens next: a hand-made queue has no record of
what it was *supposed* to be, so a visibility timeout edited during an incident
or a redrive policy dropped by a console mis-click goes unnoticed until the day
it matters.  Audit mode reads the live attributes back and reports every
difference from the spec below; `--apply` writes the spec instead.

The spec, and why each number is that number:

  DLQ (customs-processing-dlq)
    MessageRetentionPeriod  14 days — the maximum SQS allows.  A poison
        document lands here precisely when nobody was watching; a shorter
        window deletes the evidence before the operator reads the alert.

  Main queue (customs-processing-queue)
    VisibilityTimeout       5 min — the floor, not the real window.  Every
        receive in worker.py names its own (EASYCUSTOMS_SQS_VISIBILITY_EXTEND_
        SECONDS, 10 min by default) and a heartbeat thread re-extends it for as
        long as the run lasts, so this value only covers a consumer that
        forgets to ask — never the normal path.
    ReceiveMessageWaitTimeSeconds  20 s — long polling.  The worker also passes
        WaitTimeSeconds=20 per receive; setting it on the queue too means an
        idle worker costs ~3 API calls a minute instead of thousands.
    RedrivePolicy           maxReceiveCount 3 -> the DLQ.  This is NOT
        optional decoration.  A message dies with its attempt in this design
        (see worker.py), so redelivery only ever means "the worker died
        mid-run" — and the ONE message the worker deliberately never deletes,
        a well-formed body with a `v`/`kind` it does not understand, has no
        other exit.  With no redrive policy that message redelivers until
        retention expires, and every delivery wakes a worker to reject it
        again.  The DLQ is the design's only drain.
    SqsManagedSseEnabled    on (both queues).  Bodies name a job and a
        document; the audit trail says who pressed Continue.

Access policy: a queue created with no `Policy` at all is already owner-only —
that implicit default is stricter than anything worth writing down, so this
script does not write one.  What it *does* do is read an existing policy back
and complain if a statement opens the queue wider than the owner (a bare
`Principal: "*"` with no account condition), which is the failure mode that
actually happens when a policy is pasted in from a tutorial.

Credentials and region come from the same place the producer and worker take
them (app.queueing.make_sqs_client): backend/.env via Settings, or boto3's own
chain — an instance role on EC2.  The IAM identity running this needs
sqs:CreateQueue, sqs:GetQueueUrl, sqs:GetQueueAttributes, sqs:ListQueueTags and
(for --apply) sqs:SetQueueAttributes, sqs:TagQueue — a broader identity than
the worker's, so run it from a dev machine, not from the worker's role.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings          # noqa: E402
from app.queueing import make_sqs_client     # noqa: E402

DEFAULT_QUEUE_NAME = "customs-processing-queue"
DEFAULT_DLQ_NAME = "customs-processing-dlq"

RETENTION_14_DAYS = 1209600
VISIBILITY_5_MIN = 300
LONG_POLL_SECONDS = 20
MAX_RECEIVES = 3

# Attributes compared in audit mode and written by --apply.  RedrivePolicy is
# absent here because it names the DLQ's ARN, which is only known at runtime.
DLQ_ATTRS = {
    "MessageRetentionPeriod": str(RETENTION_14_DAYS),
    "SqsManagedSseEnabled": "true",
}
QUEUE_ATTRS = {
    "VisibilityTimeout": str(VISIBILITY_5_MIN),
    "ReceiveMessageWaitTimeSeconds": str(LONG_POLL_SECONDS),
    "SqsManagedSseEnabled": "true",
}


def _tags(environment: str) -> dict[str, str]:
    return {
        "Project": "easy-customs",
        "Component": "extraction-queue",
        "Environment": environment,
        "ManagedBy": "backend/scripts/provision_sqs.py",
    }


class Report:
    """Findings, and whether anything is wrong.

    OK lines are printed too: an audit that only speaks up when it is unhappy
    leaves you unable to tell "checked, all good" from "never ran".
    """

    def __init__(self) -> None:
        self.problems = 0

    def ok(self, msg: str) -> None:
        print(f"  ok    {msg}")

    def drift(self, msg: str) -> None:
        self.problems += 1
        print(f"  DRIFT {msg}")

    def warn(self, msg: str) -> None:
        # Not a spec violation, but something the operator should know.
        print(f"  warn  {msg}")

    def fixed(self, msg: str) -> None:
        print(f"  set   {msg}")


def resolve_queue(sqs: Any, name: str) -> str | None:
    """The queue's URL, or None when it does not exist yet."""
    try:
        return sqs.get_queue_url(QueueName=name)["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist:
        return None


def read_attributes(sqs: Any, url: str) -> dict[str, str]:
    return sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]


def create_queue(sqs: Any, name: str, attrs: dict[str, str],
                 tags: dict[str, str]) -> str:
    return sqs.create_queue(QueueName=name, Attributes=dict(attrs), tags=dict(tags))["QueueUrl"]


def audit_attributes(live: dict[str, str], want: dict[str, str], report: Report,
                     *, label: str, apply_to: tuple[Any, str] | None) -> None:
    """Compare the attributes we care about; write them when --apply is on.

    Attributes we did not ask about are left alone — this script owns a spec,
    not the whole queue.
    """
    changes: dict[str, str] = {}
    for key, expected in want.items():
        actual = live.get(key)
        if actual == expected:
            report.ok(f"{label} {key} = {expected}")
        else:
            report.drift(f"{label} {key} = {actual!r}, expected {expected!r}")
            changes[key] = expected
    if changes and apply_to is not None:
        sqs, url = apply_to
        sqs.set_queue_attributes(QueueUrl=url, Attributes=changes)
        for key, value in changes.items():
            report.fixed(f"{label} {key} -> {value}")
        report.problems -= len(changes)


def audit_redrive(live: dict[str, str], dlq_arn: str, report: Report,
                  *, apply_to: tuple[Any, str] | None) -> None:
    """The redrive policy: present, pointed at OUR dlq, and 3 receives."""
    want = {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEIVES}
    raw = live.get("RedrivePolicy")
    current: dict[str, Any] = {}
    if raw:
        try:
            current = json.loads(raw)
        except ValueError:
            report.drift(f"queue RedrivePolicy is not valid JSON: {raw!r}")
    if not raw:
        report.drift("queue has NO RedrivePolicy — a message the worker cannot "
                     "understand would redeliver until retention expires, with "
                     "no DLQ to absorb it")
    elif current.get("deadLetterTargetArn") != dlq_arn:
        report.drift(f"queue RedrivePolicy points at "
                     f"{current.get('deadLetterTargetArn')!r}, not {dlq_arn!r}")
    elif int(current.get("maxReceiveCount", 0)) != MAX_RECEIVES:
        report.drift(f"queue maxReceiveCount = {current.get('maxReceiveCount')!r}, "
                     f"expected {MAX_RECEIVES}")
    else:
        report.ok(f"queue RedrivePolicy -> DLQ after {MAX_RECEIVES} receives")
        return
    if apply_to is not None:
        sqs, url = apply_to
        sqs.set_queue_attributes(
            QueueUrl=url, Attributes={"RedrivePolicy": json.dumps(want)})
        report.fixed(f"queue RedrivePolicy -> {dlq_arn} after {MAX_RECEIVES} receives")
        report.problems -= 1


def audit_policy(live: dict[str, str], report: Report, *, label: str) -> None:
    """Complain only about a policy that opens the queue beyond its owner.

    No `Policy` attribute at all is the *good* case: SQS then allows the
    account that owns the queue and nobody else.
    """
    raw = live.get("Policy")
    if not raw:
        report.ok(f"{label} has no access policy — owner-only by default")
        return
    try:
        policy = json.loads(raw)
    except ValueError:
        report.drift(f"{label} access policy is not valid JSON")
        return
    wide = []
    for stmt in policy.get("Statement", []):
        if stmt.get("Effect") != "Allow":
            continue
        principal = stmt.get("Principal")
        flat = principal.get("AWS") if isinstance(principal, dict) else principal
        values = flat if isinstance(flat, list) else [flat]
        if "*" in values and not stmt.get("Condition"):
            wide.append(stmt.get("Sid") or "(unnamed statement)")
    if wide:
        report.drift(f"{label} access policy allows ANY principal, unconditionally, "
                     f"in: {', '.join(wide)} — restrict it to the queue owner")
    else:
        report.ok(f"{label} access policy names specific principals")


def audit_tags(sqs: Any, url: str, want: dict[str, str], report: Report,
               *, label: str, apply_changes: bool) -> None:
    live = sqs.list_queue_tags(QueueUrl=url).get("Tags", {})
    missing = {k: v for k, v in want.items() if live.get(k) != v}
    if not missing:
        report.ok(f"{label} tags present ({', '.join(sorted(want))})")
        return
    # Tags are bookkeeping, not behaviour: report, but never fail the audit.
    report.warn(f"{label} tags missing or different: "
                f"{', '.join(f'{k}={v}' for k, v in sorted(missing.items()))}")
    if apply_changes:
        sqs.tag_queue(QueueUrl=url, Tags=missing)
        report.fixed(f"{label} tags updated")


def audit_settings(settings: Any, queue_url: str, dlq_url: str,
                   live: dict[str, str], report: Report) -> None:
    """Cross-check the live queue against what backend/.env says about it.

    A queue that is perfect and a backend pointed at a different one is the
    same outage as a queue that is wrong.
    """
    configured = (settings.sqs_queue_url or "").strip()
    if not configured:
        report.warn("SQS_QUEUE_URL is not set in backend/.env — set it to "
                    f"{queue_url}")
    elif configured == dlq_url:
        # Its own failure mode, because it does not look like one: work would
        # flow and workers would consume it.  What is missing is the drain —
        # a DLQ has no dead-letter queue of its own, so the message the worker
        # never deletes (unknown v/kind) and any document that kills a worker
        # redeliver until retention expires instead of being parked.
        report.drift(
            "SQS_QUEUE_URL points at the DEAD-LETTER queue, not the main queue. "
            "Extraction would appear to work — the worker would poll it happily "
            "— but a DLQ has no redrive policy, so a poison document would "
            "crash-loop for its whole retention window and nothing would ever "
            f"be quarantined for inspection. Point it at {queue_url}")
    elif configured != queue_url:
        report.drift(f"backend/.env SQS_QUEUE_URL = {configured!r}, but this "
                     f"queue is {queue_url!r}")
    else:
        report.ok("backend/.env SQS_QUEUE_URL matches this queue")

    if settings.queue_provider != "sqs":
        report.warn(f"EASYCUSTOMS_QUEUE_PROVIDER is {settings.queue_provider!r}: "
                    "the API still extracts inside the request and the worker "
                    "will refuse to start. Set it to 'sqs' to use these queues.")

    # The queue's own timeout only covers a consumer that does not name its
    # own.  Ours does — so this is a warning about *other* consumers, not a
    # fault in the queue.
    beat = settings.sqs_heartbeat_seconds
    if int(live.get("VisibilityTimeout", "0")) <= beat:
        report.warn(f"queue VisibilityTimeout ({live.get('VisibilityTimeout')}s) is "
                    f"not above the worker heartbeat ({beat}s); harmless for "
                    "worker.py, which names its own per receive, but any other "
                    "consumer would see duplicate deliveries")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or audit the Easy Customs SQS queues.")
    parser.add_argument("--apply", action="store_true",
                        help="create missing queues and correct drift "
                             "(default is a read-only audit)")
    parser.add_argument("--queue-name", default=DEFAULT_QUEUE_NAME)
    parser.add_argument("--dlq-name", default=DEFAULT_DLQ_NAME)
    parser.add_argument("--environment", default="production",
                        help="value for the Environment tag (default: production)")
    args = parser.parse_args()

    settings = get_settings()
    sqs = make_sqs_client(settings)
    tags = _tags(args.environment)
    report = Report()
    mode = "APPLY" if args.apply else "AUDIT (read-only)"
    print(f"{mode}  region={settings.sqs_region or '(boto3 default chain)'}")

    # ---- DLQ first: the main queue's redrive policy needs its ARN ----------
    print(f"\n{args.dlq_name}")
    dlq_url = resolve_queue(sqs, args.dlq_name)
    if dlq_url is None:
        if not args.apply:
            report.drift(f"{args.dlq_name} does not exist — re-run with --apply")
            print(f"\n{report.problems} problem(s). Nothing was changed.")
            return 1
        dlq_url = create_queue(sqs, args.dlq_name, DLQ_ATTRS, tags)
        report.fixed(f"created {dlq_url}")
    dlq_live = read_attributes(sqs, dlq_url)
    dlq_arn = dlq_live["QueueArn"]
    audit_attributes(dlq_live, DLQ_ATTRS, report, label="dlq",
                     apply_to=(sqs, dlq_url) if args.apply else None)
    audit_policy(dlq_live, report, label="dlq")
    audit_tags(sqs, dlq_url, tags, report, label="dlq", apply_changes=args.apply)

    # ---- Main queue --------------------------------------------------------
    print(f"\n{args.queue_name}")
    queue_url = resolve_queue(sqs, args.queue_name)
    if queue_url is None:
        if not args.apply:
            report.drift(f"{args.queue_name} does not exist — re-run with --apply")
            # Say this HERE rather than in the .env section below, which a
            # missing queue never reaches: a backend pointed at the DLQ while
            # the main queue is absent is the worst of the two findings, and
            # the one most likely to be mistaken for a working setup.
            if (settings.sqs_queue_url or "").strip() == dlq_url:
                report.drift(
                    "…and SQS_QUEUE_URL points at the DEAD-LETTER queue, so "
                    "extraction would run with no drain behind it: a poison "
                    "document would redeliver for its whole retention window "
                    "and nothing would ever be quarantined")
            print(f"\n{report.problems} problem(s). Nothing was changed.")
            return 1
        attrs = dict(QUEUE_ATTRS)
        attrs["RedrivePolicy"] = json.dumps(
            {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": MAX_RECEIVES})
        queue_url = create_queue(sqs, args.queue_name, attrs, tags)
        report.fixed(f"created {queue_url}")
    queue_live = read_attributes(sqs, queue_url)
    audit_attributes(queue_live, QUEUE_ATTRS, report, label="queue",
                     apply_to=(sqs, queue_url) if args.apply else None)
    audit_redrive(queue_live, dlq_arn, report,
                  apply_to=(sqs, queue_url) if args.apply else None)
    audit_policy(queue_live, report, label="queue")
    audit_tags(sqs, queue_url, tags, report, label="queue", apply_changes=args.apply)

    # ---- The backend's own view of all this --------------------------------
    print("\nbackend/.env")
    audit_settings(settings, queue_url, dlq_url, queue_live, report)

    print()
    if report.problems:
        print(f"{report.problems} problem(s) remain. "
              f"{'Re-run --apply after fixing them by hand.' if args.apply else 'Re-run with --apply to correct them.'}")
        return 1
    print(f"Queues match the spec.\n  SQS_QUEUE_URL={queue_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
