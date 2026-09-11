"""Regression tests for the 2026-09-11 job-search audit. Offline: no
network, no keys, temp database. Run: python3 -m app.tests_regression"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.pop("ANTHROPIC_API_KEY", None)

from . import db as dbm  # noqa: E402
from . import funnel, ingest, main, signals  # noqa: E402
from .bot import _scrub, digest_payload  # noqa: E402
from .ingest import Job  # noqa: E402

NOW = datetime.now(timezone.utc)


def _hash_embed(texts):
    out = []
    for t in texts:
        vec = [0.0] * 64
        for tok in (t or "").lower().split():
            vec[hash(tok) % 64] += 1.0
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        out.append([x / n for x in vec])
    return out


def _job(i, **kw):
    base = dict(source="jobicy", source_id=f"id{i}", url=f"https://example.com/{i}",
                title=f"Crypto BD Lead {i}", company=f"Co{i}",
                description="Remote business development for a crypto exchange.",
                location="Remote", posted_at=NOW - timedelta(hours=i % 20))
    base.update(kw)
    return Job(**base)


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._db_path = dbm.DB_PATH
        dbm.DB_PATH = Path(self.tmp.name) / "t.db"
        self.con = dbm.connect()
        self._embed = funnel._embed
        funnel._embed = _hash_embed
        self.cfg = {"profile": "crypto BD research VC remote", "max_age_days": 7,
                    "require_remote": True, "exclude_keywords": ["developer"],
                    "include_keywords": [], "rank_candidates_max": 600, "sources": {}}

    def tearDown(self):
        funnel._embed = self._embed
        self.con.close()
        dbm.DB_PATH = self._db_path
        self.tmp.cleanup()


class SanitizeTests(TempDB):
    def test_lone_surrogate_is_stripped_at_job_boundary(self):
        j = _job(1, description="Great role \ud83c and more", title="Head of BD \ud83c")
        self.assertNotIn("\ud83c", j.description)
        self.assertNotIn("\ud83c", j.title)
        self.assertEqual(j.description, "Great role  and more")
        self.assertGreaterEqual(ingest.SANITIZED.get("jobicy", 0), 1)

    def test_poisoned_posting_can_be_stored(self):
        j = _job(2, description="x" * 500 + "\ud83c" + "y" * 500)
        dbm.upsert_opportunity(self.con, j)  # used to raise UnicodeEncodeError
        row = self.con.execute("SELECT description FROM opportunities WHERE key=?",
                               (j.key,)).fetchone()
        self.assertNotIn("\ud83c", row["description"])

    def test_nul_and_none_fields_are_normalised(self):
        j = _job(3, salary=None, location="NYC\x00")
        self.assertEqual(j.salary, "")
        self.assertEqual(j.location, "NYC")


class ScanIsolationTests(TempDB):
    def test_one_bad_upsert_does_not_kill_the_scan(self):
        jobs = [_job(i) for i in range(1, 8)]
        pipe = main.Pipeline(self.con, self.cfg)
        real_upsert = dbm.upsert_opportunity

        def boom(con, job, role_family=""):
            if job.key == jobs[3].key:
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
            return real_upsert(con, job, role_family)

        orig_collect, orig_signals, orig_pacing = ingest.collect_jobs, signals.collect_signals, main.Pipeline._apply_planner_and_pacing
        ingest.collect_jobs = lambda cfg: (list(jobs), {"ok": [("jobicy", 7)], "failed": [], "errors": {}})
        signals.collect_signals = lambda con: []
        main.Pipeline._apply_planner_and_pacing = lambda self: dict(self.cfg)
        dbm.upsert_opportunity = boom
        try:
            stats = pipe.scan()
        finally:
            dbm.upsert_opportunity = real_upsert
            ingest.collect_jobs, signals.collect_signals = orig_collect, orig_signals
            main.Pipeline._apply_planner_and_pacing = orig_pacing
        self.assertEqual(stats["scanned"], 7)
        self.assertEqual(len(stats["health"]["upsert_errors"]), 1)
        stored = self.con.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
        self.assertEqual(stored, 6)
        triaged = self.con.execute("SELECT COUNT(*) c FROM flight WHERE stage='triage'").fetchone()["c"]
        self.assertGreaterEqual(triaged, 6)
        log = dbm.kv_get(self.con, "fetch_log")
        self.assertTrue(log and log[-1]["ok"] and log[-1]["n"] == 7)

    def test_scan_failure_is_recorded_and_withholds_ping(self):
        pipe = main.Pipeline(self.con, self.cfg)
        pipe.record_scan_failure(UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed"))
        pipe.record_scan_failure(RuntimeError("boom"))
        stats = pipe.digest_stats()
        self.assertEqual(stats["scans_failed"], 2)
        self.assertEqual(stats["scans_ok"], 0)
        self.assertIn("RuntimeError", stats["last_error"])
        self.assertFalse(main.should_ping(stats))
        pipe._log_scan(n=5000, ok=True)
        stats = pipe.digest_stats()
        self.assertEqual(stats["fetched"], 5000)
        self.assertTrue(main.should_ping(stats))


class FreshFilterTests(TempDB):
    def test_skips_recent_screen_drop_dup_url_and_delivered(self):
        pipe = main.Pipeline(self.con, self.cfg)
        a, b, c, d, e = (_job(i) for i in range(1, 6))
        for j in (a, b, c, d, e):
            dbm.upsert_opportunity(self.con, j)
        # a: screened out 1 day ago -> not fresh
        self.con.execute("INSERT INTO flight(opp_key, ts, stage, score, detail) VALUES(?,?,?,?,?)",
                         (a.key, time.time() - 86400, "screen", None, json.dumps({"keep": False})))
        # b: screened out 8 days ago -> fresh again
        self.con.execute("INSERT INTO flight(opp_key, ts, stage, score, detail) VALUES(?,?,?,?,?)",
                         (b.key, time.time() - 8 * 86400, "screen", None, json.dumps({"keep": False})))
        # c: deep-evaluated under its own key -> not fresh
        dbm.record(self.con, c.key, "deep_eval", 70.0, {"dims": {}})
        # d: a different key for c's URL -> not fresh (already judged)
        dup = Job(source="jsearch via LinkedIn", source_id="zzz", url=c.url, title=c.title,
                  company=c.company, description=c.description, location="Remote", posted_at=NOW)
        # e: delivered -> not fresh
        self.con.execute("UPDATE opportunities SET status='delivered' WHERE key=?", (e.key,))
        fresh = pipe._fresh_jobs([a, b, c, dup, d, e])
        keys = {j.key for j in fresh}
        self.assertEqual(keys, {b.key, d.key})


class PrefilterTests(TempDB):
    def test_cap_comes_from_config_not_legacy_100(self):
        jobs = [_job(i) for i in range(1, 251)]
        self.assertEqual(len(ingest.prefilter(jobs, self.cfg, NOW)), 250)
        self.assertEqual(len(ingest.prefilter(jobs, {**self.cfg, "rank_candidates_max": 100}, NOW)), 100)


class RssTests(unittest.TestCase):
    FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:media="http://search.yahoo.com/mrss/" xmlns:a="u:a" xmlns:b="u:b" xmlns:c="u:c"
     xmlns:d="u:d" xmlns:e="u:e" xmlns:f="u:f" xmlns:g="u:g" xmlns:h="u:h">
<channel><title>CryptoJobsList</title><atom:link href="https://x/feed" rel="self"/>
<item><title>Head of Partnerships at Berachain</title><link>https://cryptojobslist.com/jobs/1</link>
<pubDate>Wed, 10 Sep 2026 12:00:00 +0000</pubDate>
<content:encoded><![CDATA[<p>Remote BD role, <b>async</b> team.</p>]]></content:encoded>
<media:content url="https://x/img.png"/></item>
<item><title>Research Analyst at Messari</title><link>https://cryptojobslist.com/jobs/2</link>
<pubDate>Thu, 11 Sep 2026 08:00:00 +0000</pubDate><description>Crypto research.</description></item>
</channel></rss>"""

    def test_namespaced_feed_parses(self):
        class R:
            content = self.FEED
            text = self.FEED.decode()
            def raise_for_status(self): pass
        orig = ingest.requests.get
        ingest.requests.get = lambda *a, **k: R()
        try:
            jobs = ingest.fetch_rss({"feeds": [{"name": "cryptojobslist", "url": "https://x/feed"}]})
        finally:
            ingest.requests.get = orig
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].title, "Head of Partnerships")
        self.assertEqual(jobs[0].company, "Berachain")
        self.assertEqual(jobs[0].url, "https://cryptojobslist.com/jobs/1")
        self.assertIn("Remote BD role", jobs[0].description)
        self.assertEqual(jobs[0].posted_at.day, 10)
        self.assertEqual(jobs[1].description, "Crypto research.")


