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

# ── Search queries to rotate ───────────────────────────────────────────────
SEARCH_QUERIES = [
    ["Technical Account Manager Ireland hiring 2025", "Solutions Engineer Dublin SaaS"],
    ["Customer Success Manager Dublin Ireland hiring", "Implementation Consultant SaaS Ireland"],
    ["Client Solutions Manager Ireland tech", "Solutions Consultant Dublin Ireland SaaS"],
    ["Customer Success Manager SaaS Dublin", "Technical Account Manager fintech Ireland"],
    ["TAM role Ireland SaaS enterprise", "Pre-Sales Engineer Dublin Ireland"],
    ["Technical Account Manager cybersecurity Ireland", "Partner Success Manager Dublin tech"],
    ["Technical Account Manager Ireland remote", "IT Service Management Consultant Ireland"],
]

def build_prompt():
    """Build the job search prompt."""
    roles_str = ", ".join(PROFILE["target_roles"])
    skills_str = ", ".join(PROFILE["key_skills"])

    return f"""Find 10 current {roles_str} job openings in {PROFILE['location']} (open to remote/hybrid). 
Candidate has {PROFILE['experience_years']} years experience at Freshworks and an {PROFILE['education']}.
Key skills to match: {skills_str}.

For each job found, provide:
- Company, Title, Location, and URL.
- A match_score (0-100).
- 3 specific match_reasons.
- 3 CV keywords to add.
- A 50-word outreach message for a recruiter.
- A 2-sentence cover letter hook.

If specific details like salary or a recruiter name are not explicitly found, use "N/A"."""

def search_jobs():
    """Call Claude API with web search to find jobs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting job search with queries: {queries}")

    system_prompt = (
        "You are a professional job-search assistant. "
        "Your goal is to provide a valid JSON array of job matches based on web search results. "
        "Do not provide any introductory text, copyright warnings, or explanations. "
        "Output ONLY the JSON array."
    )

    # Note: We use the 'assistant' pre-fill strategy here
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=8000,
        system=system_prompt,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[
            {"role": "user", "content": build_prompt()},
            {"role": "assistant", "content": "["} # Force-starts the JSON list
        ],
    )

    # We manually add back the '[' that we used for pre-filling
    full_text = "[" 
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    return full_text

def parse_jobs(raw_text):
    """Parse the JSON response from Claude."""
    cleaned = raw_text.strip()
    
    # Handle the case where pre-filling might result in [[ or other artifacts
    if cleaned.startswith("[["): 
        cleaned = cleaned[1:]
    
    # Locate the outermost [ ] brackets
    start = cleaned.find("[")
    end = cleaned.rfind("]") + 1

    if start == -1 or end == 0:
        print("[ERROR] No JSON array found")
        return []

    json_str = cleaned[start:end]

    try:
        jobs = json.loads(json_str)
        # Ensure all required keys exist to prevent email template crashes
        required_keys = ["match_score", "company", "title", "priority"]
        for job in jobs:
            for key in required_keys:
                if key not in job: job[key] = "N/A"
            # Auto-assign priority if missing
            if job["priority"] == "N/A":
                score = job.get("match_score", 0)
                job["priority"] = "HIGH" if score >= 85 else "MEDIUM" if score >= 75 else "LOW"
        
        return jobs
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse failed: {e}")
        return []

def build_email_html(jobs):
    """Build a clean HTML email with job results."""
    def score_color(score):
        try:
            s = int(score)
            if s >= 85: return "#16a34a"
            if s >= 75: return "#ca8a04"
            return "#dc2626"
        except: return "#6b7280"

    def priority_badge(priority):
        colors = {
            "HIGH": ("#dcfce7", "#16a34a"),
            "MEDIUM": ("#fef9c3", "#ca8a04"),
            "LOW": ("#fee2e2", "#dc2626"),
        }
        bg, fg = colors.get(str(priority).upper(), ("#f3f4f6", "#6b7280"))
        return f'<span style="background:{bg};color:{fg};padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;">{priority}</span>'

    job_cards = ""
    for job in jobs:
        score = job.get("match_score", 0)
        job_cards += f"""
        <tr>
          <td style="padding:16px 20px;border-bottom:1px solid #e5e7eb;">
            <div style="font-size:11px;color:#6b7280;margin-bottom:4px;">
                {priority_badge(job.get('priority', 'LOW'))} 
                <span style="margin-left:8px;">Match: <strong style="color:{score_color(score)}">{score}%</strong></span>
            </div>
            <div style="font-size:17px;font-weight:700;color:#111827;margin:4px 0;">{job.get('title')}</div>
            <div style="font-size:14px;color:#4b5563;">{job.get('company')} — {job.get('location')}</div>
            <div style="font-size:13px;color:#374151;margin-top:8px;"><strong>Why:</strong> {", ".join(job.get('match_reasons', []))}</div>
            <div style="font-size:13px;color:#374151;margin-top:4px;"><strong>Hook:</strong> <em>"{job.get('cover_letter_hook', 'N/A')}"</em></div>
            <div style="margin-top:12px;">
                <a href="{job.get('url', '#')}" style="background:#2563eb;color:#ffffff;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:600;">Apply Now</a>
            </div>
          </td>
        </tr>"""

    return f"""
    <html>
    <body style="font-family:sans-serif;background:#f9fafb;padding:20px;">
      <table width="100%" max-width="600px" style="background:#ffffff;border-radius:8px;margin:auto;border:1px solid #e5e7eb;">
        <tr><td style="background:#1e3a5f;padding:20px;color:#ffffff;text-align:center;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">Daily Job Digest</h2>
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

def send_error_email(error_msg):
    msg = MIMEMultipart()
    msg["Subject"] = f"⚠️ Job Hunter Error — {TODAY}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(f"Error: {error_msg}", "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    except: pass

def main():
    try:
        raw_response = search_jobs()
        jobs = parse_jobs(raw_response)

        if not jobs:
            print("[WARN] No jobs found or parsing failed.")
            send_error_email("No jobs parsed. Check GitHub logs for raw response.")
            return

        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)
        html = build_email_html(jobs)
        send_email(html, len(jobs))
        
        print(f"DONE. Found {len(jobs)} jobs.")

    except Exception as e:
        print(f"[FATAL] {e}")
        send_error_email(str(e))
        raise

if __name__ == "__main__":
    main()
