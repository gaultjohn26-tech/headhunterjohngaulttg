"""Runtime: one always-on process = Telegram bot + scheduled pipeline.
Deploy from GitHub to any small worker host (Railway/Render/Fly)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import time
from zoneinfo import ZoneInfo

import requests
import yaml

from . import db as dbm
from . import funnel, ingest, planner, signals
from .bot import Bot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("main")
try:
    NY = ZoneInfo("America/New_York")
except Exception:
    import datetime as _dt
    NY = _dt.timezone(_dt.timedelta(hours=-5), "ET")
SCAN_EVERY_H = 3
FLASH_BAR = 92
ROOT = dbm.DB_PATH.parent.parent


def load_cfg() -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    spec_p = ROOT / "search_spec.yaml"
    if spec_p.exists():
        spec = yaml.safe_load(spec_p.read_text()) or {}
        cfg["profile"] = spec.get("profile") or cfg.get("profile")
        cfg["scoring_weights"] = spec.get("scoring_weights") or cfg.get("scoring_weights")
    return cfg


class _Opp:
    """Lightweight rehydrated opportunity for delivery-time selection."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Pipeline:
    def __init__(self, con, cfg):
        self.con, self.cfg = con, cfg

    # ------------------------------------------------------------ scan cycle
    def _apply_planner_and_pacing(self) -> dict:
        cfg = json.loads(json.dumps(self.cfg))  # deep copy per cycle
        src = cfg.setdefault("sources", {})
        budgets = {"jsearch": int((src.get("jsearch") or {}).get("daily_request_budget") or 100),
                   "adzuna": 25, "jooble": 15}
        for name, budget in budgets.items():
            qs = planner.live_queries(self.con, name, budget)
            if qs:
                src.setdefault(name, {})["searches"] = qs
        ts = src.get("theirstack") or {}
        if ts.get("enabled"):
            plan = int(ts.get("plan_credits_month") or 30000)
            today = dt.date.today()
            days_left = max(1, (dt.date(today.year + (today.month == 12),
                                        (today.month % 12) + 1, 1) - today).days)
            spent = dbm.kv_get(self.con, f"ts_spent_{today:%Y%m}") or 0
            ts["daily_record_limit"] = max(0, (plan - spent) // days_left)
        # regret-driven universe additions feed the resolver candidates
        extra = dbm.kv_get(self.con, "extra_candidates") or []
        cfg["watchlist_candidates"] = list({*(cfg.get("watchlist_candidates") or []), *extra})
        # NL prefs ride along in the profile context immediately
        prefs = dbm.kv_get(self.con, "nl_prefs") or []
        if prefs:
            cfg["profile"] = (cfg.get("profile") or "") + "\n\nRecent standing " \
                "instructions from the candidate (obey):\n" + \
                "\n".join("- " + p["text"] for p in prefs[-10:])
        return cfg

    def _update_canaries(self, health: dict) -> None:
        now = time.time()
        for name, count in health.get("ok") or []:
            self.con.execute(
                "INSERT INTO sources(name,last_ok,last_count,fail_streak,total_items) "
                "VALUES(?,?,?,0,?) ON CONFLICT(name) DO UPDATE SET last_ok=?, last_count=?, "
                "fail_streak=0, total_items=total_items+?",
                (name, now, count, count, now, count, count))
        for name in health.get("failed") or []:
            self.con.execute(
                "INSERT INTO sources(name,fail_streak) VALUES(?,1) "
                "ON CONFLICT(name) DO UPDATE SET fail_streak=fail_streak+1", (name,))

    def scan(self) -> dict:
        cfg = self._apply_planner_and_pacing()
        now = dt.datetime.now(dt.timezone.utc)
        jobs, health = ingest.collect_jobs(cfg)
        jobs += signals.collect_signals(self.con)
        self._update_canaries(health)
        if any(n == "theirstack" for n, _ in health.get("ok") or []):
            got = next(c for n, c in health["ok"] if n == "theirstack")
            k = f"ts_spent_{dt.date.today():%Y%m}"
            dbm.kv_set(self.con, k, (dbm.kv_get(self.con, k) or 0) + got)
        for j in jobs:
            dbm.upsert_opportunity(self.con, j)
        survivors = funnel.hard_constraints(self.con, jobs, cfg, now)
        kept = funnel.triage(self.con, survivors, cfg)
        screened = funnel.screen(self.con, kept, cfg)
        evaluated = funnel.deep_eval(self.con, screened, cfg)
        # earliest-wins query credit
        for j in kept:
            base = j.source.split(":")[0].split(" ")[0]
            if base in ("jsearch", "adzuna", "jooble"):
                planner.credit_query(self.con, base, "", 0)  # coarse v1 credit
        self.con.commit()
        return {"scanned": len(jobs), "evaluated": evaluated, "health": health}

    async def scan_and_maybe_flash(self, bot: Bot) -> dict:
        stats = await asyncio.to_thread(self.scan)
        flashed = 0
        now_ts = time.time()
        sent_today = dbm.kv_get(self.con, "flashed_today") or {"d": "", "n": 0}
        today = dt.date.today().isoformat()
        if sent_today["d"] != today:
            sent_today = {"d": today, "n": 0}
        for rec in stats["evaluated"]:
            rec["final"] = funnel.confidence_adjust(self.con, rec, now_ts)
            if rec["final"] >= FLASH_BAR and sent_today["n"] < 3:
                chat = dbm.kv_get(self.con, "chat_id")
                if chat:
                    from .bot import _card_text, _card_kb
                    await bot.app.bot.send_message(
                        chat, "🚨 Exceptional find:\n" + _card_text(0, rec)
                        .replace("<b>0. ", "<b>"), parse_mode="HTML",
                        reply_markup=_card_kb(rec["job"].key))
                    await bot.app.bot.send_message(chat, rec["job"].url)
                    dbm.record(self.con, rec["job"].key, "delivered", rec["final"])
                    self.con.execute("UPDATE opportunities SET status='delivered', "
                                     "delivered_at=? WHERE key=?", (now_ts, rec["job"].key))
                    sent_today["n"] += 1
                    flashed += 1
        dbm.kv_set(self.con, "flashed_today", sent_today)
        self.con.commit()
        stats["flashed"] = flashed
        return stats

    # ------------------------------------------------------------ daily drop
    def select_daily(self) -> tuple[list[dict], dict | None, dict]:
        cutoff = time.time() - 24 * 3600
        rows = self.con.execute(
            "SELECT o.*, f.score s, f.detail d FROM opportunities o JOIN flight f "
            "ON f.opp_key=o.key AND f.stage='deep_eval' AND f.ts>? "
            "WHERE o.status!='delivered' ORDER BY f.score DESC LIMIT 120",
            (cutoff,)).fetchall()
        evaluated = []
        for r in rows:
            det = json.loads(r["d"] or "{}")
            j = _Opp(key=r["key"], source=r["source"], url=r["url"], title=r["title"],
                     company=r["company"], description=r["description"] or "",
                     location=r["location"] or "", salary=r["salary"] or "")
            evaluated.append({"job": j, "score": r["s"], "dims": det.get("dims") or {},
                              "verdict": det.get("verdict") or "WATCH",
                              "blurb": (r["description"] or r["title"])[:200],
                              "risk": ""})
        bar_delta = dbm.kv_get(self.con, "bar_delta") or 0
        funnel.BAR = 80.0 + bar_delta
        picked, near = funnel.select_daily(self.con, evaluated, time.time())
        self.con.commit()
        scanned = self.con.execute("SELECT COUNT(*) c FROM flight WHERE stage='triage' "
                                   "AND ts>?", (cutoff,)).fetchone()["c"]
        health = {"failed": [r["name"] for r in self.con.execute(
            "SELECT name FROM sources WHERE fail_streak>=2")]}
        return picked, near, {"scanned": scanned, "health": health}

    # ------------------------------------------------------------ sunday
    def sunday_brief(self) -> dict:
        wk = time.time() - 7 * 86400
        delivered = self.con.execute("SELECT COUNT(*) c FROM opportunities WHERE "
                                     "delivered_at>?", (wk,)).fetchone()["c"]
        pursued = self.con.execute("SELECT COUNT(*) c FROM verdicts WHERE ts>? AND "
                                   "verdict IN ('apply','outreach','intro')", (wk,)).fetchone()["c"]
        hard_neg = self.con.execute("SELECT COUNT(*) c FROM verdicts WHERE ts>? AND "
                                    "verdict LIKE 'hide%'", (wk,)).fetchone()["c"]
        regs = self.con.execute("SELECT verdict_class, COUNT(*) c FROM regrets WHERE ts>? "
                                "GROUP BY verdict_class", (wk,)).fetchall()
        tops = self.con.execute("SELECT name, total_items FROM sources ORDER BY "
                                "pursue_weighted DESC, total_items DESC LIMIT 4").fetchall()
        return {"delivered": delivered, "pursued": pursued,
                "pursue_rate": pursued / delivered if delivered else 0.0,
                "hard_neg_rate": hard_neg / delivered if delivered else 0.0,
                "regrets": sum(r["c"] for r in regs),
                "regret_mix": ", ".join(f"{r['verdict_class']}×{r['c']}" for r in regs) or "none",
                "top_sources": ", ".join(r["name"] for r in tops),
                "degraded": ", ".join(r["name"] for r in self.con.execute(
                    "SELECT name FROM sources WHERE fail_streak>=2")),
                "proposals": dbm.kv_get(self.con, "pending_proposals") or []}


async def run():
    con = dbm.connect()
    cfg = load_cfg()
    planner.ensure_seeded(con)
    con.commit()
    pipe = Pipeline(con, cfg)
    bot = Bot(con, cfg, pipe)

    async def loop():
        last_scan = 0.0
        while True:
            now = dt.datetime.now(NY)
            try:
                if time.time() - last_scan > SCAN_EVERY_H * 3600:
                    last_scan = time.time()
                    await pipe.scan_and_maybe_flash(bot)
                today = now.date().isoformat()
                if now.hour == 7 and dbm.kv_get(con, "dropped") != today:
                    picked, near, stats = pipe.select_daily()
                    await bot.send_daily(picked, near, stats)
                    dbm.kv_set(con, "dropped", today); con.commit()
                    ping = os.environ.get("HEALTHCHECK_PING_URL")
                    if ping:
                        try:
                            requests.get(ping, timeout=10)
                        except Exception:  # noqa: BLE001
                            pass
                if now.weekday() == 6 and now.hour == 17 and \
                        dbm.kv_get(con, "sunday") != today:
                    await bot.send_sunday(pipe.sunday_brief())
                    rot = planner.weekly_rotate(con)
                    try:
                        await asyncio.to_thread(ingest.resolve_watchlist,
                                               pipe._apply_planner_and_pacing())
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("resolver failed: %s", exc)
                    dbm.kv_set(con, "sunday", today); con.commit()
                    LOG.info("weekly rotation: %s", rot)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("loop error: %s", exc)
            await asyncio.sleep(120)

    async with bot.app:
        await bot.app.updater.start_polling()
        await bot.app.start()
        LOG.info("bot polling; pipeline loop running")
        await loop()


if __name__ == "__main__":
    asyncio.run(run())
