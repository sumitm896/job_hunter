# 🎯 Daily Job Hunter

Automated job search powered by the **Claude API + web search**. Runs daily on **GitHub Actions** and emails a scored, prioritized HTML digest straight to your inbox — no servers, no manual steps.

![Pipeline architecture](pipeline.png)

## What it does

Every morning at 8 AM (Dublin time), the pipeline:

- Searches LinkedIn, Indeed Ireland, IrishJobs.ie and Jobs.ie via Claude's web search
- Surfaces ~10 relevant **TAM / CSM / Solutions Engineer** roles
- Scores each role **0–100** against a target profile and tags it **HIGH / MEDIUM / LOW**
- Emails a digest with, per role:
  - Direct application link
  - Match score and the reasons behind it
  - CV keywords worth adding for that job
  - A cover-letter opening hook
  - Recruiter / hiring-manager name + a LinkedIn search link
  - A ready-to-send connection request message

## Tech

Python · Anthropic Claude API (Sonnet) + Web Search tool · GitHub Actions (scheduled cron) · Google Sheets (dedup) · Gmail SMTP

Fully unattended: secrets live in GitHub Actions, and the whole thing runs on the free tier for a couple of dollars a month in API cost.

---

## Setup (~15 minutes)

### 1. Create a Gmail App Password
1. Go to [myaccount.google.com](https://myaccount.google.com)
2. **Security → 2-Step Verification** (turn it on if it isn't already)
3. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. App: **Mail**, device: **Other** → name it "Job Hunter"
5. **Generate** and copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

### 2. Create the repository
1. Create a new repo (e.g. `job-hunter`)
2. Push this code:

```bash
cd job-hunter
git init
git add .
git commit -m "Initial commit - daily job hunter"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/job-hunter.git
git push -u origin main
```

### 3. Add GitHub Actions secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Add these four:

| Secret Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (starts with `sk-ant-`) |
| `GMAIL_ADDRESS` | The Gmail address that sends the digest |
| `GMAIL_APP_PASSWORD` | The 16-char app password from Step 1 (no spaces) |
| `RECIPIENT_EMAIL` | Where you want the digest delivered |

> Secrets are encrypted and stay private even on a public repo. Never hardcode keys in the code.

### 4. Test it
1. Open the **Actions** tab
2. Select the **Daily Job Hunter** workflow
3. **Run workflow → Run workflow**
4. Wait 2–3 minutes and check your email

Done. It now runs automatically every morning, and you can trigger it manually anytime from the Actions tab.

---

## Customization

**Target roles** — edit the `PROFILE` dict in `job_hunter.py`:

```python
"target_roles": [
    "Technical Account Manager",
    "Solutions Engineer",
    # add or remove roles here
],
```

**Search queries** — edit `SEARCH_QUERIES` in `job_hunter.py`. The script rotates through 7 sets (one per weekday) for wider coverage.

**Schedule** — edit `.github/workflows/daily-jobs.yml`:

```yaml
- cron: '0 7 * * *'     # 7 AM UTC ≈ 8 AM Dublin
- cron: '0 7 * * 1-5'   # weekdays only
- cron: '0 7,13 * * *'  # twice daily (≈ 8 AM + 2 PM Dublin)
```

---

## Cost

| Service | Cost |
|---|---|
| GitHub Actions | Free — ~3 min/day of the 2,000 free minutes/month |
| Anthropic API | ~$0.05–0.10 per run (Sonnet + web search) ≈ $1.50–3.00/month |
| Gmail | Free |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| No email received | Check the Actions logs; verify the Gmail app password |
| "Authentication failed" | Regenerate the Gmail app password; confirm 2FA is enabled |
| Empty results | Web search may have returned little that day; it retries tomorrow with different queries |
| API rate limit | Check usage at [console.anthropic.com](https://console.anthropic.com) |

---

## Architecture

```
GitHub Actions (daily cron)
  └─ job_hunter.py
       ├─ Claude API (Sonnet) + Web Search  →  finds & structures postings
       ├─ Score & filter against target profile
       ├─ Dedup (Google Sheets) + URL verification
       └─ Build HTML digest  →  send via Gmail SMTP
                                     └─ delivered to your inbox ☕
```
