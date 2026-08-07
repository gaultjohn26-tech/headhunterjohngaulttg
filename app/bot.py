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

HIDE_REASONS = [("too_junior", "Too junior"), ("too_sales", "Too sales-heavy"),
                ("wrong_industry", "Wrong industry"), ("wrong_comp", "Wrong comp"),
                ("low_upside", "Not enough upside"), ("never_co", "Never this company")]


def _card_text(i: int, rec: dict, show_score: bool = False) -> str:
    j = rec["job"]
    esc = html.escape
    score = f" · {rec.get('final', rec.get('score', 0)):.0f}" if show_score else ""
    head = f"<b>{i}. {rec['verdict']}{score}</b> — {esc(j.title)}, {esc(j.company)}"
    meta = " · ".join(x for x in (j.location, j.salary) if x)
    risk = f"\n<i>Risk: {esc(rec['risk'])}</i>" if rec.get("risk") else ""
    return f"{head}\n{esc(rec['blurb'])}\n{esc(meta)}{risk}"


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
            chat, f"⚡ <b>{d}</b> — scanned {stats.get('scanned', 0):,} new · "
                  f"{len(picked)} cleared the bar", parse_mode=ParseMode.HTML)
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
                chat, "── Below the bar — best of the rest. Scores shown; "
                      "your taps teach the ranker. ──")
            for n, rec in enumerate(below, len(picked) + 1):
                self.con.execute("UPDATE opportunities SET decision=? WHERE key=?",
                                 (rec["verdict"], rec["job"].key))
                await self.app.bot.send_message(
                    chat, _card_text(n, rec, show_score=True), parse_mode=ParseMode.HTML,
                    reply_markup=_card_kb(rec["job"].key),
                    disable_web_page_preview=True)
                await self.app.bot.send_message(chat, rec["job"].url,
                                                disable_web_page_preview=True)
        health = stats.get("health") or {}
        if health.get("failed"):
            await self.app.bot.send_message(
                chat, f"⚠ degraded sources: {', '.join(health['failed'])} — recall reduced.")
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
        # anything else forwarded = regret intake
        await update.message.reply_text("Running postmortem…")
        pm = regret_mod.postmortem(self.con, self.cfg, txt)
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
        await update.message.reply_text(
            f"{n_opp:,} opportunities tracked · {live}/{len(srcs)} sources healthy"
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
