cd /opt/headhunter && cat > app/funnel.py << '__EOF__'
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
BAR = 80.0                 # absolute calibrated bar for delivery
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
              "belongs in the 80s. Only positive evidence of meeting-heavy or "
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
cat > app/bot.py << '__EOF__'
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
                    chat, f"⬆ Updated to v{VERSION}: TheirStack now reads its plan cap out of "
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
echo 'VERSION = "1.6.3"' > app/__init__.py
python3 << '__EOF__'
import re
from pathlib import Path
s = Path('app/ingest.py').read_text()
if "plan caps limit at" not in s:
    anchor = ('        last_err = f"{label} -> HTTP {resp.status_code}: {resp.text[:250]}"\n'
              '        LOG.warning("theirstack %s", last_err)\n')
    assert s.count(anchor) == 1, "theirstack anchor not found"
    addition = (
        '        cap = re.search(r"limit parameter cannot be greater than (\\d+)", resp.text)\n'
        '        if cap:\n'
        '            n = int(cap.group(1))\n'
        '            LOG.warning("theirstack: plan caps limit at %d - retrying at the cap", n)\n'
        '            r3 = requests.post("https://api.theirstack.com/v1/jobs/search",\n'
        '                               json={**body, "limit": n}, timeout=45, headers=hdrs)\n'
        '            if r3.ok:\n'
        '                payload = r3.json()\n'
        '                LOG.warning("theirstack: ACCEPTED at plan cap limit=%d (shape %r)", n, label)\n'
        '                break\n'
        '            last_err = f"{label}@cap{n} -> HTTP {r3.status_code}: {r3.text[:250]}"\n'
        '            LOG.warning("theirstack %s", last_err)\n')
    s = s.replace(anchor, anchor + addition, 1)
if not re.search(r"^import re$", s, re.M):
    s = s.replace("import os\n", "import os\nimport re\n", 1)
Path('app/ingest.py').write_text(s)
print("ingest: cap-adaptive patch OK")
__EOF__
python3 -m py_compile app/*.py && docker build -t headhunter . && docker rm -f headhunter && docker run -d --name headhunter --restart=always --env-file /opt/headhunter/.env -v /opt/headhunter-data:/app/data headhunter && git add -A && git commit -m "v1.6.3: cap-adaptive theirstack, card redesign, employer correction (full-file deploy)" && git push origin main && echo ALL DONE
