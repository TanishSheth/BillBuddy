# ShareSquad — how to run, change, and deploy

This is the working guide for local development, GitHub, the Oracle cloud VM, DNS, backups, and the mistakes that wipe data or leave the live site on old code.

Product name: **ShareSquad**  
GitHub repo: `https://github.com/TanishSheth/BillBuddy.git` (`main` branch)  
Production URL: `https://sharesquad.tanishsheth.com`  
Older URL: `https://billbuddy.tanishsheth.com` (Cloudflare redirect to ShareSquad)

Older files (`DEPLOY.md`, `deploy/ORACLE-ALWAYS-FREE.md`, `deploy/AWS-FREE-TIER.md`) describe first-time server setup. Prefer this file for day-to-day work.

---

## 1. Two environments (do not mix them)

| | Your PC | Oracle cloud |
|--|---------|----------------|
| How you start it | `.\run.ps1` | Docker Compose + Caddy |
| Database | SQLite file under the project data dir | Postgres in Docker volume `pg-data` |
| URL | http://localhost:8001 | https://sharesquad.tanishsheth.com |
| Uploads / PDFs | local folders | Docker volume `billbuddy-data` |

Creating a group on localhost does **not** create it on the live site. The live site does **not** use your laptop’s SQLite file.

Postgres user, database, and Docker volume names are still `billbuddy` / `billbuddy-data`. **Do not rename them.**

---

## 2. Run locally (Windows)

### First time

In PowerShell, from `Expense Splitter Web`:

```powershell
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

- `SECRET_KEY` — any long random string for local use is fine
- `BASE_URL=http://localhost:8001`
- Leave `DATABASE_URL` unset (SQLite)
- SMTP optional
- Do not set `DOMAIN` locally

Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) on Windows if receipt scan should work locally. The Docker image already includes Tesseract for production.

### Start

```powershell
.\run.ps1
```

Open **http://localhost:8001**. The dev server watches `app/` and restarts on Python changes. Hard-refresh the browser after HTML/JS edits (`Ctrl+Shift+R`) or use a private window.

### Health check

http://localhost:8001/api/health

You should see `"app": "ShareSquad"` and a `"version"` string (for example `20260916e`).

### Stop

Ctrl+C in the terminal running `run.ps1`.

---

## 3. Change code and put it on GitHub

Do the work on your PC in this folder. Then:

```powershell
cd "C:\Users\tanis\OneDrive\Desktop\Expense Splitter\Expense Splitter Web"
git status
git add -A
git commit -m "Short description of why you changed it."
git push origin HEAD
```

- Branch used in production is **`main`**.
- **Never commit `.env`** (secrets: `SECRET_KEY`, `POSTGRES_PASSWORD`, SMTP, API keys).
- Pushing to GitHub does **not** update the live website by itself. You must pull on the VM (section 5).

If `git push` asks for GitHub login, use a Personal Access Token as the password (GitHub no longer accepts account passwords for git).

---

## 4. Production layout (Oracle)

Typical pieces:

- Ubuntu VM, SSH user **`ubuntu`** (not `ubantu`)
- Docker Compose services: `app`, `db`, `caddy` (`caddy` uses profile `https`)
- App listens on `127.0.0.1:8000` inside the VM
- Caddy terminates HTTPS for `$DOMAIN` and reverse-proxies to the app
- Cloudflare DNS for `tanishsheth.com`

### `.env` on the server (not the same file as your laptop)

Required:

```bash
SECRET_KEY=          # long random; do not change casually (logs everyone out)
POSTGRES_PASSWORD=   # must match the existing Postgres volume
BASE_URL=https://sharesquad.tanishsheth.com
DOMAIN=sharesquad.tanishsheth.com
APP_HOST_PORT=127.0.0.1:8000
```

Optional: `SMTP_*`, `SERVER_VLM_API_KEY`.

After any `.env` change, recreate containers so they reread the file:

```bash
sudo docker compose --profile https up -d --force-recreate caddy app
```

