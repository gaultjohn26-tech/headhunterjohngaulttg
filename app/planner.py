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
    if dbm.kv_get(con, "planner_seeded"):
        return 0
    now, n = time.time(), 0
    combos = [f"{s} {r} remote".strip() for r, s in itertools.product(ROLES, SECTORS)]
    random.shuffle(combos)
    for q in combos:
        for src in ("jsearch", "adzuna", "jooble"):
            con.execute("INSERT OR IGNORE INTO queries(source,text,born,state) VALUES(?,?,?,'live')",
                        (src, q, now)); n += 1
    dbm.kv_set(con, "planner_seeded", True)
    return n

def live_queries(con, source: str, budget: int) -> list[str]:
    # yield-weighted rotation: proven queries daily, the long tail round-robins
    rows = con.execute("SELECT text, unique_earliest, last_yield FROM queries "
                       "WHERE source=? AND state='live'", (source,)).fetchall()
    proven = [r["text"] for r in rows if r["unique_earliest"] > 0]
    rest = sorted((r for r in rows if r["unique_earliest"] == 0),
                  key=lambda r: (r["last_yield"] or 0))
    picked = proven[:budget]
    for r in rest:
        if len(picked) >= budget: break
        picked.append(r["text"])
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
