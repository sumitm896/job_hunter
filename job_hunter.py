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

# ── Profile (edit these to update your search) ─────────────────────────────
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
    "platforms": ["LinkedIn", "Indeed Ireland", "IrishJobs.ie", "Jobs.ie"],
}

# ── Search queries to rotate (script picks based on day of week) ───────────
SEARCH_QUERIES = [
    [
        "Technical Account Manager Ireland 2025 2026 hiring",
        "Solutions Engineer Dublin SaaS jobs",
        "Customer Success Manager Dublin Ireland hiring",
    ],
    [
        "Technical Account Manager Dublin remote hybrid",
        "Implementation Consultant SaaS Ireland",
        "Enterprise Support Engineer Ireland jobs",
    ],
    [
        "Client Solutions Manager Ireland tech",
        "Solutions Consultant Dublin Ireland SaaS",
        "Technical Account Manager ITSM Ireland",
    ],
    [
        "Customer Success Manager SaaS Dublin hiring",
        "Technical Account Manager fintech Ireland",
        "Solutions Engineer cloud Ireland jobs",
    ],
    [
        "TAM role Ireland SaaS enterprise",
        "Onboarding Manager SaaS Dublin Ireland",
        "Pre-Sales Engineer Dublin Ireland",
    ],
    [
        "Technical Account Manager cybersecurity Ireland",
        "Partner Success Manager Dublin tech",
        "Platform Specialist SaaS Ireland jobs",
    ],
    [
        "Technical Account Manager Ireland remote",
        "Digital Transformation Consultant Dublin",
        "IT Service Management Consultant Ireland",
    ],
]


def build_prompt():
    """Build the job search prompt for Claude."""
    roles_str = ", ".join(PROFILE["target_roles"])
    skills_str = ", ".join(PROFILE["key_skills"])

    return f"""Find 10 current {roles_str} job openings in {PROFILE['location']} (open to remote/hybrid). 
Candidate has {PROFILE['experience_years']} years experience at Freshworks and an {PROFILE['education']}.
Key skills to match: {skills_str}.

For each job found, provide:
- rank (number)
- company (string)
- title (string)
- location (string)
- url (string)
- salary (string, default 'Not listed')
- match_score (number between 0 and 100)
- match_reasons (list of strings)
- missing_skills (list of strings)
- priority (HIGH or MEDIUM or LOW)
- cv_keywords (list of strings)
- outreach_name (string, default 'N/A')
- outreach_title (string, default 'N/A')
- outreach_linkedin_search (string)
- outreach_message (string)
- cover_letter_hook (string)

If specific details like salary or a recruiter name are not explicitly found, use "Not listed" or "N/A" rather than refusing the prompt. 
Do not write copyright or disclaimer messages; output only the best available data."""


