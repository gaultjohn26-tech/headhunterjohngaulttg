"""Telegram surface: the entire product UI. Verb-first cards, one-tap
verdicts, forward-anything regret intake, /why, Sunday brief, canaries."""
from __future__ import annotations

import asyncio
import html
import requests
import json
import logging
import os
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import VERSION
from . import db as dbm
from . import regret as regret_mod

LOG = logging.getLogger("bot")

INGEST_URL = os.environ.get("INGEST_URL", "")  # dashboard channel — inert until set
# The dashboard's digest endpoint sits next to its item endpoint.
INGEST_DIGEST_URL = os.environ.get("INGEST_DIGEST_URL") or (
    INGEST_URL.replace("/api/ingest/jobs", "/api/ingest/jobs-digest") if INGEST_URL else "")


def _scrub(text: str, n: int = 160) -> str:
    """Error text can carry a full request URL — including API keys in its
    query string (Adzuna's app_key was sitting in sources.notes). Never let
    that leave this process."""
    import re
    return re.sub(r"\?[^\s\"']*", "", str(text or ""))[:n]


def _post_ingest(recs: list[dict]) -> None:
    """Additive second delivery channel: POST the day's picks to a dashboard.
    Does nothing unless INGEST_URL is explicitly configured. Sends the same
    fields the Telegram card has; `key` is what lets a dashboard tap train
    the ranker (verdict_server looks the row up by it)."""
    if not INGEST_URL or not recs:
        return
    items = [{
        "key": r["job"].key,
        "title": f"{r['job'].title} — {r['job'].company}",
        "company": r["job"].company,
        "location": r["job"].location,
        "comp": r["job"].salary,
        "url": r["job"].url,
        "source": _src_label(r["job"].source),
        "verdict": r["verdict"],
        "score": round(r.get("final", r.get("score", 0)) or 0),
        "blurb": r["blurb"],
        "risk": r.get("risk", ""),
        "note": f"{r['verdict']} {r.get('final', r.get('score', 0)):.0f} · {r['blurb']}",
    } for r in recs]
    try:
        # raise_for_status: a 4xx/5xx is a failed delivery, not a success —
        # the digest endpoint answered 400 for two weeks and this said "posted".
        requests.post(INGEST_URL, json={"items": items}, timeout=10).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("ingest POST failed: %s", exc)


def digest_payload(stats: dict, cleared: int, below: int) -> dict:
    health = stats.get("health") or {}
    errs = health.get("errors") or {}
    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "scanned": stats.get("fetched", 0),
        "new": stats.get("considered", stats.get("scanned", 0)),
        "evaluated": stats.get("evaluated", 0),
        "cleared_bar": cleared,
        "below_bar": below,
        "scans_ok": stats.get("scans_ok", 0),
        "scans_failed": stats.get("scans_failed", 0),
        "last_error": _scrub(stats.get("last_error", "")),
        "degraded": list(health.get("failed") or []),
        "coverage_gaps": [{"source": n, "detail": _scrub(errs.get(n, ""))}
                          for n in (health.get("failed") or [])],
    }


def _post_digest(stats: dict, cleared: int, below: int) -> None:
    if not INGEST_DIGEST_URL:
        return
    try:
        requests.post(INGEST_DIGEST_URL, json=digest_payload(stats, cleared, below),
                      timeout=10).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("digest POST failed: %s", exc)

ADVISORY_NETWORKS = [
    ("GLG", "https://glginsights.com/council-members/"),
    ("AlphaSights", "https://www.alphasights.com/advisors/"),
    ("Third Bridge", "https://www.thirdbridge.com/en/specialists/"),
    ("Guidepoint", "https://www.guidepoint.com/advisors/"),
    ("Dialectica", "https://www.dialecticanet.com/experts"),
    ("NewtonX", "https://www.newtonx.com/experts/"),
    ("Coleman Research", "https://www.colemanrg.com/"),
    ("Catalant", "https://gocatalant.com/"),
    ("Graphite", "https://www.graphite.com/"),
    ("Business Talent Group", "https://www.businesstalentgroup.com/"),
    ("Toptal Business", "https://www.toptal.com/"),
    ("Bolster", "https://bolster.com/"),
    ("GoFractional", "https://www.gofractional.com/"),
]

