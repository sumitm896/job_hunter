"""
URL verification for Lane 2 (web_search) results.

Primary path (when FIRECRAWL_API_KEY is set): scrape each candidate URL with
Firecrawl /v2/scrape — it renders JavaScript and gets past most anti-bot walls,
so it can actually READ pages that Claude's web_fetch went blind on. A status
code catches dead links (404/410) instantly; Haiku judges the rest from the
real page content.

Fallback path (no Firecrawl key): the original Claude + web_fetch verifier,
preserved verbatim, so removing the key never breaks the pipeline.

Public entry point — unchanged signature:
    filter_verified(jobs, keep_unsure=True) -> (kept_jobs, metadata)

Lane 1 (ATS API) results skip this entirely; they're live by construction.
"""

import json
import os
import re
import time

import requests
from anthropic import Anthropic


VERIFIER_MODEL = "claude-haiku-4-5"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
_SCRAPE_CHAR_LIMIT = 1500        # markdown per job handed to Haiku
_SCRAPE_TIMEOUT_MS = 45000       # Firecrawl-side render timeout
_HTTP_TIMEOUT_S = 60             # our HTTP client timeout


# ── Public entry point ─────────────────────────────────────────────────────

def filter_verified(jobs: list[dict], keep_unsure: bool = True) -> tuple[list[dict], dict]:
    """Verify each job's URL. Dispatches to the Firecrawl verifier when a key
    is present, else the legacy Claude web_fetch verifier."""
    if not jobs:
        return [], _empty_meta()

    firecrawl_key = os.environ.get("FIRECRAWL_API_KEY")
    if firecrawl_key:
        return _verify_firecrawl(jobs, keep_unsure, firecrawl_key)

    print("[VERIFY] No FIRECRAWL_API_KEY — using legacy Claude web_fetch verifier.")
    return _verify_claude(jobs, keep_unsure)


def _empty_meta() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "kept": 0, "dropped": 0,
            "unsure": 0, "fake_count": 0, "firecrawl_scrapes": 0}


# ── Firecrawl scrape path ──────────────────────────────────────────────────

