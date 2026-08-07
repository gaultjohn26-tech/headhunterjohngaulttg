#!/usr/bin/env python3
"""
job_digest.py — fully automated daily job digest.

Pipeline:  fetch (8 sources) -> dedupe against seen_jobs.json -> keyword
prefilter -> Claude AI ranking -> HTML email via Gmail SMTP.

Designed to run unattended on GitHub Actions (see .github/workflows/).

Environment variables (set as GitHub repo secrets):
    ANTHROPIC_API_KEY     for AI ranking (optional if ai_ranking: false)
    GMAIL_ADDRESS         the Gmail account that sends the digest
    GMAIL_APP_PASSWORD    a Gmail *app password* (not your login password)

Local testing:
    python job_digest.py --sample --dry-run     # bundled fake jobs, no email
    python job_digest.py --dry-run              # live fetch, preview only
Both write digest_preview.html so you can see exactly what the email looks like.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

LOG = logging.getLogger("job_digest")
ROOT = Path(__file__).resolve().parent
SEEN_PATH = ROOT / "seen_jobs.json"
PREVIEW_PATH = ROOT / "digest_preview.html"
USER_AGENT = "job-digest/1.0 (personal daily job alert)"
SEEN_KEEP_DAYS = 90


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    source_id: str
    url: str
    title: str
    company: str
    description: str = ""
    location: str = ""
    job_type: str = ""
    salary: str = ""
    posted_at: datetime | None = None
    score: float = 0.0
    reason: str = ""
    kw_hits: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id or self.url}"

    def age_days(self, now: datetime) -> float | None:
        if not self.posted_at:
            return None
        return (now - self.posted_at).total_seconds() / 86400.0


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def parse_when(value) -> datetime | None:
    """Best-effort timestamp parsing: ISO strings, epoch seconds, epoch ms."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        # "2026-08-01 09:30:12" -> "2026-08-01T09:30:12"
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def http_get_json(url: str, params: dict | None = None, timeout: float = 30.0):
    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


def http_get_json_extra(url: str, params: dict | None = None,
                        headers: dict | None = None, timeout: float = 30.0):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    h.update(headers or {})
    resp = requests.get(url, params=params, timeout=timeout, headers=h)
    resp.raise_for_status()
    return resp.json()


def looks_remote(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in ("remote", "anywhere", "worldwide", "flexible"))


