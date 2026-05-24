"""
Daily Job Hunter — Automated job search using Claude API + Web Search.

Finds TAM/adjacent roles in Ireland, scores them, deduplicates against
recent history, emails results, and logs run metadata to Google Sheets.
"""

import anthropic
import smtplib
import json
import os
import re
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

import sheet  # local module for Google Sheets I/O
import verify  # local module for URL verification

# ── Configuration ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

MODEL_ID = "claude-haiku-4-5"

# Drop jobs scoring below this from the digest. Keeps marginal-fit roles from
# padding the email. Set to 0 to disable. Transferable-fit roles should clear
# this comfortably; genuinely weak matches fall below it.
MIN_MATCH_SCORE = 65

# Pricing (USD per million tokens) for cost reporting.
# Source: anthropic.com/claude/haiku, Oct 2025 pricing.
PRICE_INPUT_PER_M = 1.00
PRICE_OUTPUT_PER_M = 5.00
PRICE_CACHE_READ_PER_M = 0.10  # 10% of input
PRICE_CACHE_WRITE_PER_M = 1.25  # 1.25x input for 5-min cache
PRICE_WEB_SEARCH_PER_QUERY = 0.01  # $10 per 1,000 searches
USD_TO_EUR = 0.92  # Approximate. Refresh occasionally.

TODAY = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

# ── Profile ────────────────────────────────────────────────────────────────
PROFILE = {
    "name": "Sumit Mishra",
    # Geography: open to all of Ireland plus Ireland-based remote roles.
    "location": "Ireland (incl. remote)",
    "based_in": "Dublin, Ireland",
    "target_roles": [
        "Technical Account Manager",
        "Solutions Engineer",
        "Solutions Consultant",
        "Customer Success Manager",
        "Enterprise Support Engineer",
        "Client Solutions Manager",
        "Implementation Consultant",
        # Adjacent titles — same post-sales technical-relationship motion under
        # different company naming conventions.
        "Technical Success Manager",
        "Customer Engineer",
        "Onboarding Manager",
    ],
    "experience_years": 4,
    # Transferable strengths — these are the CORE of what makes Sumit a fit for
    # technical-relationship roles regardless of the company's specific stack.
    # The matcher scores primarily on these.
    "core_strengths": [
        "Post-sales enterprise technical relationship management",
        "Turning messy escalations into structured outcomes",
        "Leading both technical (engineer) and commercial (executive) conversations",
        "API / webhook / integration troubleshooting and root-cause diagnosis",
        "QBR facilitation and consultative account reviews",
        "Driving platform adoption, retention, and expansion",
        "Cross-functional escalation process design",
        "Technical documentation and self-serve playbooks",
    ],
    # Concrete tooling/stack — used as a BONUS signal to rank closely-matching
    # roles higher when a posting happens to mention them. NEVER a requirement.
    "bonus_stack": [
        "REST APIs", "Webhooks", "Postman", "SQL", "Python",
        "Payment workflows", "Subscription lifecycle", "ITIL",
        "Workflow Automation", "GitHub Actions",
    ],
    # Quantified impact — gives the matcher evidence to judge seniority/fit and
    # to write honest match reasons.
    "impact_highlights": [
        "98% SLA compliance across API/webhook/workflow queries",
        "15-20% platform utilisation lift across enterprise accounts",
        "25% reduction in escalation resolution time via repeatable process",
        "Managed onboarding/success for 50+ SaaS clients",
    ],
    "education": "MSc Business Analytics, University of Galway",
}

# ── Search queries (rotated by weekday) ────────────────────────────────────
# Broad on ROLE, wide on geography (all Ireland + Ireland-remote). Deliberately
# NOT gated on specific tools/skills — these roles are transferable, so we cast
# wide and let the scoring rubric rank by fit rather than filtering at search.
SEARCH_QUERIES = [
    ["Technical Account Manager Ireland hiring 2026", "Solutions Engineer Ireland SaaS remote"],
    ["Customer Success Manager Ireland hiring", "Implementation Consultant SaaS Ireland"],
    ["Client Solutions Manager Ireland tech", "Technical Success Manager Ireland SaaS"],
    ["Customer Success Manager SaaS Ireland remote", "Technical Account Manager fintech Ireland"],
    ["Customer Engineer Ireland SaaS", "Onboarding Manager SaaS Ireland"],
    ["Technical Account Manager cybersecurity Ireland", "Solutions Consultant Ireland SaaS remote"],
    ["Technical Account Manager Ireland remote", "Enterprise Support Engineer Ireland SaaS"],
]

