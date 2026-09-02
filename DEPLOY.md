# Deploy BillBuddy

BillBuddy runs as a **Docker container** with **PostgreSQL** on a small Linux VM.

| Option | Cost | RAM | Best for | Full beginner guide |
|--------|------|-----|----------|---------------------|
| **Oracle Always Free** (Ampere A1) | **$0**, does not expire | **12 GB** | Best free host; reliable PDFs | [deploy/ORACLE-ALWAYS-FREE.md](deploy/ORACLE-ALWAYS-FREE.md) |
| **AWS EC2 Free Tier** | $0 for 6–12 months, then paid | 1 GB (micro) or 2 GB (`t3.small` on the new Free plan) | Try AWS; PDFs are tight on 1 GB | [deploy/AWS-FREE-TIER.md](deploy/AWS-FREE-TIER.md) |
| **AWS Lightsail** | ~$10/month | 2 GB | Simplest paid production | This file, Lightsail section |

**New to the cloud?** Follow one of the two full guides above (same step-by-step style as Lightsail: account signup → VM → SSH → Docker → `.env` → browser).

**Start from no account:** Oracle is the better long-term free choice. AWS Free Tier is a time-limited trial. Lightsail is easier than EC2 if you are willing to pay ~$10/month.

---

## What you need in `.env` (host only)

```env
SECRET_KEY=<openssl rand -hex 32>
BASE_URL=http://YOUR_IP          # or https://yourdomain.com
POSTGRES_PASSWORD=<strong password>

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=your@gmail.com
SMTP_PASSWORD=<gmail-app-password>
SMTP_FROM=your@gmail.com
```

**You do NOT need any AI API key in `.env`.** Each user adds their own in the app's **Settings** page.

