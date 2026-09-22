# ShareSquad

ShareSquad is a multi-user expense splitter and personal spending tracker. People can share a group (trips, apartments, friends), scan receipts, split bills in mixed currencies, settle up, and keep a separate personal ledger.

This folder is the web app. It is not the older desktop/local splitter. Locally it runs on **port 8001**. Production uses Docker, Postgres, and Caddy on Oracle Always Free.

Live site: **https://sharesquad.tanishsheth.com**  
Source: **https://github.com/TanishSheth/BillBuddy** (repo name is still BillBuddy; the product name is ShareSquad)

How to run it, deploy it, and update it without losing data: **[OPERATIONS.md](OPERATIONS.md)**.

## What you can do

| Area | What it does |
|------|----------------|
| Accounts | Register, sign in, JWT session in the browser |
| Shared groups | Create a group with a base currency, invite by email or link, open/leave/delete |
| Receipts | Photo upload → wrinkle-aware local OCR first; optional vision provider only if OCR is unsure **and** a key is set |
| Splits | Equal, percent, exact, or shares; payer and participants per expense |
| Money | Each expense can be USD, INR, CAD, EUR, and more; settle the month in another currency |
| Balances | Who owes whom, record a payment, recipient confirmation, and live refresh in a group |
| Group activity | A mobile-friendly activity feed, optional expense notes, and in-app notifications for expense and payment changes |
| Insights & search | Search/filter the ledger by person, category, or your involvement; review category totals and largest expenses |
| Reports | Monthly PDF with receipts (download; email if SMTP is configured) |
| Personal tracker | Your own monthly expenses, same receipt scan, category totals, PDF export |
| Settings | Preferred currency, compact list, your own vision API key, optional per-group override |

## Group history and corrections

- Removing a member removes their involvement from active periods, while completed settled reports and history remain preserved.
- Once a group payment is marked paid, ordinary expense edits are locked. A group owner can make a correction; confirmed payments remain applied, balances are recalculated, and affected members receive an in-app notification.
- Expense notes are optional and intended for clarifications, not required bookkeeping.

## Receipt reading (important)

Having an API key does **not** send every photo to the API.

1. Tesseract OCR always runs first on the server (free, no key). Receipt preparation includes perspective correction, crease-shadow normalization, adaptive thresholding, local text-line fallback, and geometry-aware total matching.
2. If merchant, date, and total all look solid, that result is used and the API is skipped.
3. If any of those is weak **and** a key is available, the photo is sent to the vision model.
4. The warning toast is based on **missing fields on the form**, not on “the API had an error.” If merchant/date/total are filled, you get a success toast even when Gemini was skipped or failed internally.

Add a key in **Settings → Use my own API key** (Google, OpenAI, Anthropic, Mistral, or Groq). Optional group override. Optional server fallback: `SERVER_VLM_API_KEY` in `.env`.

## Quick start (your PC)

```powershell
cd "Expense Splitter Web"
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy .env.example .env
# Set SECRET_KEY in .env. SMTP and API keys are optional.
.\run.ps1
```

Open **http://localhost:8001**

Local data is **SQLite** on disk. Cloud data is **Postgres**. They are not the same database.

## Production in one sentence

Oracle VM runs `app` + Postgres + Caddy. Domain DNS is on Cloudflare (**DNS only / grey cloud** for ShareSquad so Let’s Encrypt works). Updates: push to GitHub `main`, then on the VM `git fetch` + `git reset --hard origin/main` + rebuild **only the app** — never `docker compose down -v`.

## Stack

FastAPI · vanilla JS · Tailwind · SQLite (local) / PostgreSQL (cloud) · Caddy · Tesseract OCR · optional vision APIs · WeasyPrint PDFs

Internal Docker/Postgres names are still `billbuddy` on purpose (volumes and DB user). Do not rename them or existing data will look “gone.”

## Project layout

```
Expense Splitter Web/
├── app/                  # API + static UI
│   ├── main.py           # routes, startup schema updates
│   ├── models.py
│   ├── ocr_extractor.py  # local receipt OCR
│   ├── vlm.py            # OCR then optional API
│   └── static/           # HTML / JS / CSS
├── deploy/
│   ├── Caddyfile
│   └── update.sh         # production pull + rebuild
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── run.ps1
├── README.md
└── OPERATIONS.md
```