def search_jobs():
    """Call Claude API with web search to find jobs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting job search...")
    print(f"[INFO] Using query set {day_index % len(SEARCH_QUERIES)}: {queries}")

    system_prompt = (
        "You are a job-search data extractor. Search the web and return 10 jobs. "
        "Your output must be a valid JSON array of objects. Do not include any text, "
        "explanations, or conversational filler. Only the JSON array data."
    )

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        system=system_prompt,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
            }
        ],
        messages=[
            {
                "role": "user",
                "content": build_prompt(),
            },
            {
                "role": "assistant",
                "content": "["  # Pre-fill to force JSON output
            }
        ],
    )

    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    return "[" + full_text


def parse_jobs(raw_text):
    """
    Robust JSON parser that finds the array even if Claude 
    adds conversational text inside the brackets.
    """
    try:
        text = raw_text.strip()
        text = re.sub(r"```json\s*", "", text)
        text = re.sub(r"```\s*", "", text)

        # Find the FIRST '[' and the LAST ']'
        start_idx = text.find("[")
        end_idx = text.rfind("]") + 1
        
        if start_idx == -1 or end_idx == 0:
            raise ValueError("No brackets found")

        json_snippet = text[start_idx:end_idx]

        # In case Claude outputs: [ Here are the jobs I found: { ... } ]
        first_obj = json_snippet.find("{")
        if first_obj > 1:
            json_snippet = "[" + json_snippet[first_obj:]

        jobs = json.loads(json_snippet)
        
        if not isinstance(jobs, list):
            return []

        # Data normalization to prevent crashes in the email template
        for job in jobs:
            if "match_score" not in job: job["match_score"] = 0
            if "company" not in job: job["company"] = "Unknown"
            if "title" not in job: job["title"] = "Job Opportunity"
            if "priority" not in job:
                score = job.get("match_score", 0)
                if score >= 85: job["priority"] = "HIGH"
                elif score >= 75: job["priority"] = "MEDIUM"
                else: job["priority"] = "LOW"
            
        print(f"[SUCCESS] Parsed {len(jobs)} jobs")
        return jobs

    except Exception as e:
        print(f"[ERROR] Parsing failed: {e}")
        print(f"[DEBUG] Problematic text sample: {raw_text[:200]}")
        return []


def build_email_html(jobs):
    """Build a clean HTML email with job results."""
    high = [j for j in jobs if j.get("priority") == "HIGH"]
    medium = [j for j in jobs if j.get("priority") == "MEDIUM"]
    low = [j for j in jobs if j.get("priority") == "LOW"]

    def score_color(score):
        try:
            s = int(score)
            if s >= 85: return "#16a34a"  # green
            elif s >= 75: return "#ca8a04"  # amber
            else: return "#dc2626"  # red
        except:
            return "#6b7280"

    def priority_badge(priority):
        colors = {
            "HIGH": ("#dcfce7", "#16a34a"),
            "MEDIUM": ("#fef9c3", "#ca8a04"),
            "LOW": ("#fee2e2", "#dc2626"),
        }
        bg, fg = colors.get(str(priority).upper(), ("#f3f4f6", "#6b7280"))
        return f'<span style="background:{bg};color:{fg};padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;">{priority}</span>'

    def job_card(job):
        score = job.get("match_score", 0)
        return f"""
        <tr>
          <td style="padding:16px 20px;border-bottom:1px solid #e5e7eb;">
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td>
                  <div style="font-size:11px;color:#6b7280;margin-bottom:4px;">
                    {priority_badge(job.get('priority', 'LOW'))}
                    <span style="margin-left:8px;">Match Score: <strong style="color:{score_color(score)}">{score}%</strong></span>
                  </div>
                  <div style="font-size:17px;font-weight:700;color:#111827;margin:6px 0 2px 0;">
                    {job.get('title', 'Unknown')}
                  </div>
                  <div style="font-size:14px;color:#4b5563;margin-bottom:4px;">
                    {job.get('company', 'Unknown')} &mdash; {job.get('location', '')}
                  </div>
                  <div style="font-size:13px;color:#6b7280;margin-bottom:8px;">
                    Salary: {job.get('salary', 'Not listed')}
                  </div>
                  <div style="font-size:13px;color:#374151;margin-bottom:8px;">
                    <strong>Why it matches:</strong> {', '.join(job.get('match_reasons', [])) if isinstance(job.get('match_reasons'), list) else job.get('match_reasons', 'N/A')}
                  </div>
                  <div style="font-size:13px;color:#374151;margin-bottom:8px;">
                    <strong>CV Keywords to add:</strong> {', '.join(job.get('cv_keywords', [])) if isinstance(job.get('cv_keywords'), list) else job.get('cv_keywords', 'N/A')}
                  </div>
                  <div style="font-size:13px;color:#374151;margin-bottom:8px;">
                    <strong>Cover letter hook:</strong> <em>{job.get('cover_letter_hook', '')}</em>
                  </div>
                  <div style="font-size:13px;color:#374151;margin-bottom:10px;">
                    <strong>Reach out to:</strong> {job.get('outreach_name', 'N/A')} ({job.get('outreach_title', '')})
                    &mdash; <a href="{job.get('outreach_linkedin_search', '#')}" style="color:#2563eb;">Find on LinkedIn</a>
                    <br><em style="font-size:12px;color:#6b7280;">"{job.get('outreach_message', '')}"</em>
                  </div>
                  <a href="{job.get('url', '#')}" style="display:inline-block;background:#2563eb;color:#ffffff;padding:8px 20px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
                    Apply Now &rarr;
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    job_cards = "".join(job_card(j) for j in jobs)

    summary_rows = ""
    for j in jobs:
        sc = j.get("match_score", 0)
        summary_rows += f"""
        <tr>
          <td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #f3f4f6;">{j.get('company','')}</td>
          <td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #f3f4f6;">{j.get('title','')}</td>
          <td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #f3f4f6;text-align:center;">
            <strong style="color:{score_color(sc)}">{sc}%</strong>
          </td>
          <td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #f3f4f6;text-align:center;">
            {priority_badge(j.get('priority','LOW'))}
          </td>
          <td style="padding:6px 10px;font-size:13px;border-bottom:1px solid #f3f4f6;">
            <a href="{j.get('url','#')}" style="color:#2563eb;">Apply</a>
          </td>
        </tr>"""

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:20px 0;">
    <tr><td align="center">
      <table width="640" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
        
        <tr>
          <td style="background:linear-gradient(135deg,#1e3a5f,#2563eb);padding:28px 24px;text-align:center;">
            <div style="font-size:24px;font-weight:700;color:#ffffff;letter-spacing:-0.5px;">
              Daily Job Digest
            </div>
            <div style="font-size:14px;color:#93c5fd;margin-top:6px;">{TODAY}</div>
            <div style="font-size:13px;color:#bfdbfe;margin-top:4px;">
              {len(high)} High &bull; {len(medium)} Medium &bull; {len(low)} Low priority matches
            </div>
          </td>
        </tr>

        <tr>
          <td style="padding:20px 20px 0 20px;">
            <div style="font-size:15px;font-weight:700;color:#111827;margin-bottom:10px;">Quick Summary</div>
            <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
              <tr style="background:#f9fafb;">
                <th style="padding:8px 10px;font-size:12px;color:#6b7280;text-align:left;">Company</th>
                <th style="padding:8px 10px;font-size:12px;color:#6b7280;text-align:left;">Role</th>
                <th style="padding:8px 10px;font-size:12px;color:#6b7280;text-align:center;">Score</th>
                <th style="padding:8px 10px;font-size:12px;color:#6b7280;text-align:center;">Priority</th>
                <th style="padding:8px 10px;font-size:12px;color:#6b7280;text-align:left;">Link</th>
              </tr>
              {summary_rows}
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:20px 0 0 0;">
            <div style="padding:0 20px 10px 20px;font-size:15px;font-weight:700;color:#111827;">Detailed Breakdown</div>
            <table width="100%" cellpadding="0" cellspacing="0">
              {job_cards}
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:20px 24px;background:#f9fafb;text-align:center;">
            <div style="font-size:12px;color:#9ca3af;">
              Automated by Claude API + GitHub Actions<br>
              Rotate search queries weekly for best coverage<br>
              Reply to this email to save notes on any application
            </div>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return html


