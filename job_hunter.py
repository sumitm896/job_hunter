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

    return f"""You are an automated job search assistant. Today is {TODAY}.

## Candidate Profile
- **Name:** {PROFILE['name']}
- **Location:** {PROFILE['location']} (open to remote/hybrid across Ireland)
- **Target roles:** {roles_str}
- **Experience:** {PROFILE['experience_years']}+ years at Freshworks (Freshservice/Freshdesk), {PROFILE['education']}
- **Key skills:** {skills_str}

## Task
Search for 10 FRESH job openings this candidate can apply to. Focus on:
- LinkedIn Jobs Ireland
- Indeed Ireland  
- IrishJobs.ie
- Jobs.ie
- Company career pages

Search for Technical Account Manager, Customer Success Manager, Solutions Engineer, Solutions Consultant, and similar roles in Ireland.

## CRITICAL: Output Format
You MUST respond with ONLY a valid JSON array. No markdown, no backticks, no explanation text before or after. Just the raw JSON array.

Each element must have these exact fields:
{{
  "rank": 1,
  "company": "Company Name",
  "title": "Job Title",
  "location": "City, Country (onsite/hybrid/remote)",
  "url": "https://direct-application-url",
  "salary": "Listed salary or 'Not listed'",
  "match_score": 85,
  "match_reasons": ["reason 1", "reason 2", "reason 3"],
  "missing_skills": ["skill 1 you'd need to highlight"],
  "priority": "HIGH or MEDIUM or LOW",
  "cv_keywords": ["keyword1", "keyword2", "keyword3"],
  "outreach_name": "Hiring Manager or Recruiter Name",
  "outreach_title": "Their Title",
  "outreach_linkedin_search": "https://www.linkedin.com/search/results/people/?keywords=recruiter+CompanyName+Ireland",
  "outreach_message": "Short 50-word connection request message",
  "cover_letter_hook": "A 2-sentence compelling opening for the cover letter specific to this company"
}}

## Scoring Rules for match_score (0-100):
- 90-100: Perfect match — same role title, same industry (SaaS/ITSM), right experience level
- 80-89: Strong match — adjacent role, SaaS industry, most skills align  
- 70-79: Good match — related role, some skill overlap, worth applying
- 60-69: Stretch — different industry or seniority, but transferable skills
- Below 60: Don't include it

## Priority Rules:
- HIGH: match_score >= 85, role is TAM or very close, SaaS company
- MEDIUM: match_score 75-84, adjacent role or slightly different industry
- LOW: match_score 70-74, worth a shot but not ideal

Sort by match_score descending. Return exactly 10 jobs.
Remember: Output ONLY the JSON array, nothing else."""


def search_jobs():
    """Call Claude API with web search to find jobs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Pick search queries based on day of week
    day_index = datetime.now(timezone.utc).weekday()
    queries = SEARCH_QUERIES[day_index % len(SEARCH_QUERIES)]

    print(f"[{TODAY}] Starting job search...")
    print(f"[INFO] Using query set {day_index % len(SEARCH_QUERIES)}: {queries}")

    prompt = build_prompt()

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        tools=[
            {
                "type": "web_search_20250305",
                "name": "web_search",
            }
        ],
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )

    # Extract text from response (may have multiple content blocks due to web search)
    full_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            full_text += block.text

    print(f"[INFO] Response received ({len(full_text)} chars)")
    return full_text


def parse_jobs(raw_text):
    """Parse the JSON response from Claude."""
    # Try to find JSON array in the response
    # Remove markdown code fences if present
    cleaned = raw_text.strip()
    cleaned = re.sub(r"```json\s*", "", cleaned)
    cleaned = re.sub(r"```\s*", "", cleaned)
    cleaned = cleaned.strip()

    # Try to find the JSON array
    start = cleaned.find("[")
    end = cleaned.rfind("]") + 1

    if start == -1 or end == 0:
        print("[ERROR] No JSON array found in response")
        print(f"[DEBUG] Raw text: {cleaned[:500]}")
        return []

    json_str = cleaned[start:end]

    try:
        jobs = json.loads(json_str)
        print(f"[INFO] Parsed {len(jobs)} jobs")
        return jobs
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON parse failed: {e}")
        print(f"[DEBUG] JSON string: {json_str[:500]}")
        return []


def build_email_html(jobs):
    """Build a clean HTML email with job results."""
    high = [j for j in jobs if j.get("priority") == "HIGH"]
    medium = [j for j in jobs if j.get("priority") == "MEDIUM"]
    low = [j for j in jobs if j.get("priority") == "LOW"]

    def score_color(score):
        if score >= 85:
            return "#16a34a"  # green
        elif score >= 75:
            return "#ca8a04"  # amber
        else:
            return "#dc2626"  # red

    def priority_badge(priority):
        colors = {
            "HIGH": ("#dcfce7", "#16a34a"),
            "MEDIUM": ("#fef9c3", "#ca8a04"),
            "LOW": ("#fee2e2", "#dc2626"),
        }
        bg, fg = colors.get(priority, ("#f3f4f6", "#6b7280"))
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
                    <strong>Why it matches:</strong> {', '.join(job.get('match_reasons', []))}
                  </div>
                  <div style="font-size:13px;color:#374151;margin-bottom:8px;">
                    <strong>CV Keywords to add:</strong> {', '.join(job.get('cv_keywords', []))}
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
        
        <!-- Header -->
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

        <!-- Quick Summary Table -->
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

        <!-- Detailed Job Cards -->
        <tr>
          <td style="padding:20px 0 0 0;">
            <div style="padding:0 20px 10px 20px;font-size:15px;font-weight:700;color:#111827;">Detailed Breakdown</div>
            <table width="100%" cellpadding="0" cellspacing="0">
              {job_cards}
            </table>
          </td>
        </tr>

        <!-- Footer -->
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

    # Plain text fallback
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
        pass  # Don't fail on error email failure


def main():
    try:
        # Step 1: Search for jobs using Claude + web search
        raw_response = search_jobs()

        # Step 2: Parse the JSON response
        jobs = parse_jobs(raw_response)

        if not jobs:
            print("[WARN] No jobs parsed, sending error notification")
            send_error_email("No jobs were found or parsing failed. Raw response logged in GitHub Actions.")
            return

        # Step 3: Sort by match score
        jobs.sort(key=lambda x: x.get("match_score", 0), reverse=True)

        # Step 4: Build email
        html = build_email_html(jobs)

        # Step 5: Send email
        send_email(html, len(jobs))

        # Step 6: Print summary
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
