#!/bin/sh
# Update ShareSquad on the VM without deleting Postgres data.
# Run from the app repo as:  sudo sh deploy/update.sh
set -eu
cd "$(dirname "$0")/.."

git fetch origin
git reset --hard origin/main
git log -1 --oneline

docker compose --profile https build --no-cache app
docker compose --profile https up -d --force-recreate --no-deps app
docker compose --profile https up -d caddy

echo "--- app logs ---"
docker compose logs --tail=60 app

echo "--- health ---"
curl -sS http://127.0.0.1:8000/api/health || curl -sS http://127.0.0.1/api/health || true
