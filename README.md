# 🎯 Daily Job Hunter

Automated job search powered by Claude API + Web Search. Runs daily via GitHub Actions and sends a scored, prioritized HTML email digest to your inbox.

## What It Does

Every morning at 8 AM (Dublin time), this automation:

1. **Searches** LinkedIn, Indeed Ireland, IrishJobs.ie, Jobs.ie via Claude's web search
2. **Finds** 10 relevant TAM / CSM / Solutions Engineer roles
3. **Scores** each job (0-100) against your profile
4. **Prioritizes** as HIGH / MEDIUM / LOW
5. **Emails** you a beautiful digest with:
   - Direct application links
   - Match score & reasons
   - CV keywords to add per job
   - Cover letter opening hook
   - Recruiter/hiring manager name + LinkedIn search link
   - Ready-to-send connection request message

## Setup (15 minutes)

### Step 1: Create Gmail App Password

1. Go to [myaccount.google.com](https://myaccount.google.com)
2. Click **Security** → **2-Step Verification** (turn it ON if not already)
3. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
4. Select app: **Mail**, device: **Other** → type "Job Hunter"
5. Click **Generate** → copy the 16-character password (e.g., `abcd efgh ijkl mnop`)
6. Save this — you'll need it in Step 3

### Step 2: Create GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `job-hunter` (make it **Private**)
3. Click **Create repository**
4. Push this code:

```bash
cd job-hunter
git init
git add .
git commit -m "Initial commit - daily job hunter"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/job-hunter.git
git push -u origin main
```

### Step 3: Add Secrets to GitHub

1. In your repo, go to **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret** and add these 4 secrets:

| Secret Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (starts with `sk-ant-`) |
| `GMAIL_ADDRESS` | `mishra892sumit@gmail.com` |
| `GMAIL_APP_PASSWORD` | The 16-char password from Step 1 (no spaces) |
| `RECIPIENT_EMAIL` | `mishra892sumit@gmail.com` (or different email to receive digest) |

### Step 4: Test It

1. Go to **Actions** tab in your repo
2. Click **Daily Job Hunter** workflow
3. Click **Run workflow** → **Run workflow**
4. Wait 2-3 minutes → check your email!

## Done! ✅

The workflow will now run automatically every morning. You can also trigger it manually anytime from the Actions tab.

---

## Customization

### Change Target Roles
Edit the `PROFILE` dict in `job_hunter.py`:
```python
"target_roles": [
    "Technical Account Manager",
    "Solutions Engineer",
    # Add or remove roles here
],
```

### Change Search Queries
Edit `SEARCH_QUERIES` in `job_hunter.py`. The script rotates through 7 sets (one per day of the week) for maximum coverage.

### Change Schedule
Edit `.github/workflows/daily-jobs.yml`:
```yaml
- cron: '0 7 * * *'   # 7 AM UTC = ~8 AM Dublin
- cron: '0 7 * * 1-5'  # Weekdays only
- cron: '0 6,12 * * *' # Twice daily (7 AM + 1 PM Dublin)
```

### Run Twice Daily
For more aggressive searching, change the cron to run twice:
```yaml
- cron: '0 7 * * *'    # Morning run
- cron: '0 13 * * *'   # Afternoon run
```

## Cost

- **GitHub Actions**: Free (2,000 minutes/month on free plan; this uses ~3 min/day = ~90 min/month)
- **Anthropic API**: ~$0.05-0.10 per run (Claude Sonnet + web search) = ~$1.50-3.00/month
- **Gmail**: Free

## Troubleshooting

| Issue | Fix |
|---|---|
| No email received | Check GitHub Actions logs; verify Gmail App Password |
| "Authentication failed" | Regenerate Gmail App Password; ensure 2FA is enabled |
| Empty results | Claude's web search may have had limited results; it'll retry tomorrow with different queries |
| API rate limit | The free Anthropic tier has limits; check your usage at console.anthropic.com |

## Architecture

```
GitHub Actions (cron 7 AM UTC)
  → job_hunter.py
    → Anthropic API (Claude Sonnet + Web Search tool)
      → Searches job platforms
      → Returns structured JSON
    → Builds HTML email
    → Sends via Gmail SMTP
  → You receive email at 8 AM Dublin time ☕
```
