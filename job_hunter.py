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
    """Retry wrapper for handling rate limits (429 errors)"""
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

def build_prompt(job_count=5):
    """Generates the prompt for Claude."""
    roles_str = ", ".join(PROFILE["target_roles"])
    skills_str = ", ".join(PROFILE["key_skills"])

    return f"""Find exactly {job_count} current {roles_str} job openings in {PROFILE['location']}.

Candidate: {PROFILE['name']}, {PROFILE['experience_years']} years SaaS experience. 
Skills: {skills_str}.

Output a valid JSON array of objects. For each job include:
rank, company, title, location, url, salary, match_score (0-100),
match_reasons (list of 3), cv_keywords (list of 3), outreach_message, cover_letter_hook, priority (HIGH/MEDIUM/LOW).

IMPORTANT: Return ONLY the JSON. No conversational text."""

# ── Core Logic ─────────────────────────────────────────────────────────────

def search_jobs():
    """Main search loop to aggregate 10 jobs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    model_id = "claude-sonnet-4-20250514"

    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting search for {len(queries)} categories...")

    all_jobs = []

    for i, query in enumerate(queries):
        print(f"[SEARCH {i+1}/{len(queries)}] Query: {query}")

        # We ask for 5 jobs per query. With 2 queries, we get 10 total.
        response = safe_request(
            client,
            model=model_id,
            max_tokens=3500, # Sufficient for 5 detailed JSON objects
            system=(
                "You are a professional job-search assistant. "
                "Use web search to find current openings. "
                "Output ONLY a raw JSON array. Do not include any intro or outro text."
            ),
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[
                {
                    "role": "user",
                    "content": build_prompt(job_count=5) + f"\nContext: Focus search on '{query}'"
                },
                {
                    "role": "assistant",
                    "content": "["  # Pre-fill to force JSON start
                }
            ],
        )

        # Reconstruct the response text starting with our forced bracket
        full_text = "[" + "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        )

        jobs = parse_jobs(full_text)
        all_jobs.extend(jobs)

        # Cooling off period between searches to stay under Rate Limits
        if i < len(queries) - 1:
            print("[INFO] Waiting 20s to prevent rate limit...")
            time.sleep(20)

    return all_jobs

def parse_jobs(raw_text):
    """Robust JSON parser that isolates the array and handles extra text."""
    try:
        text = raw_text.strip()
        # Remove markdown code blocks if Claude adds them despite instructions
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```", "", text)

        start_idx = text.find("[")
        if start_idx == -1:
            print("[ERROR] No opening bracket found in response.")
            return []

        # Decoder.raw_decode reads the JSON and stops exactly where the JSON ends,
        # ignoring any "Extra data" (conversational text) Claude might have added.
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(text[start_idx:])

        if isinstance(parsed, dict):
            parsed = [parsed]

        cleaned = []
        for job in parsed:
            if not isinstance(job, dict): continue
            
            # Ensure keys exist for the email template
            job.setdefault("match_score", 0)
            job.setdefault("company", "N/A")
            job.setdefault("title", "Job Opportunity")
            job.setdefault("url", "#")
            
            # Auto-assign priority if missing
            try:
                s = int(job.get("match_score", 0))
            except:
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
            if s >= 85: return "#16a34a" # Green
            if s >= 75: return "#ca8a04" # Orange
            return "#dc2626" # Red
        except: return "#6b7280"

    job_cards = ""
    for job in jobs:
        score = job.get("match_score", 0)
        job_cards += f"""
        <tr>
          <td style="padding:20px;border-bottom:1px solid #e5e7eb;">
            <div style="font-size:12px;color:#6b7280;margin-bottom:5px;">
                <span style="background:#f3f4f6;color:#1f2937;padding:2px 8px;border-radius:10px;font-weight:600;">{job.get('priority')}</span>
                <span style="margin-left:10px;">Match: <strong style="color:{score_color(score)}">{score}%</strong></span>
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
        # Step 1: Search and aggregate
        jobs = search_jobs()

        if not jobs:
            print("[WARN] No jobs found today.")
            return

        # Step 2: Sort by match score
        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        # Step 3: Build and send
        html = build_email_html(jobs)
        send_email(html, len(jobs))

        print(f"Workflow finished successfully. Found {len(jobs)} jobs.")

    except Exception as e:
        print(f"[FATAL ERROR] {e}")
        # Optional: Send a failure email here if you wish
        raise

if __name__ == "__main__":
    main()