HIDE_REASONS = [("too_junior", "Too junior"), ("too_sales", "Too sales-heavy"),
                ("wrong_industry", "Wrong industry"), ("wrong_comp", "Wrong comp"),
                ("low_upside", "Not enough upside"), ("never_co", "Never this company")]


def _fit_word(rec: dict) -> str:
    s = rec.get("final", rec.get("score", 0)) or 0
    if s >= 90:
        return "Exceptional"
    if s >= 80:
        return "Strong fit"
    if s >= 70:
        return "Promising"
    return "Marginal"


def _src_label(source: str) -> str:
    s = source or ""
    if s.startswith("jsearch via "):
        return s.split("via ", 1)[1] + " (via Google Jobs)"
    if s.startswith("jsearch"):
        return "Google Jobs"
    if ":" in s:
        base, slug = s.split(":", 1)
        if base in ("greenhouse", "lever", "ashby", "workable", "smartrecruiters",
                    "recruitee", "bamboohr", "pinpoint", "teamtailor", "workday"):
            return f"{slug.replace('-', ' ').title()} careers ({base})"
        if base == "rss":
            return {"cryptojobslist": "CryptoJobsList",
                    "weworkremotely": "We Work Remotely",
                    "wwr-business": "We Work Remotely",
                    "wwr-finance": "We Work Remotely"}.get(slug, slug)
        if base == "signal":
            return f"signal · {slug}"
    return {"workingnomads": "WorkingNomads", "remoteok": "RemoteOK",
            "themuse": "The Muse", "hackernews": "HN Who's Hiring",
            "theirstack": "TheirStack", "web3career": "web3.career"}.get(s, s)


def _card_text(i: int, rec: dict, show_score: bool = False) -> str:
    j = rec["job"]
    esc = html.escape
    href = html.escape(j.url or "", quote=True)
    head = (f'<b><a href="{href}">{i}. {esc(j.title)} — {esc(j.company)}</a></b>'
            if href else f"<b>{i}. {esc(j.title)} — {esc(j.company)}</b>")
    verdict = f"{rec['verdict']} · {_fit_word(rec)}"
    comp = f" · Comp: {esc(j.salary)}" if j.salary else ""
    loc = f"Location: {esc(j.location) if j.location else 'not stated'}{comp}"
    risk = f"\n<i>Risk: {esc(rec['risk'])}</i>" if rec.get("risk") else ""
    return (f"{head}\n{verdict}\n{loc}\n{esc(rec['blurb'])}"
            f"\nSource: {esc(_src_label(j.source))}{risk}")


def _card_kb(key: str) -> InlineKeyboardMarkup:
    k = key[:40]
    return InlineKeyboardMarkup([
        [B("Apply", callback_data=f"v|apply|{k}"),
         B("Outreach", callback_data=f"v|outreach|{k}"),
         B("Intro", callback_data=f"v|intro|{k}")],
        [B("Interested", callback_data=f"v|interested|{k}"),
         B("Later ⏰", callback_data=f"v|later|{k}"),
         B("Hide ▾", callback_data=f"h|menu|{k}")],
    ])


def _hide_kb(key: str) -> InlineKeyboardMarkup:
    k = key[:40]
    rows = [[B(lbl, callback_data=f"v|hide_{code}|{k}")] for code, lbl in HIDE_REASONS]
    rows.append([B("← back", callback_data=f"h|back|{k}")])
    return InlineKeyboardMarkup(rows)


