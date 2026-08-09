"""Telegram surface: the entire product UI. Verb-first cards, one-tap
verdicts, forward-anything regret intake, /why, Sunday brief, canaries."""
from __future__ import annotations

import asyncio
import html
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
    head = f"<b>{i}. {esc(j.title)} — {esc(j.company)}</b>"
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
        chat = await self.chat_id()
        if not chat:
            return
        d = datetime.now(timezone.utc).strftime("%a %b %-d")
        await self.app.bot.send_message(
            chat, f"⚡ <b>{d}</b> — {stats.get('fetched', 0):,} scanned · "
                  f"{stats.get('scanned', 0):,} new · "
                  f"{len(picked)} cleared the bar", parse_mode=ParseMode.HTML)
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
            await self.app.bot.send_message(
                chat, _card_text(i, rec), parse_mode=ParseMode.HTML,
                reply_markup=_card_kb(rec["job"].key),
                disable_web_page_preview=True)
            await self.app.bot.send_message(chat, rec["job"].url,
                                            disable_web_page_preview=True)
        if below:
            await self.app.bot.send_message(
                chat, "── Close misses — just under my bar today; "
                      "your taps teach it what the bar should mean. ──")
            for n, rec in enumerate(below, len(picked) + 1):
                self.con.execute("UPDATE opportunities SET decision=? WHERE key=?",
                                 (rec["verdict"], rec["job"].key))
                await self.app.bot.send_message(
                    chat, _card_text(n, rec, show_score=True), parse_mode=ParseMode.HTML,
                    reply_markup=_card_kb(rec["job"].key),
                    disable_web_page_preview=True)
                await self.app.bot.send_message(chat, rec["job"].url,
                                                disable_web_page_preview=True)

        self.con.commit()

    # ------------------------------------------------------------ buttons
    async def on_button(self, update: Update, _):
        q = update.callback_query
        kind, action, key = (q.data.split("|") + ["", ""])[:3]
        if kind == "h" and action == "menu":
            await q.edit_message_reply_markup(_hide_kb(key)); await q.answer(); return
        if kind == "h" and action == "back":
            await q.edit_message_reply_markup(_card_kb(key)); await q.answer(); return
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
            self.con.commit()
            ack = {"apply": "Marked APPLY — angle drafting queued.",
                   "outreach": "Marked OUTREACH — draft queued.",
                   "intro": "Marked INTRO — warm-path lookup queued.",
                   "interested": "Noted.", "later": "Snoozed 6h.",
                   "hide_never_co": "Company permanently blocked."}
            await q.answer(ack.get(action, "Logged — ranking updated."))
            if action.startswith("hide"):
                await q.edit_message_reply_markup(None)

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
            + (f" · degraded: {', '.join(bad)}" if bad else "")
            + f" · v{VERSION}")

    async def on_scan(self, update: Update, _):
        if self.scan_lock.locked():
            await update.message.reply_text(
                "A scan is already running — its report will land here shortly.")
            return
        async with self.scan_lock:
            await update.message.reply_text("Manual scan started…")
            stats = await self.pipeline.scan_and_maybe_flash(self)
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
        for p in brief.get("proposals", []):
            lines.append(f"Proposal: {p['text']}")
        await self.app.bot.send_message(chat, "\n".join(lines), parse_mode=ParseMode.HTML)
        for i, p in enumerate(brief.get("proposals", [])):
            await self.app.bot.send_message(
                chat, f"Apply proposal {i + 1}?",
                reply_markup=InlineKeyboardMarkup([[
                    B("Approve", callback_data=f"p|ok|{i}"),
                    B("Keep 30 more days", callback_data=f"p|wait|{i}")]]))