SYSTEM_PROMPT = (
    "You are a professional job-search assistant. "
    "Use web search to find current openings. "
    "Output ONLY a raw JSON array. Do not include any intro or outro text."
)


# ── Helpers ────────────────────────────────────────────────────────────────

def safe_request(client, **kwargs):
    """Retry wrapper for handling rate limits (429 errors)."""
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            if "429" in str(e):
                wait = 30 * (attempt + 1)
                print(f"[RATE LIMIT] Hit limit. Waiting {wait}s before retry {attempt+1}/5...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded for Anthropic API")


def build_user_prompt(job_count: int, query: str, excluded_fingerprints: set[str]) -> str:
    """
    Builds the per-query user prompt.

    Note: the static role/skills text now lives in build_cached_context()
    so it can be cache_control'd. This prompt only contains per-call variation.
    """
    exclusion_block = ""
    if excluded_fingerprints:
        # Cap at ~50 to keep tokens reasonable. Most recent are most likely
        # to recur, but the set is unordered — for now just slice arbitrarily.
        sample = list(excluded_fingerprints)[:50]
        formatted = "\n".join(f"- {fp}" for fp in sample)
        exclusion_block = (
            f"\n\nEXCLUDE these jobs from your results (they have already been "
            f"shown in previous digests). Format is `company|title`, lowercase:\n"
            f"{formatted}\n"
            f"If you cannot find {job_count} new openings beyond this list, "
            f"return fewer rather than including duplicates."
        )

    return (
        f"Find up to {job_count} current job openings matching the candidate's "
        f"target role TYPES anywhere in Ireland, including Ireland-based remote "
        f"and hybrid roles (not just Dublin).\n\n"
        f"Search focus for this query: \"{query}\"\n\n"
        f"SEARCH BROADLY ON ROLE, NOT ON TOOLS:\n"
        f"These roles (TAM, CSM, Solutions Consultant, etc.) are transferable — "
        f"the candidate is a strong fit for roles built on platforms or tools "
        f"they have never used, because the job is about managing technical "
        f"relationships and learning the stack. Do NOT filter out a role just "
        f"because it uses a different product or names skills the candidate "
        f"hasn't listed. Cast wide on role type.\n\n"
        f"CRITICAL RULES — read carefully:\n"
        f"1. Only include jobs you have ACTUALLY VERIFIED in your web search results. "
        f"Do not infer, guess, or fabricate jobs based on the candidate's profile.\n"
        f"2. The `url` field MUST be a direct deep-link to the specific job posting "
        f"(e.g. https://boards.greenhouse.io/company/jobs/123456 or "
        f"https://company.com/careers/job/the-specific-role). Do NOT return company "
        f"homepages, generic /careers pages, or search-result pages.\n"
        f"3. The `url` MUST come directly from your web search results — do not construct, "
        f"guess, or pattern-match URLs based on what they 'should' look like.\n"
        f"4. If you cannot find {job_count} real, verified openings, return FEWER. "
        f"Returning 2 real jobs is far better than 5 with fabricated entries. "
        f"It is acceptable to return an empty array [] if no real matches exist.\n\n"
        f"SCORING (match_score 0-100) — score TRANSFERABLE FIT FIRST:\n"
        f"- Base the majority of the score on overlap with the candidate's CORE "
        f"TRANSFERABLE STRENGTHS (escalation ownership, API/webhook/integration "
        f"troubleshooting, technical+executive communication, QBR/account reviews, "
        f"adoption/retention impact) and on seniority fit.\n"
        f"- Treat the candidate's FAMILIAR TOOLING as a BONUS only: nudge the score "
        f"up when a posting explicitly mentions those tools, but never penalise a "
        f"role for using a different stack.\n"
        f"- In `match_reasons`, lead with the transferable reasons. Mention specific "
        f"tool/skill overlap only where it genuinely exists in the posting.\n\n"
        f"Output a valid JSON array of objects. For each job include:\n"
        f"  rank, company, title, location, url, salary, "
        f"match_score (0-100), match_reasons (list of 3), "
        f"priority (HIGH/MEDIUM/LOW)\n\n"
        f"IMPORTANT: Return ONLY the JSON. No conversational text."
        f"{exclusion_block}"
    )


