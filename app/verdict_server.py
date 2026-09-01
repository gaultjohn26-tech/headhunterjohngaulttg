"""Loopback HTTP listener so a dashboard tap (Apply/Outreach/Intro/
Interested/Later/Hide+reason in the Personal OS War Room) trains the
ranker exactly the same as a Telegram tap — same verdicts table, same
never-show-company rule. Mirrors Bot.on_button's callback logic in
bot.py; keep the two in sync if that logic changes.

Plain stdlib http.server on its own thread, not folded into the asyncio
Telegram loop: the bot has no existing web server to extend, and a
threaded stdlib server needing zero new dependencies is a much smaller
addition than wiring an async framework into the polling loop for one
low-traffic endpoint. Each request opens its own short-lived sqlite3
connection (db.connect() sets check_same_thread=False + WAL + a 5s
busy_timeout) rather than sharing one across threads, so concurrent
taps can't corrupt a shared connection's cursor state.
"""
from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db as dbm

LOG = logging.getLogger("verdict_server")


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/verdict":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._reply(400, {"error": "bad json"})
            return
        key, verdict, reason = body.get("key"), body.get("verdict"), body.get("reason")
        if not key or not verdict:
            self._reply(400, {"error": "need key + verdict"})
            return
        con = dbm.connect()
        try:
            row = con.execute("SELECT * FROM opportunities WHERE key=?", (key,)).fetchone()
            full_key = row["key"] if row else key
            con.execute("INSERT INTO verdicts(opp_key, ts, verdict, note) VALUES(?,?,?,?)",
                        (full_key, time.time(), verdict, reason or ""))
            if verdict == "hide" and reason == "never_co" and row:
                con.execute(
                    "INSERT INTO companies(name, never_show, added_by, created_at) "
                    "VALUES(?,1,'dashboard',?) ON CONFLICT(name) DO UPDATE SET never_show=1",
                    (row["company"], time.time()))
            con.commit()
        finally:
            con.close()
        LOG.info("verdict via dashboard: %s -> %s%s", key, verdict, f" ({reason})" if reason else "")
        self._reply(200, {"ok": True})

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # route through our own logger, not stderr
        LOG.info(fmt, *args)


def start(port: int = 8765) -> ThreadingHTTPServer:
    """Bind loopback-only — this is the same posture as the OS's own API
    (127.0.0.1, reachable only because both containers share the VPS's
    network namespace via --network host), never exposed publicly."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    import threading
    threading.Thread(target=server.serve_forever, daemon=True, name="verdict-http").start()
    LOG.info("verdict HTTP listener on 127.0.0.1:%d", port)
    return server