Optional shared fallback (if users haven't configured their own key):

```env
SERVER_VLM_PROVIDER=google
SERVER_VLM_MODEL=gemini-2.5-flash
SERVER_VLM_API_KEY=your-key
```

---

## AWS EC2 Free Tier

**Full beginner walkthrough (account → browser):** [deploy/AWS-FREE-TIER.md](deploy/AWS-FREE-TIER.md)

New AWS accounts (created on or after **July 15, 2025**) get a **6-month Free account plan** with credits, not the old 12-month 750-hour micro. Prefer **`t3.small` (2 GB)** if the console marks it free-eligible; otherwise use a micro and add swap.

Use this condensed path if you already have an AWS account.

### Free tier limits

| Resource | Allowance |
|----------|-----------|
| Instance | **One** t2.micro or t3.micro |
| Hours | **750 hours/month** |
| 24/7 for 31 days | **744 hours** — fits within free tier |
| RAM | **1 GB** — PDF generation may be slow; swap helps |
| Duration | **12 months** from AWS account creation, then paid |

**Use EC2, not Lightsail** — Lightsail has no 750-hour model; it bills ~$5–10/month flat.

### What to expect on 1 GB

| Feature | Expected |
|---------|----------|
| Login, groups, expenses, balances | Works |
| Invite links (copy/paste) | Works |
| Invite / password-reset emails | Works if `SMTP_*` is set |
| Receipt scan | Works (users add API keys in Settings) |
| Manual PDF (Reports → Generate Report) | Test early — may be slow |
| Auto month-end PDF (11:55 PM last day) | Same RAM limits as manual |

---

### Step 1 — Create an AWS account

1. Go to [aws.amazon.com](https://aws.amazon.com) → **Create an AWS Account**
2. Complete signup (credit card required for verification)
3. Choose **Basic Support — Free**
4. Sign in to the [AWS Console](https://console.aws.amazon.com)

---

### Step 2 — Launch a free-tier EC2 instance

1. Search **EC2** → **Launch instance**
2. Configure:
   - **Name:** `billbuddy`
   - **AMI:** Ubuntu Server **22.04 LTS**
   - **Instance type:** **t2.micro** or **t3.micro** (must show *Free tier eligible*)
   - **Key pair:** Create new → download the `.pem` file
   - **Network settings → Security group:** allow
     - **SSH (22)** — My IP (recommended)
     - **HTTP (80)** — Anywhere (`0.0.0.0/0`)
   - **Storage:** 20–30 GB gp3 (within free tier)
3. **Launch instance**
4. Wait until **Instance state** = **Running**
5. Copy **Public IPv4 address** → this is your `BASE_URL` host

---

### Step 3 — Connect to the server

**Browser (easiest):** EC2 → select instance → **Connect** → **EC2 Instance Connect**

**Windows PowerShell:**

```powershell
ssh -i "C:\path\to\your-key.pem" ubuntu@YOUR_PUBLIC_IP
```

---

### Step 4 — Deploy BillBuddy

**Option A — Setup script (recommended for free tier):**

```bash
git clone https://github.com/YOUR_USERNAME/billbuddy.git
cd billbuddy
chmod +x deploy/ec2-free-tier-setup.sh
./deploy/ec2-free-tier-setup.sh
```

The script installs Docker, adds **2 GB swap** (helps PDFs on 1 GB RAM), creates `.env` with a random `SECRET_KEY`, and starts the app. Edit `.env` when prompted for `POSTGRES_PASSWORD` and `SMTP_*`.

**Option B — Manual:**

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2

# Optional but recommended on 1 GB — swap for PDF generation
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

git clone https://github.com/YOUR_USERNAME/billbuddy.git
cd billbuddy

cp .env.example .env
openssl rand -hex 32    # paste into SECRET_KEY in .env
nano .env               # set BASE_URL, POSTGRES_PASSWORD, SMTP_*

sudo docker compose up -d --build
```

Open **http://YOUR_PUBLIC_IP**

---

### Step 5 — One-month test checklist

- [ ] Register / login
- [ ] Create a group, add expenses
- [ ] Copy invite link (Members tab)
- [ ] Send invite email (needs SMTP)
- [ ] Forgot password (needs SMTP)
- [ ] Receipt scan (add API key in Settings)
- [ ] **Reports → Generate Report** — watch `docker compose logs -f app`
- [ ] Optional: leave server on through month-end **11:55 PM** for auto PDF email

**Useful commands:**

```bash
cd ~/billbuddy
sudo docker compose ps
sudo docker compose logs -f app
sudo docker compose restart
```

**Stay in free tier:** run only **one** micro instance 24/7. Check **Billing → Free Tier** in the AWS console.

---

### Step 6 — After the trial month

| Result | Next step |
|--------|-----------|
| Everything works | Stay on EC2 free tier for remaining 11 months |
| PDF fails often | Move to **Lightsail $10** (2 GB) or **Oracle Always Free** (more RAM) |
| Don't need 24/7 | Stop instance when not using; use **manual Generate Report** |
| Free tier ending | Upgrade instance or migrate |

---

## AWS Lightsail (~$10/mo, recommended for production)

Better for **reliable PDF generation** (2 GB RAM).

### 1. Create instance

- [AWS Lightsail](https://lightsail.aws.amazon.com/) → **Ubuntu 22.04**
- Plan: **$10/mo** (2 GB RAM)

### 2. Open port 80

Instance → **Networking** → Add **HTTP** on port **80**

### 3. Deploy

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2

git clone https://github.com/YOUR_USERNAME/billbuddy.git
cd billbuddy

cp .env.example .env
nano .env    # fill in values above

sudo docker compose up -d --build
```

Open **http://YOUR_INSTANCE_IP**

### 4. Custom domain (optional)

1. Lightsail → DNS zone → A record → your IP
2. Set `BASE_URL=https://yourdomain.com` in `.env`
3. `sudo docker compose up -d --build`
4. Add HTTPS via [Lightsail load balancer](https://lightsail.aws.amazon.com/ls/docs/en_us/articles/create-lightsail-load-balancer-and-attach-lightsail-instances)

---

## AWS EC2 (paid / larger instances)

For t3.small+ or the generic setup script:

```bash
chmod +x deploy/aws-setup.sh
REPO_URL=https://github.com/YOUR_USERNAME/billbuddy.git ./deploy/aws-setup.sh
```

Or follow the Lightsail manual steps.

---

## What runs automatically

| Feature | When |
|---------|------|
| Month-end PDF | Last day of month, 11:55 PM (server must be running) |
| Email report | Sent to all members after PDF is generated |
| Manual report | Same — PDF + email from **Reports** tab (anytime) |

Members can opt out of report emails in **Settings → Email monthly reports**.

---

## Update after code changes

```bash
cd ~/billbuddy
git pull
sudo docker compose up -d --build
```

---

## Environment reference

| Variable | Required | Purpose |
|----------|----------|---------|
| `SECRET_KEY` | Yes | Login security |
| `BASE_URL` | Yes | Invite & email links |
| `POSTGRES_PASSWORD` | Yes | Database password |
| `SMTP_*` | For emails | Invites + password reset + monthly reports |
| `SERVER_VLM_*` | No | Optional shared AI fallback |
| `DATA_DIR` | Auto | `/data` in Docker |
| `DATABASE_URL` | Auto | Set by docker-compose |

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Site won't load | Open port **80** in security group / Lightsail firewall |
| Login fails | `sudo docker compose logs app` — check `SECRET_KEY` and DB |
| No emails | Verify `SMTP_*`, restart containers |
| Receipt scan empty | User must add API key in **Settings** |
| PDF fails on free tier | Enable swap (`deploy/ec2-free-tier-setup.sh` does this); retry when idle |
| PDF fails on Lightsail | Use **$10 / 2 GB** plan |
| Wrong invite links | Set `BASE_URL` to public URL, redeploy |
| Over free tier hours | Run only **one** micro instance; stop extra instances |

---

## Local Docker test

```powershell
copy .env.example .env
# Set SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
```

Open http://localhost (port 80)