def build_cached_context() -> str:
    """
    Static profile/role context that gets cache_control'd.

    Pulled out as its own block so the prompt cache key is stable across
    runs within a 5-minute window.

    Framed transferable-FIRST: the core strengths drive scoring; specific
    tooling is a bonus signal only, never a filter. These roles (TAM, CSM,
    Solutions Consultant, etc.) are fundamentally about managing technical
    relationships and learning a platform — not about prior use of a specific
    tool — so the candidate is a strong fit for roles on stacks they have
    never touched.
    """
    roles_str = ", ".join(PROFILE["target_roles"])
    strengths_str = "\n".join(f"  - {s}" for s in PROFILE["core_strengths"])
    bonus_str = ", ".join(PROFILE["bonus_stack"])
    impact_str = "\n".join(f"  - {s}" for s in PROFILE["impact_highlights"])
    return (
        f"CANDIDATE PROFILE (use this to score and rank matches):\n"
        f"- Name: {PROFILE['name']}\n"
        f"- Based in: {PROFILE['based_in']} | Open to: {PROFILE['location']}\n"
        f"- Experience: {PROFILE['experience_years']} years in enterprise SaaS "
        f"technical-relationship roles\n"
        f"- Education: {PROFILE['education']}\n"
        f"- Target role types: {roles_str}\n\n"
        f"CORE TRANSFERABLE STRENGTHS (the basis for scoring — these travel to "
        f"ANY platform):\n{strengths_str}\n\n"
        f"PROVEN IMPACT:\n{impact_str}\n\n"
        f"FAMILIAR TOOLING (BONUS signal only — rank a role higher if it mentions "
        f"these, but NEVER require them; the candidate readily learns new stacks):\n"
        f"  {bonus_str}"
    )


# ── Core Logic ─────────────────────────────────────────────────────────────

