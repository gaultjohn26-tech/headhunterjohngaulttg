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
              '"blurb":"<=200 chars, verb-first, why it is top-decile or not",'
              '"risk":"<=90 chars"}]\n'
              "Prestige counts only when paired with autonomy (conditional). "
              "Calibration: missing information is NEUTRAL — never deduct for "
              "unlisted salary, unstated culture, or unknown meeting load; score "
              "expected value from what IS stated and put uncertainty in risk. "
              "A strong-fit role at a strong company with typical unknowns "
              "belongs in the 80s. Only positive evidence of meeting-heavy or "
              "quota patterns caps the score at 60. If host= is a repost or "
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
