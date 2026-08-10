cd /opt/headhunter && cat > app/planner.py << '__EOF__'
"""Dynamic query planner: generates the query space from the spec taxonomy,
tracks unique-or-earliest yield per query, retires only on 21 days of zero
yield (reversible), spawns exploration + regret-driven queries weekly."""
from __future__ import annotations
import itertools, random, time
from . import db as dbm

ROLES = ["business development", "partnerships", "ecosystem", "corporate development",
         "strategic partnerships", "chief of staff", "founder's associate", "operator",
         "research analyst", "market research", "macro research", "crypto research",
         "investment analyst", "venture capital analyst", "venture capital associate",
         "private equity associate", "growth equity", "investor relations",
         "fund operations", "portfolio operations", "platform", "capital formation",
         "due diligence", "treasury", "strategy", "special projects", "advisor"]
SECTORS = ["crypto", "web3", "digital assets", "defi", "stablecoin", "blockchain",
           "AI", "venture capital", "private equity", "family office", "fintech",
           "venture studio", "incubator", "fund", ""]

def ensure_seeded(con) -> int:
    """Versioned seeding: v1 = remote-phrased pool; v2 adds the NYC lane
    (query text decides per-query remote filtering downstream)."""
    ver = int(dbm.kv_get(con, "planner_seed_v") or (1 if dbm.kv_get(con, "planner_seeded") else 0))
    now, n = time.time(), 0
    if ver < 1:
        combos = [f"{s} {r} remote".strip() for r, s in itertools.product(ROLES, SECTORS)]
        random.shuffle(combos)
        for q in combos:
            for src in ("jsearch", "adzuna", "jooble"):
                con.execute("INSERT OR IGNORE INTO queries(source,text,born,state) "
                            "VALUES(?,?,?,'live')", (src, q, now))
                n += 1
    if ver < 2:
        nyc = [f"{s} {r} new york".strip() for r, s in itertools.product(ROLES, SECTORS)]
        random.shuffle(nyc)
        for q in nyc:
            for src in ("jsearch", "adzuna", "jooble"):
                con.execute("INSERT OR IGNORE INTO queries(source,text,born,state) "
                            "VALUES(?,?,?,'live')", (src, q, now))
                n += 1
    dbm.kv_set(con, "planner_seed_v", 2)
    dbm.kv_set(con, "planner_seeded", True)
    return n

