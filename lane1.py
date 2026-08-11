"""
lane1.py — turn the fetched target-company roles into scored digest jobs.

ats_detect.py already FETCHES live roles from the 16 API-backed target
companies. This module does the three things needed to turn those raw roles
into emailable digest entries:

    1. prefilter()   — cut the obvious non-fits (Accountant, Software Engineer,
                       Recruiter, pure-sales AE/BDR, ...) cheaply, WITHOUT
                       dropping genuine fits that have invented/off-label
                       titles. A role in a customer-facing DEPARTMENT is kept
                       regardless of its title; everything not hard-excluded
                       falls through to scoring. Titles never silently sink a
                       real fit — the score does.
    2. score_jobs()  — one (batched) Haiku call scores the survivors against
                       Sumit's profile + transferable-first rubric, adding
                       match_score / match_reasons / priority. Only title +
                       company + location + department are sent, so it's cheap.
    3. get_scored_jobs() — the single entry point job_hunter.main() calls.

Lane 1 roles come from live ATS boards, so they are fresh by construction and
DO NOT go through verify.py. Tag them and merge them straight into the digest.
"""

from __future__ import annotations

import json
import re

from ats_detect import run_detection

# ── Filter vocab ────────────────────────────────────────────────────────────
# Departments that make a role relevant REGARDLESS of title. If an ATS puts the
# role in one of these, we keep it even if the title is something invented.
RELEVANT_DEPARTMENT = (
    "customer success", "customer experience", "customer support", "support",
    "solution", "professional services", "post-sales", "post sales",
    "implementation", "onboarding", "technical account", "customer engineering",
    "services", "delivery", "deployment", "client services", "enablement",
    "customer", "technical services",
)

# Titles with essentially zero chance for Sumit's profile — dropped BEFORE
# scoring to save tokens. Deliberately conservative: only clear non-fits.
# NOTE: we never blanket-exclude "engineer" — Solutions/Sales/Customer/Support/
# Forward-Deployed/Partner/Implementation Engineer are all valid fits.
HARD_EXCLUDE_TITLE = (
    "accountant", "accounting", "payroll", "bookkeeper", "tax ", "treasury",
    "controller", "auditor", "financial analyst", "fp&a", "strategic finance",
    "software engineer", "software developer", "backend", "front end",
    "frontend", "full stack", "fullstack", "devops", "site reliability",
    " sre", "data engineer", "data scientist", "machine learning",
    "ml engineer", "research engineer", "ai research", "infrastructure engineer",
    "platform engineer", "security engineer", "qa engineer", "test engineer",
    "mobile engineer", "android engineer", "ios engineer",
    "recruiter", "talent acquisition", "sourcer", "people partner", "hrbp",
    "human resources",
    "designer", "ux ", "ui ", "graphic",
    "marketing", "brand", "content writer", "copywriter", "seo ",
    "demand generation", "social media", "communications", "public relations",
    "legal counsel", "paralegal", "attorney",
    "account executive", "business development representative",
    "sales development", "sdr", "bdr", "sales manager", "sales director",
    "executive assistant", "administrative assistant", "office manager",
    "receptionist", "product manager", "product owner", "product designer",
    "intern", "internship",
)


def _relevant(title: str, department: str) -> bool:
    """Keep unless clearly impossible. Department override protects invented
    titles; only hard-excluded titles are dropped."""
    d = (department or "").lower()
    if any(k in d for k in RELEVANT_DEPARTMENT):
        return True                      # customer-facing dept -> keep, any title
    t = (title or "").lower()
    if any(k in t for k in HARD_EXCLUDE_TITLE):
        return False                     # clear non-fit
    return True                          # ambiguous -> let the scorer judge


def prefilter(jobs: list[dict]) -> list[dict]:
    """Drop obvious non-fits; keep everything plausibly relevant for scoring."""
    return [j for j in jobs if _relevant(j.get("title", ""), j.get("department", ""))]


# ── Scoring ─────────────────────────────────────────────────────────────────

_SCORE_SYSTEM = (
    "You score how well job postings fit a candidate. You output ONLY a raw "
    "JSON array, no prose. Score transferable fit FIRST: the candidate's core "
    "strengths (post-sales technical-relationship management, ITSM/Freshservice "
    "support engineering, IAM/SSO, API/webhook/integration troubleshooting, "
    "escalation ownership, adoption/retention) travel to any platform. Treat "
    "specific tooling as a bonus, never a requirement, and never penalise a "
    "role for using an unfamiliar stack. A relevant role with an unusual or "
    "invented title should still score on its substance."
)


