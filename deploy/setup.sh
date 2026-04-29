#!/usr/bin/env bash
# Bootstrap script for a fresh Ubuntu 24.04 ARM64 VM (Oracle Always Free).
# Run from the project root: bash deploy/setup.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "[1/6] Installing system packages..."
sudo apt update
sudo apt install -y \
    python3.12 python3.12-venv python3.12-dev \
    build-essential \
    ffmpeg \
    git curl ca-certificates

echo "[2/6] Creating Python virtualenv..."
if [ ! -d .venv ]; then
    python3.12 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools

echo "[3/6] Installing Python dependencies (this may take several minutes on ARM)..."
pip install -r requirements.txt

echo "[4/6] Creating data + secrets directories..."
mkdir -p data/{raw,reports,projections,depth_charts,transcripts,logs}
mkdir -p secrets
chmod 700 secrets

echo "[5/6] Writing .env.example if missing..."
if [ ! -f .env.example ]; then
    cp deploy/.env.example .env.example
fi

echo "[6/6] Done."
cat <<EOF

Next steps:
  1. cp .env.example .env      # then edit in your API keys
  2. Copy Google service account JSON to:
       secrets/service_account.json
     (or set GOOGLE_SERVICE_ACCOUNT_KEY in .env to an absolute path)
  3. (Optional) Drop Athletic cookies into secrets/athletic_cookies.txt
  4. Verify:  source .venv/bin/activate && python scripts/run_daily.py
  5. Install the dashboard service:
       sudo cp deploy/nfl-dashboard.service /etc/systemd/system/
       sudo systemctl daemon-reload
       sudo systemctl enable --now nfl-dashboard
  6. Add the cron line from deploy/crontab.example

See deploy/README.md for the Cloudflare Tunnel + Access setup.
EOF
