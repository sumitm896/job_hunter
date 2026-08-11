"""
ats_detect.py — Lane 1 ATS auto-detection + live job fetch.

WHAT THIS DOES
    Given the target companies in targets.yaml, this module figures out — at
    runtime — which Applicant Tracking System (ATS) each company's public job
    board runs on, and pulls their CURRENTLY-OPEN roles straight from the
    source. Because a public board only ever lists live reqs, everything this
    returns is fresh by construction: no expired-URL problem, no verification
    step needed for these roles.

    Detection is not a hand-maintained table. For each company we probe the
    public JSON endpoints of the major ATSs in order; the first one that
    returns a real board *is* the answer, and we cache it. Companies where no
    public API responds (Workday, Taleo, fully custom sites, LinkedIn-only
    boards like Circit) fall through with ats=None — those are the ones that
    route to the Firecrawl /crawl lane instead.

PUBLIC ATS ENDPOINTS (all unauthenticated GET, verified Aug 2026)
    greenhouse      https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
    lever           https://api.lever.co/v0/postings/{slug}?mode=json
    ashby           https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
    smartrecruiters https://api.smartrecruiters.com/v1/companies/{slug}/postings   (paginated)

    Each returns a completely different shape, so every ATS gets its own
    normalizer that maps into one common job dict (see _COMMON_SCHEMA below).

NETWORK NOTE
    These endpoints live on greenhouse.io / lever.co / ashbyhq.com /
    smartrecruiters.com. Run this where outbound HTTP to those hosts is
    allowed (e.g. your GitHub Actions runner). It will NOT reach them from a
    locked-down sandbox whose egress allowlist excludes them.

USAGE
    # detect + fetch for every company in targets.yaml, cache results back:
    python ats_detect.py targets.yaml --write-back

    # just print the detection report without touching the file:
    python ats_detect.py targets.yaml

    # from Lane 1 code:
    from ats_detect import run_detection
    jobs, report = run_detection("targets.yaml", write_back=True)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
import time
from typing import Callable, Optional

import requests

# ── Common normalized schema ────────────────────────────────────────────────
# Every ATS normalizer returns a list of dicts in THIS shape, so Lane 1 and the
# Haiku scorer see one consistent structure regardless of source ATS.
_COMMON_SCHEMA = {
    "company": str,        # display name from targets.yaml
    "ats": str,            # greenhouse | lever | ashby | smartrecruiters
    "board_id": str,       # the slug that resolved
    "job_id": str,
    "title": str,
    "location": str,       # free-text location string
    "url": str,            # public apply / hosted URL
    "updated_at": str,     # ISO-8601 if the ATS provides it, else ""
    "department": str,
    "remote": object,      # True / False / None (not all ATSs say)
}

# ── Probe order ─────────────────────────────────────────────────────────────
# Tried top to bottom. First ATS whose endpoint returns a real board wins.
PROBE_ORDER = ["greenhouse", "lever", "ashby", "smartrecruiters"]

REQUEST_TIMEOUT = 12          # seconds per HTTP call
RETRIES = 2                   # transient-failure retries per call
USER_AGENT = "lane1-ats-detect/1.0 (+job-hunter-pipeline)"

# ── Manual overrides ────────────────────────────────────────────────────────
# For companies whose board slug isn't a clean derivation of the name, or that
# we've already confirmed, pin them here as "ats:slug". Detection skips probing
# and uses these directly. Add to this map whenever a probe guesses wrong.
OVERRIDES: dict[str, str] = {
    "Harvey": "ashby:harvey",     # confirmed: jobs.ashbyhq.com/harvey
    "Ashby": "ashby:ashby",       # confirmed: jobs.ashbyhq.com/ashby
}

# Companies known to have NO public ATS API — route straight to Firecrawl,
# don't waste probe requests. (ats is recorded as the string given.)
KNOWN_NON_API: dict[str, str] = {
    "Circit": "custom",           # applies via LinkedIn Easy Apply, no public board
}


# ── Slug generation ─────────────────────────────────────────────────────────

def slug_candidates(company: str) -> list[str]:
    """
    Produce an ordered list of plausible board slugs for a company name.

    Most ATS slugs are just the company name normalized. We try a few common
    forms; a wrong guess simply 404s and we move on, so over-generating is
    cheap. Order matters — most-likely first.
    """
    name = company.strip().lower()
    # strip a few common corporate suffixes that rarely appear in slugs
    name = re.sub(r"\b(inc|ltd|llc|limited|software|technologies|labs)\b", "", name)
    compact = re.sub(r"[^a-z0-9]", "", name)          # "newrelic", "commandlink"
    hyphen = re.sub(r"[^a-z0-9]+", "-", name).strip("-")  # "new-relic"
    first = compact  # fallback

    cands: list[str] = []
    for c in (compact, hyphen, first):
        if c and c not in cands:
            cands.append(c)
    return cands


# ── HTTP helper ─────────────────────────────────────────────────────────────

def _get(url: str, params: Optional[dict] = None) -> Optional[requests.Response]:
    """GET with a short timeout and a couple of transient retries.

    Returns the Response on any completed request (including 404 — the caller
    inspects status), or None if the request never completed after retries.
    """
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            return requests.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
        except requests.RequestException as e:
            last_exc = e
            time.sleep(1.5 * (attempt + 1))
    print(f"    [http] gave up on {url}: {last_exc}")
    return None


def _ms_to_iso(ms) -> str:
    """Lever returns createdAt as a millisecond epoch int. Convert to ISO."""
    try:
        return _dt.datetime.fromtimestamp(
            int(ms) / 1000, tz=_dt.timezone.utc
        ).isoformat()
    except (TypeError, ValueError):
        return ""


# ── Per-ATS fetch + normalize ───────────────────────────────────────────────
# Each returns (jobs, board_exists):
#   board_exists=True  -> this slug is a real board on this ATS (even if 0 jobs)
#   board_exists=False -> not this ATS / unknown slug; keep probing

def fetch_greenhouse(slug: str, company: str) -> tuple[list[dict], bool]:
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
             params={"content": "true"})
    if r is None or r.status_code == 404:
        return [], False
    if r.status_code != 200:
        return [], False
    try:
        data = r.json()
    except ValueError:
        return [], False
    if "jobs" not in data:
        return [], False

    jobs = []
    for j in data.get("jobs", []):
        dept = ""
        for m in (j.get("metadata") or []):
            if str(m.get("name", "")).lower() in ("department", "team"):
                dept = str(m.get("value") or "")
                break
        jobs.append({
            "company": company, "ats": "greenhouse", "board_id": slug,
            "job_id": str(j.get("id", "")),
            "title": (j.get("title") or "").strip(),
            "location": ((j.get("location") or {}).get("name") or "").strip(),
            "url": j.get("absolute_url", ""),
            "updated_at": j.get("updated_at") or "",
            "department": dept,
            "remote": None,
        })
    return jobs, True


def fetch_lever(slug: str, company: str) -> tuple[list[dict], bool]:
    r = _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if r is None or r.status_code == 404:
        return [], False
    if r.status_code != 200:
        return [], False
    try:
        data = r.json()
    except ValueError:
        return [], False
    if not isinstance(data, list):
        return [], False

    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        wt = (cats.get("workplaceType") or "").lower()
        jobs.append({
            "company": company, "ats": "lever", "board_id": slug,
            "job_id": str(j.get("id", "")),
            "title": (j.get("text") or "").strip(),
            "location": loc.strip(),
            "url": j.get("hostedUrl") or j.get("applyUrl") or "",
            "updated_at": _ms_to_iso(j.get("createdAt")),
            "department": (cats.get("team") or "").strip(),
            "remote": True if wt == "remote" else (False if wt else None),
        })
    return jobs, True


def fetch_ashby(slug: str, company: str) -> tuple[list[dict], bool]:
    r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
             params={"includeCompensation": "true"})
    if r is None or r.status_code in (404, 400):
        return [], False
    if r.status_code != 200:
        return [], False
    try:
        data = r.json()
    except ValueError:
        return [], False
    if "jobs" not in data:
        return [], False

    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "company": company, "ats": "ashby", "board_id": slug,
            "job_id": str(j.get("id", "")),
            "title": (j.get("title") or "").strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "updated_at": j.get("publishedAt") or "",
            "department": (j.get("department") or j.get("team") or "").strip(),
            "remote": j.get("isRemote"),
        })
    return jobs, True


def fetch_smartrecruiters(slug: str, company: str) -> tuple[list[dict], bool]:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    first = _get(base, params={"limit": 100, "offset": 0})
    if first is None or first.status_code == 404:
        return [], False
    if first.status_code != 200:
        return [], False
    try:
        data = first.json()
    except ValueError:
        return [], False
    if "content" not in data:
        return [], False

    rows = list(data.get("content", []))
    total = int(data.get("totalFound", len(rows)) or len(rows))
    offset = len(rows)
    # page through the rest, capped so a huge board can't loop forever
    while offset < total and offset < 1000:
        nxt = _get(base, params={"limit": 100, "offset": offset})
        if nxt is None or nxt.status_code != 200:
            break
        page = nxt.json().get("content", [])
        if not page:
            break
        rows.extend(page)
        offset += len(page)

    jobs = []
    for j in rows:
        loc = j.get("location") or {}
        loc_str = ", ".join(
            x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x
        )
        jobs.append({
            "company": company, "ats": "smartrecruiters", "board_id": slug,
            "job_id": str(j.get("id", "")),
            "title": (j.get("name") or "").strip(),
            "location": loc_str,
            "url": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id','')}",
            "updated_at": j.get("releasedDate") or "",
            "department": ((j.get("department") or {}) or {}).get("label", "") or "",
            "remote": bool(loc.get("remote")) if "remote" in loc else None,
        })
    return jobs, True


FETCHERS: dict[str, Callable[[str, str], tuple[list[dict], bool]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


# ── Detection ───────────────────────────────────────────────────────────────

def probe_company(company: str) -> dict:
    """
    Detect the ATS for one company and fetch its live roles.

    Returns a result dict:
        {company, ats, board_id, jobs, source}
    where ats is None if no public API responded (-> Firecrawl lane), and
    `source` is one of: override | known-non-api | probed | none.
    """
    # 1. explicit non-API companies: don't probe, hand to Firecrawl
    if company in KNOWN_NON_API:
        return {"company": company, "ats": None, "board_id": None,
                "jobs": [], "source": "known-non-api",
                "note": KNOWN_NON_API[company]}

    # 2. pinned overrides: human-verified slug, trust it even if empty today
    if company in OVERRIDES:
        ats, slug = OVERRIDES[company].split(":", 1)
        jobs, ok = FETCHERS[ats](slug, company)
        if ok:
            return {"company": company, "ats": ats, "board_id": slug,
                    "jobs": jobs, "source": "override", "empty_probes": []}
        # override stale -> fall through to probing

    # 3. probe each ATS/slug. A *guessed* slug counts as a real detection ONLY
    #    if the board actually returns roles. Several ATSs — SmartRecruiters
    #    especially — answer 200 with an empty list for unknown slugs, which
    #    would otherwise be a false positive (e.g. "salesforce" on
    #    SmartRecruiters returning 0 jobs). Empty boards are remembered for the
    #    report, but we keep probing and ultimately route the company to
    #    Firecrawl rather than falsely claiming an ATS that yields nothing.
    empty_probes = []
    for ats in PROBE_ORDER:
        fetch = FETCHERS[ats]
        for slug in slug_candidates(company):
            jobs, ok = fetch(slug, company)
            if ok and jobs:
                return {"company": company, "ats": ats, "board_id": slug,
                        "jobs": jobs, "source": "probed",
                        "empty_probes": empty_probes}
            if ok and not jobs:
                empty_probes.append(f"{ats}:{slug}")

    # 4. no ATS returned real roles -> Firecrawl lane (note any empty boards)
    return {"company": company, "ats": None, "board_id": None,
            "jobs": [], "source": "none", "empty_probes": empty_probes}


def _load_targets(path: str):
    """Load targets.yaml with ruamel (round-trip, preserves comments) if we'll
    write back, else plain yaml. Returns (data, ruamel_obj_or_None)."""
    from ruamel.yaml import YAML
    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    with open(path) as f:
        data = yaml_rt.load(f)
    return data, yaml_rt


# ── Location pre-filter tokens ──────────────────────────────────────────────
# This is a COARSE pre-filter to trim obvious out-of-region roles before
# scoring — NOT the final eligibility judge. The Haiku scorer reads the full JD
# and makes the real "can Sumit work this from Ireland?" call. We only cut the
# clear noise (e.g. Snowflake's US postings) so the scorer isn't flooded.
_EU_IE_TOKENS = (
    "ireland", "dublin", "galway", "cork", "limerick", "emea", "europe",
    "european", " eu ", "eu-", "united kingdom", " uk ", "london", "germany",
    "france", "spain", "netherlands", "poland", "portugal", "italy", "sweden",
    "remote - emea", "remote europe", "remote, europe", "remote - eu",
)
# Clear non-EU / US-only markers -> drop even when the remote flag is set.
_NON_EU_TOKENS = (
    "united states", "u.s.", "usa", " us ", "us-remote", "us remote",
    "remote - us", "remote (us", "remote, us", "canada", "australia",
    "singapore", "india", "apac", "latam", "brazil", "japan", "mexico",
    "new york", "san francisco", "boston", "seattle", "austin", "denver",
    "chicago", "atlanta", "los angeles", "toronto",
)


def _match_location(loc: str, remote, keywords: list[str]) -> bool:
    """
    Coarse Ireland/EU pre-filter. Keep a role if it's plausibly workable from
    Ireland; drop obvious out-of-region roles even when they're flagged remote
    (a "Remote - US" role is still not workable from Dublin). Region-less
    "Remote" is kept and left for the scorer to judge.
    """
    blob = f" {(loc or '').lower()} "
    if any(t in blob for t in _EU_IE_TOKENS):
        return True                       # explicitly Ireland / EU / EMEA
    if any(t in blob for t in _NON_EU_TOKENS):
        return False                      # explicitly another region only
    if remote is True:
        return True                       # region-less remote -> scorer decides
    return False


def run_detection(targets_path: str, write_back: bool = False,
                  apply_location_filter: bool = True,
                  detect_unknown: bool = True) -> tuple[list[dict], list[dict]]:
    """
    Detect + fetch for every company in targets.yaml.

    Returns (all_jobs, report):
        all_jobs — flat list of normalized job dicts across all API-backed
                   companies (optionally filtered to target locations)
        report   — one row per company: ats, board_id, job_count, source
    If write_back, caches detected ats/board_id back into targets.yaml
    (comments preserved) so future runs skip re-probing.

    detect_unknown:
        True  (default) — full detection: probe companies that aren't yet
               cached to a public-API ATS. Use for the weekly refresh.
        False — fast path: only fetch companies already cached to an API ATS;
               skip probing TBD/custom/none companies entirely. Use for the
               daily digest so it doesn't re-probe the ~24 Firecrawl companies
               every run.
    """
    data, yaml_rt = _load_targets(targets_path)
    companies = data.get("companies", [])
    loc_keywords = (data.get("meta", {}) or {}).get(
        "location_filter", ["Ireland", "Dublin", "Galway", "Cork", "Remote"]
    )

    all_jobs: list[dict] = []
    report: list[dict] = []
    firecrawl_queue: list[str] = []

    for entry in companies:
        name = entry.get("name", "?")
        # respect an already-cached detection unless it's still TBD
        cached_ats = entry.get("ats")
        cached_id = entry.get("board_id")
        is_cached_api = (
            cached_ats and cached_ats not in ("TBD", "custom", None)
            and cached_id and cached_id not in ("TBD", "n/a", None)
        )
        if is_cached_api:
            fetch = FETCHERS.get(cached_ats)
            if fetch:
                jobs, ok = fetch(cached_id, name)
                res = {"company": name, "ats": cached_ats, "board_id": cached_id,
                       "jobs": jobs, "source": "cached", "empty_probes": []}
            else:
                res = probe_company(name)
        elif detect_unknown:
            res = probe_company(name)
        else:
            # daily fast path: don't re-probe unknown/custom companies
            res = {"company": name, "ats": None, "board_id": None,
                   "jobs": [], "source": "skipped", "empty_probes": []}

        jobs = res["jobs"]
        if apply_location_filter and jobs:
            jobs = [j for j in jobs
                    if _match_location(j["location"], j["remote"], loc_keywords)]

        all_jobs.extend(jobs)
        report.append({
            "company": name, "ats": res["ats"], "board_id": res["board_id"],
            "job_count": len(jobs), "raw_count": len(res["jobs"]),
            "source": res["source"],
            "sample": jobs[0]["title"] if jobs else "",
            "empty_probes": res.get("empty_probes", []),
        })

        if res["ats"] is None:
            firecrawl_queue.append(name)

        # write detection back into the yaml structure (cache)
        if write_back:
            entry["ats"] = res["ats"] if res["ats"] else "custom"
            entry["board_id"] = res["board_id"] if res["board_id"] else "n/a"

        # be polite between companies
        time.sleep(0.4)

    if write_back:
        with open(targets_path, "w") as f:
            yaml_rt.dump(data, f)

    _print_report(report, firecrawl_queue)
    return all_jobs, report


def _print_report(report: list[dict], firecrawl_queue: list[str]) -> None:
    api_hits = [r for r in report if r["ats"]]
    print("\n" + "=" * 68)
    print(f"ATS DETECTION REPORT — {len(report)} companies")
    print("=" * 68)
    for r in sorted(report, key=lambda x: (x["ats"] or "zzz", x["company"])):
        ats = r["ats"] or "— (Firecrawl)"
        line = f"  {r['company']:<20} {ats:<16}"
        if r["ats"]:
            line += f" {r['board_id']:<16} {r['job_count']:>3} IE/remote"
            line += f" / {r['raw_count']:>3} total"
            if r["sample"]:
                line += f"   e.g. {r['sample'][:40]}"
        elif r.get("empty_probes"):
            # a board existed but returned 0 roles — likely wrong slug; the
            # company may be reachable under a different one (add an OVERRIDE)
            line += f"   [empty board at {', '.join(r['empty_probes'])}]"
        print(line)
    print("-" * 68)
    print(f"  API-backed (Lane 1 direct): {len(api_hits)}")
    print(f"  Firecrawl /crawl needed:    {len(firecrawl_queue)}  "
          f"{firecrawl_queue if firecrawl_queue else ''}")
    print("=" * 68 + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lane 1 ATS auto-detection")
    ap.add_argument("targets", help="path to targets.yaml")
    ap.add_argument("--write-back", action="store_true",
                    help="cache detected ats/board_id back into the yaml")
    ap.add_argument("--no-location-filter", action="store_true",
                    help="return all roles, not just Ireland/remote")
    args = ap.parse_args(argv)

    run_detection(args.targets, write_back=args.write_back,
                  apply_location_filter=not args.no_location_filter)


if __name__ == "__main__":
    main()
