"""
Daily Job Hunter — Automated job search using Claude API + Web Search
Finds 10 TAM/adjacent roles in Ireland, scores them, and emails results.
"""

import anthropic
import smtplib
import json
import os
import re
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

def build_prompt():
    roles_str = ", ".join(PROFILE["target_roles"])
    skills_str = ", ".join(PROFILE["key_skills"])

    return f"""Find 10 current {roles_str} job openings in {PROFILE['location']}. 
Candidate: {PROFILE['name']}, {PROFILE['experience_years']} years at Freshworks, {PROFILE['education']}.
Key skills: {skills_str}.

Output exactly 10 jobs in a JSON array. For each:
- rank (int), company, title, location, url, salary, match_score (0-100)
- match_reasons (list of 3 strings)
- cv_keywords (list of 3 strings)
- outreach_message (50 words)
- cover_letter_hook (2 sentences)
- priority (HIGH/MEDIUM/LOW)

If data is missing, use "N/A"."""

def search_jobs():
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting search: {queries}")

    # As requested: claude-sonnet-4-20250514
    model_id = "claude-sonnet-4-20250514" 

    system_prompt = (
        "You are a professional job-search assistant. "
        "Search the web and return 10 jobs as a JSON array. "
        "Do not include any intro, outro, or conversational text. "
        "Output ONLY the raw JSON array."
    )

    response = client.messages.create(
        model=model_id,
        max_tokens=8000,
        system=system_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {"role": "user", "content": build_prompt()},
            {"role": "assistant", "content": "["} 
        ],
    )

    # Combine pre-filled bracket with text
    full_text = "[" 
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    return full_text

def parse_jobs(raw_text):
    """
    Robust parser using raw_decode to stop exactly when the JSON ends, 
    ignoring any 'Extra data' Claude might have added.
    """
    try:
        # 1. Basic cleanup
        text = raw_text.strip()
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*", "", text)

        # 2. Find the first bracket
        start_idx = text.find("[")
        if start_idx == -1:
            raise ValueError("No opening bracket found")
        
        # 3. Use JSONDecoder to find the valid JSON object/array
        # This will ignore any text that comes AFTER the closing bracket
        decoder = json.JSONDecoder()
        jobs, index = decoder.raw_decode(text[start_idx:])
        
        if not isinstance(jobs, list):
            return []

        # 4. Normalize data
        for job in jobs:
            if "match_score" not in job: job["match_score"] = 0
            if "company" not in job: job["company"] = "Unknown"
            if "title" not in job: job["title"] = "Job Link"
            if "priority" not in job:
                s = int(job.get("match_score", 0))
                job["priority"] = "HIGH" if s >= 85 else "MEDIUM" if s >= 75 else "LOW"
            
        print(f"[SUCCESS] Parsed {len(jobs)} jobs.")
        return jobs

    except Exception as e:
        print(f"[ERROR] Parsing failed: {e}")
        # Print a snippet of the end of the text to see what caused the "Extra data"
        print(f"[DEBUG] End of response: ...{raw_text[-100:]}")
        return []

def build_email_html(jobs):
    def score_color(score):
        try:
            s = int(score)
            if s >= 85: return "#16a34a"
            if s >= 75: return "#ca8a04"
            return "#dc2626"
        except: return "#6b7280"

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
      <table width="100%" max-width="600px" style="background:#ffffff;border-radius:12px;margin:auto;border:1px solid #e5e7eb;overflow:hidden;">
        <tr><td style="background:#1e3a5f;padding:30px;color:#ffffff;text-align:center;">
            <h1 style="margin:0;font-size:22px;">Daily Job Digest</h1>
            <p style="margin:5px 0 0 0;font-size:14px;opacity:0.8;">{TODAY}</p>
        </td></tr>
        {job_cards}
      </table>
    </body>
    </html>"""

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

def main():
    try:
        raw_response = search_jobs()
        jobs = parse_jobs(raw_response)

        if not jobs:
            print("[WARN] No jobs parsed.")
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