class Bot:
    def __init__(self, con, cfg, pipeline):
        self.con, self.cfg, self.pipeline = con, cfg, pipeline
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        self.scan_lock = asyncio.Lock()
        self.app = Application.builder().token(token).concurrent_updates(True).build()
        self.app.add_handler(CommandHandler("start", self.on_start))
        self.app.add_handler(CommandHandler("why", self.on_why))
        self.app.add_handler(CommandHandler("status", self.on_status))
        self.app.add_handler(CommandHandler("scan", self.on_scan))
        self.app.add_handler(CommandHandler("drop", self.on_drop))
        self.app.add_handler(CommandHandler("debug", self.on_debug))
        self.app.add_handler(CommandHandler("advisory", self.on_advisory))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

    # ------------------------------------------------------------ lifecycle
    async def on_start(self, update: Update, _):
        dbm.kv_set(self.con, "chat_id", update.effective_chat.id)
        self.con.commit()
        await update.message.reply_text(
            "Bound. I scan continuously and drop the day's best at 7am ET.\n"
            "Forward me ANY job/opportunity you find elsewhere and I run a "
            "postmortem on why I missed it.\nSay things like 'stricter', "
            "'more crypto', 'status', or /why 3.")

    async def chat_id(self):
        return dbm.kv_get(self.con, "chat_id")

    # ------------------------------------------------------------ daily drop
    async def send_daily(self, picked: list[dict], below: list | None, stats: dict):
        await asyncio.to_thread(_post_ingest, picked + (below or []))
        await asyncio.to_thread(_post_digest, stats, len(picked), len(below or []))
        chat = await self.chat_id()
        if not chat:
            return
        d = datetime.now(timezone.utc).strftime("%a %b %-d")
        await self.app.bot.send_message(
            chat, f"⚡ <b>{d}</b> — {stats.get('fetched', 0):,} fetched · "
                  f"{stats.get('considered', stats.get('scanned', 0)):,} considered · "
                  f"{stats.get('evaluated', 0):,} evaluated · "
                  f"{len(picked)} cleared the bar", parse_mode=ParseMode.HTML)
        n_fail = int(stats.get("scans_failed") or 0)
        if n_fail:
            n_all = n_fail + int(stats.get("scans_ok") or 0)
            await self.app.bot.send_message(
                chat, f"⚠ {n_fail} of {n_all} scans in the last 24h CRASHED — this slate "
                      f"is partial. Last error: {_scrub(stats.get('last_error', ''), 200)}")
        health = stats.get("health") or {}
        if health.get("failed"):
            errs = health.get("errors") or {}
            lines = [f"{n} — {errs[n][:150]}" if errs.get(n) else n
                     for n in health["failed"]]
            await self.app.bot.send_message(
                chat, "🛑 COVERAGE GAP — missing right now: " + " ; ".join(lines)
                      + "\nTreat this slate as PARTIAL until recovery is announced.")
        for i, rec in enumerate(picked, 1):
            self.con.execute("UPDATE opportunities SET decision=? WHERE key=?",
                             (rec["verdict"], rec["job"].key))
            msg = await self.app.bot.send_message(
                chat, _card_text(i, rec), parse_mode=ParseMode.HTML,
                reply_markup=_card_kb(rec["job"].key),
                disable_web_page_preview=True)
            self._remember_card_message(rec["job"].key, chat, msg.message_id)
        if below:
            await self.app.bot.send_message(
                chat, "── Close misses — just under my bar today; "
                      "your taps teach it what the bar should mean. ──")
            for n, rec in enumerate(below, len(picked) + 1):
                self.con.execute("UPDATE opportunities SET decision=? WHERE key=?",
                                 (rec["verdict"], rec["job"].key))
                msg = await self.app.bot.send_message(
                    chat, _card_text(n, rec, show_score=True), parse_mode=ParseMode.HTML,
                    reply_markup=_card_kb(rec["job"].key),
                    disable_web_page_preview=True)
                self._remember_card_message(rec["job"].key, chat, msg.message_id)

        self.con.commit()

    # ------------------------------------------------------------ card message tracking
    def _remember_card_message(self, opp_key: str, chat_id: int, msg_id: int) -> None:
        """So a Hide tap (or the dead-link sweep) can delete the actual card,
        not just its buttons — the card's own title is now the job link, one
        message per job, so there's only ever one id to remember."""
        dbm.kv_set(self.con, f"msg_{opp_key[:40]}", [chat_id, msg_id])

    async def _delete_card_message(self, opp_key: str, forget: bool = True) -> bool:
        k = f"msg_{opp_key[:40]}"
        ids = dbm.kv_get(self.con, k)
        if not ids:
            return False
        chat_id, msg_id = ids
        try:
            await self.app.bot.delete_message(chat_id, msg_id)
        except Exception:  # noqa: BLE001 — already deleted/too old, fine
            pass
        if forget:
            self.con.execute("DELETE FROM kv WHERE k=?", (k,))
        return True

    # ------------------------------------------------------------ dead-link sweep
    async def check_dead_links(self) -> int:
        """Daily sweep: a job whose link has gone dead (404/410) while it was
        still sitting on-screen, undecided, gets pulled with a note — Matt
        asked not to have to discover this by clicking through stale links.
        Conservative on purpose: only a clear 404/410 counts as dead; any
        other outcome (timeout, bot-blocking, 5xx) is left for the next
        sweep rather than risk deleting a job that's still actually open."""
        rows = self.con.execute("SELECT k, v FROM kv WHERE k LIKE 'msg\\_%' ESCAPE '\\'").fetchall()
        removed = 0
        for r in rows:
            opp_key = r["k"][len("msg_"):]
            chat_id, _msg_id = json.loads(r["v"])
            row = self.con.execute(
                "SELECT key, title, company, url FROM opportunities WHERE key LIKE ?",
                (opp_key + "%",)).fetchone()
            if not row or not row["url"]:
                continue
            try:
                resp = await asyncio.to_thread(
                    requests.head, row["url"], timeout=15, allow_redirects=True)
                if resp.status_code == 405:  # some ATS boards reject HEAD
                    resp = await asyncio.to_thread(
                        requests.get, row["url"], timeout=15, allow_redirects=True)
            except Exception:  # noqa: BLE001 — network hiccup, recheck next sweep
                continue
            if resp.status_code not in (404, 410):
                continue
            await self._delete_card_message(row["key"])
            await self.app.bot.send_message(
                chat_id, f"⌛ Missed: <b>{html.escape(row['title'])}</b> — "
                         f"{html.escape(row['company'])} (link's dead now — "
                         f"gone before you got to it)",
                parse_mode=ParseMode.HTML)
            self.con.execute(
                "INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                (row["key"], time.time(), "expired_unactioned", ""))
            self._drop_followup(row["key"])
            removed += 1
        if removed:
            self.con.commit()
        return removed

    # ------------------------------------------------------------ apply/outreach follow-ups
    FOLLOWUP_ACTIONS = ("apply", "outreach", "intro")
    FOLLOWUP_LABELS = {"apply": "apply to", "outreach": "reach out to", "intro": "ask for an intro to"}

    def _track_followup(self, opp_key: str, action: str) -> None:
        k = opp_key[:40]
        pending = dbm.kv_get(self.con, "pending_followups") or []
        if any(p["key"] == k for p in pending):
            return  # already tracking this job (whichever action came first)
        pending.append({"key": k, "action": action, "since": time.time(), "nudged_at": None})
        dbm.kv_set(self.con, "pending_followups", pending)

    def _drop_followup(self, opp_key: str) -> None:
        k = opp_key[:40]
        pending = dbm.kv_get(self.con, "pending_followups") or []
        kept = [p for p in pending if p["key"] != k]
        if len(kept) != len(pending):
            dbm.kv_set(self.con, "pending_followups", kept)

    async def check_followup_nudges(self) -> int:
        """Matt: 'if I'm going to apply or do outreach they should stay
        visible so I don't forget to do it' — Apply/Outreach/Intro cards are
        never auto-hidden, but a single nudge after 24h and, if still
        ignored, showing up in the Sunday brief (see sunday_brief's
        `lingering`) means it isn't purely on Matt to remember to check."""
        chat = await self.chat_id()
        if not chat:
            return 0
        pending = dbm.kv_get(self.con, "pending_followups") or []
        now = time.time()
        nudged = 0
        for p in pending:
            if p.get("nudged_at") or now - p["since"] < 24 * 3600:
                continue
            row = self.con.execute(
                "SELECT key, title, company FROM opportunities WHERE key LIKE ?",
                (p["key"] + "%",)).fetchone()
            if not row:
                p["nudged_at"] = now  # opportunity gone (pruned/etc) — stop trying
                nudged += 1
                continue
            label = self.FOLLOWUP_LABELS.get(p["action"], "follow up on")
            await self.app.bot.send_message(
                chat, f"Did you {label} <b>{html.escape(row['title'])}</b> — "
                      f"{html.escape(row['company'])} yet?", parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    B("Done ✅", callback_data=f"f|done|{p['key']}"),
                    B("Not yet ⏰", callback_data=f"f|not_yet|{p['key']}"),
                    B("Drop it", callback_data=f"f|drop|{p['key']}")]]))
            p["nudged_at"] = now
            nudged += 1
        if nudged:
            dbm.kv_set(self.con, "pending_followups", pending)
            self.con.commit()
        return nudged

    # ------------------------------------------------------------ buttons
    async def on_button(self, update: Update, _):
        q = update.callback_query
        kind, action, key = (q.data.split("|") + ["", ""])[:3]
        if kind == "h" and action == "menu":
            await q.edit_message_reply_markup(_hide_kb(key)); await q.answer(); return
        if kind == "h" and action == "back":
            await q.edit_message_reply_markup(_card_kb(key)); await q.answer(); return
        if kind == "f":
            if action == "done":
                self._drop_followup(key)
                self.con.execute(
                    "INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                    (key, time.time(), "followup_done", ""))
                self.con.commit()
                await q.answer("Nice — logged as done.")
                await q.edit_message_reply_markup(None)
            elif action == "drop":
                self._drop_followup(key)
                await q.answer("Dropped — won't remind again.")
                await q.edit_message_reply_markup(None)
            else:  # not_yet
                await q.answer("OK — I'll check back.")
            return
        if kind == "v":
            row = self.con.execute("SELECT * FROM opportunities WHERE key LIKE ?",
                                   (key + "%",)).fetchone()
            full_key = row["key"] if row else key
            self.con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                             (full_key, time.time(), action, ""))
            if action == "hide_never_co" and row:
                self.con.execute(
                    "INSERT INTO companies(name, never_show, added_by, created_at) "
                    "VALUES(?,1,'verdict',?) ON CONFLICT(name) DO UPDATE SET never_show=1",
                    (row["company"], time.time()))
            if action == "later" and row:
                snooze = dbm.kv_get(self.con, "snoozed") or []
                snooze.append({"key": full_key, "at": time.time() + 6 * 3600})
                dbm.kv_set(self.con, "snoozed", snooze)
            if action in self.FOLLOWUP_ACTIONS:
                self._track_followup(full_key, action)
            self.con.commit()
            ack = {"apply": "Marked APPLY — angle drafting queued.",
                   "outreach": "Marked OUTREACH — draft queued.",
                   "intro": "Marked INTRO — warm-path lookup queued.",
                   "interested": "Noted.", "later": "Snoozed 6h.",
                   "hide_never_co": "Company permanently blocked."}
            await q.answer(ack.get(action, "Logged — ranking updated."))
            if action.startswith("hide"):
                # Hide + a reason means "get this off my screen" — remove the
                # card outright, no separate X tap needed afterward. Apply/
                # Outreach/Interested/Later are untouched — those stay
                # visible on purpose so Matt doesn't forget to act on them.
                self._drop_followup(full_key)
                if not await self._delete_card_message(full_key):
                    await q.edit_message_reply_markup(None)  # untracked older card

    async def on_advisory(self, update: Update, _):
        status = dbm.kv_get(self.con, "advisory_status") or {}
        lines = ["<b>Advisory & expert-network channel</b>",
                 "These are enrollment marketplaces — nothing to scrape; being "
                 "registered IS the coverage. Reply e.g. <i>enrolled GLG</i> and "
                 "I'll track it.\n"]
        for name, url in ADVISORY_NETWORKS:
            mark = "✅" if name.lower() in status else "⬜"
            lines.append(f"{mark} {name} — {url}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

    # ------------------------------------------------------------ text & regret
    async def on_text(self, update: Update, _):
        txt = (update.message.text or "").strip()
        low = txt.lower()
        if low in ("stricter", "be stricter"):
            dbm.kv_set(self.con, "bar_delta", (dbm.kv_get(self.con, "bar_delta") or 0) + 3)
            self.con.commit()
            await update.message.reply_text("Bar raised. Fewer, better."); return
        if low in ("looser", "more volume"):
            dbm.kv_set(self.con, "bar_delta", (dbm.kv_get(self.con, "bar_delta") or 0) - 3)
            self.con.commit()
            await update.message.reply_text("Bar lowered slightly."); return
        if low.startswith(("more ", "less ", "never ", "no more ")):
            prefs = dbm.kv_get(self.con, "nl_prefs") or []
            prefs.append({"ts": time.time(), "text": txt})
            dbm.kv_set(self.con, "nl_prefs", prefs[-40:])
            self.con.commit()
            await update.message.reply_text(
                "Preference logged — applied to ranking context immediately, "
                "weight change proposed Sunday.")
            return
        first_word = low.split(" ", 1)[0]
        if first_word in ("enrolled", "applied", "joined", "done") and len(txt.split()) <= 5:
            target = txt.split(" ", 1)[1].strip() if " " in txt else ""
            match = next((n for n, _ in ADVISORY_NETWORKS
                          if target.lower() in n.lower()), None)
            if match:
                st = dbm.kv_get(self.con, "advisory_status") or {}
                st[match.lower()] = time.time()
                dbm.kv_set(self.con, "advisory_status", st)
                self.con.commit()
                await update.message.reply_text(f"Tracked: {match} ✅ — /advisory shows the board.")
                return
        has_url = "http://" in low or "https://" in low
        first = low.split(" ", 1)[0]
        if not has_url and (low.rstrip().endswith("?") or first in (
                "which", "what", "why", "how", "when", "who", "where",
                "did", "does", "do", "is", "are", "can", "will")):
            ans = await asyncio.to_thread(
                regret_mod.answer_question, self.con, self.cfg, txt)
            await update.message.reply_text(ans)
            return
        # anything else forwarded = regret intake
        await update.message.reply_text("Running postmortem…")
        pm = await asyncio.to_thread(regret_mod.postmortem, self.con, self.cfg, txt)
        self.con.commit()
        await update.message.reply_text(regret_mod.regret_card(pm))

    # ------------------------------------------------------------ commands
    async def on_debug(self, update: Update, _):
        await update.message.reply_text("Probing sources — errors will appear here, "
                                        "in the API's own words…")
        report = await asyncio.to_thread(self.pipeline.debug_sources)
        for i in range(0, len(report), 3500):
            await update.message.reply_text(report[i:i + 3500])

    async def on_why(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        n = int(ctx.args[0]) if ctx.args else 1
        rows = self.con.execute(
            "SELECT o.title, o.company, f.detail, f.score FROM opportunities o "
            "JOIN flight f ON f.opp_key=o.key AND f.stage='deep_eval' "
            "WHERE o.status='delivered' ORDER BY o.delivered_at DESC LIMIT 12").fetchall()
        if not rows or n > len(rows):
            await update.message.reply_text("No scored item at that slot."); return
        r = rows[min(n, len(rows)) - 1]
        dims = json.loads(r["detail"] or "{}").get("dims") or {}
        lines = "\n".join(f"  {k}: {v}" for k, v in dims.items())
        await update.message.reply_text(
            f"{r['title']}, {r['company']} — {r['score']:.0f}\n{lines or 'dimension detail unavailable'}")

    async def on_status(self, update: Update, _):
        srcs = self.con.execute("SELECT name, state, last_count, fail_streak FROM sources "
                                "ORDER BY name").fetchall()
        live = sum(1 for s in srcs if s["fail_streak"] == 0)
        bad = [s["name"] for s in srcs if s["fail_streak"] >= 2]
        n_opp = self.con.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
        import datetime as _dt
        today = _dt.date.today().isoformat()
        js = dbm.kv_get(self.con, f"spent_jsearch_{today}") or 0
        az = dbm.kv_get(self.con, f"spent_adzuna_{today}") or 0
        jb = dbm.kv_get(self.con, f"spent_jooble_{today}") or 0
        await update.message.reply_text(
            f"{n_opp:,} opportunities tracked · {live}/{len(srcs)} sources healthy"
            + f"\nToday's paid/rate-limited usage — JSearch {js}/250 · "
              f"Adzuna {az}/50 · Jooble {jb}/25"
            + (lambda tsm, tsd, plan:
               f"\nTheirStack — {tsd} credits today · {tsm:,}/{plan:,} this month")(
                dbm.kv_get(self.con, f"ts_spent_{_dt.date.today():%Y%m}") or 0,
                dbm.kv_get(self.con, f"ts_day_{today}") or 0,
                int(((self.cfg.get("sources") or {}).get("theirstack") or {})
                    .get("plan_credits_month") or 5200))
            + (f" · degraded: {', '.join(bad)}" if bad else "")
            + self._last_scan_line()
            + f" · v{VERSION}")

    def _last_scan_line(self) -> str:
        log = dbm.kv_get(self.con, "fetch_log") or []
        if not log:
            return "\nNo scan has run yet."
        e = log[-1]
        when = datetime.fromtimestamp(e.get("ts", 0), tz=timezone.utc).strftime("%b %-d %H:%M UTC")
        d = self.pipeline.digest_stats()
        if e.get("ok", True):
            head = f"\nLast scan {when}: OK — {e.get('n', 0):,} fetched"
        else:
            head = f"\nLast scan {when}: FAILED — {_scrub(e.get('error', ''), 120)}"
        return head + (f" · {d['scans_failed']} of {d['scans_failed'] + d['scans_ok']} "
                       f"scans failed in 24h" if d["scans_failed"] else "")

    async def on_scan(self, update: Update, _):
        if self.scan_lock.locked():
            await update.message.reply_text(
                "A scan is already running — its report will land here shortly.")
            return
        async with self.scan_lock:
            await update.message.reply_text("Manual scan started…")
            try:
                stats = await self.pipeline.scan_and_maybe_flash(self)
            except Exception as exc:  # noqa: BLE001
                self.pipeline.record_scan_failure(exc)
                await update.message.reply_text(
                    f"Scan CRASHED: {_scrub(f'{type(exc).__name__}: {exc}', 300)}")
                return
        await update.message.reply_text(
            f"Scan done: {stats.get('scanned', 0)} items, "
            f"{stats.get('flashed', 0)} exceptional flash(es) sent.")

    async def on_drop(self, update: Update, _):
        await update.message.reply_text("Building a drop from the last 24h of evaluations…")
        picked, below, stats = self.pipeline.select_daily()
        if not picked and not below:
            await update.message.reply_text(
                "Nothing evaluated in the last 24h yet — send /scan first, then /drop.")
            return
        await self.send_daily(picked, below, stats)

    # ------------------------------------------------------------ Sunday brief
    async def send_sunday(self, brief: dict):
        chat = await self.chat_id()
        if not chat:
            return
        lines = [f"📊 <b>Sunday brief</b>",
                 f"Delivered {brief['delivered']} · pursued {brief['pursued']} "
                 f"({brief['pursue_rate']:.0%}) · hard-negatives {brief['hard_neg_rate']:.0%}",
                 f"Regrets this week: {brief['regrets']} ({brief['regret_mix']})",
                 f"Top sources: {brief['top_sources']}"]
        if brief.get("degraded"):
            lines.append(f"⚠ degraded: {brief['degraded']}")
        if brief.get("lingering"):
            lines.append("Still sitting there (marked, never confirmed done):")
            lines.extend(f"  · {x}" for x in brief["lingering"])
        for p in brief.get("proposals", []):
            lines.append(f"Proposal: {p['text']}")
        await self.app.bot.send_message(chat, "\n".join(lines), parse_mode=ParseMode.HTML)
        for i, p in enumerate(brief.get("proposals", [])):
            await self.app.bot.send_message(
                chat, f"Apply proposal {i + 1}?",
                reply_markup=InlineKeyboardMarkup([[
                    B("Approve", callback_data=f"p|ok|{i}"),
                    B("Keep 30 more days", callback_data=f"p|wait|{i}")]]))
