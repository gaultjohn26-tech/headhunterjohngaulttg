"""Infer engine v1: DefiLlama raises (verified endpoint), SEC EDGAR Form D
full-text feed (defensive), Messari Pro (key-gated, field-defensive).
Each signal attaches to a company and can create a proactive target."""
from __future__ import annotations
import logging, os, time
import requests
from . import db as dbm
from .ingest import Job, USER_AGENT, parse_when, strip_html
from datetime import datetime, timezone

LOG = logging.getLogger("signals")

def _get(url, params=None, headers=None, timeout=30):
    r = requests.get(url, params=params, timeout=timeout,
                     headers={"User-Agent": USER_AGENT, **(headers or {})})
    r.raise_for_status()
    return r.json()

def defillama_raises(con, days=2) -> list[Job]:
    out = []
    cutoff = time.time() - days * 86400
    try:
        data = _get("https://api.llama.fi/raises")
        for r in (data.get("raises") or [])[:400]:
            if (r.get("date") or 0) < cutoff: continue
            name = r.get("name") or ""
            amt = r.get("amount")
            out.append(Job(
                source="signal:defillama", source_id=f"{name}-{r.get('date')}",
                url=r.get("source") or "https://defillama.com/raises",
                title=f"PROACTIVE — {name} raised ${amt}m" if amt else f"PROACTIVE — {name} raised",
                company=name, location="Remote (proactive)",
                description=f"Fresh raise: {r.get('round') or ''} led by "
                            f"{', '.join((r.get('leadInvestors') or [])[:3])}. Crypto cos "
                            "historically hire BD/ops/research within 60 days of a raise. "
                            "Outreach window open.",
                posted_at=datetime.fromtimestamp(r.get("date"), tz=timezone.utc)))
    except Exception as exc:
        LOG.warning("defillama failed: %s", exc)
    return out

def edgar_form_d(con, days=2) -> list[Job]:
    out = []
    try:
        data = _get("https://efts.sec.gov/LATEST/search-index",
                    params={"q": "\"pooled investment fund\"", "forms": "D",
                            "dateRange": "custom",
                            "startdt": time.strftime("%Y-%m-%d", time.localtime(time.time()-days*86400)),
                            "enddt": time.strftime("%Y-%m-%d")},
                    headers={"Accept": "application/json"})
        hits = ((data.get("hits") or {}).get("hits") or [])[:60]
        for h in hits:
            src = h.get("_source") or {}
            names = src.get("display_names") or []
            name = names[0].split("(")[0].strip() if names else ""
            if not name: continue
            out.append(Job(
                source="signal:edgar_form_d", source_id=h.get("_id",""),
                url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company="
                    + name.replace(" ", "+"),
                title=f"PROACTIVE — {name} filed Form D (new fund/raise)",
                company=name, location="Remote (proactive)",
                description="New exempt offering filed — new funds hire IR, ops, "
                            "and research early. Family vehicles surface here first.",
                posted_at=parse_when(src.get("file_date"))))
    except Exception as exc:
        LOG.warning("edgar failed: %s", exc)
    return out

def messari_intel(con, days=2) -> list[Job]:
    key = os.environ.get("MESSARI_API_KEY", "")
    if not key:
        LOG.info("messari skipped (no MESSARI_API_KEY)"); return []
    out = []
    try:  # field-defensive; verify mapping on first live run
        data = _get("https://data.messari.io/api/v1/news",
                    headers={"x-messari-api-key": key})
        for a in (data.get("data") or [])[:80]:
            title = a.get("title") or ""
            tl = title.lower()
            if not any(k in tl for k in ("raise", "fund", "launch", "hire", "expand", "acqui")):
                continue
            out.append(Job(
                source="signal:messari", source_id=str(a.get("id","")),
                url=a.get("url") or "", title=f"SIGNAL — {title[:110]}",
                company=(a.get("references") or [{}])[0].get("name","") if a.get("references") else "",
                location="Remote (proactive)",
                description=strip_html(a.get("content") or "")[:600],
                posted_at=parse_when(a.get("published_at"))))
    except Exception as exc:
        LOG.warning("messari failed: %s", exc)
    return out

def collect_signals(con) -> list[Job]:
    jobs = defillama_raises(con) + edgar_form_d(con) + messari_intel(con)
    for j in jobs:
        con.execute("INSERT OR IGNORE INTO companies(name, added_by, created_at) VALUES(?,?,?)",
                    (j.company, "signal", time.time()))
    return jobs
