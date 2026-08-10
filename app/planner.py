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