def live_queries(con, source: str, budget: int) -> list[str]:
    """Proven queries get priority seats; the rest of the budget is a
    circular window over the whole pool, advancing every call — so a day's
    scans sweep different queries instead of repeating one slice."""
    rows = con.execute("SELECT text, unique_earliest FROM queries "
                       "WHERE source=? AND state='live'", (source,)).fetchall()
    proven = [r["text"] for r in rows if r["unique_earliest"] > 0]
    rest = [r["text"] for r in rows if r["unique_earliest"] == 0]
    head = proven[: max(1, budget // 5)] if proven else []
    room = max(0, budget - len(head))
    picked = list(head)
    if rest and room:
        cur = int(dbm.kv_get(con, f"qcursor_{source}") or 0) % len(rest)
        for i in range(min(room, len(rest))):
            picked.append(rest[(cur + i) % len(rest)])
        dbm.kv_set(con, f"qcursor_{source}", (cur + room) % len(rest))
    return picked

def credit_query(con, source: str, text: str, unique_earliest: int) -> None:
    con.execute("UPDATE queries SET last_yield=?, unique_earliest=unique_earliest+? "
                "WHERE source=? AND text=?", (time.time(), unique_earliest, source, text))

def weekly_rotate(con) -> dict:
    now = time.time(); cutoff = now - 21 * 86400
    retired = 0
    if dbm.kv_get(con, "per_query_credit_live"):  # evidence rule: no retirement
        retired = con.execute("UPDATE queries SET state='retired' WHERE state='live' "
                              "AND unique_earliest=0 AND born<? AND (last_yield IS NULL OR last_yield<?)",
                              (cutoff, cutoff)).rowcount
    spawned = 0
    for q in (dbm.kv_get(con, "spawn_queries") or []):
        for src in ("jsearch", "adzuna", "jooble"):
            spawned += con.execute("INSERT OR IGNORE INTO queries(source,text,born,state) "
                                   "VALUES(?,?,?,'live')", (src, q, now)).rowcount
    dbm.kv_set(con, "spawn_queries", [])
    for _ in range(5):  # standing exploration budget
        q = f"{random.choice(SECTORS)} {random.choice(ROLES)} remote".strip()
        for src in ("jsearch",):
            spawned += con.execute("INSERT OR IGNORE INTO queries(source,text,born,state) "
                                   "VALUES(?,?,?,'live')", (src, q, now)).rowcount
    return {"retired": retired, "spawned": spawned}
__EOF__
cat > app/main.py << '__EOF__'
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
from . import VERSION
from .bot import Bot

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("main")
try:
    NY = ZoneInfo("America/New_York")
except Exception:  # tz database missing on minimal images — approximate ET
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


def _trim(text: str, n: int = 200) -> str:
    t = (text or "").strip()
    if len(t) <= n:
        return t
    return t[:n].rsplit(" ", 1)[0] + "…"


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
        daily = {"jsearch": int((src.get("jsearch") or {}).get("daily_request_budget") or 250),
                 "adzuna": 50, "jooble": 25}
        today = dt.date.today().isoformat()
        for name, day_budget in daily.items():
            k = f"spent_{name}_{today}"
            spent = int(dbm.kv_get(self.con, k) or 0)
            per_scan = max(1, day_budget // (24 // SCAN_EVERY_H))
            allowance = min(per_scan, max(0, day_budget - spent))
            if allowance <= 0:
                src.setdefault(name, {})["searches"] = []
                LOG.info("%s: daily budget %d spent — resting until tomorrow",
                         name, day_budget)
                continue
            qs = planner.live_queries(self.con, name, allowance)
            if qs:
                src.setdefault(name, {})["searches"] = qs
                dbm.kv_set(self.con, k, spent + len(qs))
        ts = src.get("theirstack") or {}
        if ts.get("enabled"):
            plan = int(ts.get("plan_credits_month") or 30000)
            today = dt.date.today()
            days_left = max(1, (dt.date(today.year + (today.month == 12),
                                        (today.month % 12) + 1, 1) - today).days)
            spent = dbm.kv_get(self.con, f"ts_spent_{today:%Y%m}") or 0
            daily_target = max(0, (plan - spent) // days_left)
            day_key = f"ts_day_{today.isoformat()}"
            day_spent = int(dbm.kv_get(self.con, day_key) or 0)
            per_scan = max(1, daily_target // (24 // SCAN_EVERY_H))
            allowance = min(per_scan, max(0, daily_target - day_spent))
            if allowance <= 0:
                ts["enabled"] = False  # today's credit slice is spent — rest
                LOG.info("theirstack: daily credit target %d reached", daily_target)
            ts["daily_record_limit"] = max(1, allowance)
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
            note = (health.get("errors") or {}).get(name, "")
            self.con.execute(
                "INSERT INTO sources(name,fail_streak,notes) VALUES(?,1,?) "
                "ON CONFLICT(name) DO UPDATE SET fail_streak=fail_streak+1, "
                "notes=excluded.notes", (name, note))

    def debug_sources(self) -> str:
        """Re-run every degraded source once; return real errors as text."""
        cfg = self._apply_planner_and_pacing()
        bad = [r["name"] for r in self.con.execute(
            "SELECT name FROM sources WHERE fail_streak>=1 ORDER BY name")]
        keyless = [n for n in ingest.KEYED_SOURCES if not ingest._has_key(n)]
        lines = []
        for name in bad:
            fn = ingest.SOURCES.get(name)
            scfg = dict((cfg.get("sources") or {}).get(name) or {})
            if not fn or not scfg.get("enabled", False):
                continue
            try:
                got = fn(scfg)
                lines.append(f"✅ {name}: recovered — {len(got)} items just now")
            except Exception as exc:  # noqa: BLE001
                body = ""
                r = getattr(exc, "response", None)
                if r is not None:
                    body = f"\n   ↳ {r.text[:300]}"
                lines.append(f"❌ {name}: {type(exc).__name__}: {str(exc)[:300]}{body}")
        for n in keyless:
            lines.append(f"⏸ {n}: no key entered — waiting, not broken")
        if not lines:
            lines = ["All sources healthy — nothing to probe."]
        return "\n".join(lines)

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
            dk = f"ts_day_{dt.date.today().isoformat()}"
            dbm.kv_set(self.con, dk, (dbm.kv_get(self.con, dk) or 0) + got)
        for j in jobs:
            dbm.upsert_opportunity(self.con, j)
        evaled = {r["opp_key"]: r["ts"] for r in self.con.execute(
            "SELECT opp_key, MAX(ts) ts FROM flight WHERE stage='deep_eval' "
            "GROUP BY opp_key")}
        fresh = []
        for j in jobs:
            t = evaled.get(j.key)
            row = self.con.execute("SELECT last_signal, status FROM opportunities "
                                   "WHERE key=?", (j.key,)).fetchone()
            if row and row["status"] == "delivered":
                continue
            if t and (not row or (row["last_signal"] or 0) <= t):
                continue  # already evaluated, nothing new since
            fresh.append(j)
        LOG.info("%d fetched, %d new-or-resignaled", len(jobs), len(fresh))
        survivors = funnel.hard_constraints(self.con, fresh, cfg, now)
        kept = funnel.triage(self.con, survivors, cfg)
        screened = funnel.screen(self.con, kept, cfg)
        evaluated = funnel.deep_eval(self.con, screened, cfg)
        # earliest-wins query credit
        for j in kept:
            base = j.source.split(":")[0].split(" ")[0]
            if base in ("jsearch", "adzuna", "jooble"):
                planner.credit_query(self.con, base, "", 0)  # coarse v1 credit
        log = (dbm.kv_get(self.con, "fetch_log") or [])[-30:]
        log.append({"ts": time.time(), "n": len(jobs)})
        dbm.kv_set(self.con, "fetch_log", log)
        self.con.commit()
        return {"scanned": len(jobs), "evaluated": evaluated, "health": health}

    async def scan_and_maybe_flash(self, bot: Bot) -> dict:
        stats = await asyncio.to_thread(self.scan)
        # loud coverage transitions: new failures push immediately, in the API's words
        failed_now = set((stats.get("health") or {}).get("failed") or [])
        errs = (stats.get("health") or {}).get("errors") or {}
        prev = set(dbm.kv_get(self.con, "failing_set") or [])
        chat_id = dbm.kv_get(self.con, "chat_id")
        if chat_id:
            for name in sorted(failed_now - prev):
                try:
                    await bot.app.bot.send_message(
                        chat_id, f"🔴 SOURCE DOWN: {name}\n"
                                 f"{(errs.get(name) or 'no detail')[:280]}\n"
                                 "Coverage is reduced until this recovers — "
                                 "treat drops as partial. I'll announce recovery.")
                except Exception:  # noqa: BLE001
                    pass
            for name in sorted(prev - failed_now):
                try:
                    await bot.app.bot.send_message(chat_id, f"🟢 Source recovered: {name}")
                except Exception:  # noqa: BLE001
                    pass
        dbm.kv_set(self.con, "failing_set", sorted(failed_now))
        self.con.commit()
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
        seen_urls, seen_roles = set(), set()
        for r in rows:
            u = (r["url"] or "").rstrip("/").lower()
            role = ((r["company"] or "").lower(), (r["title"] or "").lower()[:60])
            if (u and u in seen_urls) or role in seen_roles:
                continue  # duplicate posting reached us via two keys — keep best
            if u:
                seen_urls.add(u)
            seen_roles.add(role)
            det = json.loads(r["d"] or "{}")
            j = _Opp(key=r["key"], source=r["source"], url=r["url"], title=r["title"],
                     company=r["company"], description=r["description"] or "",
                     location=r["location"] or "", salary=r["salary"] or "")
            evaluated.append({"job": j, "score": r["s"], "dims": det.get("dims") or {},
                              "verdict": det.get("verdict") or "WATCH",
                              "blurb": det.get("blurb") or _trim(r["description"] or r["title"]),
                              "risk": det.get("risk") or ""})
        bar_delta = dbm.kv_get(self.con, "bar_delta") or 0
        funnel.BAR = 80.0 + bar_delta
        picked, below = funnel.select_daily(self.con, evaluated, time.time())
        self.con.commit()
        scanned = self.con.execute("SELECT COUNT(*) c FROM flight WHERE stage='triage' "
                                   "AND ts>?", (cutoff,)).fetchone()["c"]
        frows = self.con.execute(
            "SELECT name, notes FROM sources WHERE fail_streak>=2").fetchall()
        health = {"failed": [r["name"] for r in frows],
                  "errors": {r["name"]: (r["notes"] or "") for r in frows}}
        cutoff24 = time.time() - 24 * 3600
        fetched = sum(e["n"] for e in (dbm.kv_get(self.con, "fetch_log") or [])
                      if e["ts"] > cutoff24)
        return picked, below, {"scanned": scanned, "fetched": fetched, "health": health}

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
    # hygiene: purge junk the old regret engine may have written
    cands = [c for c in (dbm.kv_get(con, "extra_candidates") or [])
             if len(c) > 2 and c.lower() not in ("unknown", "n/a", "none")]
    dbm.kv_set(con, "extra_candidates", cands)
    qs = [q for q in (dbm.kv_get(con, "spawn_queries") or [])
          if "?" not in q and len(q) < 70]
    dbm.kv_set(con, "spawn_queries", qs)
    for name in (cfg.get("company_blocklist") or []):
        con.execute("INSERT INTO companies(name, never_show, added_by, created_at) "
                    "VALUES(?,1,'config',?) ON CONFLICT(name) DO UPDATE SET never_show=1",
                    (name, time.time()))
    con.commit()
    pipe = Pipeline(con, cfg)
    bot = Bot(con, cfg, pipe)

    async def loop():
        last_scan = 0.0
        while True:
            now = dt.datetime.now(NY)
            try:
                if time.time() - last_scan > SCAN_EVERY_H * 3600 \
                        and not bot.scan_lock.locked():
                    last_scan = time.time()
                    async with bot.scan_lock:
                        await pipe.scan_and_maybe_flash(bot)
                today = now.date().isoformat()
                if now.hour == 7 and dbm.kv_get(con, "dropped") != today:
                    picked, below, stats = pipe.select_daily()
                    await bot.send_daily(picked, below, stats)
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
        LOG.info("bot polling; pipeline loop running (v%s)", VERSION)
        chat = dbm.kv_get(con, "chat_id")
        if chat and dbm.kv_get(con, "code_version") != VERSION:
            try:
                await bot.app.bot.send_message(
                    chat, f"⬆ Updated to v{VERSION}: pass line recalibrated — roles like the "
                          "78-scored OpenAI BD now DELIVER as cleared, not close-miss "
                          "· gig-marketplace junk banned (SaidGig, FlexBoard, GrabJobs) "
                          "· plus v1.6.6: NYC query lane + credit-efficient TheirStack."
                          "asks the boards for New York roles too (remote filters "
                          "auto-drop on those queries) · TheirStack tries title-"
                          "filtered shapes first so credits buy relevant roles · "
                          "digital-asset pattern added."
                          "5,200/mo plan — ~170 credits/day sliced across scans, "
                          "rests when the day\'s slice is spent · /status shows day + "
                          "month consumption · includes v1.6.4: one linked card per "
                          "job, duplicates collapsed, candidate crypto feeds."
                          "link · duplicate postings collapsed · candidate feeds added "
                          "for cryptocurrencyjobs.co, cryptojobs.com, remote3, "
                          "crypto.jobs (wrong guesses skip silently)."
                          "the error and retries at exactly that number — any tier "
                          "works · cards redesigned: role+company bold, labeled "
                          "Location/Comp lines · repost boards replaced by the true "
                          "employer on cards."
                          "own documented shapes; the failure reason now pushes to you "
                          "automatically) · 🔴/🟢 source alerts the moment coverage "
                          "changes · every drop opens with a coverage banner when "
                          "anything is missing · plus all of v1.6/v1.6.1: remote-or-NYC, "
                          "word tiers, source labels, capacity pacing, /advisory."
                          "hold across all scans (JSearch was burning 8x intended; "
                          "plan protected) · query windows rotate so the full pool "
                          "sweeps each day · /status shows live paid-API consumption."
                          "replaced with plain words (/why keeps the numbers) · every "
                          "card names its source · paid capacity raised to ~75% of plan "
                          "· Workday prefilled + WorkingNomads + WWR category feeds · "
                          "/advisory launches the expert-network checklist."
                          "your links (LinkedIn included) and never invent names · "
                          "junk purged from the universe · plain questions get direct "
                          "answers · degraded notices include the API's own error "
                          "text · TheirStack tries page-free payloads."
                          "all four families score equally · employer universe (VC/PE/"
                          "fintech/AI) wires in tonight and persists · JSearch survives "
                          "slow queries · TheirStack self-diagnoses rejections · /debug "
                          "now shows full API error text · repost-site links penalized.")
            except Exception:  # noqa: BLE001
                pass
            dbm.kv_set(con, "code_version", VERSION)
            con.commit()

        async def _resolve_once():
            try:
                if not ingest.RESOLVED_PATH.exists():
                    LOG.info("first-boot watchlist resolve starting")
                    await asyncio.to_thread(ingest.resolve_watchlist,
                                            pipe._apply_planner_and_pacing())
                    n = sum(len(v) for v in ingest.load_resolved().values())
                    if chat:
                        await bot.app.bot.send_message(
                            chat, f"🧭 Employer universe wired: {n} company boards "
                                  "now fully ingested — VC, PE, fintech, and AI "
                                  "included. The next scan reads all of them.")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("startup resolver failed: %s", exc)
        asyncio.create_task(_resolve_once())
        await loop()


if __name__ == "__main__":
    asyncio.run(run())
__EOF__
cat > app/funnel.py << '__EOF__'
"""Funnel: hard constraints -> embedding triage -> screen -> anchored deep
eval -> calibrated bar -> daily N. Every stage writes the flight recorder."""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone

from . import db as dbm
from . import ingest

LOG = logging.getLogger("funnel")

TRIAGE_KEEP = 300          # items per cycle that reach the screen
DEEP_KEEP = 60             # items per cycle that reach deep evaluation
BAR = 75.0                 # pass line matched to observed grader distribution
DAILY_N = 10
POSTING_HALF_LIFE_D = 6.0  # posted roles decay ~5-7d; signals reset clock
SIGNAL_HALF_LIFE_D = 30.0

_ANCHORS = """Reference points (score relative to these, held constant):
A1=95: Remote Head of BD at a category-leading company or top-tier fund, deliverable-led,
  async culture, $200k+ meaningful equity, direct exec exposure.
A2=85: Remote VC platform/research associate at a respected fund, moderate
  meetings, strong network value, mid comp.
A3=70: Remote partnerships/ecosystem role at a mid-sized company, decent
  comp, unclear autonomy.
A4=50: Hybrid corp-dev analyst at an unremarkable fintech, heavy meetings.
A5=30: Quota-carrying SaaS AE role relabeled "partnerships"."""

_DIMENSIONS = ["fit", "comp_upside", "network_value", "prestige_conditional",
               "intensity_inverse", "remote", "autonomy", "meeting_load_inverse",
               "market_timing", "freshness"]


# ---------------------------------------------------------------- embeddings
_MODEL = None


def _embed(texts: list[str]) -> list[list[float]]:
    """Local sentence-transformers when available; deterministic hashed
    bag-of-words fallback otherwise (tests, cold boxes). Same interface."""
    global _MODEL
    try:
        if _MODEL is None:
            from sentence_transformers import SentenceTransformer
            _MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        return [list(map(float, v)) for v in _MODEL.encode(texts, show_progress_bar=False)]
    except Exception:  # noqa: BLE001
        out = []
        for t in texts:
            vec = [0.0] * 256
            for tok in (t or "").lower().split():
                vec[hash(tok) % 256] += 1.0
            n = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / n for x in vec])
        return out


def _cos(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db_ = math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (da * db_)


def _centroid(vs):
    if not vs:
        return None
    n = len(vs)
    return [sum(v[i] for v in vs) / n for i in range(len(vs[0]))]


def profile_vector(con, cfg) -> list[float]:
    cached = dbm.kv_get(con, "profile_vec")
    ptxt = (cfg.get("profile") or "")[:2000]
    if cached and cached.get("src") == ptxt[:200]:
        return cached["vec"]
    vec = _embed([ptxt])[0]
    dbm.kv_set(con, "profile_vec", {"src": ptxt[:200], "vec": vec})
    return vec


def preference_vectors(con):
    """Revealed preferences: centroid of accepts minus centroid of hides.
    Logged from day one; weight ramps with label count (activates ~40)."""
    pos_rows = con.execute(
        "SELECT o.title, o.company, o.description FROM verdicts v JOIN opportunities o "
        "ON o.key=v.opp_key WHERE v.verdict IN ('apply','outreach','intro','interested','regret_gold')"
    ).fetchall()
    neg_rows = con.execute(
        "SELECT o.title, o.company, o.description FROM verdicts v JOIN opportunities o "
        "ON o.key=v.opp_key WHERE v.verdict LIKE 'hide%'").fetchall()
    mk = lambda r: f"{r['title']} at {r['company']}. {(r['description'] or '')[:400]}"
    pos = _centroid(_embed([mk(r) for r in pos_rows])) if pos_rows else None
    neg = _centroid(_embed([mk(r) for r in neg_rows])) if neg_rows else None
    n_labels = len(pos_rows) + len(neg_rows)
    ramp = min(1.0, n_labels / 150.0) if n_labels >= 40 else 0.0
    return pos, neg, ramp


# ---------------------------------------------------------------- stages
def hard_constraints(con, jobs, cfg, now) -> list:
    kept = ingest.prefilter(jobs, cfg, now)  # recency + remote + exclusions only
    never = {r["name"].lower() for r in con.execute(
        "SELECT name FROM companies WHERE never_show=1")}
    out = []
    for j in kept:
        if (j.company or "").lower() in never:
            dbm.record(con, j.key, "excluded", None, {"rule": "never_show_company"})
            continue
        out.append(j)
    return out


def triage(con, jobs, cfg) -> list:
    if not jobs:
        return []
    pvec = profile_vector(con, cfg)
    pos, neg, ramp = preference_vectors(con)
    texts = [f"{j.title} at {j.company}. {(j.description or '')[:400]}" for j in jobs]
    vecs = _embed(texts)
    scored = []
    for j, v in zip(jobs, vecs):
        s = _cos(v, pvec)
        if ramp and pos:
            pref = _cos(v, pos) - (_cos(v, neg) if neg else 0.0)
            s = (1 - 0.5 * ramp) * s + (0.5 * ramp) * pref
        scored.append((s, j))
        dbm.record(con, j.key, "triage", round(s, 4))
    scored.sort(key=lambda x: x[0], reverse=True)
    kept = [j for _, j in scored[:TRIAGE_KEEP]]
    for _, j in scored[TRIAGE_KEEP:]:
        dbm.record(con, j.key, "excluded", None, {"rule": "triage_cut"})
    return kept


def _claude(model: str, prompt: str, max_tokens: int = 3000) -> str:
    from anthropic import Anthropic
    resp = Anthropic().messages.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def screen(con, jobs, cfg) -> list:
    """Cheap-model pass: keep plausibly-excellent, drop clear misses."""
    if not jobs:
        return []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jobs[:DEEP_KEEP]
    keep: list = []
    model = cfg.get("screen_model") or "claude-haiku-4-5"
    for i in range(0, len(jobs), 30):
        batch = jobs[i:i + 30]
        listing = "\n".join(
            f"j{n}: {j.title} | {j.company} | {j.location} | {(j.description or '')[:200]}"
            for n, j in enumerate(batch))
        try:
            txt = _claude(model,
                          "Candidate profile:\n" + (cfg.get("profile") or "")[:1200]
                          + "\n\nFor each job below answer keep/drop: keep anything "
                            "plausibly strong for this candidate, drop clear misses "
                            "(wrong field, engineering, quota sales, on-site). JSON only: "
                            '[{"id":"j0","keep":true}...]\n\n' + listing, 1500)
            decisions = {d.get("id"): bool(d.get("keep"))
                         for d in json.loads(txt[txt.find("["):txt.rfind("]") + 1])
                         if isinstance(d, dict)}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("screen batch failed (%s) — keeping batch", exc)
            decisions = {}
        for n, j in enumerate(batch):
            k = decisions.get(f"j{n}", True)
            dbm.record(con, j.key, "screen", None, {"keep": k})
            if k:
                keep.append(j)
    return keep[:DEEP_KEEP * 2]


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url or "").netloc
    except Exception:  # noqa: BLE001
        return ""


def deep_eval(con, jobs, cfg) -> list[dict]:
    """Anchored, dimension-scored evaluation on the best model tier."""
    out: list[dict] = []
    if not jobs:
        return out
    model = cfg.get("deep_model") or "claude-sonnet-4-6"
    have_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    for i in range(0, len(jobs[:DEEP_KEEP]), 10):
        batch = jobs[i:i + 10]
        if not have_key:
            for j in batch:
                out.append({"job": j, "score": 55.0, "dims": {}, "blurb": j.title,
                            "verdict": "WATCH", "risk": "unscored (no API key)"})
            continue
        listing = "\n".join(
            f"j{n}: title={j.title} | company={j.company} | loc={j.location} | "
            f"salary={j.salary} | src={j.source} | host={_host(j.url)} | "
            f"desc={(j.description or '')[:700]}"
            for n, j in enumerate(batch))
        w = cfg.get("scoring_weights") or {}
        prompt = (
            "You are evaluating opportunities for one specific candidate.\n\n"
            f"<profile>{(cfg.get('profile') or '')[:1600]}</profile>\n\n"
            + _ANCHORS + "\n\n"
            + (f"Weights: {json.dumps(w)}\n" if w else "")
            + "For each job return JSON only:\n"
              '[{"id":"j0","score":0-100,"dims":{'
            + ",".join(f'"{d}":0-100' for d in _DIMENSIONS)
            + '},"verdict":"APPLY|OUTREACH|INTRO|WATCH",'
              '"employer":"true employer if the listed company is a job board/repost host; else omit",'
              '"blurb":"<=200 chars, verb-first, why it is top-decile or not",'
              '"risk":"<=90 chars"}]\n'
              "Prestige counts only when paired with autonomy (conditional). "
              "Calibration: missing information is NEUTRAL — never deduct for "
              "unlisted salary, unstated culture, or unknown meeting load; score "
              "expected value from what IS stated and put uncertainty in risk. "
              "A strong-fit role at a strong company with typical unknowns "
              "belongs in the 80s — an opportunity clearly worth 15 minutes of "
              "this candidate's attention today scores 80+; reserve the 70s for "
              "genuine maybes. Only positive evidence of meeting-heavy or "
              "quota patterns caps the score at 60. Location rule: remote is "
              "preferred; NYC-based is acceptable ONLY when office presence is "
              "clearly infrequent and meetings light — frequent in-person NYC "
              "caps at 70; any other location requiring presence caps at 40. "
              "If host= is a repost or "
              "aggregator site rather than the employer or a major job board, "
              "note 'unverified listing' in risk and score conservatively.\n\n" + listing)
        try:
            txt = _claude(model, prompt, 3500)
            arr = json.loads(txt[txt.find("["):txt.rfind("]") + 1])
        except Exception as exc:  # noqa: BLE001
            LOG.warning("deep eval batch failed: %s", exc)
            arr = []
        got = {d.get("id"): d for d in arr if isinstance(d, dict)}
        for n, j in enumerate(batch):
            d = got.get(f"j{n}") or {}
            score = float(d.get("score", 50))
            emp = (d.get("employer") or "").strip()
            if emp and len(emp) > 2 and emp.lower() != (j.company or "").lower():
                j.company = emp
                con.execute("UPDATE opportunities SET company=? WHERE key=?",
                            (emp, j.key))
            rec = {"job": j, "score": score, "dims": d.get("dims") or {},
                   "verdict": d.get("verdict") or "WATCH",
                   "blurb": (d.get("blurb") or j.title)[:220],
                   "risk": (d.get("risk") or "")[:100]}
            out.append(rec)
            dbm.record(con, j.key, "deep_eval", score,
                       {"dims": rec["dims"], "verdict": rec["verdict"],
                        "blurb": rec["blurb"], "risk": rec["risk"]})
            con.execute("UPDATE opportunities SET best_score=MAX(best_score,?) WHERE key=?",
                        (score, j.key))
    return out


def confidence_adjust(con, rec, now_ts: float) -> float:
    """Decay by age; boost by distinct independent signals (sub-linear)."""
    row = con.execute("SELECT posted_at, last_signal, signal_log FROM opportunities "
                      "WHERE key=?", (rec["job"].key,)).fetchone()
    score = rec["score"]
    if row:
        anchor = row["last_signal"] or row["posted_at"] or now_ts
        age_d = max(0.0, (now_ts - anchor) / 86400.0)
        age_d = max(0.0, age_d - 2.0)  # 48h grace before any decay
        half = SIGNAL_HALF_LIFE_D if "signal" in rec["job"].source else POSTING_HALF_LIFE_D
        score *= 0.5 ** (age_d / half) if age_d > half else 1.0 - 0.35 * (age_d / half)
        n_sig = dbm.distinct_signal_count(row["signal_log"])
        score = min(100.0, score * (1 + 0.06 * math.log2(n_sig)))
    return round(score, 1)


def select_daily(con, evaluated: list[dict], now_ts: float) -> tuple[list[dict], dict | None]:
    """Never lower the bar. Short days return the first miss + why."""
    for r in evaluated:
        r["final"] = confidence_adjust(con, r, now_ts)
    ranked = sorted(evaluated, key=lambda r: r["final"], reverse=True)
    picked = [r for r in ranked if r["final"] >= BAR][:DAILY_N]
    rest = [r for r in ranked if r not in picked]
    below = rest[: max(0, min(5, DAILY_N - len(picked)))]
    for r in picked + below:
        dbm.record(con, r["job"].key, "delivered", r["final"])
        con.execute("UPDATE opportunities SET status='delivered', delivered_at=? WHERE key=?",
                    (now_ts, r["job"].key))
    return picked, below
__EOF__
cat > config.yaml << '__EOF__'
# ============================================================
#  job-digest configuration — full-aggregation build
#  Edit on GitHub, commit; next run picks it up.
# ============================================================

profile: |
  Candidate: broad-range operator with experience across finance, economics,
  trading, and startups. Has held multiple jobs with very wide
  responsibilities; resume adapts across the whole spectrum below.

  Target roles, in priority order:
  1. Crypto / web3 / digital assets — any strong non-engineering role.
  2. Business development & partnerships — but NOT traditional sales:
     no quota-carrying, cold-calling, or meeting-heavy AE-style roles.
     Minimal meetings is a hard preference.
  3. Research — markets, economics, crypto, investment, strategy research.
  4. Venture capital / investing / private equity / incubation, and
     VC-adjacent roles: investor relations, fund operations, portfolio
     operations, platform roles at funds, scout programs.

  Score high: fully remote; low-touch and async-friendly (few meetings,
  deliverable-based, easily systematized or automated); OR a genuinely
  exceptional opportunity worth real involvement.
  Score low: on-site/hybrid, meeting-heavy calendars, traditional sales
  quotas, hands-on software engineering or design work.

# ---- AI ranking ---------------------------------------------
ai_ranking: true
screen_model: claude-haiku-4-5
deep_model: claude-sonnet-4-6
min_score: 60
max_jobs_per_email: 15
rank_candidates_max: 100     # deep funnel; ~4-5 cents/day at this cap

# ---- Filters (cheap screen BEFORE the AI ranker) ------------
max_age_days: 7
require_remote: true

url_blocklist:              # repost/spam domains — never deliver these links
  - mysmartpros.com
  - liveblog365.com
  - grabjobs.co
  - grabjobs

company_blocklist:          # marketplaces posing as employers — never deliver
  - SaidGig
  - FlexBoard

include_keywords:
  - crypto
  - web3
  - blockchain
  - defi
  - digital asset
  - stablecoin
  - tokenization
  - business development
  - partnership
  - research
  - analyst
  - venture
  - private equity
  - investor relations
  - investment
  - fund
  - portfolio
  - incubat
  - accelerator
  - trading
  - economist
  - economics
  - strategy
  - treasury
  - operations
  - chief of staff
  - due diligence
  - capital
  - ecosystem
  - grants

exclude_keywords:            # TITLE matches only
  - account executive
  - sales development
  - sales representative
  - inside sales
  - field sales
  - software engineer
  - developer
  - frontend
  - front end
  - backend
  - full stack
  - devops
  - designer
  - nurse
  - physician
  - recruiter
  - customer support

# ============================================================
# SOURCES — 18 fetchers across 4 layers
# Aggregators activate when their key secret exists; missing
# keys log "skipped" and everything else runs regardless.
# ============================================================
sources:
  # --- Layer 1: remote job boards (no keys) ---
  remotive:
    enabled: true
    searches: [crypto, "business development", "research analyst", venture, "investor relations", treasury, "private equity", partnerships]
  remoteok:
    enabled: true
  jobicy:
    enabled: true
    tags: [crypto, "business development", finance, analyst, marketing]
  themuse:
    enabled: true
    pages: 4
    categories: []
  arbeitnow:
    enabled: true
  hackernews:
    enabled: true
  rss:                        # niche boards without APIs — add any feed URL
    enabled: true
    feeds:
      - {name: cryptojobslist, url: "https://cryptojobslist.com/jobs.rss?jobLocation=Remote"}
      - {name: weworkremotely, url: "https://weworkremotely.com/remote-jobs.rss"}
      # candidate feeds - no public feed documented; misses skip silently
      - {name: cryptocurrencyjobs, url: "https://cryptocurrencyjobs.co/feed/"}
      - {name: cryptojobs-com, url: "https://www.cryptojobs.com/feed"}
      - {name: remote3, url: "https://www.remote3.co/feed"}
      - {name: crypto-jobs, url: "https://crypto.jobs/feed"}
      - {name: wwr-business, url: "https://weworkremotely.com/categories/remote-business-and-management-jobs.rss"}
      - {name: wwr-finance, url: "https://weworkremotely.com/categories/remote-finance-and-legal-jobs.rss"}
      # - {name: jobspresso, url: "https://jobspresso.co/feed/"}

  # --- Layer 2: major-site aggregators ---
  adzuna:                     # thousands of boards; free key, queries cost nothing
    enabled: true
    country: us
    searches:
      - crypto business development remote
      - web3 partnerships remote
      - digital assets analyst remote
      - venture capital analyst remote
      - venture capital associate remote
      - private equity research remote
      - private equity associate remote
      - investor relations remote
      - fund operations remote
      - portfolio operations remote
      - crypto research analyst remote
      - market research economist remote
      - trading research analyst remote
      - investment analyst remote
      - strategy research remote
      - treasury analyst remote
  jooble:                     # major-board aggregator; free key
    enabled: true
    searches:
      - crypto business development
      - web3 partnerships
      - venture capital analyst
      - private equity research
      - investor relations
      - fund operations
      - crypto research
      - investment analyst
      - digital assets
      - trading analyst
  jsearch:                    # Google for Jobs = LinkedIn, Indeed, Glassdoor,
    enabled: true             #   ZipRecruiter, Monster + public web
    date_posted: 3days
    pages: 2
    # BUDGET: requests/day ~= (search lines x pages). Defaults below assume
    # the verified $25/mo OpenWeb Ninja plan (10,000 req/mo): 45 lines x 2
    # pages x 31 days ~= 2,800/mo — under a third of quota. On the FREE tier
    # (200/mo) set pages: 1 and daily_request_budget: 6 instead.
    daily_request_budget: 250  # ~7,500/mo of the 10k plan
    searches:
      - crypto business development remote
      - venture capital analyst remote
      - investor relations remote
      - web3 partnerships remote
      - private equity research analyst remote
      - crypto research analyst remote
      - digital assets business development remote
      - blockchain partnerships remote
      - defi business development remote
      - stablecoin partnerships remote
      - tokenization business development remote
      - crypto ecosystem lead remote
      - web3 grants program remote
      - crypto operations remote
      - exchange listings manager remote
      - venture capital associate remote
      - venture capital platform remote
      - vc scout program remote
      - private equity associate remote
      - growth equity analyst remote
      - fund operations remote
      - fund administration analyst remote
      - portfolio operations remote
      - portfolio support analyst remote
      - investor relations associate remote
      - LP relations remote
      - capital formation remote
      - fundraising associate remote
      - incubator program manager remote
      - accelerator program remote
      - venture studio remote
      - market research analyst finance remote
      - economic research analyst remote
      - macro research analyst remote
      - equity research associate remote
      - crypto market analyst remote
      - trading desk analyst remote
      - quantitative research analyst remote
      - due diligence analyst remote
      - investment research remote
      - strategy analyst remote
      - corporate development analyst remote
      - treasury analyst remote
      - chief of staff investment remote
      - business operations analyst remote

  # --- Layer 3: specialized crypto board ---
  web3career:
    enabled: true

  # --- Layer 4: company watchlists (per-company ATS boards) ---
  # Manual lists below are merged with watchlist_resolved.json, which the
  # weekly "Resolve watchlist" workflow builds from watchlist_candidates.
  greenhouse:
    enabled: true
    companies: [coinbase, uniswaplabs, consensys, circle, chainalysis, gemini, ripple, paxos, alchemy]
  lever:
    enabled: true
    companies: [kraken, ledger]
  ashby:
    enabled: true
    companies: [ramp, mercury]
  workable:
    enabled: true
    companies: []
  smartrecruiters:
    enabled: true
    companies: []
  recruitee:
    enabled: true
    companies: []
  workingnomads:              # workingnomads.com — direct, no key
    enabled: true
  workday:                    # big banks / asset managers post here.
    enabled: true             # candidates below — wrong ones skip silently;
    urls:                     # forward any Workday careers page to add more
      - "https://jpmc.wd5.myworkdayjobs.com/JPMC"
      - "https://blackrock.wd1.myworkdayjobs.com/BlackRock_Professional"
      - "https://fmr.wd1.myworkdayjobs.com/FidelityCareers"
      - "https://blackstone.wd1.myworkdayjobs.com/Blackstone_Careers"
      - "https://pimco.wd1.myworkdayjobs.com/PIMCO"
      - "https://point72.wd5.myworkdayjobs.com/Point72"                  #   e.g. "https://tenant.wd5.myworkdayjobs.com/SiteName"
  bamboohr:                   # <slug>.bamboohr.com — auto-filled by resolver
    enabled: true
    companies: []
  pinpoint:                   # <slug>.pinpointhq.com — auto-filled by resolver
    enabled: true
    companies: []
  teamtailor:                 # <slug>.teamtailor.com — auto-filled by resolver
    enabled: true
    companies: []

  # --- Layer 5: enterprise feed (ATS-crawled + LinkedIn-derived, deduped) ---
  theirstack:                 # activates with THEIRSTACK_API_KEY
    enabled: true             # 1 credit = 1 job record returned
    max_age_days: 1             # only pull the last day's postings
    plan_credits_month: 5200    # confirmed from dashboard Aug 9; the
                                # scheduler paces spend evenly across the month
    title_patterns: ["digital asset", crypto, web3, blockchain, "business development",
                     partnerships, "venture capital", "private equity",
                     "investor relations", "research analyst", "fund operations",
                     treasury, "portfolio operations"]

# ---- Watchlist candidates -----------------------------------
# Company NAMES only — the weekly resolver probes all six ATS platforms,
# finds each company's live board, and adds it automatically. Wrong or
# unresolvable names are simply reported. Add freely.
watchlist_candidates:
  # crypto: exchanges, infra, custody, compliance, data
  - binance
  - okx
  - bybit
  - crypto.com
  - bitstamp
  - bitpanda
  - moonpay
  - transak
  - fireblocks
  - anchorage digital
  - bitgo
  - copper
  - taxbit
  - trm labs
  - elliptic
  - blockdaemon
  - figment
  - galaxy digital
  - grayscale
  - blockchain.com
  - opensea
  - magic eden
  - phantom
  - exodus
  # crypto: protocols and labs
  - polygon
  - ava labs
  - solana foundation
  - offchain labs
  - op labs
  - starkware
  - matter labs
  - eigen labs
  - celestia
  - wormhole foundation
  - lido
  - aave
  - chainlink labs
  - ethereum foundation
  - near foundation
  - aptos labs
  - sui foundation
  - berachain
  - monad
  - story protocol
  # crypto: research, data, media
  - messari
  - nansen
  - kaiko
  - dune
  - the block
  - blockworks
  - delphi digital
  - coingecko
  - glassnode
  - chainforensics
  # trading firms and market makers
  - wintermute
  - gsr
  - b2c2
  - flow traders
  - imc trading
  - optiver
  - drw
  - jump trading
  - hudson river trading
  - virtu financial
  - amber group
  - cumberland
  - keyrock
  # fintech
  - stripe
  - plaid
  - brex
  - wise
  - revolut
  - affirm
  - marqeta
  - chime
  - block
  - robinhood
  - etoro
  - public.com
  - alpaca
  # venture capital and private equity
  - a16z
  - andreessen horowitz
  - sequoia capital
  - paradigm
  - pantera capital
  - polychain capital
  - dragonfly
  - multicoin capital
  - electric capital
  - haun ventures
  - variant fund
  - placeholder
  - framework ventures
  - 1confirmation
  - blockchain capital
  - coinfund
  - castle island ventures
  - hack vc
  - union square ventures
  - first round capital
  - founders fund
  - general catalyst
  - lightspeed venture partners
  - accel
  - index ventures
  - bessemer venture partners
  - insight partners
  - craft ventures
  - felicis
  - ribbit capital
  - qed investors
  - khosla ventures
  - greylock
  - initialized capital
  - tiger global
  - coatue
  - thrive capital
  - general atlantic
  - blackstone
  - kkr
  - carlyle
  - apollo global management
  - tpg
  - vista equity partners
__EOF__
echo 'VERSION = "1.6.7"' > app/__init__.py
python3 << '__EOF__'
import re
from pathlib import Path
s = Path('app/ingest.py').read_text()
if "nyc_query" not in s:
    old = ('        params = {"query": q, "date_posted": scfg.get("date_posted") or "3days"}\n'
           '        if own_key:\n'
           '            params["work_from_home"] = "true"   # documented remote filter (direct API)\n'
           '        else:\n'
           '            params.update({"page": 1, "num_pages": int(scfg.get("pages") or 1),\n'
           '                           "remote_jobs_only": "true"})')
    new = ('        params = {"query": q, "date_posted": scfg.get("date_posted") or "3days"}\n'
           '        nyc_query = "new york" in q.lower() or "nyc" in q.lower()\n'
           '        if own_key:\n'
           '            if not nyc_query:\n'
           '                params["work_from_home"] = "true"  # remote filter only on remote-lane queries\n'
           '        else:\n'
           '            params.update({"page": 1, "num_pages": int(scfg.get("pages") or 1)})\n'
           '            if not nyc_query:\n'
           '                params["remote_jobs_only"] = "true"')
    assert s.count(old) == 1, "jsearch params anchor"
    s = s.replace(old, new, 1)
if "titles_us_order" not in s:
    LADDER = (
'    probe = {"limit": 25, "page": 0, "posted_at_max_age_days": cfg_age}\n'
'    tfilter = {"job_title_or": titles} if titles else {}\n'
'    ladder = [  # most-filtered first: credits should buy RELEVANT jobs\n'
'        ("titles_us_order", {**probe, **tfilter,\n'
'                             "job_country_code_or": ["US"],\n'
'                             "order_by": [{"desc": True, "field": "date_posted"}]}),\n'
'        ("titles_order", {**probe, **tfilter,\n'
'                          "order_by": [{"desc": True, "field": "date_posted"}]}),\n'
'        ("titles_only", {**probe, **tfilter}),\n'
'        ("offset_titles", {"offset": 0, "limit": 25,\n'
'                           "posted_at_max_age_days": cfg_age, **tfilter}),\n'
'        ("official_clone", dict(probe)),  # unfiltered last resort\n'
'    ]')
    pat = "    probe = " + re.escape('{"limit": 25, "page": 0, "posted_at_max_age_days": cfg_age}') + "\n    ladder = \\[.*?\n    \\]"
    hits = re.findall(pat, s, re.S)
    assert len(hits) == 1, f"ladder span: {len(hits)}"
    s = re.sub(pat, LADDER.replace("\\", "\\\\"), s, count=1, flags=re.S)
Path('app/ingest.py').write_text(s)
print("ingest: v1.6.6 patch OK")
__EOF__
python3 -m py_compile app/*.py && docker rm -f headhunter 2>/dev/null ; docker rmi -f headhunter 2>/dev/null ; docker build -t headhunter . && docker run -d --name headhunter --restart=always --env-file /opt/headhunter/.env -v /opt/headhunter-data:/app/data headhunter && git add -A && git commit -m "v1.6.7: pass line 75, junk banned; includes v1.6.6 NYC lane + theirstack ladder" && git push origin main && echo ALL DONE
