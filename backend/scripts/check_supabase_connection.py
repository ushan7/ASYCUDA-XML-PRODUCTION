"""Supabase connectivity check — proves the URL and service key in
backend/.env actually reach the project and that a core table answers.

Run from the backend directory:

    python scripts/check_supabase_connection.py

This was `backend/test_db.py`, where the `test_` prefix made pytest COLLECT
it: there are no test functions, so importing the module was the whole
program, and `pytest -q` fired this live query at the real project on every
run — silently, and only when the network and credentials happened to work.
Renamed out of both collection patterns (`test_*.py`, `*_test.py`) and put
behind main(), the same shape as live_smoke_test.py.

Note this proves the SERVICE key works, which bypasses row-level security.
It says nothing about what an end user's anon key can actually see.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

BACKEND_ROOT = Path(__file__).resolve().parent.parent
TABLE = "document_generations"


def main() -> int:
    # Explicit path: bare load_dotenv() searches upward from the CWD, so
    # running this from the repo root would quietly find nothing and report a
    # missing key rather than a missing file.
    load_dotenv(BACKEND_ROOT / ".env")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("[!] Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in backend/.env.")
        return 1

    print("Supabase URL :", url)
    print(f">> Selecting one row from {TABLE} ...")
    try:
        client = create_client(url, key)
        client.table(TABLE).select("*").limit(1).execute()
    except Exception as e:
        print(f"\n[!] Connection failed: {e}")
        return 1

    # ASCII only in printed strings: the Windows console is cp1252 here and
    # turns an em-dash into a literal '?'.
    print(f"\nOK - the project is reachable and {TABLE} answered.")
    print("    (This is the Supabase prototype schema, not the schema the")
    print("     FastAPI app uses - that one is Alembic's, see CLAUDE.md.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