class SubFailureTests(unittest.TestCase):
    def test_source_with_all_inputs_dead_is_reported_failed(self):
        def dead(scfg):
            for f in scfg["feeds"]:
                ingest._note_subfail("rss", f["name"], RuntimeError("404"))
            return []
        orig = dict(ingest.SOURCES)
        ingest.SOURCES.clear(); ingest.SOURCES["rss"] = dead
        try:
            jobs, health = ingest.collect_jobs({"sources": {"rss": {"enabled": True, "feeds": [{"name": "a"}, {"name": "b"}]}}})
        finally:
            ingest.SOURCES.clear(); ingest.SOURCES.update(orig)
        self.assertEqual(jobs, [])
        self.assertIn("rss", health["failed"])
        self.assertIn("all 2 inputs failed", health["errors"]["rss"])

    def test_partial_failures_are_reported_but_not_fatal(self):
        def half(scfg):
            ingest._note_subfail("greenhouse", "deadslug", RuntimeError("404"))
            return [_job(9, source="greenhouse:ok")]
        orig = dict(ingest.SOURCES)
        ingest.SOURCES.clear(); ingest.SOURCES["greenhouse"] = half
        try:
            jobs, health = ingest.collect_jobs({"sources": {"greenhouse": {"enabled": True}}})
        finally:
            ingest.SOURCES.clear(); ingest.SOURCES.update(orig)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(health["failed"], [])
        self.assertIn("greenhouse", health["partial"])


