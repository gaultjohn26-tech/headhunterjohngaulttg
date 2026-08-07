# CLAUDE.md — project brain for Claude Code

## What this is
A personal opportunity-intelligence system ("headhunter") for one user: it
continuously scans 22 job/signal sources, funnels everything through
embedding triage → cheap-model screen → anchored deep evaluation
(claude-sonnet), and delivers the day's best opportunities to the user's
Telegram at 7:00am America/New_York, with intraday 🚨 flashes (max 3/day),
one-tap verdict buttons, a /debug probe, and a forward-anything regret
postmortem engine. The user is NOT a programmer: they interact only through
Telegram and through you. Explain everything in plain language; never ask
them to write code.

## Non-negotiables (frozen spec — do not violate)
- Telegram is the ONLY surface. No email anywhere in the system, ever —
  not sending, not reading. (healthchecks.io dead-man ping is allowed.)
- Never lower the delivery bar (80 + user bar_delta) to pad the daily 10.
  Short days fill with a clearly labeled below-bar tier (scores shown,
  max 5) — never presented as having cleared; it exists to feed the
  preference model with verdict labels.
- Coverage honesty: query-based sampling is never described as complete
  coverage. Sources are labeled complete / sampled / enterprise-feed /
  native-alert / unsupported.
- Evidence protocol: never remove or downgrade a source or query on
  judgment alone — only with measured data over a defined window, proposed
  to the user for approval (Sunday brief proposals).
- Feedback drives proposals, not silent global changes. "Never this
  company" is the one instant hard rule.
- Secrets live ONLY in /opt/headhunter/.env (gitignored). Never commit
  keys. Never print full keys into chat or logs.

## Architecture map
- app/main.py — runtime: Pipeline (scan cycle, planner/pacing, daily
  selection, Sunday brief, debug_sources), scheduler loop, version
  announcement. VERSION lives in app/__init__.py — bump it on every
  user-visible change; the bot announces updates in chat automatically.
- app/bot.py — the entire UI: cards, buttons, /start /status /scan /drop
  /debug /why, natural-language prefs, regret intake on any forward.
- app/funnel.py — triage embeddings (sentence-transformers, hashed
  fallback), screen (claude-haiku), deep eval (claude-sonnet, anchored,
  10 dimensions, weights from search_spec.yaml), decay (48h grace),
  distinct-signal confidence, select_daily.
- app/ingest.py — all 22 source fetchers + watchlist resolver + prefilter.
  Per-company/per-feed fault isolation. KEYED_SOURCES gate on env keys.
- app/signals.py — DefiLlama raises, EDGAR Form D, Messari (key-gated).
- app/planner.py — query planner (~1,200 seeded); retirement locked until
  per-query attribution exists (kv per_query_credit_live).
- app/regret.py — 8-class postmortem, bounded auto-fixes, benchmark seeds.
- app/db.py — SQLite at data/headhunter.db (host volume
  /opt/headhunter-data). Tables: opportunities, flight (the flight
  recorder — every funnel decision), verdicts, sources, queries, regrets, kv.
- config.yaml (sources/filters) + search_spec.yaml (profile + scoring
  weights; overlays config) + benchmark.yaml (regression seeds).

## Ops runbook (on this VPS)
- Container: `headhunter`. Rebuild+restart:
  `docker build -t headhunter . && docker rm -f headhunter && docker run -d
   --name headhunter --restart=always --env-file /opt/headhunter/.env
   -v /opt/headhunter-data:/app/data headhunter`
- Logs: `docker logs --tail 50 headhunter`. Offline test:
  `python3 -m app.selftest` (must PASS before any rebuild).
- A systemd timer pulls GitHub every 10 min and redeploys on change —
  therefore ALWAYS `git add -A && git commit && git push origin main`
  after local edits, or the next pull will conflict.
- The user deploys big drops by uploading files to GitHub from their
  browser; you deploy by editing locally + push. Same pipeline.

## Current state: v1.2 — known issues (start here)
1. jsearch (OpenWeb Ninja direct): returned a non-list "data" shape;
   parser hardened with dual-endpoint fallback (/jsearch/search then
   /jsearch/search-v2) and error-body logging. Verify which endpoint the
   user's key actually works with; fix params to match their docs.
2. theirstack: request shape written defensively from public docs, never
   verified live. Error body now logged on failure. If key present, make
   one real call, read the 4xx body, fix the payload.
3. Recent fail_streak on: greenhouse (likely one bad prefilled slug — now
   isolated per-company; identify and correct/remove the bad slug),
   rss (check both feed URLs live).
4. First 7am drop not yet observed. Verify schedule fires, healthcheck
   ping succeeds, and card blurbs render complete (persisted blurb/risk).

## v2 queue (build in this order, one at a time, after v1 is green)
1. Per-query attribution (earliest-wins credit) → then unlock evidence-
   based query retirement.
2. LinkedIn connections CSV import (user uploads via Telegram) → warm-path
   "you know X at Y" on cards + INTRO routing.
3. Send-ready outreach drafts generated on Apply/Outreach taps.
4. Hash-gated LLM page reader for non-ATS career pages + Getro portfolio
   universe expansion (fund portfolio job boards).
5. LinkedIn dataset API layer (budget approved ranges in VENDORS.md).
6. Recruiter access program: DB of ~50-70 named recruiters, drafted
   intros, quarterly touch tracking via bot.
7. X (Twitter) 60-day gated trial vs curated handle list; kill gate =
   unique-or-earliest top-10 contributions.
8. Expert-network / advisory-channel enrollment tracking (GLG etc.).

## Working style
Small changes; selftest before rebuild; push after every change; bump
VERSION for anything the user can see; report to the user in plain English
("fixed X, you'll notice Y"); when uncertain about their preference, ask
them in one short question, never assume.
