"""
URL verification using Claude + web_fetch.

Why this exists: Haiku 4.5 with web search occasionally fabricates job URLs
when search results are weak. It constructs plausible-looking URLs that
don't exist. The script previously trusted these and wrote fake jobs to
the email and Sheet.

This module sends all candidate URLs to Claude in a single call with the
web_fetch tool enabled. Claude fetches each URL and returns a JSON verdict
(real / fake / unsure) plus a one-line reason per URL.

Cost: ~$0.005 per run. Worth it for trustworthy output.
"""

import json
import os
import re
from anthropic import Anthropic


VERIFIER_MODEL = "claude-haiku-4-5"
VERIFIER_SYSTEM = (
    "You are a strict URL verifier for a job-search pipeline. "
    "For each URL given, fetch it using the web_fetch tool, then judge "
    "whether it points to a SPECIFIC, currently-open job posting. "
    "Be strict: company homepages, generic /careers landing pages, expired "
    "postings, and 404s are all FAKE. Only a URL that clearly displays a "
    "specific job's title, description, and apply button is REAL.\n\n"
    "Return ONLY a JSON array. No prose."
)


def _build_verifier_prompt(jobs: list[dict]) -> str:
    """Build the user message for the verifier. Includes URL + label per job."""
    lines = []
    for i, j in enumerate(jobs):
        url = j.get("url", "")
        company = j.get("company", "?")
        title = j.get("title", "?")
        lines.append(f"{i}. {company} - {title} -> {url}")
    job_list = "\n".join(lines)

    return (
        f"Verify the following {len(jobs)} job URLs. Use web_fetch on each "
        f"URL. Then return a JSON array with one object per URL, in the same "
        f"order. Each object has:\n"
        f'  "index": int (matches the number in the input)\n'
        f'  "verdict": "real" | "fake" | "unsure"\n'
        f'  "reason": short string (under 15 words)\n\n'
        f"Verdicts:\n"
        f'  "real" — the page shows a specific job posting that is currently open.\n'
        f'  "fake" — 404, redirected to homepage, expired, or shows a generic '
        f'/careers listing rather than one specific role.\n'
        f'  "unsure" — could not fetch (timeout, anti-bot block) AND cannot verify.\n\n'
        f"URLs to verify:\n{job_list}\n\n"
        f"Return ONLY a JSON array. No conversational text."
    )


def _parse_verdicts(raw_text: str) -> list[dict]:
    """Robust parser — same approach as parse_jobs in main script."""
    text = raw_text.strip()
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)

    start_idx = text.find("[")
    if start_idx == -1:
        return []

    try:
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(text[start_idx:])
        if isinstance(parsed, list):
            return parsed
        return []
    except Exception as e:
        print(f"[VERIFY] Failed to parse verdicts: {e}")
        return []


def filter_verified(jobs: list[dict], keep_unsure: bool = True) -> tuple[list[dict], dict]:
    """
    Verify each job's URL using Claude + web_fetch.

    Returns (kept_jobs, verification_metadata).

    `keep_unsure=True` (default) means jobs Claude couldn't fetch (anti-bot,
    timeout) are kept rather than dropped. For our use case, keeping is
    better — false negatives are worse than the small chance of a kept
    "unsure" being fake.

    Metadata returned: {"input_tokens", "output_tokens", "kept", "dropped",
    "unsure", "fake_count"}
    """
    if not jobs:
        return [], {"input_tokens": 0, "output_tokens": 0, "kept": 0,
                    "dropped": 0, "unsure": 0, "fake_count": 0}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[VERIFY] ANTHROPIC_API_KEY missing — skipping verification.")
        return jobs, {"input_tokens": 0, "output_tokens": 0, "kept": len(jobs),
                      "dropped": 0, "unsure": 0, "fake_count": 0}

    client = Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=VERIFIER_MODEL,
            max_tokens=2000,
            system=VERIFIER_SYSTEM,
            tools=[{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 15}],
            messages=[
                {"role": "user", "content": _build_verifier_prompt(jobs)},
            ],
        )
    except Exception as e:
        # If verification call fails entirely, prefer keeping jobs over
        # dropping them silently. Log and pass through.
        print(f"[VERIFY] Verifier call failed: {e}. Keeping all jobs.")
        return jobs, {"input_tokens": 0, "output_tokens": 0, "kept": len(jobs),
                      "dropped": 0, "unsure": 0, "fake_count": 0}

    # Extract response text. With web_fetch enabled, response.content contains
    # interleaved tool_use / tool_result / text blocks. Claude's narration
    # ("Let me fetch these...") can appear as earlier text blocks and may
    # contain brackets. Concatenating everything and grabbing the first "["
    # can latch onto narration instead of the verdict JSON, which would return
    # [] and silently fall through to "keep all jobs" — defeating the verifier.
    # The verdict is always the FINAL text block, so use that.
    text_blocks = [
        block.text for block in response.content
        if getattr(block, "type", "") == "text" and getattr(block, "text", "").strip()
    ]
    full_text = text_blocks[-1] if text_blocks else ""

    verdicts = _parse_verdicts(full_text)
    if not verdicts:
        print("[VERIFY] No verdicts parsed. Keeping all jobs.")
        return jobs, {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
            "kept": len(jobs), "dropped": 0, "unsure": 0, "fake_count": 0,
        }

    # Build index -> verdict map for safe lookup
    by_index = {}
    for v in verdicts:
        if isinstance(v, dict) and "index" in v:
            by_index[v["index"]] = v

    kept = []
    fake_count = 0
    unsure_count = 0
    for i, job in enumerate(jobs):
        verdict = by_index.get(i, {})
        decision = verdict.get("verdict", "unsure")
        reason = verdict.get("reason", "no verdict returned")
        company = job.get("company", "?")
        title = job.get("title", "?")
        url = job.get("url", "")

        if decision == "real":
            kept.append(job)
            print(f"[VERIFY] KEEP: {company} - {title} (real)")
        elif decision == "fake":
            fake_count += 1
            print(f"[VERIFY] DROP: {company} - {title} ({reason}) {url}")
        else:  # unsure
            unsure_count += 1
            if keep_unsure:
                kept.append(job)
                print(f"[VERIFY] KEEP-UNSURE: {company} - {title} ({reason})")
            else:
                print(f"[VERIFY] DROP-UNSURE: {company} - {title} ({reason}) {url}")

    metadata = {
        "input_tokens": getattr(response.usage, "input_tokens", 0),
        "output_tokens": getattr(response.usage, "output_tokens", 0),
        "kept": len(kept),
        "dropped": len(jobs) - len(kept),
        "unsure": unsure_count,
        "fake_count": fake_count,
    }

    print(
        f"[VERIFY] Result: {metadata['kept']} kept, {metadata['fake_count']} fake, "
        f"{metadata['unsure']} unsure (kept={keep_unsure})"
    )

    return kept, metadata