def _firecrawl_scrape(url: str, api_key: str) -> dict:
    """Scrape one URL. Returns {ok, status, markdown, final_url, error}."""
    try:
        resp = requests.post(
            FIRECRAWL_SCRAPE_URL,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "proxy": "auto",            # auto-retries with enhanced proxy, no surcharge
                "timeout": _SCRAPE_TIMEOUT_MS,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
    except Exception as e:
        return {"ok": False, "status": None, "markdown": "", "final_url": url,
                "error": f"request failed: {e}"}

    # Firecrawl returns 402/429/500 with an error body when it can't serve.
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", f"HTTP {resp.status_code}")
        except Exception:
            err = f"HTTP {resp.status_code}"
        return {"ok": False, "status": None, "markdown": "", "final_url": url,
                "error": str(err)}

    try:
        body = resp.json()
    except Exception as e:
        return {"ok": False, "status": None, "markdown": "", "final_url": url,
                "error": f"bad JSON: {e}"}

    data = body.get("data") or {}
    meta = data.get("metadata") or {}
    return {
        "ok": bool(body.get("success")),
        "status": meta.get("statusCode"),
        "markdown": data.get("markdown") or "",
        "final_url": meta.get("url") or meta.get("sourceURL") or url,
        "error": meta.get("error"),
    }


def _verify_firecrawl(jobs: list[dict], keep_unsure: bool, api_key: str) -> tuple[list[dict], dict]:
    # 1. Scrape every URL; apply the cheap status-code heuristics first.
    records = []           # per-job working state
    to_judge = []          # indices whose content needs Haiku
    scrapes = 0
    for i, job in enumerate(jobs):
        url = job.get("url", "")
        company = job.get("company", "?")
        title = job.get("title", "?")
        rec = {"index": i, "verdict": None, "reason": ""}

        if not url or url == "#":
            rec["verdict"] = "unsure"
            rec["reason"] = "no url to verify"
            records.append(rec)
            continue

        res = _firecrawl_scrape(url, api_key)
        scrapes += 1
        rec["scrape"] = res

        status = res.get("status")
        if not res["ok"] and status is None:
            rec["verdict"] = "unsure"
            rec["reason"] = f"could not scrape ({res.get('error', 'unknown')})"
        elif status in (404, 410):
            rec["verdict"] = "fake"
            rec["reason"] = f"page returns {status}"
        elif status in (401, 403, 429, 500, 502, 503):
            rec["verdict"] = "unsure"
            rec["reason"] = f"blocked/unreadable ({status})"
        elif not res["markdown"].strip():
            rec["verdict"] = "unsure"
            rec["reason"] = "empty page content"
        else:
            to_judge.append(i)   # 200 with content -> Haiku decides

        records.append(rec)
        time.sleep(0.3)          # be gentle on the free-tier rate limit

    # 2. One Haiku call judges all the readable pages.
    meta_tokens = {"input_tokens": 0, "output_tokens": 0}
    if to_judge:
        verdicts = _judge_scraped(jobs, records, to_judge, meta_tokens)
        for idx, v in verdicts.items():
            records[idx]["verdict"] = v.get("verdict", "unsure")
            records[idx]["reason"] = v.get("reason", "")

    # 3. Assemble kept list + metadata.
    kept, fake_count, unsure_count = [], 0, 0
    for rec in records:
        job = jobs[rec["index"]]
        company = job.get("company", "?")
        title = job.get("title", "?")
        verdict = rec["verdict"] or "unsure"
        reason = rec["reason"] or "no verdict"

        if verdict == "real":
            kept.append(job)
            print(f"[VERIFY] KEEP: {company} - {title} (real)")
        elif verdict == "fake":
            fake_count += 1
            print(f"[VERIFY] DROP: {company} - {title} ({reason}) {job.get('url','')}")
        else:
            unsure_count += 1
            if keep_unsure:
                job["verification_status"] = "unconfirmed"
                kept.append(job)
                print(f"[VERIFY] KEEP-UNSURE: {company} - {title} ({reason})")
            else:
                print(f"[VERIFY] DROP-UNSURE: {company} - {title} ({reason})")

    metadata = {
        "input_tokens": meta_tokens["input_tokens"],
        "output_tokens": meta_tokens["output_tokens"],
        "kept": len(kept),
        "dropped": len(jobs) - len(kept),
        "unsure": unsure_count,
        "fake_count": fake_count,
        "firecrawl_scrapes": scrapes,
    }
    print(f"[VERIFY] Firecrawl result: {metadata['kept']} kept, {fake_count} fake, "
          f"{unsure_count} unsure ({scrapes} scrapes, kept_unsure={keep_unsure})")
    return kept, metadata


def _judge_scraped(jobs, records, to_judge, meta_tokens) -> dict:
    """One Haiku call: judge the scraped pages as real/fake/unsure."""
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    blocks = []
    for i in to_judge:
        job = jobs[i]
        res = records[i]["scrape"]
        excerpt = res["markdown"][:_SCRAPE_CHAR_LIMIT].replace("\n", " ")
        redirect = ""
        if res["final_url"] and res["final_url"] != job.get("url", ""):
            redirect = f" [redirected to: {res['final_url']}]"
        blocks.append(
            f"### index {i}\ncompany: {job.get('company','?')}\n"
            f"title: {job.get('title','?')}\n"
            f"requested_url: {job.get('url','')}{redirect}\n"
            f"page_content: {excerpt}"
        )
    prompt = (
        "For each job below you are given the scraped page content. Decide "
        "whether the page is a SPECIFIC, currently-open posting for that role at "
        "that company.\n"
        '  "real"  — the page shows this specific open role (title/description/apply).\n'
        '  "fake"  — expired, "no longer available", a generic careers/listing '
        "page, a redirect to a homepage, or a different role.\n"
        '  "unsure" — genuinely cannot tell from the content.\n\n'
        "Return ONLY a JSON array, one object per job, same indexes:\n"
        '[{"index": <int>, "verdict": "real|fake|unsure", "reason": "<8 words>"}]\n\n'
        + "\n\n".join(blocks)
    )

    try:
        resp = client.messages.create(
            model=VERIFIER_MODEL,
            max_tokens=1500,
            system="You verify job postings from scraped page content. Output ONLY a JSON array.",
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[VERIFY] Haiku judge failed ({e}); marking judged pages unsure.")
        return {i: {"verdict": "unsure", "reason": "judge call failed"} for i in to_judge}

    meta_tokens["input_tokens"] += getattr(resp.usage, "input_tokens", 0)
    meta_tokens["output_tokens"] += getattr(resp.usage, "output_tokens", 0)

    text_blocks = [b.text for b in resp.content
                   if getattr(b, "type", "") == "text" and getattr(b, "text", "").strip()]
    verdicts = _parse_verdicts(text_blocks[-1] if text_blocks else "")
    out = {}
    for v in verdicts:
        if isinstance(v, dict) and "index" in v:
            out[v["index"]] = v
    # any judged index the model skipped -> unsure
    for i in to_judge:
        out.setdefault(i, {"verdict": "unsure", "reason": "no verdict returned"})
    return out


def _parse_verdicts(raw_text: str) -> list[dict]:
    text = re.sub(r"```json\s*|```", "", (raw_text or "").strip())
    start = text.find("[")
    if start == -1:
        return []
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        return parsed if isinstance(parsed, list) else []
    except Exception:
        # salvage complete objects from a truncated array
        out, depth, obj_start = [], 0, None
        body = text[start:]
        for idx, ch in enumerate(body):
            if ch == "{":
                if depth == 0:
                    obj_start = idx
                depth += 1
            elif ch == "}" and depth > 0:
                depth -= 1
                if depth == 0 and obj_start is not None:
                    try:
                        out.append(json.loads(body[obj_start:idx + 1]))
                    except Exception:
                        pass
                    obj_start = None
        return out


# ── Legacy Claude web_fetch path (fallback when no Firecrawl key) ───────────

_LEGACY_SYSTEM = (
    "You are a strict URL verifier for a job-search pipeline. For each URL, "
    "fetch it with the web_fetch tool, then judge whether it points to a "
    "SPECIFIC, currently-open job posting. Company homepages, generic /careers "
    "pages, expired postings and 404s are all FAKE. Return ONLY a JSON array."
)


def _verify_claude(jobs: list[dict], keep_unsure: bool) -> tuple[list[dict], dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[VERIFY] ANTHROPIC_API_KEY missing — keeping all jobs unverified.")
        m = _empty_meta(); m["kept"] = len(jobs)
        return jobs, m

    client = Anthropic(api_key=api_key)
    lines = [f"{i}. {j.get('company','?')} - {j.get('title','?')} -> {j.get('url','')}"
             for i, j in enumerate(jobs)]
    prompt = (
        f"Verify these {len(jobs)} job URLs. Use web_fetch on each, then return a "
        f"JSON array with one object per URL in the same order:\n"
        f'  "index": int\n  "verdict": "real" | "fake" | "unsure"\n'
        f'  "reason": short string\n\n' + "\n".join(lines) +
        "\n\nReturn ONLY the JSON array."
    )

    try:
        resp = client.messages.create(
            model=VERIFIER_MODEL,
            max_tokens=2000,
            system=_LEGACY_SYSTEM,
            tools=[{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 15}],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[VERIFY] Verifier call failed: {e}. Keeping all jobs.")
        m = _empty_meta(); m["kept"] = len(jobs)
        return jobs, m

    text_blocks = [b.text for b in resp.content
                   if getattr(b, "type", "") == "text" and getattr(b, "text", "").strip()]
    verdicts = _parse_verdicts(text_blocks[-1] if text_blocks else "")
    by_index = {v["index"]: v for v in verdicts if isinstance(v, dict) and "index" in v}

    kept, fake_count, unsure_count = [], 0, 0
    for i, job in enumerate(jobs):
        v = by_index.get(i, {})
        decision = v.get("verdict", "unsure")
        reason = v.get("reason", "no verdict returned")
        company, title = job.get("company", "?"), job.get("title", "?")
        if decision == "real":
            kept.append(job)
            print(f"[VERIFY] KEEP: {company} - {title} (real)")
        elif decision == "fake":
            fake_count += 1
            print(f"[VERIFY] DROP: {company} - {title} ({reason}) {job.get('url','')}")
        else:
            unsure_count += 1
            if keep_unsure:
                job["verification_status"] = "unconfirmed"
                kept.append(job)
                print(f"[VERIFY] KEEP-UNSURE: {company} - {title} ({reason})")
            else:
                print(f"[VERIFY] DROP-UNSURE: {company} - {title} ({reason}) {job.get('url','')}")

    metadata = {
        "input_tokens": getattr(resp.usage, "input_tokens", 0),
        "output_tokens": getattr(resp.usage, "output_tokens", 0),
        "kept": len(kept), "dropped": len(jobs) - len(kept),
        "unsure": unsure_count, "fake_count": fake_count, "firecrawl_scrapes": 0,
    }
    print(f"[VERIFY] Result: {metadata['kept']} kept, {fake_count} fake, "
          f"{unsure_count} unsure (kept_unsure={keep_unsure})")
    return kept, metadata