def send_email(html_body, job_count):
    """Send the email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Daily Job Digest — {job_count} matches ({TODAY})"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    plain = f"Your daily job digest for {TODAY} is ready. View this email in HTML to see the full report with {job_count} job matches."
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
        print(f"[SUCCESS] Email sent to {RECIPIENT_EMAIL}")
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")
        raise


def send_error_email(error_msg):
    """Send a notification if the job search fails."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ Job Hunter Error — {TODAY}"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    body = f"""<html><body style="font-family:sans-serif;padding:20px;">
    <h2 style="color:#dc2626;">Job Search Failed</h2>
    <p>The automated job search encountered an error on {TODAY}:</p>
    <pre style="background:#f3f4f6;padding:16px;border-radius:8px;overflow:auto;">{error_msg}</pre>
    <p>Check your GitHub Actions logs for details.</p>
    </body></html>"""

    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT_EMAIL, msg.as_string())
    except Exception:
        pass


def main():
    try:
        raw_response = search_jobs()

        jobs = parse_jobs(raw_response)

        if not jobs:
            print("[WARN] No jobs found, sending error notification")
            send_error_email(f"Search ran but no jobs could be parsed from Claude's response. Raw output started with: {raw_response[:200]}")
            return

        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        html = build_email_html(jobs)

        send_email(html, len(jobs))

        print(f"\n{'='*60}")
        print(f"JOB SEARCH COMPLETE — {TODAY}")
        print(f"{'='*60}")
        for j in jobs:
            print(f"  [{j.get('priority','?'):6}] {j.get('match_score',0)}% | {j.get('company','?'):20} | {j.get('title','?')}")
        print(f"{'='*60}")

    except Exception as e:
        print(f"[FATAL] {e}")
        try:
            send_error_email(str(e))
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
