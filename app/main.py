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
from . import verdict_server
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


def should_ping(stats: dict) -> bool:
    return int(stats.get("scans_ok") or 0) > 0


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
        # One poisoned record must never take the other ~5,000 with it
        # (2026-09-08..11: a single un-encodable posting aborted 16 scans in
        # a row). Text is scrubbed at Job construction; this is the backstop.
        upsert_errors = []
        for j in jobs:
            try:
                dbm.upsert_opportunity(self.con, j)
            except Exception as exc:  # noqa: BLE001
                upsert_errors.append(f"{j.key[:60]}: {type(exc).__name__}: {str(exc)[:80]}")
        if upsert_errors:
            LOG.error("%d posting(s) could not be stored: %s", len(upsert_errors),
                      " | ".join(upsert_errors[:3]))
        health["upsert_errors"] = upsert_errors
        # Commit here and after each stage below: the funnel makes minutes of
        # LLM calls, and holding SQLite's single write lock across them is
        # what made the dashboard's verdict endpoint fail "database is locked".
        self.con.commit()
        fresh = self._fresh_jobs(jobs)
        LOG.info("%d fetched, %d new-or-resignaled", len(jobs), len(fresh))
        survivors = funnel.hard_constraints(self.con, fresh, cfg, now)
        kept = funnel.triage(self.con, survivors, cfg)
        self.con.commit()
        screened = funnel.screen(self.con, kept, cfg)
        self.con.commit()
        evaluated = funnel.deep_eval(self.con, screened, cfg)
        self.con.commit()
        LOG.info("funnel: %d survivors -> %d triaged -> %d screened -> %d evaluated",
                 len(survivors), len(kept), len(screened), len(evaluated))
        # earliest-wins query credit
        for j in kept:
            base = j.source.split(":")[0].split(" ")[0]
            if base in ("jsearch", "adzuna", "jooble"):
                planner.credit_query(self.con, base, "", 0)  # coarse v1 credit
        self._log_scan(n=len(jobs), ok=True, considered=len(kept), evaluated=len(evaluated))
        self.con.commit()
        return {"scanned": len(jobs), "evaluated": evaluated, "health": health}

    def _fresh_jobs(self, jobs) -> list:
        """Which of this scan's postings still need judging.
        Skip: already delivered; deep-evaluated with no newer signal; a
        different key for a URL that was already deep-evaluated (the same
        posting via two publishers cost a second Sonnet call 30% of the
        time); screened-out within the last 7 days (the old rule re-screened
        every rejected posting on every 3-hour scan — 79% of all screening
        was repeats — and those repeats crowded the per-scan cap)."""
        evaled = {r["opp_key"]: r["ts"] for r in self.con.execute(
            "SELECT opp_key, MAX(ts) ts FROM flight WHERE stage='deep_eval' "
            "GROUP BY opp_key")}
        evaled_urls = {(r["url"] or "").rstrip("/").lower() for r in self.con.execute(
            "SELECT DISTINCT o.url FROM flight f JOIN opportunities o ON o.key=f.opp_key "
            "WHERE f.stage='deep_eval' AND o.url!=''")}
        recent_drop = time.time() - 7 * 86400
        dropped = {r["opp_key"] for r in self.con.execute(
            "SELECT DISTINCT opp_key FROM flight WHERE stage='screen' AND ts>? "
            "AND detail LIKE '%\"keep\": false%'", (recent_drop,))}
        fresh = []
        for j in jobs:
            t = evaled.get(j.key)
            row = self.con.execute("SELECT last_signal, status FROM opportunities "
                                   "WHERE key=?", (j.key,)).fetchone()
            if row and row["status"] == "delivered":
                continue
            if t and (not row or (row["last_signal"] or 0) <= t):
                continue  # already evaluated, nothing new since
            if not t and (j.url or "").rstrip("/").lower() in evaled_urls:
                continue  # same posting, different key — already judged
            if j.key in dropped:
                continue  # screened out recently; nothing new to learn
            fresh.append(j)
        return fresh

    # ------------------------------------------------------------ scan ledger
    def _log_scan(self, n: int, ok: bool, error: str = "", **extra) -> None:
        log = (dbm.kv_get(self.con, "fetch_log") or [])[-30:]
        entry = {"ts": time.time(), "n": n, "ok": ok, **extra}
        if error:
            entry["error"] = error[:200]
        log.append(entry)
        dbm.kv_set(self.con, "fetch_log", log)

    def record_scan_failure(self, exc: Exception) -> None:
        """A crashed scan used to vanish into the log; the 7am drop then
        reported "0 scanned" as if the world were empty. Now it is a ledger
        entry the drop, /status, the dashboard digest, and the dead-man ping
        all read."""
        try:
            self.con.rollback()
        except Exception:  # noqa: BLE001
            pass
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
        self._log_scan(n=0, ok=False, error=err)
        dbm.kv_set(self.con, "last_scan_error", {"ts": time.time(), "error": err})
        self.con.commit()

    def digest_stats(self) -> dict:
        """Read-only 24h funnel numbers with honest labels: `fetched` is what
        the sources returned across SUCCESSFUL scans, `considered` is what
        reached triage, `evaluated` is what Sonnet actually scored, and
        `scans_failed` says how much of the window is missing."""
        cutoff = time.time() - 24 * 3600
        log = [e for e in (dbm.kv_get(self.con, "fetch_log") or []) if e.get("ts", 0) > cutoff]
        ok = [e for e in log if e.get("ok", True)]
        failed = [e for e in log if not e.get("ok", True)]
        considered = self.con.execute("SELECT COUNT(*) c FROM flight WHERE stage='triage' "
                                      "AND ts>?", (cutoff,)).fetchone()["c"]
        evaluated = self.con.execute("SELECT COUNT(*) c FROM flight WHERE stage='deep_eval' "
                                     "AND ts>?", (cutoff,)).fetchone()["c"]
        frows = self.con.execute(
            "SELECT name, notes FROM sources WHERE fail_streak>=2").fetchall()
        health = {"failed": [r["name"] for r in frows],
                  "errors": {r["name"]: (r["notes"] or "") for r in frows}}
        last_err = failed[-1].get("error", "") if failed else ""
        return {"fetched": sum(e.get("n", 0) for e in ok),
                "considered": considered, "scanned": considered,  # `scanned` = legacy key
                "evaluated": evaluated,
                "scans_ok": len(ok), "scans_failed": len(failed),
                "last_error": last_err, "health": health}

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
        return picked, below, self.digest_stats()

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
        # Apply/Outreach/Intro nudged 24h in and still not marked done after
        # another 48h (72h total) — surfaced here rather than paging Matt
        # again, per his ask: nudge once, then let it show up on its own.
        lingering = []
        for p in (dbm.kv_get(self.con, "pending_followups") or []):
            if time.time() - p["since"] < 72 * 3600:
                continue
            row = self.con.execute(
                "SELECT title, company FROM opportunities WHERE key LIKE ?",
                (p["key"] + "%",)).fetchone()
            if row:
                lingering.append(f"{p['action']}: {row['title']} — {row['company']}")
        return {"delivered": delivered, "pursued": pursued,
                "pursue_rate": pursued / delivered if delivered else 0.0,
                "hard_neg_rate": hard_neg / delivered if delivered else 0.0,
                "regrets": sum(r["c"] for r in regs),
                "regret_mix": ", ".join(f"{r['verdict_class']}×{r['c']}" for r in regs) or "none",
                "top_sources": ", ".join(r["name"] for r in tops),
                "degraded": ", ".join(r["name"] for r in self.con.execute(
                    "SELECT name FROM sources WHERE fail_streak>=2")),
                "lingering": lingering,
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
    if os.environ.get("VERDICT_HTTP_ENABLED", "1") != "0":
        verdict_server.start(int(os.environ.get("VERDICT_HTTP_PORT", "8765")))

    async def loop():
        last_scan = 0.0
        while True:
            now = dt.datetime.now(NY)
            try:
                if time.time() - last_scan > SCAN_EVERY_H * 3600 \
                        and not bot.scan_lock.locked():
                    last_scan = time.time()
                    async with bot.scan_lock:
                        try:
                            await pipe.scan_and_maybe_flash(bot)
                        except Exception as exc:  # noqa: BLE001
                            pipe.record_scan_failure(exc)
                            raise
                today = now.date().isoformat()
                if now.hour == 7 and dbm.kv_get(con, "dropped") != today:
                    picked, below, stats = pipe.select_daily()
                    await bot.send_daily(picked, below, stats)
                    dbm.kv_set(con, "dropped", today); con.commit()
                    ping = os.environ.get("HEALTHCHECK_PING_URL")
                    # The dead-man switch pinged on 16 straight crashed scans
                    # because it was tied to the drop firing, not to the
                    # drop having anything real behind it.
                    if ping and should_ping(stats):
                        try:
                            requests.get(ping, timeout=10)
                        except Exception:  # noqa: BLE001
                            pass
                    elif ping:
                        LOG.error("dead-man ping WITHHELD: no successful scan in 24h (%s)",
                                  stats.get("last_error") or "no scans ran")
                if now.hour == 12 and dbm.kv_get(con, "deadlink_swept") != today:
                    removed = await bot.check_dead_links()
                    dbm.kv_set(con, "deadlink_swept", today); con.commit()
                    if removed:
                        LOG.info("dead-link sweep: removed %d expired card(s)", removed)
                if now.hour in (10, 16) and \
                        dbm.kv_get(con, "followups_checked") != f"{today}-{now.hour}":
                    nudged = await bot.check_followup_nudges()
                    dbm.kv_set(con, "followups_checked", f"{today}-{now.hour}"); con.commit()
                    if nudged:
                        LOG.info("followup nudge: pinged %d pending item(s)", nudged)
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
                    chat, f"⬆ Updated to v{VERSION}: fixed the crash that killed every "
                          "scan since Sep 8 (one posting with a broken emoji aborted the "
                          "whole run, which is why the 7am drop said \"0 scanned\") · a "
                          "crashed scan can no longer hide — the drop, /status and the "
                          "dashboard now say how many scans failed and why · removed a "
                          "leftover cap that only ever let the 100 newest postings per "
                          "scan reach the ranker (85% of fresh matches were thrown away "
                          "unranked) · stopped re-judging the same rejected postings "
                          "every 3 hours · RSS feeds (CryptoJobsList, WWR) parse for the "
                          "first time · dashboard cards now carry company/location/comp/"
                          "source and your taps there train the ranker.")
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
