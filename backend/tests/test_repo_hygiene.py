"""Customer documents must not become tracked files (security round 2).

Seven real commercial invoices (`ALL7_split/`), a complete second git
repository committed inside this one (`.git1/`, 378 files with its own object
store and reflog), and a real import declaration carrying a named importer's
EXIM code, a broker's personal PAN, an LC number and a bank BIC
(`backend/sample_data/`) were all tracked — and all pushed.  Nothing in the
tooling noticed: `.gitignore` covered `.env` variants and `backend/storage/`,
and everything else was simply never looked at.

`backend/sample_data` has since been rebuilt from synthetic parties, so it is
no longer in the backlog below; what guards it now is
`test_sample_documents_are_generated_not_real`.

**The other two are now gone as well.**  `.git1/` and `ALL7_split/` were
purged from every commit on every branch with `git filter-repo`, and the
rewritten history force-pushed (the pack fell from 29.2 MiB to 5.3 MiB, and
every surviving blob hash is unchanged, so nothing else moved).  The backlog
below is therefore EMPTY, and the two tests above now guard the whole tree
with no exemptions at all.

One thing the rewrite could not reach, recorded here because it is invisible
from a checkout: GitHub's `refs/pull/*` refs still point at the pre-rewrite
commits, so `refs/pull/20/head` continues to resolve to a tree containing all
385 files.  Those refs are read-only to clients; clearing them needs a GitHub
Support request to garbage-collect unreachable objects, or recreating the
repository.  Until that happens the data is still fetchable by anyone with
repository access, and no test here can see it.

This is a ratchet, not a wish.  `_TRACKED_BACKLOG` lists exactly what is known
to be wrong TODAY, so the suite is green on the current tree and turns red the
moment anything NEW of the same shape is added.  Every entry removed from that
list is a fix that can never silently regress.  The list has reached empty;
keep it there.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent

# Paths whose tracking is a KNOWN, OPEN finding.  Shrink this list; never grow
# it.  Prefixes, matched against the repo-relative posix path.
#
# EMPTY, and it should stay that way.  It used to hold ".git1/" (a nested
# second git repository) and "ALL7_split/" (seven real commercial invoices);
# both were purged from the entire history, so the two tests above now run
# with no exemptions.  Adding an entry here is not a way to make a red suite
# green — it is a promise to come back and remove it.
_TRACKED_BACKLOG: tuple[str, ...] = ()

# Extensions that carry a scanned or exported customer document.
_DOCUMENT_SUFFIXES = {".pdf", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"}

# Reference data and generated fixtures the application genuinely ships.
# sample_data earns its place only because every PDF in it is now produced by
# scripts/generate_sample_documents.py from the JSON beside it — which is what
# test_sample_documents_are_generated_not_real proves, below.
_ALLOWED_DOCUMENTS = (
    "backend/reference_data/",
    "backend/sample_data/",
)

# Stamped on every page by the generator.  A real customer document dropped
# into sample_data will not carry it.
_SYNTHETIC_MARKER = "SYNTHETIC DEMO DOCUMENT"


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=_REPO, capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git here
        pytest.skip("git is not available")
    if out.returncode != 0:  # pragma: no cover - not a checkout
        pytest.skip("not a git checkout")
    return [p for p in out.stdout.split("\0") if p]


def _backlogged(path: str) -> bool:
    return path.startswith(_TRACKED_BACKLOG)


def test_no_new_customer_document_is_tracked():
    """A scanned document committed to the repo is a disclosure, not an asset."""
    offenders = sorted(
        p for p in _tracked_files()
        if Path(p).suffix.lower() in _DOCUMENT_SUFFIXES
        and not p.startswith(_ALLOWED_DOCUMENTS)
        and not _backlogged(p))
    assert not offenders, (
        "These document files are tracked and would be published to everyone with "
        "repository access, permanently — history keeps the blob even after a later "
        "delete:\n  " + "\n  ".join(offenders) +
        "\n\nCustomer paperwork belongs in backend/storage/ (already ignored), never in "
        "the repo. If a fixture is genuinely needed, it must carry invented parties.")


def test_no_second_git_repository_is_tracked():
    """A committed foreign git directory hides a whole history from review."""
    offenders = sorted(
        p for p in _tracked_files()
        if (p.startswith(".git") and "/" in p and not p.startswith(".github/"))
        and not _backlogged(p))
    assert not offenders, (
        "A git internal directory is tracked inside this repository:\n  "
        + "\n  ".join(offenders[:20]) +
        "\n\nNo review, `git log` or secret scanner reads a nested object store.")


def test_no_environment_file_is_tracked():
    """`.env.example` is the only env file that may ever be committed."""
    offenders = sorted(
        p for p in _tracked_files()
        if Path(p).name.startswith(".env") and Path(p).name != ".env.example")
    assert not offenders, (
        "An environment file is tracked:\n  " + "\n  ".join(offenders) +
        "\n\nRotate every credential it contains before doing anything else — it is in "
        "the history, not just the working tree.")


def test_sample_documents_are_generated_not_real():
    """sample_data is exempt from the document rule, so it needs its own lock.

    The five sample PDFs were real customer paperwork with an extractable text
    layer — the exporter, the bank reference and the broker all came out of
    `extract_text()` in one call. They are now generated from the JSON fixtures
    beside them and stamped on every page. Checking for that stamp is what
    stops the exemption above from quietly re-authorising the next real scan
    someone drops in for convenience.
    """
    from pypdf import PdfReader

    sample_dir = _REPO / "backend" / "sample_data"
    pdfs = sorted(sample_dir.glob("*.pdf"))
    assert pdfs, "no sample PDFs found — the demo and several tests need them"

    unmarked = []
    for pdf in pdfs:
        text = "".join((page.extract_text() or "") for page in PdfReader(str(pdf)).pages)
        if _SYNTHETIC_MARKER not in text:
            unmarked.append(pdf.name)
    assert not unmarked, (
        "These sample documents are not the generator's output:\n  " + "\n  ".join(unmarked) +
        "\n\nIf a real document was copied in, remove it — it will be published to "
        "everyone with repository access and stay in the history afterwards. Regenerate "
        "with: python backend/scripts/generate_sample_documents.py")


def test_the_backlog_only_shrinks():
    """Every backlog entry must still be a real, tracked problem.

    Left unmaintained, a quarantine list becomes a permanent exemption. When a
    prefix stops matching anything, the fix has landed and the entry must go —
    otherwise it silently re-authorises the next file that lands there.
    """
    tracked = _tracked_files()
    stale = [prefix for prefix in _TRACKED_BACKLOG
             if not any(p.startswith(prefix) for p in tracked)]
    assert not stale, (
        "These paths are no longer tracked, so the exemption is now covering nothing "
        "except the next mistake. Delete them from _TRACKED_BACKLOG:\n  "
        + "\n  ".join(stale))