def _build_score_prompt(profile_context: str, batch: list[dict]) -> str:
    lines = []
    for i, j in enumerate(batch):
        lines.append(
            f'{i}. {j.get("title","?")} — {j.get("company","?")} — '
            f'{j.get("location","?")} — dept: {j.get("department","") or "n/a"}'
        )
    listing = "\n".join(lines)
    return (
        f"{profile_context}\n\n"
        f"Score each of the following {len(batch)} roles for THIS candidate. "
        f"Return a JSON array with one object per role, in the same order:\n"
        f'  "index": int (matches the number below)\n'
        f'  "match_score": int 0-100 (transferable fit first)\n'
        f'  "match_reasons": array of exactly 3 short strings (lead with '
        f'transferable reasons; mention tool overlap only where it truly exists)\n'
        f'  "priority": "HIGH" | "MEDIUM" | "LOW"\n\n'
        f"ROLES:\n{listing}\n\n"
        f"Return ONLY the JSON array."
    )


def _parse_scores(raw_text: str) -> list[dict]:
    text = re.sub(r"```json\s*|```", "", raw_text.strip())
    start = text.find("[")
    if start == -1:
        return []
    try:
        parsed, _ = json.JSONDecoder().raw_decode(text[start:])
        return parsed if isinstance(parsed, list) else []
    except Exception as e:
        print(f"[LANE1] score parse failed: {e}")
        return []


def score_jobs(client, model: str, profile_context: str,
               jobs: list[dict], batch_size: int = 25) -> list[dict]:
    """Score pre-fetched roles with Haiku (no web tools). Adds match_score,
    match_reasons, priority. Returns only successfully-scored roles."""
    scored: list[dict] = []
    for start in range(0, len(jobs), batch_size):
        batch = jobs[start:start + batch_size]
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=2000,
                system=_SCORE_SYSTEM,
                messages=[{"role": "user",
                           "content": _build_score_prompt(profile_context, batch)}],
            )
        except Exception as e:
            print(f"[LANE1] scoring call failed for batch {start}: {e}")
            continue

        text = "".join(
            b.text for b in resp.content
            if getattr(b, "type", "") == "text" and getattr(b, "text", "")
        )
        by_index = {}
        for v in _parse_scores(text):
            if isinstance(v, dict) and "index" in v:
                by_index[v["index"]] = v

        for i, job in enumerate(batch):
            verdict = by_index.get(i)
            if not verdict:
                continue
            try:
                s = int(verdict.get("match_score", 0))
            except (ValueError, TypeError):
                s = 0
            job = dict(job)  # don't mutate caller's dict
            job["match_score"] = s
            job["match_reasons"] = verdict.get("match_reasons", [])[:3] or ["Transferable fit"]
            job["priority"] = "HIGH" if s >= 85 else "MEDIUM" if s >= 75 else "LOW"
            job["source_lane"] = "lane1-ats"
            job["verification_status"] = "api-verified"  # skip verify.py
            scored.append(job)
    return scored


# ── Entry point ─────────────────────────────────────────────────────────────

def get_scored_jobs(client, model: str, profile_context: str,
                    targets_path: str = "targets.yaml",
                    min_score: int = 0,
                    detect_unknown: bool = False) -> list[dict]:
    """
    Full Lane 1 pass: fetch live roles from cached API boards -> prefilter ->
    score. Returns scored job dicts ready to merge into the digest (already
    tagged so they bypass verify.py).

    detect_unknown=False (default) uses the fast path: only the companies
    already cached to an API ATS in targets.yaml are fetched — no re-probing.
    """
    all_jobs, _report = run_detection(
        targets_path, write_back=False,
        apply_location_filter=True, detect_unknown=detect_unknown,
    )
    print(f"[LANE1] fetched {len(all_jobs)} IE/remote roles from API boards")
    kept = prefilter(all_jobs)
    print(f"[LANE1] {len(kept)} roles after title/department pre-filter")
    scored = score_jobs(client, model, profile_context, kept)
    if min_score > 0:
        scored = [j for j in scored if j.get("match_score", 0) >= min_score]
    print(f"[LANE1] {len(scored)} roles scored"
          + (f" (>= {min_score})" if min_score else ""))
    return scored