def search_jobs():
    """Main search loop. Returns (jobs, run_metadata)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    excluded = sheet.read_seen_fingerprints(days=14)
    cached_context = build_cached_context()

    print(f"[{TODAY}] Starting search for {len(queries)} categories...")
    print(f"[INFO] Excluding {len(excluded)} previously-seen jobs.")

    all_jobs = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_write_tokens = 0
    total_web_searches = 0

    for i, query in enumerate(queries):
        print(f"[SEARCH {i+1}/{len(queries)}] Query: {query}")

        response = safe_request(
            client,
            model=MODEL_ID,
            max_tokens=2500,  # Lower than before — fewer fields per job
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                },
                {
                    "type": "text",
                    "text": cached_context,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[
                {
                    "role": "user",
                    "content": build_user_prompt(
                        job_count=5, query=query, excluded_fingerprints=excluded
                    ),
                },
                # NOTE: do NOT prefill an assistant turn here. With a server-side
                # web_search tool, ending on a prefilled assistant message
                # suppresses the tool-use loop — the model completes the JSON
                # from context instead of actually searching (observed as
                # searches=0 with fabricated results). Let the model run search,
                # then parse its final text block.
            ],
        )

        # Token accounting
        usage = response.usage
        total_input_tokens += getattr(usage, "input_tokens", 0)
        total_output_tokens += getattr(usage, "output_tokens", 0)
        total_cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        total_cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        # Web search count — billed at $0.01 per search, separate from tokens.
        # Located at usage.server_tool_use.web_search_requests.
        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use is not None:
            total_web_searches += getattr(server_tool_use, "web_search_requests", 0) or 0

        # With web_search active, response.content interleaves tool_use /
        # tool_result / text blocks. The JSON answer is the FINAL text block;
        # earlier blocks may be search narration. Parse the last one.
        text_blocks = [
            block.text for block in response.content
            if getattr(block, "type", "") == "text" and getattr(block, "text", "").strip()
        ]
        full_text = text_blocks[-1] if text_blocks else ""

        jobs = parse_jobs(full_text)

        # Safety net: drop anything matching the seen set even if Claude ignored
        # the EXCLUDE block in the prompt.
        before = len(jobs)
        jobs = [j for j in jobs if sheet.fingerprint(j.get("company", ""), j.get("title", "")) not in excluded]
        if before != len(jobs):
            print(f"[INFO] Dedup filter dropped {before - len(jobs)} duplicate(s).")

        # Score floor: drop marginal-fit roles so they don't pad the digest.
        if MIN_MATCH_SCORE > 0:
            before = len(jobs)
            kept = []
            for j in jobs:
                try:
                    s = int(j.get("match_score", 0))
                except (ValueError, TypeError):
                    s = 0
                if s >= MIN_MATCH_SCORE:
                    kept.append(j)
            jobs = kept
            if before != len(jobs):
                print(f"[INFO] Score floor (<{MIN_MATCH_SCORE}) dropped {before - len(jobs)} job(s).")

        all_jobs.extend(jobs)

        # Add freshly-seen fingerprints to the local set so the next query in
        # this same run also excludes them (prevents within-run duplicates).
        for j in jobs:
            excluded.add(sheet.fingerprint(j.get("company", ""), j.get("title", "")))

        if i < len(queries) - 1:
            print("[INFO] Waiting 20s to prevent rate limit...")
            time.sleep(20)

    cost_usd = (
        total_input_tokens / 1_000_000 * PRICE_INPUT_PER_M
        + total_output_tokens / 1_000_000 * PRICE_OUTPUT_PER_M
        + total_cache_read_tokens / 1_000_000 * PRICE_CACHE_READ_PER_M
        + total_cache_write_tokens / 1_000_000 * PRICE_CACHE_WRITE_PER_M
        + total_web_searches * PRICE_WEB_SEARCH_PER_QUERY
    )
    cost_eur = cost_usd * USD_TO_EUR

    metadata = {
        "model": MODEL_ID,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "cache_read_tokens": total_cache_read_tokens,
        "cache_write_tokens": total_cache_write_tokens,
        "web_searches": total_web_searches,
        "cost_eur": cost_eur,
    }

    print(
        f"[USAGE] in={total_input_tokens} out={total_output_tokens} "
        f"cache_r={total_cache_read_tokens} cache_w={total_cache_write_tokens} "
        f"searches={total_web_searches} cost=€{cost_eur:.4f}"
    )

    return all_jobs, metadata


def parse_jobs(raw_text):
    """Robust JSON parser that isolates the array and handles extra text."""
    try:
        text = raw_text.strip()
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```", "", text)

        start_idx = text.find("[")
        if start_idx == -1:
            print("[ERROR] No opening bracket found in response.")
            return []

        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(text[start_idx:])

        if isinstance(parsed, dict):
            parsed = [parsed]

        cleaned = []
        for job in parsed:
            if not isinstance(job, dict):
                continue

            job.setdefault("match_score", 0)
            job.setdefault("company", "N/A")
            job.setdefault("title", "Job Opportunity")
            job.setdefault("url", "#")

            try:
                s = int(job.get("match_score", 0))
            except (ValueError, TypeError):
                s = 0
            job["priority"] = "HIGH" if s >= 85 else "MEDIUM" if s >= 75 else "LOW"

            cleaned.append(job)

        print(f"[SUCCESS] Parsed {len(cleaned)} jobs.")
        return cleaned

    except Exception as e:
        print(f"[ERROR] Parsing failed: {e}")
        return []


# ── Email ──────────────────────────────────────────────────────────────────

def build_email_html(jobs):
    def score_color(score):
        try:
            s = int(score)
            if s >= 85:
                return "#16a34a"
            if s >= 75:
                return "#ca8a04"
            return "#dc2626"
        except (ValueError, TypeError):
            return "#6b7280"

    job_cards = ""
    for job in jobs:
        score = job.get("match_score", 0)
        unconfirmed_badge = ""
        if job.get("verification_status") == "unconfirmed":
            unconfirmed_badge = (
                '<span style="margin-left:10px;background:#fef3c7;color:#92400e;'
                'padding:2px 8px;border-radius:10px;font-weight:600;">'
                '&#9888; Unconfirmed</span>'
            )
        job_cards += f"""
        <tr>
          <td style="padding:20px;border-bottom:1px solid #e5e7eb;">
            <div style="font-size:12px;color:#6b7280;margin-bottom:5px;">
                <span style="background:#f3f4f6;color:#1f2937;padding:2px 8px;border-radius:10px;font-weight:600;">{job.get('priority')}</span>
                <span style="margin-left:10px;">Match: <strong style="color:{score_color(score)}">{score}%</strong></span>
                {unconfirmed_badge}
            </div>
            <div style="font-size:18px;font-weight:700;color:#111827;">{job.get('title')}</div>
            <div style="font-size:15px;color:#4b5563;margin-bottom:10px;">{job.get('company')} — {job.get('location')}</div>
            <div style="font-size:13px;color:#374151;"><strong>Why:</strong> {", ".join(job.get('match_reasons', ['N/A']))}</div>
            <div style="margin-top:12px;">
                <a href="{job.get('url', '#')}" style="background:#2563eb;color:#ffffff;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">Apply on Company Site</a>
            </div>
          </td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:sans-serif;background:#f9fafb;padding:20px;">
      <table width="100%" style="max-width:600px;background:#ffffff;border-radius:12px;margin:auto;border:1px solid #e5e7eb;overflow:hidden;border-collapse:collapse;">
        <tr><td style="background:#1e3a5f;padding:30px;color:#ffffff;text-align:center;">
            <h1 style="margin:0;font-size:24px;">Daily Job Digest</h1>
            <p style="margin:5px 0 0 0;font-size:14px;opacity:0.8;">{TODAY}</p>
        </td></tr>
        {job_cards}
        <tr><td style="background:#f9fafb;padding:15px;text-align:center;font-size:11px;color:#9ca3af;">
            Generated for {PROFILE['name']} | Dublin, Ireland
        </td></tr>
      </table>
    </body>
    </html>"""


def send_email(html_body, job_count):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 {job_count} Job Matches found for {TODAY}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    print(f"[SUCCESS] Email sent to {RECIPIENT_EMAIL}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    try:
        jobs, metadata = search_jobs()

        if not jobs:
            print("[WARN] No new jobs found today.")
            return

        # Verification step. keep_unsure=True: jobs the verifier couldn't
        # fetch+confirm (anti-bot, timeout, ambiguous) are KEPT but tagged
        # "unconfirmed" so the email badges them visibly. Confidently
        # closed/expired/404 jobs are still dropped. An empty inbox (strict mode)
        # is worse than a few flagged-uncertain roles, given thin search yield.
        jobs, verify_meta = verify.filter_verified(jobs, keep_unsure=True)

        if not jobs:
            print("[WARN] All jobs failed verification — nothing to email.")
            return

        # Roll verifier token usage into the run cost.
        v_input = verify_meta.get("input_tokens", 0)
        v_output = verify_meta.get("output_tokens", 0)
        verify_cost_usd = (
            v_input / 1_000_000 * PRICE_INPUT_PER_M
            + v_output / 1_000_000 * PRICE_OUTPUT_PER_M
        )
        metadata["input_tokens"] += v_input
        metadata["output_tokens"] += v_output
        metadata["cost_eur"] += verify_cost_usd * USD_TO_EUR
        metadata["jobs_verified_real"] = verify_meta.get("kept", 0)
        metadata["jobs_verified_fake"] = verify_meta.get("fake_count", 0)
        metadata["jobs_verified_unsure"] = verify_meta.get("unsure", 0)

        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        html = build_email_html(jobs)
        send_email(html, len(jobs))

        # Sheet writes happen after email — if Sheets fails, you still got the
        # email. Both calls degrade to no-ops if creds are missing.
        sheet.append_seen_jobs(jobs)
        sheet.append_daily_log(jobs, metadata)

        print(f"Workflow finished successfully. Sent {len(jobs)} verified jobs.")

    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        raise


if __name__ == "__main__":
    main()
