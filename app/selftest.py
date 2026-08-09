"""Offline end-to-end: sample jobs -> funnel (mocked LLM) -> selection ->
cards -> verdict -> regret postmortem. No network, no keys."""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone
from . import db as dbm, funnel, regret
from .ingest import _sample_jobs
from .bot import _card_text

def main() -> int:
    os.environ.pop("ANTHROPIC_API_KEY", None)
    dbm.DB_PATH.unlink(missing_ok=True)
    con = dbm.connect()
    now = datetime.now(timezone.utc)
    jobs = _sample_jobs(now)
    for j in jobs:
        dbm.upsert_opportunity(con, j)
    cfg = {"profile": "crypto BD research VC PE fractional low-touch remote",
           "max_age_days": 7, "require_remote": True,
           "exclude_keywords": ["developer"], "include_keywords": []}
    kept = funnel.hard_constraints(con, jobs, cfg, now)
    tri = funnel.triage(con, kept, cfg)
    ev = funnel.deep_eval(con, tri, cfg)          # keyless -> WATCH@55 path
    for r in ev[:2]:
        r["score"] = 90.0
        dbm.record(con, r["job"].key, "deep_eval", 90.0, {"dims": {"fit": 92}})
    picked, below = funnel.select_daily(con, ev, time.time())
    assert picked and all(p["final"] >= funnel.BAR for p in picked)
    assert below, "below-bar tier must fill when fewer than 10 clear"
    assert all(b["final"] < funnel.BAR for b in below)
    import html as _html
    card = _card_text(1, picked[0])
    assert _html.escape(picked[0]["job"].title) in card
    assert 'href=' in card  # the title itself carries the link
    con.execute("INSERT INTO verdicts(opp_key,ts,verdict,note) VALUES(?,?,?,?)",
                (picked[0]["job"].key, time.time(), "apply", ""))
    pm = regret.postmortem(con, cfg, "Head of Ecosystem at Berachain https://x/y")
    assert pm["class"] in ("source_gap", "query_gap")
    pm2 = regret.postmortem(con, cfg, f"{picked[0]['job'].title} at {picked[0]['job'].company}")
    assert pm2["class"] == "attention_miss", pm2
    flights = con.execute("SELECT COUNT(*) c FROM flight").fetchone()["c"]
    print(f"selftest PASS — {len(jobs)} jobs, {len(picked)} delivered, near-miss shown, "
          f"regret classes: {pm['class']}/{pm2['class']}, flight rows: {flights}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
