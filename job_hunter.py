"""
Daily Job Hunter — Automated job search using Claude API + Web Search
Finds 10 TAM/adjacent roles in Ireland, scores them, and emails results.
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

# ── Configuration ──────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", GMAIL_ADDRESS)

TODAY = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

# ── Profile ────────────────────────────────────────────────────────────────
PROFILE = {
    "name": "Sumit Mishra",
    "location": "Dublin, Ireland",
    "target_roles": [
        "Technical Account Manager",
        "Solutions Engineer",
        "Solutions Consultant",
        "Customer Success Manager",
        "Enterprise Support Engineer",
        "Client Solutions Manager",
        "Implementation Consultant",
    ],
    "experience_years": 3,
    "key_skills": [
        "Freshservice", "Freshdesk", "ITSM", "ITIL",
        "Python", "SQL", "REST APIs", "Webhooks",
        "SaaS", "Workflow Automation", "Power BI",
        "Enterprise Account Management", "QBR Delivery",
        "Stakeholder Management", "Onboarding",
    ],
    "education": "MSc Business Analytics, University of Galway",
}

# ── Search queries ─────────────────────────────────────────────────────────
SEARCH_QUERIES = [
    ["Technical Account Manager Ireland hiring 2026", "Solutions Engineer Dublin SaaS"],
    ["Customer Success Manager Dublin Ireland hiring", "Implementation Consultant SaaS Ireland"],
    ["Client Solutions Manager Ireland tech", "Solutions Consultant Dublin Ireland SaaS"],
    ["Customer Success Manager SaaS Dublin", "Technical Account Manager fintech Ireland"],
    ["TAM role Ireland SaaS enterprise", "Pre-Sales Engineer Dublin Ireland"],
    ["Technical Account Manager cybersecurity Ireland", "Partner Success Manager Dublin tech"],
    ["Technical Account Manager Ireland remote", "IT Service Management Consultant Ireland"],
]

# ── Helpers ────────────────────────────────────────────────────────────────

def safe_request(client, **kwargs):
    """Retry wrapper for handling rate limits"""
    for attempt in range(5):
        try:
            return client.messages.create(**kwargs)
        except Exception as e:
            if "429" in str(e):
                wait = 20 * (attempt + 1)
                print(f"[RATE LIMIT] Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded")


def build_prompt(job_count=2):
    roles_str = ", ".join(PROFILE["target_roles"])
    skills_str = ", ".join(PROFILE["key_skills"])

    return f"""Find {job_count} current {roles_str} job openings in {PROFILE['location']}.

Candidate: {PROFILE['experience_years']} years SaaS experience. Skills: {skills_str}.

Return ONLY valid JSON:
- No explanation
- No markdown
- Must start with [ and end with ]

Each job must include:
rank, company, title, location, url, salary, match_score,
match_reasons (3), cv_keywords (3), outreach_message, cover_letter_hook, priority.
"""


# ── Core Logic ─────────────────────────────────────────────────────────────

def search_jobs():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    model_id = "claude-sonnet-4-20250514"

    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting search...")

    all_jobs = []

    for i, query in enumerate(queries):
        print(f"[SEARCH {i+1}] {query}")

        response = safe_request(
            client,
            model=model_id,
            max_tokens=1200,
            system=(
                "You are a professional job-search assistant. "
                "Use web search. Return ONLY raw JSON array."
            ),
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": build_prompt(job_count=2) + f"\nSearch query: {query}"
                }
            ],
        )

        # Extract only text blocks
        full_text = "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        )

        jobs = parse_jobs(full_text)
        all_jobs.extend(jobs)

        # Prevent hitting TPM limits
        time.sleep(20)

    return all_jobs[:10]


def parse_jobs(raw_text):
    """Robust JSON parser"""
    try:
        text = raw_text.strip()
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```", "", text)

        start_candidates = [i for i in [text.find("["), text.find("{")] if i != -1]
        if not start_candidates:
            raise ValueError("No JSON start found")

        start_idx = min(start_candidates)

        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(text[start_idx:])

        if isinstance(parsed, dict):
            parsed = [parsed]

        if not isinstance(parsed, list):
            return []

        cleaned = []
        for job in parsed:
            if not isinstance(job, dict):
                continue

            job.setdefault("match_score", 0)
            job.setdefault("company", "Unknown")
            job.setdefault("title", "Job Link")

            try:
                s = int(job.get("match_score", 0))
            except:
                s = 0

            job["priority"] = (
                "HIGH" if s >= 85 else "MEDIUM" if s >= 75 else "LOW"
            )

            cleaned.append(job)

        print(f"[SUCCESS] Parsed {len(cleaned)} jobs.")
        return cleaned

    except Exception as e:
        print(f"[ERROR] Parsing failed: {e}")
        print(f"[DEBUG] Raw tail: ...{raw_text[-200:]}")
        return []


# ── Email ──────────────────────────────────────────────────────────────────

def build_email_html(jobs):
    def score_color(score):
        try:
            s = int(score)
            if s >= 85: return "#16a34a"
            if s >= 75: return "#ca8a04"
            return "#dc2626"
        except:
            return "#6b7280"

    job_cards = ""
    for job in jobs:
        score = job.get("match_score", 0)
        job_cards += f"""
        <tr>
          <td style="padding:20px;border-bottom:1px solid #e5e7eb;">
            <div style="font-size:12px;color:#6b7280;margin-bottom:5px;">
                <span style="background:#f3f4f6;padding:2px 8px;border-radius:10px;font-weight:600;">{job.get('priority')}</span>
                <span style="margin-left:10px;">Match: <strong style="color:{score_color(score)}">{score}%</strong></span>
            </div>
            <div style="font-size:18px;font-weight:700;color:#111827;">{job.get('title')}</div>
            <div style="font-size:15px;color:#4b5563;margin-bottom:10px;">{job.get('company')} — {job.get('location')}</div>
            <div style="font-size:13px;color:#374151;"><strong>Match:</strong> {", ".join(job.get('match_reasons', []))}</div>
            <div style="margin-top:12px;">
                <a href="{job.get('url', '#')}" style="background:#2563eb;color:#ffffff;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">Apply Now</a>
            </div>
          </td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:sans-serif;background:#f9fafb;padding:20px;">
      <table width="100%" style="max-width:600px;background:#ffffff;border-radius:12px;margin:auto;border:1px solid #e5e7eb;">
        <tr><td style="background:#1e3a5f;padding:30px;color:#ffffff;text-align:center;">
            <h1 style="margin:0;">Daily Job Digest</h1>
            <p style="margin:5px 0;">{TODAY}</p>
        </td></tr>
        {job_cards}
      </table>
    </body>
    </html>
    """


def send_email(html_body, job_count):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 {job_count} Job Matches for {TODAY}"
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
        jobs = search_jobs()

        if not jobs:
            print("[WARN] No jobs found.")
            return

        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        html = build_email_html(jobs)
        send_email(html, len(jobs))

        print("Done.")

    except Exception as e:
        print(f"[FATAL] {e}")
        raise


if __name__ == "__main__":
    main()