class DigestTests(unittest.TestCase):
    def test_scrub_removes_query_strings_with_keys(self):
        s = "503 for url: https://api.adzuna.com/v1/api/jobs/us/search/1?app_id=abc&app_key=SECRET&what=x"
        out = _scrub(s)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("app_key", out)
        self.assertIn("api.adzuna.com", out)

    def test_digest_payload_labels(self):
        stats = {"fetched": 37000, "considered": 640, "evaluated": 150, "scans_ok": 6,
                 "scans_failed": 2, "last_error": "UnicodeEncodeError: x?y=z",
                 "health": {"failed": ["rss"], "errors": {"rss": "all 8 inputs failed?k=1"}}}
        p = digest_payload(stats, 4, 5)
        self.assertEqual(p["scanned"], 37000)
        self.assertEqual(p["new"], 640)
        self.assertEqual(p["evaluated"], 150)
        self.assertEqual(p["cleared_bar"], 4)
        self.assertEqual(p["below_bar"], 5)
        self.assertEqual(p["scans_failed"], 2)
        self.assertEqual(p["degraded"], ["rss"])
        self.assertEqual(p["coverage_gaps"][0]["source"], "rss")
        self.assertNotIn("?", p["coverage_gaps"][0]["detail"])
        self.assertNotIn("?", p["last_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