# ----------------------------------------------------------------------------
# Sources — each returns list[Job]; failures are logged, never fatal
# ----------------------------------------------------------------------------
def fetch_remotive(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for q in scfg.get("searches") or [""]:
        data = http_get_json(
            "https://remotive.com/api/remote-jobs", params={"search": q} if q else None
        )
        for j in data.get("jobs") or []:
            out.append(
                Job(
                    source="remotive",
                    source_id=str(j.get("id", "")),
                    url=j.get("url", ""),
                    title=(j.get("title") or "").strip(),
                    company=(j.get("company_name") or "").strip(),
                    description=strip_html(j.get("description") or ""),
                    location=j.get("candidate_required_location") or "Worldwide",
                    job_type=j.get("job_type") or "",
                    salary=j.get("salary") or "",
                    posted_at=parse_when(j.get("publication_date")),
                )
            )
    return out


def fetch_remoteok(scfg: dict) -> list[Job]:
    data = http_get_json("https://remoteok.com/api")
    out: list[Job] = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
            continue  # first element is a legal notice
        sal = ""
        if j.get("salary_min") and j.get("salary_max"):
            sal = f"${int(j['salary_min']):,} – ${int(j['salary_max']):,}"
        out.append(
            Job(
                source="remoteok",
                source_id=str(j.get("id", "")),
                url=j.get("url") or j.get("apply_url") or "",
                title=(j.get("position") or "").strip(),
                company=(j.get("company") or "").strip(),
                description=strip_html(j.get("description") or ""),
                location=j.get("location") or "Remote",
                salary=sal,
                posted_at=parse_when(j.get("date") or j.get("epoch")),
            )
        )
    return out


def fetch_jobicy(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for tag in scfg.get("tags") or [""]:
        params = {"count": 50}
        if tag:
            params["tag"] = tag
        data = http_get_json("https://jobicy.com/api/v2/remote-jobs", params=params)
        for j in data.get("jobs") or []:
            smin = j.get("annualSalaryMin") or j.get("salaryMin")
            smax = j.get("annualSalaryMax") or j.get("salaryMax")
            cur = j.get("salaryCurrency") or ""
            sal = f"{smin:,} – {smax:,} {cur}".strip() if smin and smax else ""
            out.append(
                Job(
                    source="jobicy",
                    source_id=str(j.get("id", "")),
                    url=j.get("url", ""),
                    title=(j.get("jobTitle") or "").strip(),
                    company=(j.get("companyName") or "").strip(),
                    description=strip_html(
                        j.get("jobDescription") or j.get("jobExcerpt") or ""
                    ),
                    location=j.get("jobGeo") or "Anywhere",
                    job_type=", ".join(j["jobType"])
                    if isinstance(j.get("jobType"), list)
                    else (j.get("jobType") or ""),
                    salary=sal,
                    posted_at=parse_when(j.get("pubDate")),
                )
            )
    return out


def fetch_themuse(scfg: dict) -> list[Job]:
    out: list[Job] = []
    pages = int(scfg.get("pages") or 2)
    for page in range(1, pages + 1):
        params: dict = {"page": page}
        if scfg.get("categories"):
            params["category"] = scfg["categories"]  # repeated ?category= params
        data = http_get_json("https://www.themuse.com/api/public/jobs", params=params)
        for j in data.get("results") or []:
            locs = [l.get("name", "") for l in j.get("locations") or []]
            out.append(
                Job(
                    source="themuse",
                    source_id=str(j.get("id", "")),
                    url=(j.get("refs") or {}).get("landing_page", ""),
                    title=(j.get("name") or j.get("title") or "").strip(),
                    company=((j.get("company") or {}).get("name") or "").strip(),
                    description=strip_html(j.get("contents") or ""),
                    location="; ".join(locs),
                    job_type=", ".join(
                        lv.get("name", "") for lv in j.get("levels") or []
                    ),
                    posted_at=parse_when(j.get("publication_date")),
                )
            )
    return out


def fetch_arbeitnow(scfg: dict) -> list[Job]:
    data = http_get_json("https://arbeitnow.com/api/job-board-api")
    out: list[Job] = []
    for j in data.get("data") or []:
        out.append(
            Job(
                source="arbeitnow",
                source_id=j.get("slug", ""),
                url=j.get("url", ""),
                title=(j.get("title") or "").strip(),
                company=(j.get("company_name") or "").strip(),
                description=strip_html(j.get("description") or ""),
                location=("Remote — " if j.get("remote") else "")
                + (j.get("location") or ""),
                job_type=", ".join(j.get("job_types") or []),
                posted_at=parse_when(j.get("created_at")),
            )
        )
    return out


def fetch_hackernews(scfg: dict) -> list[Job]:
    """Parse the current 'Ask HN: Who is hiring?' thread via the Algolia API."""
    search = http_get_json(
        "https://hn.algolia.com/api/v1/search",
        params={
            "query": "Ask HN: Who is hiring",
            "tags": "story,author_whoishiring",
            "hitsPerPage": 3,
        },
    )
    hits = search.get("hits") or []
    if not hits:
        return []
    hits.sort(key=lambda h: h.get("created_at_i") or 0, reverse=True)
    thread_id = hits[0]["objectID"]
    thread = http_get_json(f"https://hn.algolia.com/api/v1/items/{thread_id}")

    url_re = re.compile(r"https?://[^\s<>\)\]]+")
    out: list[Job] = []
    for c in thread.get("children") or []:
        text = strip_html(c.get("text") or "")
        if len(text) < 40 or "remote" not in text.lower():
            continue
        first = text.split(". ")[0][:220]
        parts = [p.strip() for p in first.split("|") if p.strip()]
        company = parts[0][:80] if parts else "See posting"
        title = parts[1][:120] if len(parts) > 1 else "See posting"
        m = url_re.search(c.get("text") or "")
        out.append(
            Job(
                source="hackernews",
                source_id=str(c.get("id", "")),
                url=m.group(0) if m else f"https://news.ycombinator.com/item?id={c.get('id')}",
                title=title,
                company=company,
                description=text[:2000],
                location="Remote (per posting)",
                posted_at=parse_when(c.get("created_at")),
            )
        )
    return out


def fetch_greenhouse(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(
                f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                params={"content": "true"},
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("greenhouse:%s skipped: %s", slug, exc)
            continue
        for j in data.get("jobs") or []:
            out.append(
                Job(
                    source=f"greenhouse:{slug}",
                    source_id=str(j.get("id", "")),
                    url=j.get("absolute_url", ""),
                    title=(j.get("title") or "").strip(),
                    company=slug.replace("-", " ").title(),
                    description=strip_html(j.get("content") or ""),
                    location=((j.get("location") or {}).get("name") or ""),
                    posted_at=parse_when(j.get("updated_at") or j.get("first_published")),
                )
            )
    return out


def fetch_lever(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(
                f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("lever:%s skipped: %s", slug, exc)
            continue
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            out.append(
                Job(
                    source=f"lever:{slug}",
                    source_id=str(j.get("id", "")),
                    url=j.get("hostedUrl", ""),
                    title=(j.get("text") or "").strip(),
                    company=slug.replace("-", " ").title(),
                    description=strip_html(
                        j.get("descriptionPlain") or j.get("description") or ""
                    ),
                    location=cats.get("location") or "",
                    job_type=cats.get("commitment") or "",
                    posted_at=parse_when(j.get("createdAt")),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Aggregators — these cover the "major sites" (incl. LinkedIn/Indeed/Glassdoor
# postings via Google for Jobs). Each activates only when its key is set.
# ---------------------------------------------------------------------------
def fetch_adzuna(scfg: dict) -> list[Job]:
    """Adzuna aggregates thousands of boards. Free API key."""
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        LOG.info("adzuna       skipped (no ADZUNA_APP_ID / ADZUNA_APP_KEY)")
        return []
    out: list[Job] = []
    country = scfg.get("country") or "us"
    for q in scfg.get("searches") or []:
        data = http_get_json(
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
            params={
                "app_id": app_id, "app_key": app_key,
                "what": q, "results_per_page": 50,
                "max_days_old": 7, "sort_by": "date",
                "content-type": "application/json",
            },
        )
        for j in data.get("results") or []:
            out.append(
                Job(
                    source="adzuna",
                    source_id=str(j.get("id", "")),
                    url=j.get("redirect_url", ""),
                    title=(j.get("title") or "").replace("<strong>", "").replace("</strong>", "").strip(),
                    company=((j.get("company") or {}).get("display_name") or "").strip(),
                    description=strip_html(j.get("description") or ""),
                    location=((j.get("location") or {}).get("display_name") or ""),
                    salary=(f"${int(j['salary_min']):,} – ${int(j['salary_max']):,}"
                            if j.get("salary_min") and j.get("salary_max") else ""),
                    posted_at=parse_when(j.get("created")),
                )
            )
    return out


def fetch_jooble(scfg: dict) -> list[Job]:
    """Jooble aggregates most major boards. Free API key by signup."""
    key = os.environ.get("JOOBLE_API_KEY", "")
    if not key:
        LOG.info("jooble       skipped (no JOOBLE_API_KEY)")
        return []
    out: list[Job] = []
    for q in scfg.get("searches") or []:
        resp = requests.post(
            f"https://jooble.org/api/{key}",
            json={"keywords": q, "location": "Remote"},
            timeout=30, headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        for j in resp.json().get("jobs") or []:
            out.append(
                Job(
                    source="jooble",
                    source_id=str(j.get("id", "")),
                    url=j.get("link", ""),
                    title=(j.get("title") or "").strip(),
                    company=(j.get("company") or "").strip(),
                    description=strip_html(j.get("snippet") or ""),
                    location=j.get("location") or "",
                    job_type=j.get("type") or "",
                    salary=j.get("salary") or "",
                    posted_at=parse_when(j.get("updated")),
                )
            )
    return out


def fetch_jsearch(scfg: dict) -> list[Job]:
    """JSearch (RapidAPI) = Google for Jobs = LinkedIn, Indeed, Glassdoor,
    ZipRecruiter, Monster + most public job sites. Free plan: 200 req/month;
    each query below costs exactly 1 request per day."""
    own_key = os.environ.get("OPENWEBNINJA_KEY", "")
    rapid_key = os.environ.get("RAPIDAPI_KEY", "")
    if not own_key and not rapid_key:
        LOG.info("jsearch      skipped (no OPENWEBNINJA_KEY / RAPIDAPI_KEY)")
        return []
    if own_key:
        endpoints = ["https://api.openwebninja.com/jsearch/search-v2",
                     "https://api.openwebninja.com/jsearch/search"]
        auth_headers = {"x-api-key": own_key}
    else:
        endpoints = ["https://jsearch.p.rapidapi.com/search"]
        auth_headers = {"X-RapidAPI-Key": rapid_key,
                        "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    out: list[Job] = []
    budget = int(scfg.get("daily_request_budget") or 999)
    for q in (scfg.get("searches") or [])[:budget]:
        params = {"query": q, "date_posted": scfg.get("date_posted") or "3days"}
        if own_key:
            params["work_from_home"] = "true"   # documented remote filter (direct API)
        else:
            params.update({"page": 1, "num_pages": int(scfg.get("pages") or 1),
                           "remote_jobs_only": "true"})
        data, last_exc = None, None
        for ep in list(endpoints):
            try:
                cand = http_get_json_extra(ep, params=params, headers=auth_headers)
                rows_chk = cand.get("data") if isinstance(cand, dict) else None
                if not isinstance(rows_chk, list):
                    raise ValueError(f"unexpected reply from {ep}: {str(cand)[:200]}")
                data = cand
                if ep != endpoints[0]:
                    endpoints.remove(ep); endpoints.insert(0, ep)  # remember winner
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
        if data is None:
            raise last_exc
        for j in data.get("data") or []:
            if not isinstance(j, dict):
                continue
            city = j.get("job_city") or ""
            country = j.get("job_country") or ""
            loc = ", ".join(x for x in (city, country) if x)
            if j.get("job_is_remote"):
                loc = ("Remote — " + loc) if loc else "Remote"
            sal = ""
            if j.get("job_min_salary") and j.get("job_max_salary"):
                sal = f"{int(j['job_min_salary']):,} – {int(j['job_max_salary']):,}"
            via = j.get("job_publisher") or ""
            out.append(
                Job(
                    source="jsearch" + (f" via {via}" if via else ""),
                    source_id=str(j.get("job_id", ""))[:80],
                    url=j.get("job_apply_link", ""),
                    title=(j.get("job_title") or "").strip(),
                    company=(j.get("employer_name") or "").strip(),
                    description=strip_html(j.get("job_description") or ""),
                    location=loc,
                    job_type=j.get("job_employment_type") or "",
                    salary=sal,
                    posted_at=parse_when(
                        j.get("job_posted_at_timestamp")
                        or j.get("job_posted_at_datetime_utc")
                    ),
                )
            )
    return out


def fetch_web3career(scfg: dict) -> list[Job]:
    """web3.career — largest crypto/web3 job board. Free API token."""
    token = os.environ.get("WEB3CAREER_TOKEN", "")
    if not token:
        LOG.info("web3career   skipped (no WEB3CAREER_TOKEN)")
        return []
    data = http_get_json(
        "https://web3.career/api/v1",
        params={"token": token, "remote": "true", "limit": 100},
    )
    # Their API sometimes nests the jobs list inside the response array.
    rows = _find_job_dicts(data)
    out: list[Job] = []
    for j in rows:
        out.append(
            Job(
                source="web3career",
                source_id=str(j.get("id", "")),
                url=j.get("apply_url") or j.get("url") or "",
                title=(j.get("title") or "").strip(),
                company=(j.get("company") or "").strip(),
                description=strip_html(j.get("description") or "") or
                            ", ".join(j.get("tags") or []),
                location=j.get("location") or "Remote",
                posted_at=parse_when(j.get("date_epoch") or j.get("date")),
            )
        )
    return out


def _find_job_dicts(data) -> list[dict]:
    """Return the first list of job-looking dicts found anywhere in `data`."""
    if isinstance(data, dict):
        for v in data.values():
            found = _find_job_dicts(v)
            if found:
                return found
        return []
    if isinstance(data, list):
        dicts = [x for x in data if isinstance(x, dict) and x.get("title")]
        if dicts:
            return dicts
        for v in data:
            found = _find_job_dicts(v)
            if found:
                return found
    return []


# ---------------------------------------------------------------------------
# More ATS platforms — per-company boards, same idea as Greenhouse/Lever.
# Most crypto companies and VC funds run one of these six.
# ---------------------------------------------------------------------------
def fetch_ashby(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ashby:%s skipped: %s", slug, exc)
            continue
        for j in data.get("jobs") or []:
            if j.get("isListed") is False:
                continue
            loc = j.get("location") or ""
            if j.get("isRemote"):
                loc = ("Remote — " + loc) if loc else "Remote"
            out.append(
                Job(
                    source=f"ashby:{slug}",
                    source_id=str(j.get("id", "")),
                    url=j.get("jobUrl") or j.get("applyUrl") or "",
                    title=(j.get("title") or "").strip(),
                    company=slug.replace("-", " ").title(),
                    description=strip_html(j.get("descriptionHtml")
                                           or j.get("descriptionPlain") or ""),
                    location=loc,
                    job_type=j.get("employmentType") or "",
                    posted_at=parse_when(j.get("publishedAt")),
                )
            )
    return out


def fetch_workable(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(
                f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                params={"details": "true"},
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("workable:%s skipped: %s", slug, exc)
            continue
        for j in data.get("jobs") or []:
            loc = j.get("location") or {}
            loc_s = ", ".join(
                x for x in (loc.get("city"), loc.get("country")) if x
            ) if isinstance(loc, dict) else str(loc)
            if j.get("telecommuting"):
                loc_s = ("Remote — " + loc_s) if loc_s else "Remote"
            out.append(
                Job(
                    source=f"workable:{slug}",
                    source_id=str(j.get("shortcode", "")),
                    url=j.get("url") or j.get("application_url") or "",
                    title=(j.get("title") or "").strip(),
                    company=(data.get("name") or slug).strip(),
                    description=strip_html(j.get("description") or ""),
                    location=loc_s,
                    posted_at=parse_when(j.get("published_on") or j.get("created_at")),
                )
            )
    return out


def fetch_smartrecruiters(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                params={"limit": 100},
            )
        except Exception as exc:  # noqa: BLE001
            LOG.warning("smartrecruiters:%s skipped: %s", slug, exc)
            continue
        for j in data.get("content") or []:
            loc = j.get("location") or {}
            loc_s = ", ".join(
                x for x in (loc.get("city"), loc.get("country")) if x
            )
            if loc.get("remote"):
                loc_s = ("Remote — " + loc_s) if loc_s else "Remote"
            out.append(
                Job(
                    source=f"smartrecruiters:{slug}",
                    source_id=str(j.get("id", "")),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                    title=(j.get("name") or "").strip(),
                    company=slug,
                    description="",  # postings endpoint has no body; title carries the filter
                    location=loc_s,
                    posted_at=parse_when(j.get("releasedDate")),
                )
            )
    return out


def fetch_recruitee(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(f"https://{slug}.recruitee.com/api/offers/")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("recruitee:%s skipped: %s", slug, exc)
            continue
        for j in data.get("offers") or []:
            out.append(
                Job(
                    source=f"recruitee:{slug}",
                    source_id=str(j.get("id", "")),
                    url=j.get("careers_url")
                        or f"https://{slug}.recruitee.com/o/{j.get('slug', '')}",
                    title=(j.get("title") or "").strip(),
                    company=slug.replace("-", " ").title(),
                    description=strip_html(j.get("description") or ""),
                    location=("Remote — " if j.get("remote") else "")
                             + (j.get("location") or j.get("city") or ""),
                    posted_at=parse_when(j.get("published_at") or j.get("created_at")),
                )
            )
    return out


# ---------------------------------------------------------------------------
# Workday — where most large banks / asset managers post. Config takes the
# careers-page URL verbatim; tenant, host, and site are parsed out of it.
# ---------------------------------------------------------------------------
_WD_URL_RE = re.compile(
    r"https://([a-z0-9\-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)"
)
_WD_AGO_RE = re.compile(r"(\d+)\+?\s+day", re.I)


def _parse_posted_on(text: str, now: datetime) -> datetime | None:
    t = (text or "").lower()
    if not t:
        return None
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = _WD_AGO_RE.search(t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None


def fetch_workday(scfg: dict) -> list[Job]:
    now = datetime.now(timezone.utc)
    out: list[Job] = []
    for url in scfg.get("urls") or []:
        m = _WD_URL_RE.match(url.strip())
        if not m:
            LOG.warning("workday: unrecognized URL %s (want https://tenant.wdN.myworkdayjobs.com/Site)", url)
            continue
        tenant, host, site = m.groups()
        api = f"https://{tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        for offset in (0, 20, 40):
            try:
                resp = requests.post(
                    api, json={"limit": 20, "offset": offset, "searchText": ""},
                    timeout=30, headers={"User-Agent": USER_AGENT,
                                         "Accept": "application/json",
                                         "Content-Type": "application/json"},
                )
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                LOG.warning("workday:%s skipped: %s", tenant, exc)
                break
            postings = resp.json().get("jobPostings") or []
            for j in postings:
                path = j.get("externalPath") or ""
                out.append(
                    Job(
                        source=f"workday:{tenant}",
                        source_id=path or j.get("title", ""),
                        url=f"https://{tenant}.{host}.myworkdayjobs.com/en-US/{site}{path}",
                        title=(j.get("title") or "").strip(),
                        company=tenant.upper() if len(tenant) <= 4 else tenant.title(),
                        description=" ".join(j.get("bulletFields") or []),
                        location=j.get("locationsText") or "",
                        posted_at=_parse_posted_on(j.get("postedOn"), now),
                    )
                )
            if len(postings) < 20:
                break
    return out


# ---------------------------------------------------------------------------
# Generic RSS/Atom — covers niche boards without APIs (CryptoJobsList,
# We Work Remotely, and anything else: paste a feed URL into config).
# ---------------------------------------------------------------------------
def _rss_text(node, *names) -> str:
    for n in names:
        found = node.find(n)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def fetch_rss(scfg: dict) -> list[Job]:
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    out: list[Job] = []
    for feed in scfg.get("feeds") or []:
        name, url = feed.get("name") or "rss", feed.get("url") or ""
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
            resp.raise_for_status()
            # strip namespaces so RSS2 and Atom parse with the same tag names
            xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', "", resp.text, count=10)
            root = ET.fromstring(xml_text)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("rss:%s skipped (fetch or parse): %s", name, exc)
            continue
        items = root.findall(".//item") or root.findall(".//entry")
        for it in items:
            title = _rss_text(it, "title")
            link = _rss_text(it, "link")
            if not link:  # Atom puts it in an attribute
                ln = it.find("link")
                link = ln.get("href", "") if ln is not None else ""
            when = _rss_text(it, "pubDate", "published", "updated")
            posted = None
            if when:
                try:
                    posted = parsedate_to_datetime(when)
                except (TypeError, ValueError):
                    posted = parse_when(when)
            if posted and posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            company = name
            jtitle = title
            for sep in (" at ", " @ ", " — ", ": "):
                if sep in title:
                    left, right = title.split(sep, 1)
                    if sep in (" at ", " @ "):
                        jtitle, company = left.strip(), right.strip()
                    else:
                        company, jtitle = left.strip(), right.strip()
                    break
            out.append(
                Job(
                    source=f"rss:{name}",
                    source_id=link,
                    url=link,
                    title=jtitle,
                    company=company,
                    description=strip_html(
                        _rss_text(it, "description", "summary", "content")
                    )[:2000],
                    location="",
                    posted_at=posted,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Watchlist auto-resolver: probe every candidate company name against all six
# ATS platforms and record which board answers. Run weekly by its own
# workflow; the daily run merges the results in automatically.
# ---------------------------------------------------------------------------
RESOLVED_PATH = ROOT / "watchlist_resolved.json"
ATS_PLATFORMS = ("greenhouse", "lever", "ashby", "workable",
                 "smartrecruiters", "recruitee", "bamboohr", "pinpoint",
                 "teamtailor")


def _slug_variants(name: str) -> list[str]:
    base = name.lower().strip()
    base = base.replace("&", "and").removeprefix("the ")
    for suffix in (".com", " inc", " labs", " capital"):
        pass  # keep full name; variants below cover shapes
    joined = re.sub(r"[^a-z0-9]+", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    seen, out = set(), []
    for v in (joined, hyphen, base.replace(" ", "")):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _probe(ats: str, slug: str) -> bool:
    try:
        if ats == "greenhouse":
            d = http_get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=8)
            return isinstance(d, dict) and "jobs" in d
        if ats == "lever":
            d = http_get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json", "limit": 1}, timeout=8)
            return isinstance(d, list)
        if ats == "ashby":
            d = http_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=8)
            return isinstance(d, dict) and "jobs" in d
        if ats == "workable":
            d = http_get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=8)
            return isinstance(d, dict) and "jobs" in d
        if ats == "smartrecruiters":
            d = http_get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 1}, timeout=8)
            return isinstance(d, dict) and "content" in d
        if ats == "recruitee":
            d = http_get_json(f"https://{slug}.recruitee.com/api/offers/", timeout=8)
            return isinstance(d, dict) and "offers" in d
        if ats == "bamboohr":
            d = http_get_json(f"https://{slug}.bamboohr.com/careers/list", timeout=8)
            return isinstance(d, dict) and "result" in d
        if ats == "pinpoint":
            d = http_get_json(f"https://{slug}.pinpointhq.com/postings.json", timeout=8)
            return isinstance(d, (dict, list))
        if ats == "teamtailor":
            r = requests.get(f"https://{slug}.teamtailor.com/jobs.rss", timeout=8,
                             headers={"User-Agent": USER_AGENT})
            return r.status_code == 200 and "<" in r.text[:200]
    except Exception:
        return False
    return False


def load_resolved() -> dict:
    try:
        return json.loads(RESOLVED_PATH.read_text()).get("resolved") or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def resolve_watchlist(cfg: dict) -> int:
    import time

    candidates = list(cfg.get("watchlist_candidates") or [])
    try:
        candidates += json.loads(RESOLVED_PATH.read_text()).get("discovered_pending") or []
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    manual: set[str] = set()
    for ats in ATS_PLATFORMS:
        manual |= {s.lower() for s in ((cfg.get("sources") or {}).get(ats) or {}).get("companies") or []}
    previous = load_resolved()
    already: dict[str, str] = {}
    for ats, slugs in previous.items():
        for s in slugs:
            already[s] = ats

    resolved: dict[str, list[str]] = {a: list(previous.get(a) or []) for a in ATS_PLATFORMS}
    unresolved: list[str] = []
    for name in candidates:
        variants = _slug_variants(name)
        if any(v in manual or v in already for v in variants):
            continue  # covered already — don't re-probe every week
        hit = None
        for ats in ATS_PLATFORMS:
            for slug in variants:
                if _probe(ats, slug):
                    hit = (ats, slug)
                    break
                time.sleep(0.1)
            if hit:
                break
        if hit:
            ats, slug = hit
            resolved[ats].append(slug)
            already[slug] = ats
            print(f"  FOUND {name:>28s} -> {ats}:{slug}")
        else:
            unresolved.append(name)
            print(f"  ----- {name:>28s} -> no public board found")

    RESOLVED_PATH.write_text(json.dumps(
        {"resolved": {a: sorted(set(v)) for a, v in resolved.items() if v},
         "unresolved": sorted(unresolved),
         "checked_at": datetime.now(timezone.utc).isoformat()},
        indent=1) + "\n")
    total = sum(len(v) for v in resolved.values())
    print(f"\nwatchlist_resolved.json written: {total} boards live, "
          f"{len(unresolved)} candidates unresolved (own career sites or agencies).")
    return 0


# ---------------------------------------------------------------------------
# v3 ATS additions: BambooHR, Pinpoint, Teamtailor (feed-based)
# ---------------------------------------------------------------------------
def fetch_bamboohr(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(f"https://{slug}.bamboohr.com/careers/list")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("bamboohr:%s skipped: %s", slug, exc)
            continue
        for j in data.get("result") or []:
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x) \
                if isinstance(loc, dict) else str(loc or "")
            if j.get("isRemote") or "remote" in str(j.get("locationType", "")).lower():
                loc_s = ("Remote — " + loc_s) if loc_s else "Remote"
            out.append(Job(
                source=f"bamboohr:{slug}",
                source_id=str(j.get("id", "")),
                url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
                title=(j.get("jobOpeningName") or j.get("title") or "").strip(),
                company=slug.replace("-", " ").title(),
                description=str(j.get("departmentLabel") or ""),
                location=loc_s,
            ))
    return out


def _pp_attr(j: dict, *keys):
    """Pinpoint returns JSON:API-style records; tolerate flat dicts too."""
    attrs = j.get("attributes") if isinstance(j.get("attributes"), dict) else j
    for k in keys:
        v = attrs.get(k)
        if v:
            return v
    return ""


def fetch_pinpoint(scfg: dict) -> list[Job]:
    out: list[Job] = []
    for slug in scfg.get("companies") or []:
        try:
            data = http_get_json(f"https://{slug}.pinpointhq.com/postings.json")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("pinpoint:%s skipped: %s", slug, exc)
            continue
        rows = data.get("data") if isinstance(data, dict) else data
        for j in rows or []:
            loc = _pp_attr(j, "location", "workplace-type", "workplace_type")
            if isinstance(loc, dict):
                loc = loc.get("name") or ""
            out.append(Job(
                source=f"pinpoint:{slug}",
                source_id=str(j.get("id", "")),
                url=_pp_attr(j, "url") or f"https://{slug}.pinpointhq.com/postings/{j.get('id')}",
                title=str(_pp_attr(j, "title")).strip(),
                company=slug.replace("-", " ").title(),
                description=strip_html(str(_pp_attr(j, "description") or ""))[:2000],
                location=str(loc),
            ))
    return out


def fetch_teamtailor(scfg: dict) -> list[Job]:
    """Teamtailor career sites expose a jobs feed; reuse the RSS engine."""
    feeds = [{"name": slug, "url": f"https://{slug}.teamtailor.com/jobs.rss"}
             for slug in scfg.get("companies") or []]
    jobs = fetch_rss({"feeds": feeds})
    for j in jobs:
        j.source = "teamtailor:" + j.source.split(":", 1)[1]
    return jobs


# ---------------------------------------------------------------------------
# TheirStack — enterprise job-data feed (ATS-crawled + LinkedIn-derived,
# pre-deduplicated). Activates with THEIRSTACK_API_KEY. 1 credit = 1 job
# record returned, so the query below is tightly scoped to the last day.
# ---------------------------------------------------------------------------
def fetch_theirstack(scfg: dict) -> list[Job]:
    key = os.environ.get("THEIRSTACK_API_KEY", "")
    if not key:
        LOG.info("theirstack   skipped (no THEIRSTACK_API_KEY)")
        return []
    body = {  # documented fields only: theirstack.com/en/docs/api-reference
        "page": 0,
        "limit": int(scfg.get("daily_record_limit") or 100),
        "posted_at_max_age_days": int(scfg.get("max_age_days") or 1),
        "order_by": [{"desc": True, "field": "date_posted"},
                     {"desc": True, "field": "discovered_at"}],
    }
    if scfg.get("title_patterns"):
        body["job_title_or"] = scfg["title_patterns"]
    resp = requests.post(
        "https://api.theirstack.com/v1/jobs/search",
        json=body, timeout=45,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "User-Agent": USER_AGENT},
    )
    if not resp.ok:
        LOG.warning("theirstack HTTP %s: %s", resp.status_code, resp.text[:300])
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") or payload.get("jobs") or []
    out: list[Job] = []
    for j in rows:
        comp = j.get("company_object") or j.get("company") or {}
        if isinstance(comp, str):
            comp = {"name": comp}
        smin, smax = j.get("min_annual_salary"), j.get("max_annual_salary")
        out.append(Job(
            source="theirstack",
            source_id=str(j.get("id", "")),
            url=j.get("final_url") or j.get("url") or j.get("source_url") or "",
            title=(j.get("job_title") or j.get("title") or "").strip(),
            company=(comp.get("name") or "").strip(),
            description=strip_html(j.get("description") or "")[:3000],
            location=j.get("location") or ("Remote" if j.get("remote") else ""),
            salary=(f"{int(smin):,} – {int(smax):,}" if smin and smax else ""),
            posted_at=parse_when(j.get("date_posted")),
        ))
    return out


# ---------------------------------------------------------------------------
# Employer auto-discovery: companies seen in sampled sources get queued and
# probed by the weekly resolver, graduating them to complete ingestion.
# ---------------------------------------------------------------------------
SAMPLED_SOURCES = {"adzuna", "jooble", "jsearch", "theirstack"}


def harvest_discovered(candidates: list[Job], cap: int = 40) -> int:
    try:
        state = json.loads(RESOLVED_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    pending = set(state.get("discovered_pending") or [])
    known = {s for slugs in (state.get("resolved") or {}).values() for s in slugs}
    added = 0
    for j in candidates:
        base = j.source.split(":")[0].split(" ")[0]
        name = (j.company or "").strip()
        if base not in SAMPLED_SOURCES or not (2 < len(name) < 60):
            continue
        if any(v in known for v in _slug_variants(name)) or name in pending:
            continue
        pending.add(name)
        added += 1
        if added >= cap:
            break
    if added:
        state["discovered_pending"] = sorted(pending)
        RESOLVED_PATH.write_text(json.dumps(state, indent=1) + "\n")
        LOG.info("auto-discovery: queued %d new employers for Sunday's resolver", added)
    return added


SOURCES = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "jobicy": fetch_jobicy,
    "themuse": fetch_themuse,
    "arbeitnow": fetch_arbeitnow,
    "hackernews": fetch_hackernews,
    "adzuna": fetch_adzuna,
    "jooble": fetch_jooble,
    "jsearch": fetch_jsearch,
    "web3career": fetch_web3career,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
    "workday": fetch_workday,
    "rss": fetch_rss,
    "bamboohr": fetch_bamboohr,
    "pinpoint": fetch_pinpoint,
    "teamtailor": fetch_teamtailor,
    "theirstack": fetch_theirstack,
}

ALL_REMOTE_SOURCES = {"remotive", "remoteok", "jobicy", "jsearch", "web3career"}  # remote by construction


def collect_jobs(cfg: dict) -> tuple[list[Job], dict]:
    resolved = load_resolved()
    jobs: list[Job] = []
    health = {"ok": [], "failed": [], "keyless": [], "disabled": []}
    for name, fn in SOURCES.items():
        scfg = dict((cfg.get("sources") or {}).get(name) or {})
        if not scfg.get("enabled", False):
            health["disabled"].append(name)
            continue
        if name in ATS_PLATFORMS and resolved.get(name):
            scfg["companies"] = sorted(
                {*(scfg.get("companies") or []), *resolved[name]}
            )
        try:
            got = fn(scfg)
            if got or name not in KEYED_SOURCES or _has_key(name):
                health["ok"].append((name, len(got)))
            else:
                health["keyless"].append(name)
            LOG.info("%-12s -> %d postings", name, len(got))
            jobs.extend(got)
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the run
            health["failed"].append(name)
            LOG.warning("%-12s FAILED: %s", name, exc)
    return jobs, health


KEYED_SOURCES = {"adzuna": ("ADZUNA_APP_ID",), "jooble": ("JOOBLE_API_KEY",),
                 "jsearch": ("OPENWEBNINJA_KEY", "RAPIDAPI_KEY"),
                 "web3career": ("WEB3CAREER_TOKEN",),
                 "theirstack": ("THEIRSTACK_API_KEY",)}


def _has_key(name: str) -> bool:
    return any(os.environ.get(k) for k in KEYED_SOURCES.get(name, ()))


# ----------------------------------------------------------------------------
# Prefilter — cheap keyword screening before anything hits the AI ranker
# ----------------------------------------------------------------------------
def prefilter(jobs: list[Job], cfg: dict, now: datetime) -> list[Job]:
    inc = [k.lower() for k in cfg.get("include_keywords") or []]
    exc = [k.lower() for k in cfg.get("exclude_keywords") or []]
    max_age = float(cfg.get("max_age_days") or 7)
    require_remote = bool(cfg.get("require_remote", True))

    kept: list[Job] = []
    seen_pairs: set[tuple[str, str]] = set()
    for j in jobs:
        if not j.title or not j.url:
            continue
        age = j.age_days(now)
        if age is not None and age > max_age:
            continue
        base_source = j.source.split(":")[0].split(" ")[0]
        if (
            require_remote
            and base_source not in ALL_REMOTE_SOURCES
            and base_source != "hackernews"  # already screened for "remote"
            and not looks_remote(j.location)
            and not looks_remote(j.title)
            and not looks_remote(j.description[:800])
        ):
            continue
        title_l = j.title.lower()
        if any(k in title_l for k in exc):
            continue
        blob = f"{title_l} {j.description.lower()}"
        hits = [k for k in inc if k in blob]  # informational only — NOT a gate
        pair = (title_l, j.company.lower())
        if pair in seen_pairs:  # same job cross-posted on two boards
            continue
        seen_pairs.add(pair)
        j.kw_hits = hits
        # crude keyword score, used only as the AI-less fallback
        title_hits = sum(1 for k in inc if k in title_l)
        age_eff = age if age is not None else 3.0
        recency = max(0.0, 1.0 - age_eff / max_age) if max_age else 0.5
        j.score = min(95.0, 30 + 14 * title_hits + 6 * len(hits) + 15 * recency)
        j.reason = "Matched: " + ", ".join(hits[:4]) if hits else "Keyword match"
        kept.append(j)

    kept.sort(key=lambda x: (x.posted_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    cap = int(cfg.get("rank_candidates_max") or 5000)
    return kept[:cap]


# ----------------------------------------------------------------------------
# AI ranking via the Claude API
# ----------------------------------------------------------------------------
_RANK_INSTRUCTIONS = """You are a meticulous recruiting assistant. Score each job \
posting below for fit against this candidate profile.

<candidate_profile>
{profile}
</candidate_profile>

{weights}Scoring rubric (0-100):
  90-100  near-perfect: remote, matches target titles AND work style
  70-89   strong: clearly relevant role, minor mismatches
  50-69   plausible: partially relevant, worth a glance
  0-49    poor fit: wrong field, wrong seniority, or not truly remote

For each job, also write ONE short sentence (max 18 words) explaining the fit
from the candidate's point of view. Be concrete, not generic.

Respond with ONLY a JSON array, no markdown fences, no commentary:
[{{"id": "j1", "score": 85, "reason": "..."}}, ...]
Include every job id exactly once.

<jobs>
{jobs}
</jobs>"""


def _job_line(idx: int, j: Job) -> str:
    desc = (j.description or "")[:400]
    bits = [f"title={j.title}", f"company={j.company}"]
    if j.location:
        bits.append(f"location={j.location}")
    if j.job_type:
        bits.append(f"type={j.job_type}")
    if j.salary:
        bits.append(f"salary={j.salary}")
    bits.append(f"desc={desc}")
    return f'j{idx}: ' + " | ".join(bits)


def _parse_scores(text: str) -> dict[str, tuple[float, str]]:
    """Pull a JSON array out of a model reply, tolerating fences/preamble."""
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON array in model output")
    arr = json.loads(cleaned[start : end + 1])
    out: dict[str, tuple[float, str]] = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("id", "")).strip()
        if not jid:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            continue
        out[jid] = (max(0.0, min(100.0, score)), str(item.get("reason", "")).strip())
    return out


def rank_with_claude(jobs: list[Job], cfg: dict) -> bool:
    """Score jobs in place. Returns True on success, False -> keyword fallback."""
    if not jobs:
        return True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        LOG.warning("ANTHROPIC_API_KEY not set — falling back to keyword ranking")
        return False
    try:
        from anthropic import Anthropic
    except ImportError:
        LOG.warning("anthropic package missing — falling back to keyword ranking")
        return False

    model = cfg.get("model") or "claude-haiku-4-5"
    profile = (cfg.get("profile") or "").strip() or "No profile provided."
    client = Anthropic()

    try:
        batch_size = 25
        for i in range(0, len(jobs), batch_size):
            batch = jobs[i : i + batch_size]
            listing = "\n".join(_job_line(i + n + 1, j) for n, j in enumerate(batch))
            w = cfg.get("scoring_weights") or {}
            weights_txt = ""
            if w:
                weights_txt = ("Weight the dimensions as follows when scoring: "
                               + "; ".join(f"{k.replace('_',' ')} {v}%" for k, v in w.items())
                               + ".\n\n")
            prompt = _RANK_INSTRUCTIONS.format(profile=profile, jobs=listing,
                                               weights=weights_txt)
            resp = client.messages.create(
                model=model,
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            scores = _parse_scores(text)
            for n, j in enumerate(batch):
                jid = f"j{i + n + 1}"
                if jid in scores:
                    j.score, reason = scores[jid]
                    if reason:
                        j.reason = reason
                else:
                    LOG.info("ranker skipped %s — keeping keyword score", jid)
        LOG.info("AI ranking complete for %d jobs (%s)", len(jobs), model)
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("AI ranking failed (%s) — falling back to keyword ranking", exc)
        return False


# ----------------------------------------------------------------------------
# Seen-jobs state (committed back to the repo by the workflow)
# ----------------------------------------------------------------------------
def load_seen(path: Path = SEEN_PATH) -> dict[str, str]:
    try:
        return json.loads(path.read_text() or "{}")
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict[str, str], fetched: list[Job], now: datetime,
              path: Path = SEEN_PATH) -> None:
    today = now.date().isoformat()
    for j in fetched:
        seen.setdefault(j.key, today)
    cutoff = (now - timedelta(days=SEEN_KEEP_DAYS)).date().isoformat()
    pruned = {k: v for k, v in seen.items() if v >= cutoff}
    path.write_text(json.dumps(pruned, indent=0, sort_keys=True) + "\n")
    LOG.info("seen_jobs.json: %d ids tracked", len(pruned))


# ----------------------------------------------------------------------------
# Sample data for local testing (no network, no email)
# ----------------------------------------------------------------------------
def _sample_jobs(now: datetime) -> list[Job]:
    mk = lambda **kw: Job(**kw)  # noqa: E731
    fresh = now - timedelta(hours=20)
    return [
        mk(source="remotive", source_id="s1", url="https://example.com/1",
           title="Treasury Operations Analyst", company="Northwind Digital",
           description="Remote-first fintech seeks analyst for treasury operations, cash management, risk reporting. Async culture, contract-friendly.",
           location="Worldwide", job_type="contract", salary="$90,000 – $120,000", posted_at=fresh),
        mk(source="jobicy", source_id="s2", url="https://example.com/2",
           title="Compliance Research Associate (Part-time)", company="Ledgerline",
           description="Part-time remote compliance research for a crypto exchange. Deliverable-based, flexible hours.",
           location="Anywhere", job_type="part-time", posted_at=fresh),
        mk(source="hackernews", source_id="s3", url="https://example.com/3",
           title="Fractional Finance Lead", company="Seed-stage SaaS (YC)",
           description="REMOTE. Fractional finance lead for seed-stage SaaS: modeling, board prep, cash strategy. ~10 hrs/week.",
           location="Remote (per posting)", posted_at=fresh),
        mk(source="themuse", source_id="s4", url="https://example.com/4",
           title="Business Development Manager, Partnerships", company="Atlas Markets",
           description="Own partnership pipeline for a market-data platform. Remote, async standups, quarterly travel.",
           location="Flexible / Remote", posted_at=now - timedelta(days=2)),
        mk(source="remoteok", source_id="s5", url="https://example.com/5",
           title="Strategy & Operations Associate", company="Harbor Labs",
           description="Generalist strategy and operations role at a remote crypto infra startup.",
           location="Remote", posted_at=now - timedelta(days=1)),
        mk(source="arbeitnow", source_id="s6", url="https://example.com/6",
           title="Senior Golang Developer", company="Techwerk GmbH",
           description="Backend services in Go.", location="Remote — Berlin", posted_at=fresh),
        mk(source="lever:acme", source_id="s7", url="https://example.com/7",
           title="Onsite Facilities Coordinator", company="Acme",
           description="Front-desk and facilities work.", location="New York HQ", posted_at=fresh),
        mk(source="greenhouse:vertex", source_id="s8", url="https://example.com/8",
           title="Risk & Investor Relations Analyst", company="Vertex Fund",
           description="Remote analyst supporting IR decks, LP reporting, and portfolio risk dashboards.",
           location="Remote — US", posted_at=fresh),
    ]
