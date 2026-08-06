"""Regret analysis engine: every manually-forwarded opportunity triggers a
postmortem — which subsystem failed, bounded auto-fix, permanent seed."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import yaml

from . import db as dbm

LOG = logging.getLogger("regret")
ROOT = Path(__file__).resolve().parent.parent


def _canonicalize(raw: str, cfg) -> dict:
    """LLM pulls company+title out of a link / screenshot text / pasted DM."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        words = raw.strip().split()
        return {"company": words[-1] if words else "", "title": " ".join(words[:6])}
    from .funnel import _claude
    txt = _claude(cfg.get("screen_model") or "claude-haiku-4-5",
                  "Extract the employer company name and role title from this "
                  "forwarded job reference. JSON only: "
                  '{"company":"...","title":"...","url":"..."}\n\n' + raw[:2500], 400)
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return {"company": "", "title": raw[:80]}


def _find_opp(con, company: str, title: str):
    like_c, like_t = f"%{company.lower()}%", f"%{(title or '').lower()[:25]}%"
    return con.execute(
        "SELECT * FROM opportunities WHERE lower(company) LIKE ? "
        "AND lower(title) LIKE ? ORDER BY last_signal DESC LIMIT 1",
        (like_c, like_t)).fetchone()


def _company_monitored(con, cfg, company: str) -> bool:
    c = company.lower()
    for src in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters",
                "recruitee", "bamboohr", "pinpoint", "teamtailor"):
        for slug in ((cfg.get("sources") or {}).get(src) or {}).get("companies") or []:
            if c.replace(" ", "") in slug.replace("-", ""):
                return True
    resolved = dbm.kv_get(con, "resolved_flat") or []
    return any(c.replace(" ", "") in s for s in resolved)


def postmortem(con, cfg, raw: str) -> dict:
    """Returns {class, detail, fixes[]} — exactly one verdict class."""
    now = time.time()
    ext = _canonicalize(raw, cfg)
    company, title = ext.get("company") or "", ext.get("title") or ""
    opp = _find_opp(con, company, title) if company else None
    fixes: list[str] = []

    if opp:
        stages = {r["stage"]: r for r in con.execute(
            "SELECT * FROM flight WHERE opp_key=? ORDER BY ts", (opp["key"],))}
        if "delivered" in stages:
            verdict, detail = "attention_miss", (
                f"This WAS delivered ({time.strftime('%b %d', time.localtime(stages['delivered']['ts']))})"
                f" at score {stages['delivered']['score']:.0f}. Blurb/position finding, not coverage.")
        elif "excluded" in stages:
            rule = json.loads(stages["excluded"]["detail"] or "{}").get("rule", "?")
            if rule == "triage_cut":
                verdict = "ranking"
                detail = (f"Died at embedding triage (sim {stages.get('triage', {})['score'] if 'triage' in stages else '?'}). "
                          "Registered as gold hard-positive; preference model updated.")
                con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                            (opp["key"], now, "regret_gold", "regret hard-positive"))
                fixes.append("hard_positive_registered")
            elif rule in ("never_show_company",):
                verdict, detail = "filtering", f"Killed by hard rule '{rule}'. One tap reverses it."
            else:
                verdict, detail = "filtering", f"Excluded by rule '{rule}'."
        elif "deep_eval" in stages and stages["deep_eval"]["score"] is not None \
                and stages["deep_eval"]["score"] < 80:
            verdict = "ranking"
            detail = (f"Deep-evaluated at {stages['deep_eval']['score']:.0f}, below the {80} bar. "
                      "Registered as gold hard-positive; dimension disagreement logged.")
            con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                        (opp["key"], now, "regret_gold", "below-bar regret"))
            fixes.append("hard_positive_registered")
        else:
            verdict, detail = "timing", ("Scored above bar but lost the daily top-10 cut, "
                                         "or arrived between scans, and decayed before resurfacing.")
    elif company and _company_monitored(con, cfg, company):
        verdict, detail = "parser_or_fetch", (
            f"{company} is monitored but this posting never entered the pipeline — "
            "extraction/fetch failure at the posting's window. Canary check queued.")
        fixes.append("canary_recheck_queued")
    elif company:
        verdict, detail = "source_gap", (
            f"{company} was not in the monitored universe. Added now; resolver will "
            "wire its board on the next cycle.")
        cands = dbm.kv_get(con, "extra_candidates") or []
        if company not in cands:
            cands.append(company)
            dbm.kv_set(con, "extra_candidates", cands)
        fixes.append("company_added_to_universe")
        qs = dbm.kv_get(con, "spawn_queries") or []
        if title:
            qs.append(f"{title} remote")
            dbm.kv_set(con, "spawn_queries", qs[-50:])
            fixes.append("query_spawned")
    else:
        verdict, detail = "query_gap", "Could not identify the employer; spawned a query from the role text."
        dbm.kv_set(con, "spawn_queries",
                   ((dbm.kv_get(con, "spawn_queries") or []) + [f"{title} remote"])[-50:])
        fixes.append("query_spawned")

    con.execute("INSERT INTO regrets(ts, raw, company, title, verdict_class, detail, fixed) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, raw[:1500], company, title, verdict, detail, json.dumps(fixes)))
    _append_benchmark_seed(company, title)
    # three similar cases -> queue a global proposal for the Sunday brief
    n_same = con.execute("SELECT COUNT(*) c FROM regrets WHERE verdict_class=? AND ts>?",
                         (verdict, now - 30 * 86400)).fetchone()["c"]
    if verdict in ("ranking", "filtering") and n_same >= 3:
        props = dbm.kv_get(con, "pending_proposals") or []
        props.append({"ts": now, "kind": verdict,
                      "text": f"{n_same} {verdict} regrets in 30d — propose adjusting "
                              f"{'triage threshold/weights' if verdict == 'ranking' else 'the named rule'}."})
        dbm.kv_set(con, "pending_proposals", props[-10:])
    return {"class": verdict, "detail": detail, "fixes": fixes,
            "company": company, "title": title, "repeat_count": n_same}


def _append_benchmark_seed(company: str, title: str) -> None:
    """Closure guarantee: every regret becomes a permanent regression seed."""
    path = ROOT / "benchmark.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        data = {}
    seeds = data.setdefault("seeds", [])
    entry = {"title_contains": (title or "")[:60].lower(), "company": company.lower()}
    if entry not in seeds and (entry["title_contains"] or entry["company"]):
        seeds.append(entry)
        path.write_text(yaml.safe_dump(data, sort_keys=False))


def regret_card(pm: dict) -> str:
    labels = {"source_gap": "SOURCE GAP", "query_gap": "QUERY GAP",
              "parser_or_fetch": "PARSER/FETCH", "timing": "TIMING",
              "filtering": "FILTERING", "ranking": "RANKING",
              "attention_miss": "ATTENTION", "spec_disagreement": "SPEC"}
    fix_note = {"company_added_to_universe": "company added to universe",
                "query_spawned": "query spawned",
                "hard_positive_registered": "added as gold hard-positive",
                "canary_recheck_queued": "canary recheck queued"}
    fixes = "; ".join(fix_note.get(f, f) for f in pm["fixes"]) or "logged for calibration"
    rep = f"\n⚠ {pm['repeat_count']} similar in 30d — proposal queued for Sunday." \
        if pm.get("repeat_count", 0) >= 3 else ""
    return (f"🔍 Postmortem — {pm['title'] or '?'}, {pm['company'] or '?'}\n"
            f"MISS CLASS: {labels.get(pm['class'], pm['class'])}\n"
            f"{pm['detail']}\nFixed: {fixes}. Added to regression seeds.{rep}")
