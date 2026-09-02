#!/bin/bash
# BillBuddy — EC2 free tier (t2/t3.micro, 1 GB RAM) setup helper
# Run on a fresh Ubuntu 22.04 instance after opening ports 22 and 80.
#
# Usage:
#   chmod +x deploy/ec2-free-tier-setup.sh
#   REPO_URL=https://github.com/YOUR_USERNAME/billbuddy.git ./deploy/ec2-free-tier-setup.sh
#
# Or if you already cloned the repo:
#   ./deploy/ec2-free-tier-setup.sh
set -euo pipefail

ADD_SWAP="${ADD_SWAP:-true}"
SWAP_GB="${SWAP_GB:-2}"
REPO_URL="${REPO_URL:-}"

if [ -f "docker-compose.yml" ]; then
  APP_DIR="$(pwd)"
elif [ -n "${APP_DIR:-}" ]; then
  APP_DIR="$APP_DIR"
else
  APP_DIR="$HOME/billbuddy"
fi

echo "==> Installing Docker and Git..."
sudo apt-get update -qq
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER" || true

if [ "$ADD_SWAP" = "true" ] && [ ! -f /swapfile ]; then
  echo "==> Adding ${SWAP_GB}G swap (helps PDF generation on 1 GB RAM)..."
  sudo fallocate -l "${SWAP_GB}G" /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  fi
  echo "Swap enabled: $(free -h | grep Swap)"
fi

if [ -n "$REPO_URL" ] && [ ! -d "$APP_DIR/.git" ]; then
  echo "==> Cloning repository..."
  git clone "$REPO_URL" "$APP_DIR"
fi

if [ ! -d "$APP_DIR" ]; then
  echo "ERROR: App directory not found at $APP_DIR"
  echo "Clone the repo first or set REPO_URL=..."
  exit 1
fi

cd "$APP_DIR"

if [ ! -f .env ]; then
  echo "==> Creating .env from example..."
  cp .env.example .env
  SECRET=$(openssl rand -hex 32)
  sed -i "s/change-me-to-a-long-random-string/$SECRET/" .env
  PUBLIC_IP=$(curl -s http://checkip.amazonaws.com 2>/dev/null || hostname -I | awk '{print $1}')
  if [ -n "$PUBLIC_IP" ]; then
    sed -i "s|BASE_URL=http://localhost:8001|BASE_URL=http://$PUBLIC_IP|" .env
  fi
  echo ""
  echo "IMPORTANT: Edit .env and set POSTGRES_PASSWORD and SMTP_* (optional):"
  echo "  nano .env"
  echo ""
  read -r -p "Press Enter after editing .env (or Ctrl+C to exit and edit first)..."
fi

echo "==> Building and starting BillBuddy..."
sudo docker compose up -d --build

PUBLIC_IP=$(curl -s http://checkip.amazonaws.com 2>/dev/null || hostname -I | awk '{print $1}')
echo ""
echo "=============================================="
echo " BillBuddy is starting on http://${PUBLIC_IP}"
echo "=============================================="
echo ""
echo "Check status:  sudo docker compose ps"
echo "View logs:     sudo docker compose logs -f app"
echo ""
echo "Free tier: one t2/t3.micro 24/7 uses ~744 hrs/month (limit 750)."
echo "PDF reports on 1 GB RAM may be slow — test Reports -> Generate Report."
echo "If PDF fails, swap is enabled; check logs and retry when server is idle."
