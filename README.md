# ShareSquad

ShareSquad is a multi-user expense splitter and personal spending tracker. People can share a group (trips, apartments, friends), scan receipts, split bills in mixed currencies, settle up on Friends, and keep a separate personal ledger.

This folder is the web app. It is not the older desktop/local splitter. Locally it runs on **port 8001**. Production uses Docker, Postgres, and Caddy on Oracle Always Free.

Live site: **https://sharesquad.tanishsheth.com**  
Source: **https://github.com/TanishSheth/BillBuddy** (repo name is still BillBuddy; the product name is ShareSquad)

How to run it, deploy it, and update it without losing data: **[OPERATIONS.md](OPERATIONS.md)**.

## What you can do

| Area | What it does |
|------|----------------|
| Accounts | Register, sign in, JWT session in the browser |
| Shared groups | Create a group with a base currency, invite by email or link, open/leave/delete |
| Ledger | Expenses list newest-first by **save time**; each row still shows the **receipt/bill date** |
| Month rules | An expense belongs to the **calendar month it was saved**. Older receipt dates saved this month count toward this month’s balances and appear under **Added late** in digests |
| Receipts | Photo upload → wrinkle-aware local OCR first; optional vision provider only if OCR is unsure **and** a key is set |
| Splits | Equal, percent, exact, or shares; payer and participants per expense |
| Money | Each expense can be USD, INR, CAD, EUR, and more; change settlement currency from **PDF Reports** when you need a converted report |
| Balances | Who pays whom for bills saved this month (including late bills). **Record payments on Friends** — group Balances stay in sync |
| Friends | One-to-one private bills plus consolidated group Who-pays-whom; partial/full payments and past payment history |
| Group activity | Activity feed, optional expense notes, and in-app notifications |
| Insights & search | Filter the ledger by month, person, or search; review category totals |
| Reports | PDF / monthly digest with receipts (download; email if SMTP is configured). Digests do **not** lock the month |
| Personal tracker | Your own expenses, same receipt scan, category totals, PDF export |
| Settings | Preferred currency, compact list, your own vision API key, optional per-group override |

## Demo sandbox

The landing-page **Try the full demo** button creates a private, writable sandbox from an internal template keyed to `APP_VERSION`. The template itself is never exposed through a public account or URL.

- The sandbox is the **same current product** regular users get — sample roommate group, late bills, mixed currency, Friends payment history, personal expenses, and notifications.
- Visitors can add, edit, and delete bills; create groups; test receipt scanning; record payments on Friends; and configure their own AI provider key.
- **Reset demo** discards only that visitor’s sandbox and opens a clean copy of the current template. Sandboxes expire after two hours.
- Demo balances stay separated by currency. The app never silently adds INR, USD, or other currencies together.

## Group history and corrections

- Removing a member removes their involvement from active periods; completed reports and history stay preserved.
- Months are **not** auto-locked at month end. Digests are snapshots only. Generating a PDF does not settle/lock the month. Legacy SETTLED months unlock when someone saves a new expense.
- Once a group payment is applied via Friends, ordinary expense edits that affect settled debt stay carefully handled; owners can correct, and members get notified.
- Expense notes are optional clarifications, not required bookkeeping.

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

## Tests

```powershell
python -m pytest test_full_regression.py -v -W default
# or everything:
.\scripts\run_full_tests.ps1
```

## Production in one sentence

Oracle VM runs `app` + Postgres + Caddy. Domain DNS is on Cloudflare (**DNS only / grey cloud** for ShareSquad so Let’s Encrypt works). Updates: push to GitHub `main`, then on the VM `git fetch` + `git reset --hard origin/main` + rebuild **only the app** — never `docker compose down -v`. Confirm `/api/health` `"version"` matches `APP_VERSION` in `app/config.py`.

## Stack

FastAPI · vanilla JS · Tailwind · SQLite (local) / PostgreSQL (cloud) · Caddy · Tesseract OCR · optional vision APIs · WeasyPrint PDFs

Internal Docker/Postgres names are still `billbuddy` on purpose (volumes and DB user). Do not rename them or existing data will look “gone.”

## Project layout

```
Expense Splitter Web/
├── app/                  # API + static UI
│   ├── main.py           # routes, startup schema updates
│   ├── models.py
│   ├── demo_service.py   # versioned demo template + sandboxes
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
