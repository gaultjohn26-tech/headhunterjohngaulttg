# headhunter — personal opportunity intelligence, Telegram-native

Always-on system: continuous scans across all sources → embedding triage →
screen → anchored deep evaluation → the day's best at 7am ET on Telegram,
exceptional finds flashed intraday. Forward any opportunity you find
elsewhere and it runs an automatic postmortem on why it was missed. No email
anywhere. You never edit code or config — the bot is the interface.

## Setup — no coding, ~20 minutes once

Two hosting options — pick ONE. **Option A (own VPS, e.g. Contabo): $0 extra**
— do steps 1–2, then skip to "VPS path" below. **Option B (Railway, ~$5/mo,
zero terminal)** — follow steps 1–6 as written.

1. **Create your bot (2 min):** in Telegram, message **@BotFather** → send
   `/newbot` → pick any name → **copy the token** it gives you.
2. **GitHub (5 min):** create a **private** repo, upload this entire folder
   (drag-and-drop on "uploading an existing file").
3. **Railway (5 min):** railway.app → *Login with GitHub* → *New Project →
   Deploy from GitHub repo* → pick your repo. It builds automatically
   (first build takes a few minutes — the AI model downloads).
4. **Storage (1 min):** in your Railway service → **Volumes** → *Add volume*
   → mount path `/app/data`. (This keeps memory across restarts.)
5. **Keys (5 min):** Railway service → **Variables** → add:
   - `TELEGRAM_BOT_TOKEN` — from step 1 (required)
   - `ANTHROPIC_API_KEY` — console.anthropic.com (required)
   - `OPENWEBNINJA_KEY` — openwebninja.com JSearch $25 plan (LinkedIn/Indeed/
     Glassdoor reach)
   - `THEIRSTACK_API_KEY` — theirstack.com (set `plan_credits_month` in
     config.yaml to your plan's credits)
   - `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`, `JOOBLE_API_KEY`,
     `WEB3CAREER_TOKEN`, `MESSARI_API_KEY` — free/owned
   - `HEALTHCHECK_PING_URL` — optional dead-man switch: healthchecks.io →
     new check → copy ping URL; it emails you ONLY if the 7am drop ever fails
     to run (this is monitoring, not product email)
6. **Bind (30 sec):** open your bot in Telegram → `/start`. Done — it now
   runs forever. Missing keys just mean that source waits; add keys anytime.

### VPS path (Contabo or any Ubuntu/Debian box)
a. Make a GitHub token: GitHub → Settings → Developer settings →
   Fine-grained tokens → Generate; Repository access = only your repo;
   Permissions → Contents: Read-only. Copy it.
b. Connect to the VPS: open Terminal (Mac) or PowerShell (Windows) →
   `ssh root@YOUR_VPS_IP` → password from Contabo's email.
c. Type `nano setup.sh` → paste the entire contents of `vps-setup.sh`
   (open it on GitHub, copy all) → Ctrl-O, Enter, Ctrl-X.
d. Type `bash setup.sh` — it asks for the token and your keys one by one,
   builds, and starts everything with auto-restart.
e. Open your bot in Telegram → `/start`. From now on, uploading new files
   to GitHub redeploys the VPS automatically within 10 minutes — you never
   SSH in again.

## Daily use
- 7am: the day's best (never threshold-padded; short days show the first
  miss and why). Buttons: Apply / Outreach / Intro / Interested / Later /
  Hide (with reasons; "Never this company" is permanent, instantly).
- 🚨 intraday flashes only for exceptional finds (max 3/day).
- **Forward anything** — link, screenshot text, recruiter DM → instant
  postmortem: SOURCE GAP / QUERY GAP / PARSER / TIMING / FILTERING /
  RANKING / ATTENTION, with the auto-fix applied and the case added to the
  permanent regression seeds.
- Talk to it: `stricter` · `looser` · `more crypto` · `never ecosystem
  roles` · `/why 3` · `/status` · `/scan`.
- Sunday 5pm: 60-second brief — pursue rate, regret mix, top sources,
  degraded sources, and any weight-change proposals as Approve buttons.
  Nothing global ever changes without your tap.

## Guarantees encoded
Coverage only contracts by evidence dossier (query retirement is disabled
until per-query attribution is live). Every funnel decision is on the flight
recorder and replayable. Every regret becomes a regression seed forever.
