# BillBuddy — Multi-User Expense Splitter

A web app for splitting expenses with **login**, **groups**, **invite links**, **receipt scanning**, and **monthly PDF reports**.

Runs separately from the original `Expense Splitter` (port 8000). This version uses port **8001** locally.

## Quick start (local)

```powershell
cd "Expense Splitter Web"
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Edit .env — set SECRET_KEY and SMTP_* (optional)
.\run.ps1
```

Open **http://localhost:8001**

## Features

| Feature | Description |
|---------|-------------|
| Groups | Isolated expense groups per household, trip, etc. |
| Receipt scan | Upload a photo → AI auto-fills the form (you review before saving) |
| Per-user API keys | Each user picks provider, model, and key in **Settings** |
| Group settings | Override VLM settings per group |
| Balances & settlements | Who owes whom, simplified payment plan |
| PDF reports | Monthly archive with receipts; emailed to all members |
| Invites | Email invite or shareable link |

## Receipt scanning & API keys

**Users bring their own API keys** — configured in **Settings → General**:

1. Turn on **Use my own API key**
2. Choose provider (Google, OpenAI, Anthropic, Mistral, Groq)
3. Choose model
4. Paste API key → Save

Optional **group override** in **Settings → Group Settings**.

The server `.env` does **not** need any AI API key unless you want a shared fallback (`SERVER_VLM_API_KEY`).

## Server `.env` (what the host configures)

| Variable | Local | AWS |
|----------|-------|-----|
| `SECRET_KEY` | Required | Required |
| `BASE_URL` | `http://localhost:8001` | `https://yourdomain.com` |
| `SMTP_*` | Optional | Recommended |
| `POSTGRES_PASSWORD` | Not needed | Required |
| `SERVER_VLM_API_KEY` | Optional fallback | Optional fallback |

See `.env.example` for the full list.

## Email

With SMTP configured, the server sends:
- **Invite emails** when you invite someone by email
- **Monthly PDF reports** to every group member (opt-out in Settings)

Without SMTP: share the **invite link** manually; download reports from the app.

## Deploy on AWS

See **[DEPLOY.md](DEPLOY.md)** for full guides:

- **EC2 free tier** ($0 for 12 months, 1 GB — good for a one-month trial)
- **Lightsail** (~$10/mo, 2 GB — recommended for reliable PDF reports)

Quick deploy on a server:

```bash
git clone https://github.com/YOUR_USERNAME/billbuddy.git
cd billbuddy
cp .env.example .env   # edit SECRET_KEY, BASE_URL, POSTGRES_PASSWORD, SMTP_*
sudo docker compose up -d --build
```

EC2 free tier helper (adds swap for PDFs on 1 GB RAM):

```bash
chmod +x deploy/ec2-free-tier-setup.sh
REPO_URL=https://github.com/YOUR_USERNAME/billbuddy.git ./deploy/ec2-free-tier-setup.sh
```

## Project structure

```
Expense Splitter Web/
├── app/
│   ├── main.py           # API routes
│   ├── models.py         # Database models
│   ├── vlm.py            # Multi-provider receipt extraction
│   ├── settings_service.py
│   ├── emailer.py        # Invites + report emails
│   └── static/           # Frontend (HTML/JS/CSS)
├── deploy/               # AWS setup scripts
├── docker-compose.yml    # Production (app + Postgres)
├── Dockerfile
├── .env.example
├── run.ps1               # Local dev server
└── DEPLOY.md
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/register` | Create account |
| POST | `/api/auth/login` | Get JWT |
| GET | `/api/settings` | User preferences |
| PUT | `/api/settings` | Save preferences |
| GET | `/api/groups/{id}/settings` | Per-group preferences |
| POST | `/api/groups/{id}/receipts/extract` | Scan receipt image |
| * | `/api/groups/{id}/expenses` | Expenses |
| * | `/api/groups/{id}/periods` | Balances, reports |

All group routes require `Authorization: Bearer <token>`.

## Stack

FastAPI · SQLite (local) / PostgreSQL (AWS) · vanilla JS · Tailwind · WeasyPrint · multi-provider VLM
