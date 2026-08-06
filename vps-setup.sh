#!/usr/bin/env bash
# headhunter — one-time VPS setup (Ubuntu/Debian, run as root).
# Paste this whole file into `nano setup.sh`, save, then: bash setup.sh
set -e
echo "== headhunter VPS setup =="
apt-get update -y >/dev/null
apt-get install -y git docker.io curl >/dev/null
systemctl enable --now docker >/dev/null

read -p "GitHub username: " GH_USER
read -p "GitHub token (fine-grained, Contents:Read on your repo): " GH_TOKEN
read -p "GitHub repo name [headhunter]: " REPO; REPO=${REPO:-headhunter}

REMOTE="https://${GH_USER}:${GH_TOKEN}@github.com/${GH_USER}/${REPO}.git"
if [ -d /opt/headhunter/.git ]; then
  git -C /opt/headhunter remote set-url origin "$REMOTE"
  git -C /opt/headhunter pull --ff-only
else
  git clone "$REMOTE" /opt/headhunter
fi

ENVF=/opt/headhunter/.env; : > "$ENVF"; chmod 600 "$ENVF"
echo "-- paste keys (Enter to skip any) --"
for K in TELEGRAM_BOT_TOKEN ANTHROPIC_API_KEY OPENWEBNINJA_KEY THEIRSTACK_API_KEY \
         ADZUNA_APP_ID ADZUNA_APP_KEY JOOBLE_API_KEY WEB3CAREER_TOKEN \
         MESSARI_API_KEY HEALTHCHECK_PING_URL; do
  read -p "$K: " V; [ -n "$V" ] && echo "$K=$V" >> "$ENVF"
done
grep -q TELEGRAM_BOT_TOKEN "$ENVF" || { echo "TELEGRAM_BOT_TOKEN is required."; exit 1; }

echo "-- building (first build downloads the AI model; takes a few minutes) --"
docker build -q -t headhunter /opt/headhunter
docker rm -f headhunter 2>/dev/null || true
mkdir -p /opt/headhunter-data
docker run -d --name headhunter --restart=always \
  --env-file "$ENVF" -v /opt/headhunter-data:/app/data headhunter

cat > /usr/local/bin/headhunter-update.sh << 'UPEOF'
#!/usr/bin/env bash
cd /opt/headhunter || exit 0
git fetch -q origin
if [ "$(git rev-parse HEAD)" != "$(git rev-parse @{u})" ]; then
  git pull -q --ff-only
  docker build -q -t headhunter /opt/headhunter
  docker rm -f headhunter 2>/dev/null || true
  docker run -d --name headhunter --restart=always \
    --env-file /opt/headhunter/.env -v /opt/headhunter-data:/app/data headhunter
fi
UPEOF
chmod +x /usr/local/bin/headhunter-update.sh
cat > /etc/systemd/system/headhunter-update.service << 'SVEOF'
[Unit]
Description=headhunter auto-update from GitHub
[Service]
Type=oneshot
ExecStart=/usr/local/bin/headhunter-update.sh
SVEOF
cat > /etc/systemd/system/headhunter-update.timer << 'TMEOF'
[Unit]
Description=check GitHub every 10 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
TMEOF
systemctl daemon-reload
systemctl enable --now headhunter-update.timer >/dev/null
echo "== done. Open Telegram, message your bot: /start =="
echo "   logs:   docker logs -f headhunter"
echo "   update: just upload new files to GitHub — deploys itself within 10 min"