### SSH from Windows

Use the key you already set up. Example:

```powershell
ssh -i "$env:USERPROFILE\.ssh\billbuddy-oracle" ubuntu@YOUR_PUBLIC_IP
```

If OpenSSH says the key is unprotected:

```powershell
icacls $env:USERPROFILE\.ssh\billbuddy-oracle /inheritance:r
icacls $env:USERPROFILE\.ssh\billbuddy-oracle /grant "${env:USERNAME}:R"
```

Cloud Shell on Oracle is **not** the Ubuntu VM. You must SSH into the instance.

---

## 5. Update the live site (after a GitHub push)

This is the normal “I fixed something, put it online” path. It keeps users, groups, and expenses.

On the VM, in the app directory (often `~/BillBuddy` or wherever you cloned):

```bash
git fetch origin
git reset --hard origin/main
git log -1 --oneline

sudo docker compose --profile https build --no-cache app
sudo docker compose --profile https up -d --force-recreate --no-deps app
sudo docker compose --profile https up -d caddy

curl -sS https://sharesquad.tanishsheth.com/api/health
```

There is a helper script:

```bash
sudo sh deploy/update.sh
```

Confirm `"version"` in `/api/health` matches the commit you just pushed (see `APP_VERSION` in `app/config.py`). Then open the site in a **private window**.

If `git reset --hard` refuses because of local edits on the VM, move those files aside (`mv docker-compose.yml docker-compose.yml.bak`) and keep `.env` and `deploy/Caddyfile` as they are on the server.

### What not to run

```bash
# DESTROYS the database and uploaded receipts
docker compose down -v
```

Also do not delete Docker volumes `pg-data` or `billbuddy-data`.

`docker compose down` **without** `-v` stops containers and keeps data. Prefer `up -d --force-recreate --no-deps app` instead of down.

---

## 6. Domain, Cloudflare, HTTPS

ShareSquad hostname: **`sharesquad.tanishsheth.com`**

1. Cloudflare → DNS → A record:
   - Name: `sharesquad`
   - IPv4: Oracle public IP
   - Proxy: **DNS only (grey cloud)** so Caddy can get a Let’s Encrypt certificate
2. Server `.env`: `DOMAIN` and `BASE_URL` as above, then recreate `caddy` and `app`
3. Wait for DNS (`nslookup sharesquad.tanishsheth.com`), then HTTPS should work

**Do not orange-cloud (proxy) `sharesquad`.** That usually breaks HTTP-01 certificates.

**Do not** 308-redirect the raw server IP to `https://THE_IP`. Redirect hostnames only.

### Old BillBuddy hostname

To send `billbuddy.tanishsheth.com` to ShareSquad:

1. Keep an A record for `billbuddy` with the same IP
2. Set **that** record to **Proxied (orange)** so Cloudflare rules can run
3. Redirect Rule, **Wildcard pattern**:
   - Request URL: `https://billbuddy.tanishsheth.com/*`
   - Dynamic URL: `https://sharesquad.tanishsheth.com/${1}`
   - 301, preserve query string

If Cloudflare warns the rule may not apply, the `billbuddy` record is still grey. Cancel, orange-cloud `billbuddy` only, deploy again.

---

## 7. First-time cloud install (short)

If the VM is empty (already done for this project; here for later machines):

1. Install Docker + Compose on Ubuntu
2. Clone `https://github.com/TanishSheth/BillBuddy.git`
3. `cp .env.example .env` and set secrets, `BASE_URL`, `POSTGRES_PASSWORD`
4. `sudo docker compose up -d --build` for HTTP on port 80
5. Point DNS, set `DOMAIN` + `APP_HOST_PORT=127.0.0.1:8000`, then  
   `sudo docker compose --profile https up -d`

Open ports **22, 80, 443** on both Oracle’s security list **and** `iptables`/`firewalld` if used.

---

## 8. Backups (keep data)

On the VM, dump Postgres **without** tearing volumes down:

```bash
sudo docker compose exec db pg_dump -U billbuddy billbuddy > ~/sharesquad-backup.sql
```

Leave the file on the server, or copy it later with `scp`. Gzip if it is large:

```bash
gzip -f ~/sharesquad-backup.sql
```

Restoring is a separate, careful step; do not restore over production unless you intend to replace the live DB.

---

## 9. Logs and “the website looks old”

```bash
sudo docker compose logs --tail=80 app
sudo docker compose logs --tail=80 caddy
```

Useful log lines after a good deploy: schema notes like `Added missing column groups.currency`, Caddy obtaining a certificate for `sharesquad.tanishsheth.com`.

If the UI is stale:

- Health version is old → VM never rebuilt (section 5)
- Health version is new but the browser looks old → private window; static files use `?v=` cache bust
- `git pull` says already up to date but `git log -1` is an old hash → unpushed server edits; `fetch` + `reset --hard origin/main`
- Docker build `CACHED` and tiny context → working tree on the VM is still an old commit; reset to `origin/main`, then `--no-cache`

---

## 10. Receipt scanning in production

Docker image includes `tesseract-ocr`. Flow:

1. OCR on the server
2. Vision API only if OCR is unsure **and** a key exists (Settings, group override, or `SERVER_VLM_API_KEY`)
3. Toasts warn only when merchant, date, or total is actually missing

Personal tracker uses the same pipeline. Production Postgres must allow `pending_uploads.group_id` to be empty; the app adds that on startup. If personal scan fails after a deploy, check `docker compose logs app` for schema warnings.

---

## 11. Schema / “can’t create a group”

SQLAlchemy `create_all` does not add columns to an **existing** Postgres database. Startup now runs `_ensure_schema()` (each `ALTER` in its own transaction) for things like `groups.currency`.

If create-group still fails: confirm the running container is the latest commit, then logs. Do not fix it by deleting volumes.

---

## 12. Email

If `SMTP_*` is set, invite emails and optional monthly PDFs go out. If not, copy the invite link from the group Members tab. Never paste SMTP passwords into git or chat logs.

---

## 13. Common pitfalls

| Symptom | Likely cause |
|---------|----------------|
| Can’t create groups on live | Old container or Postgres missing `groups.currency` |
| Personal OCR fails, group OCR works | Old `pending_uploads.group_id` NOT NULL; needs latest app startup migration |
| Live site ignores GitHub | Didn’t `reset --hard` + `--no-cache` rebuild on the VM |
| Let’s Encrypt fails | Cloudflare orange-cloud on `sharesquad`, or DNS not pointing at the VM yet |
| Invite links say localhost | `BASE_URL` not set to the public https URL; recreate `app` |
| Logged out after deploy | `SECRET_KEY` changed in `.env` |
| Empty app after “fixing Docker” | `down -v` or renamed volumes |
| SSH “publickey” / “ubantu” | User is `ubuntu`; key permissions on Windows; not Cloud Shell |
| `git pull` needs a password | GitHub PAT |

---

## 14. Checklist before you call a deploy done

1. `git log -1` on the VM matches GitHub `main`
2. `/api/health` shows the new `"version"`
3. Private window: create a group, open it, scan a receipt, open Personal Tracker
4. Existing users and old groups still listed
5. https://sharesquad.tanishsheth.com loads (not the IP, not mixed HTTP)

---

## 15. API (authenticated)

Most routes need `Authorization: Bearer <token>` from login/register.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | App name, version, environment |
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | JWT |
| GET | `/api/auth/me` | Current user |
| GET/POST | `/api/groups` | List / create groups |
| POST | `/api/groups/{id}/receipts/upload` | Group receipt scan |
| POST | `/api/personal/receipts/upload` | Personal receipt scan |
| GET | `/api/personal/summary` | Personal month |
| GET/PUT | `/api/settings/account` | Account settings |

The browser talks to `/api/...` via `app/static/api.js`.
