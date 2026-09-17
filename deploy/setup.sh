#!/usr/bin/env bash
# One-time bootstrap for the backend on a fresh Ubuntu VM (Oracle Cloud Always Free or
# any other Ubuntu 22.04+ box). Run as the box's normal login user (not root) via:
#   curl -fsSL https://raw.githubusercontent.com/V1shnuuu/My-Tutor/main/deploy/setup.sh | bash
# or clone the repo first and run it locally — either way it's safe to re-run.
set -euo pipefail

APP_DIR=/opt/mytutor
REPO_URL="https://github.com/V1shnuuu/My-Tutor.git"

sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv git ffmpeg curl

sudo mkdir -p "$APP_DIR"
sudo chown "$(whoami)":"$(whoami)" "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull
else
  git clone "$REPO_URL" "$APP_DIR"
fi

python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/core/requirements.txt"

if [ ! -f "$APP_DIR/core/.env" ]; then
  cp "$APP_DIR/core/.env.example" "$APP_DIR/core/.env"
  echo "Created core/.env from the example. EDIT IT before starting the service — see the"
  echo "checklist this script prints at the end."
fi

sudo cp "$APP_DIR/deploy/tutor-backend.service" /etc/systemd/system/tutor-backend.service
sudo sed -i "s/^User=.*/User=$(whoami)/" /etc/systemd/system/tutor-backend.service
sudo systemctl daemon-reload
sudo systemctl enable tutor-backend

# Caddy: reverse proxy + automatic HTTPS (Let's Encrypt), no manual cert handling.
if ! command -v caddy >/dev/null; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update
  sudo apt-get install -y caddy
fi
sudo cp "$APP_DIR/deploy/Caddyfile" /etc/caddy/Caddyfile

cat <<'EOF'

======================================================================
Bootstrap done. Before the app is actually reachable, you still need to:

1. core/.env  — edit /opt/mytutor/core/.env with real values:
     JWT_SECRET, ADMIN_TOKEN   — any long random strings
     ALLOWED_ORIGINS           — your Vercel URL + custom domain
     GOOGLE_CLIENT_ID          — from Google Cloud Console
     ADMIN_EMAILS              — your Google account email
     GROQ_API_KEY              — free at console.groq.com; this is what replaces the
                                  local GPU for both the LLM and speech-to-text here
     LOCAL_LLM_ENABLED=false   — no Ollama on this box; skip straight to Groq

2. /etc/caddy/Caddyfile — replace api.YOURDOMAIN.com with your real subdomain

3. DNS — at your registrar (Hostinger), add an A record:
     api.yourdomain.com  ->  <this server's public IP>

4. Cloud firewall — Oracle Cloud blocks 80/443 by default even after you open them in
   the OS: also add ingress rules for TCP 80 and 443 in the VM's attached Security
   List / Network Security Group in the Oracle Cloud console, or nothing outside the
   VM can ever reach it.

5. Start everything:
     sudo systemctl restart caddy
     sudo systemctl start tutor-backend
     sudo systemctl status tutor-backend --no-pager

6. Check it's alive:
     curl https://api.yourdomain.com/me
======================================================================
EOF
