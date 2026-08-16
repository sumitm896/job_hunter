"""
Google Sheets I/O for job_hunter.

Two tabs:
  seen_jobs : fingerprint | company | title | first_seen_date | source_url
              | lane | verification
  daily_log : run_date | fingerprint | company | title | location | score
              | priority | lane | verification | applied | source_url
              | match_reasons | model | input_tokens | output_tokens | cost_eur

The `daily_log` tab is the browsable job board: filter by `lane` (lane1-ats =
trusted live ATS links; lane2-search = web_search, may include dead links),
sort by `score`, and tick the `applied` column yourself to track your pipeline.
The `applied` column is written empty by the script and never overwritten, so
your manual edits there are safe across runs.

Auth: uses GOOGLE_APPLICATION_CREDENTIALS env var (path to JSON key file)
      and GOOGLE_SHEET_ID env var (spreadsheet ID).

If either env var is missing, all functions in this module degrade to no-ops
with a printed warning. The script is designed to keep working even if Sheets
plumbing fails — daily emails should never break because of a logging failure.
"""

import os
from datetime import datetime, timedelta, timezone

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Header rows written when a tab is empty, so new columns get labelled.
SEEN_JOBS_HEADER = [
    "fingerprint", "company", "title", "first_seen_date", "source_url",
    "lane", "verification",
]
DAILY_LOG_HEADER = [
    "run_date", "fingerprint", "company", "title", "location", "score",
    "priority", "lane", "verification", "applied", "source_url",
    "match_reasons", "model", "input_tokens", "output_tokens", "cost_eur",
]

# Module-level cache so we don't re-auth on every call within one run.
_sheet_cache = None


def fingerprint(company: str, title: str) -> str:
    """Single source of truth for job identity. Used for dedup."""
    c = (company or "").strip().lower()
    t = (title or "").strip().lower()
    return f"{c}|{t}"


def _lane(job: dict) -> str:
    """Human-readable lane label from the tag Lane 1 sets, else Lane 2."""
    return job.get("source_lane", "lane2-search")


def _verification(job: dict) -> str:
    """api-verified (Lane 1, live-by-construction), else the verify.py verdict
    (unconfirmed) or 'verified' for Lane 2 jobs that passed verification."""
    v = job.get("verification_status")
    if v:
        return v
    return "verified"


def _get_sheet():
    """Open the spreadsheet. Returns None if creds/env are unavailable."""
    global _sheet_cache
    if _sheet_cache is not None:
        return _sheet_cache

    if not GSPREAD_AVAILABLE:
        print("[SHEET] gspread not installed — Sheets logging disabled.")
        return None

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not creds_path or not sheet_id:
        print("[SHEET] GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_SHEET_ID missing.")
        return None

    try:
        creds = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(creds)
        _sheet_cache = client.open_by_key(sheet_id)
        return _sheet_cache
    except Exception as e:
        print(f"[SHEET] Auth failed: {e}")
        return None


def _ensure_header(ws, header: list[str]) -> None:
    """Write the header row if the tab is completely empty. Never touches
    existing data — if the tab already has rows, this is a no-op, so older
    sheets keep their current layout and just gain blank cells under any new
    columns."""
    try:
        first = ws.row_values(1)
        if not first:
            ws.append_row(header, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[SHEET] header check skipped: {e}")


def read_seen_fingerprints(days: int = 14) -> set[str]:
    """
    Returns the set of fingerprints seen in the last `days` days.

    Reads the seen_jobs tab; expects header row + data rows where
    column A is fingerprint and column D is first_seen_date (YYYY-MM-DD).
    """
    sheet = _get_sheet()
    if sheet is None:
        return set()

    try:
        ws = sheet.worksheet("seen_jobs")
        # get_all_values returns list[list[str]] including header row
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return set()

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        seen = set()

        for row in rows[1:]:  # skip header
            if len(row) < 4:
                continue
            fp, _, _, first_seen, *_ = row + [""] * (4 - len(row))
            if not fp:
                continue
            try:
                first_seen_date = datetime.strptime(first_seen, "%Y-%m-%d").date()
                if first_seen_date >= cutoff:
                    seen.add(fp)
            except ValueError:
                # Malformed date — include the fingerprint to be safe
                # (better to over-exclude than show duplicates)
                seen.add(fp)

        print(f"[SHEET] Loaded {len(seen)} seen fingerprints from last {days} days.")
        return seen

    except Exception as e:
        print(f"[SHEET] read_seen_fingerprints failed: {e}")
        return set()


def append_seen_jobs(jobs: list[dict]) -> None:
    """Append today's new jobs to the seen_jobs tab."""
    sheet = _get_sheet()
    if sheet is None or not jobs:
        return

    try:
        ws = sheet.worksheet("seen_jobs")
        _ensure_header(ws, SEEN_JOBS_HEADER)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = [
            [
                fingerprint(j.get("company", ""), j.get("title", "")),
                j.get("company", ""),
                j.get("title", ""),
                today,
                j.get("url", ""),
                _lane(j),
                _verification(j),
            ]
            for j in jobs
        ]
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"[SHEET] Appended {len(rows)} rows to seen_jobs.")
    except Exception as e:
        print(f"[SHEET] append_seen_jobs failed: {e}")


def append_daily_log(jobs: list[dict], metadata: dict) -> None:
    """
    Append today's full results plus run metadata to daily_log.

    metadata expected keys:
        model, input_tokens, output_tokens, cost_eur
    """
    sheet = _get_sheet()
    if sheet is None or not jobs:
        return

    try:
        ws = sheet.worksheet("daily_log")
        _ensure_header(ws, DAILY_LOG_HEADER)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        model = metadata.get("model", "")
        input_tokens = metadata.get("input_tokens", 0)
        output_tokens = metadata.get("output_tokens", 0)
        cost_eur = metadata.get("cost_eur", 0.0)

        rows = []
        for j in jobs:
            reasons = j.get("match_reasons")
            rows.append([
                today,
                fingerprint(j.get("company", ""), j.get("title", "")),
                j.get("company", ""),
                j.get("title", ""),
                j.get("location", ""),
                j.get("match_score", 0),
                j.get("priority", ""),
                _lane(j),
                _verification(j),
                "",  # applied — left blank for you to tick manually
                j.get("url", ""),
                ", ".join(reasons) if isinstance(reasons, list) else "",
                model,
                input_tokens,
                output_tokens,
                round(cost_eur, 4),
            ])
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"[SHEET] Appended {len(rows)} rows to daily_log.")
    except Exception as e:
        print(f"[SHEET] append_daily_log failed: {e}")
