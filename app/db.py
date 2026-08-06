"""SQLite state: opportunities, companies, signals, flight recorder,
verdicts, sources, queries, regrets. Single file, WAL mode, no ORM."""
from __future__ import annotations
import json, sqlite3, time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "headhunter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, slug TEXT, ats TEXT,
  never_show INTEGER DEFAULT 0, added_by TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS opportunities(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, company TEXT, role_family TEXT,
  title TEXT, url TEXT, location TEXT, salary TEXT, description TEXT,
  source TEXT, first_seen REAL, last_signal REAL, posted_at REAL,
  best_score REAL DEFAULT 0, status TEXT DEFAULT 'candidate',
  signal_log TEXT DEFAULT '[]', delivered_at REAL, decision TEXT);
CREATE TABLE IF NOT EXISTS flight(          -- the flight recorder
  id INTEGER PRIMARY KEY, opp_key TEXT, ts REAL, stage TEXT,
  score REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS verdicts(        -- every button press = a label
  id INTEGER PRIMARY KEY, opp_key TEXT, ts REAL, verdict TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS sources(
  name TEXT PRIMARY KEY, state TEXT DEFAULT 'live', last_ok REAL,
  last_count INTEGER DEFAULT 0, fail_streak INTEGER DEFAULT 0,
  total_items INTEGER DEFAULT 0, top10_unique INTEGER DEFAULT 0,
  pursue_weighted REAL DEFAULT 0, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS queries(
  id INTEGER PRIMARY KEY, source TEXT, text TEXT, born REAL,
  last_yield REAL, unique_earliest INTEGER DEFAULT 0,
  state TEXT DEFAULT 'live', UNIQUE(source, text));
CREATE TABLE IF NOT EXISTS regrets(
  id INTEGER PRIMARY KEY, ts REAL, raw TEXT, company TEXT, title TEXT,
  verdict_class TEXT, detail TEXT, fixed TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS idx_flight_key ON flight(opp_key);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status);
"""

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con

def record(con, opp_key: str, stage: str, score: float | None = None,
           detail: dict | str | None = None) -> None:
    """Flight recorder: every funnel decision, forever replayable."""
    con.execute("INSERT INTO flight(opp_key, ts, stage, score, detail) VALUES(?,?,?,?,?)",
                (opp_key, time.time(), stage,
                 score, json.dumps(detail) if isinstance(detail, dict) else (detail or "")))

def kv_get(con, k, default=None):
    row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row["v"]) if row else default

def kv_set(con, k, v):
    con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v)))

def upsert_opportunity(con, job, role_family: str = "") -> str:
    """Lifecycle-lite: keyed by (company, role-family-ish title stem)."""
    key = job.key
    now = time.time()
    posted = job.posted_at.timestamp() if job.posted_at else None
    cur = con.execute("SELECT id, signal_log FROM opportunities WHERE key=?", (key,))
    row = cur.fetchone()
    sig = {"ts": now, "type": "posting_seen", "source": job.source}
    if row:
        log = json.loads(row["signal_log"]); log.append(sig)
        con.execute("UPDATE opportunities SET last_signal=?, signal_log=? WHERE key=?",
                    (now, json.dumps(log[-40:]), key))
    else:
        con.execute("""INSERT INTO opportunities(key,company,role_family,title,url,location,
                    salary,description,source,first_seen,last_signal,posted_at,signal_log)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, job.company, role_family, job.title, job.url, job.location,
                     job.salary, (job.description or "")[:6000], job.source, now, now,
                     posted, json.dumps([sig])))
    return key

def distinct_signal_count(signal_log: str) -> int:
    """Canonical-event rule: syndicated copies of one posting = one signal."""
    kinds = set()
    for s in json.loads(signal_log or "[]"):
        t = s.get("type", "")
        kinds.add("posting" if t == "posting_seen" else f"{t}:{s.get('ref','')}")
    return max(1, len(kinds))
