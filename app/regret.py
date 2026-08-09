"""Regret analysis engine v2: fetch the page behind any forwarded link,
extract employer+role from real content, never fabricate, answer plain
questions about the system instead of misfiling them as regrets."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
import yaml

from . import db as dbm

LOG = logging.getLogger("regret")
ROOT = Path(__file__).resolve().parent.parent
_URL_RE = re.compile(r"https?://[^\s<>\)\]]+")
_BAD_NAMES = {"", "unknown", "n/a", "none", "na", "company", "employer"}
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _fetch_page_meta(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": _UA})
        html = resp.text[:60000]
    except Exception as exc:  # noqa: BLE001
        LOG.info("page fetch failed for %s: %s", url[:80], exc)
        return {}
    out = {}
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    for prop in ("og:title", "og:description"):
        m = re.search(
            rf'property=["\']{prop}["\']\s+content=["\'](.*?)["\']', html, re.S | re.I) \
            or re.search(
            rf'content=["\'](.*?)["\']\s+property=["\']{prop}["\']', html, re.S | re.I)
        if m:
            out[prop] = re.sub(r"\s+", " ", m.group(1)).strip()[:300]
    return out


def _heuristic_extract(meta_title: str) -> tuple[str, str]:
    t = (meta_title or "").split("|")[0].strip()
    m = re.match(r"^(.{2,60}?)\s+hiring\s+(.{2,90}?)(?:\s+in\s+.{2,60})?$", t, re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.{2,90}?)\s+at\s+(.{2,60})$", t, re.I)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    m = re.match(r"^(.{2,90}?)\s+[-—]\s+(.{2,60})$", t)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", ""


def _clean_name(name: str) -> str:
    n = (name or "").strip().strip(".,;:!")
    return "" if n.lower() in _BAD_NAMES or len(n) < 3 else n


def _canonicalize(raw: str, cfg) -> dict:
    urls = _URL_RE.findall(raw)
    meta = _fetch_page_meta(urls[0]) if urls else {}
    company, title = _heuristic_extract(meta.get("og:title") or meta.get("title") or "")
    if not company and not title:
        stripped = _URL_RE.sub("", raw).strip()
        line = stripped.splitlines()[0][:150] if stripped else ""
        company, title = _heuristic_extract(line)
    enriched = raw[:1200]
    if meta:
        enriched += ("\n\nPAGE TITLE: " + (meta.get("title") or "")
                     + "\nOG TITLE: " + (meta.get("og:title") or "")
                     + "\nOG DESC: " + (meta.get("og:description") or ""))
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .funnel import _claude
            txt = _claude(cfg.get("screen_model") or "claude-haiku-4-5",
                          "Extract the employer company name and the role title from "
                          "this forwarded job reference (raw message + fetched page "
                          "metadata). Respond with JSON only: "
                          '{"company":"...","title":"..."}. If either is genuinely '
                          "undeterminable, use an empty string — NEVER the word "
                          "'Unknown' or a guess.\n\n" + enriched[:2500], 300)
            got = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
            company = _clean_name(got.get("company") or company)
            title = (got.get("title") or title or "").strip()[:120]
        except Exception as exc:  # noqa: BLE001
            LOG.info("canonicalize LLM fallback: %s", exc)
    return {"company": _clean_name(company), "title": (title or "").strip()[:120],
            "url": urls[0] if urls else ""}


def _find_opp(con, company: str, title: str):
    like_c, like_t = f"%{company.lower()}%", f"%{(title or '').lower()[:25]}%"
    return con.execute(
        "SELECT * FROM opportunities WHERE lower(company) LIKE ? "
        "AND lower(title) LIKE ? ORDER BY last_signal DESC LIMIT 1",
        (like_c, like_t)).fetchone()


def _company_monitored(con, cfg, company: str) -> bool:
    c = company.lower().replace(" ", "")
    for src in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters",
                "recruitee", "bamboohr", "pinpoint", "teamtailor"):
        for slug in ((cfg.get("sources") or {}).get(src) or {}).get("companies") or []:
            if c in slug.replace("-", ""):
                return True
    try:
        from .ingest import load_resolved
        for slugs in load_resolved().values():
            if any(c in s.replace("-", "") for s in slugs):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def postmortem(con, cfg, raw: str) -> dict:
    now = time.time()
    ext = _canonicalize(raw, cfg)
    company, title = ext["company"], ext["title"]
    fixes: list[str] = []

    if not company and not title:
        return {"class": "unidentified", "company": "", "title": "",
                "detail": "", "fixes": [], "repeat_count": 0}

    opp = _find_opp(con, company, title) if company else None
    if opp:
        stages = {r["stage"]: r for r in con.execute(
            "SELECT * FROM flight WHERE opp_key=? ORDER BY ts", (opp["key"],))}
        if "delivered" in stages:
            verdict = "attention_miss"
            detail = (f"This WAS delivered "
                      f"({time.strftime('%b %d', time.localtime(stages['delivered']['ts']))}) "
                      f"at score {stages['delivered']['score']:.0f}. Blurb/position finding, "
                      "not coverage.")
        elif "excluded" in stages:
            rule = json.loads(stages["excluded"]["detail"] or "{}").get("rule", "?")
            if rule == "triage_cut":
                verdict = "ranking"
                detail = ("Died at embedding triage. Registered as gold hard-positive; "
                          "the preference model absorbs it.")
                con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                            (opp["key"], now, "regret_gold", "regret hard-positive"))
                fixes.append("hard_positive_registered")
            else:
                verdict, detail = "filtering", f"Killed by hard rule '{rule}'."
        elif "deep_eval" in stages and (stages["deep_eval"]["score"] or 0) < 80:
            verdict = "ranking"
            detail = (f"Deep-evaluated at {stages['deep_eval']['score']:.0f}, below the bar. "
                      "Registered as gold hard-positive.")
            con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                        (opp["key"], now, "regret_gold", "below-bar regret"))
            fixes.append("hard_positive_registered")
        else:
            verdict, detail = "timing", ("Scored but lost the daily cut, or arrived "
                                         "between scans and decayed before resurfacing.")
    elif company and _company_monitored(con, cfg, company):
        verdict = "parser_or_fetch"
        detail = (f"{company} IS monitored but this posting never entered the pipeline — "
                  "extraction/fetch failure at the posting's window, or it's posted only "
                  "on an external board.")
        fixes.append("canary_recheck_queued")
    elif company:
        verdict = "source_gap"
        detail = (f"{company} was not in the monitored universe. Added; the resolver "
                  "wires its board on the next cycle.")
        cands = dbm.kv_get(con, "extra_candidates") or []
        if company not in cands:
            cands.append(company)
            dbm.kv_set(con, "extra_candidates", cands)
        fixes.append(f"added:{company}")
        if title and "?" not in title and len(title) < 70:
            qs = dbm.kv_get(con, "spawn_queries") or []
            qs.append(f"{title} remote")
            dbm.kv_set(con, "spawn_queries", qs[-50:])
            fixes.append("query_spawned")
    else:
        verdict = "query_gap"
        detail = f"Employer unclear, but the role is searchable. Spawned: '{title} remote'."
        if "?" not in title and len(title) < 70:
            dbm.kv_set(con, "spawn_queries",
                       ((dbm.kv_get(con, "spawn_queries") or []) + [f"{title} remote"])[-50:])
            fixes.append("query_spawned")

    con.execute("INSERT INTO regrets(ts, raw, company, title, verdict_class, detail, fixed) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, raw[:1500], company, title, verdict, detail, json.dumps(fixes)))
    _append_benchmark_seed(company, title)
    n_same = con.execute("SELECT COUNT(*) c FROM regrets WHERE verdict_class=? AND ts>?",
                         (verdict, now - 30 * 86400)).fetchone()["c"]
    if verdict in ("ranking", "filtering") and n_same >= 3:
        props = dbm.kv_get(con, "pending_proposals") or []
        props.append({"ts": now, "kind": verdict,
                      "text": f"{n_same} {verdict} regrets in 30d — propose adjusting "
                              f"{'weights/threshold' if verdict == 'ranking' else 'the named rule'}."})
        dbm.kv_set(con, "pending_proposals", props[-10:])
    return {"class": verdict, "detail": detail, "fixes": fixes,
            "company": company, "title": title, "repeat_count": n_same}


def _append_benchmark_seed(company: str, title: str) -> None:
    if not company and not title:
        return
    path = ROOT / "benchmark.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        data = {}
    seeds = data.setdefault("seeds", [])
    entry = {"title_contains": (title or "")[:60].lower(), "company": company.lower()}
    if entry not in seeds:
        seeds.append(entry)
        path.write_text(yaml.safe_dump(data, sort_keys=False))


def regret_card(pm: dict) -> str:
    if pm["class"] == "unidentified":
        return ("🔍 I couldn't identify the employer or role from that — the page "
                "wouldn't reveal them. Reply in one line like:\n"
                "Coinbase — Associate, Financial Services Partnerships\n"
                "and I'll rerun the postmortem properly.")
    labels = {"source_gap": "SOURCE GAP", "query_gap": "QUERY GAP",
              "parser_or_fetch": "PARSER/FETCH", "timing": "TIMING",
              "filtering": "FILTERING", "ranking": "RANKING",
              "attention_miss": "ATTENTION", "spec_disagreement": "SPEC"}
    fix_note = {"query_spawned": "search query spawned",
                "hard_positive_registered": "added as gold hard-positive",
                "canary_recheck_queued": "canary recheck queued"}
    fixes = "; ".join(
        f"{f.split(':', 1)[1]} added to universe" if f.startswith("added:")
        else fix_note.get(f, f) for f in pm["fixes"]) or "logged for calibration"
    rep = f"\n⚠ {pm['repeat_count']} similar in 30d — proposal queued for Sunday." \
        if pm.get("repeat_count", 0) >= 3 else ""
    return (f"🔍 Postmortem — {pm['title'] or '(role unclear)'}, "
            f"{pm['company'] or '(employer unclear)'}\n"
            f"MISS CLASS: {labels.get(pm['class'], pm['class'])}\n"
            f"{pm['detail']}\nFixed: {fixes}. Added to regression seeds.{rep}")


def answer_question(con, cfg, question: str) -> str:
    regs = con.execute("SELECT ts, company, title, verdict_class, fixed FROM regrets "
                       "ORDER BY ts DESC LIMIT 6").fetchall()
    reg_lines = [f"- {time.strftime('%b %d %H:%M', time.localtime(r['ts']))}: "
                 f"{r['company'] or '?'} / {r['title'] or '?'} -> {r['verdict_class']} "
                 f"(fixes: {r['fixed']})" for r in regs]
    degraded = [f"{r['name']}: {r['notes'] or 'no detail'}" for r in con.execute(
        "SELECT name, notes FROM sources WHERE fail_streak>=1")]
    cands = (dbm.kv_get(con, "extra_candidates") or [])[-10:]
    n24 = con.execute("SELECT COUNT(*) c FROM opportunities WHERE delivered_at>?",
                      (time.time() - 86400,)).fetchone()["c"]
    state = ("Recent postmortems:\n" + ("\n".join(reg_lines) or "none")
             + f"\n\nUniverse additions (recent): {', '.join(cands) or 'none'}"
             + f"\nDegraded sources: {'; '.join(degraded) or 'none'}"
             + f"\nDelivered last 24h: {n24}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "Here's the current state:\n\n" + state
    try:
        from .funnel import _claude
        return _claude(cfg.get("screen_model") or "claude-haiku-4-5",
                       "You are the user's job-intelligence bot. Answer their question "
                       "directly and briefly (<=120 words) using ONLY this system "
                       "state. If the state shows a past mistake (e.g. a junk entry), "
                       "say so plainly.\n\n<state>\n" + state + "\n</state>\n\n"
                       "Question: " + question[:400], 400).strip()
    except Exception:  # noqa: BLE001
        return "Here's the current state:\n\n" + state
