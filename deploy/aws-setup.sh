#!/bin/bash
# Run on a fresh Ubuntu 22.04+ EC2 or Lightsail instance
set -euo pipefail

echo "==> Installing Docker..."
sudo apt-get update -qq
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER" || true

APP_DIR="${APP_DIR:-$HOME/billbuddy}"
REPO_URL="${REPO_URL:-}"

if [ -n "$REPO_URL" ]; then
  echo "==> Cloning repository..."
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "==> Creating .env from example..."
  cp .env.example .env
  SECRET=$(openssl rand -hex 32)
  sed -i "s/change-me-to-a-long-random-string/$SECRET/" .env
  echo ""
  echo "IMPORTANT: Edit .env and set BASE_URL, POSTGRES_PASSWORD, and SMTP_*"
  echo "  nano .env"
fi

echo "==> Starting services..."
sudo docker compose up -d --build

echo ""
echo "Done! Open http://$(curl -s http://checkip.amazonaws.com 2>/dev/null || hostname -I | awk '{print $1}')"
echo "Set BASE_URL in .env to your domain, then: sudo docker compose up -d --build"
