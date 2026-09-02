# Deploy BillBuddy on AWS Free Tier (EC2)

A complete beginner-friendly guide, starting from no AWS account at all.

BillBuddy runs on a small cloud server (a **virtual machine**) using **Docker**. The free option on AWS is **EC2** (not Lightsail). Lightsail is simpler but always costs money.

**What you'll end up with:**
- BillBuddy live at `http://YOUR_SERVER_IP`
- PostgreSQL database (managed inside Docker)
- Emails for invites, password resets, and monthly reports (if you configure Gmail SMTP)

---

## Which AWS “free” plan am I on?

AWS changed Free Tier on **July 15, 2025**. New accounts (including anyone signing up in 2026) are on the **new** plan.

| If you created your AWS account… | What you get | How long | RAM you can actually use |
|----------------------------------|--------------|----------|---------------------------|
| **On or after July 15, 2025** | **Free account plan:** $100 credits at signup, up to $100 more by completing console activities. You **cannot be billed** until you upgrade to a paid plan. Eligible instances include `t3.micro`, `t3.small`, `t4g.micro`, `t4g.small`. | **6 months**, or until credits run out — whichever comes first | Prefer **`t3.small` (2 GB)** if the console labels it *Free tier eligible*. That is enough for PDF reports. |
| **Before July 15, 2025** (legacy) | 750 hours/month of **`t2.micro` or `t3.micro`** | **12 months** from account creation, then you pay | **1 GB only.** Core features work. PDF generation can be slow or fail unless you add swap (this guide does that). |

**Use EC2, not Lightsail.** Lightsail has no 750-hour / credits model. It bills ~$5–10/month from day one.

**Do not run two instances.** On the legacy plan, two micros use 1,500 hours and the extra 750 is billed. On the new plan, a second instance burns credits faster.

---

## What to expect on free tier

| Feature | 2 GB (`t3.small`) | 1 GB (`t2`/`t3.micro`) |
|---------|-------------------|-------------------------|
| Login, groups, expenses, balances | Works | Works |
| Invite links (copy/paste) | Works | Works |
| Invite / password-reset emails | Works if `SMTP_*` is set | Same |
| Receipt scan | Works (users add API keys in Settings) | Same |
| Manual PDF (Reports → Generate Report) | Fine | May be slow; swap helps |
| Auto month-end PDF (11:55 PM last day) | Fine if the instance is running | Same RAM limits as manual |

You do **not** need AI API keys on the server — users add their own in the app's Settings.

---

## Part 1 — Before AWS (do this on your computer)

### Step 1: Put your code on GitHub

AWS will pull your code from GitHub. If it's not there yet:

1. Create a free account at [github.com](https://github.com)
2. Create a new **private** repository (e.g. `billbuddy`)
3. Push your `Expense Splitter Web` project to it

You don't need to do this if you already have a GitHub repo — just note the URL (e.g. `https://github.com/YOUR_USERNAME/billbuddy.git`).

### Step 2: Prepare your `.env` values

You'll fill these in on the server later. Gather them now:

| Variable | What it is | How to get it |
|----------|------------|---------------|
| `SECRET_KEY` | Secures logins | Run `openssl rand -hex 32` on the server (or generate a long random string) |
| `BASE_URL` | Public URL of your app | `http://YOUR_SERVER_IP` (you'll get the IP in Part 3) |
| `POSTGRES_PASSWORD` | Database password | Make up a strong password (e.g. 20+ random characters) |
| `SMTP_*` | Email sending | Gmail app password (see Step 3) |

### Step 3: Set up Gmail for sending emails (recommended)

For invite emails, password resets, and monthly PDF reports:

1. Use a Gmail account (or Google Workspace)
2. Turn on **2-Step Verification** in your Google Account
3. Go to [Google App Passwords](https://myaccount.google.com/apppasswords)
4. Create an app password for "Mail"
5. Save that 16-character password — that's your `SMTP_PASSWORD`

Your SMTP settings will be:

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your-16-char-app-password
SMTP_FROM=your@gmail.com
```

---

## Part 2 — Create an AWS account

### Step 4: Sign up for AWS

1. Go to [aws.amazon.com](https://aws.amazon.com)
2. Click **Create an AWS Account**
3. Enter email, password, and account name
4. Choose **Personal** account type
5. Add a **credit/debit card** (required for identity verification)
6. Complete phone verification
7. **Account plan (new signups):** choose **Free account plan**, not Paid
   - Free plan: you will not be charged until you **upgrade**
   - Free plan lasts **6 months** or until credits are used up
   - You get **$100** credit at signup; you can earn up to **$100 more** from console activities
8. Choose **Basic Support — Free** if asked
9. Sign in to the [AWS Console](https://console.aws.amazon.com)

**Tip:** AWS may place a small temporary verification hold on your card (~$1). That is not your monthly bill.

**Tip:** In Billing, create a **$1 monthly budget** so you get an email if anything unexpected happens.

---

## Part 3 — Create your server (EC2)

### Step 5: Open EC2

1. In the AWS Console search bar, type **EC2**
2. Click **EC2** → **Instances** → **Launch instance**

Pick a **region** in the top-right (e.g. `US East (N. Virginia) us-east-1`) closest to you. You can change region before launching; after launch, the instance lives in that region.

### Step 6: Launch a free-tier instance

Fill in the launch form:

1. **Name:** `billbuddy`

2. **Application and OS Images (AMI):**
   - Choose **Ubuntu**
   - Pick **Ubuntu Server 22.04 LTS** (64-bit x86)
   - It should say *Free tier eligible*

3. **Instance type:**
   - **New Free plan (2026):** prefer **`t3.small`** if it is labeled *Free tier eligible* (2 GB RAM — better for PDFs)
   - If `t3.small` is not free-eligible, or you are on the **legacy 12-month** plan: choose **`t3.micro`** or **`t2.micro`** (1 GB RAM)
   - Do **not** pick `c7i-flex.large` / `m7i-flex.large` for this app — they burn credits quickly

4. **Key pair (login):**
   - Click **Create new key pair**
   - Name: `billbuddy-key`
   - Key pair type: **RSA**
   - Private key format: **`.pem`** (works with Windows PowerShell and Mac/Linux)
   - **Create key pair** — your browser downloads `billbuddy-key.pem`
   - Save it somewhere safe (e.g. `Downloads`). You cannot download it again.

5. **Network settings** — click **Edit**:
   - Auto-assign public IP: **Enable**
   - Firewall (security group): **Create security group**
   - Name: `billbuddy-sg`
   - Add these inbound rules:

     | Type | Protocol | Port | Source | Why |
     |------|----------|------|--------|-----|
     | SSH | TCP | 22 | **My IP** | So only you can log in |
     | HTTP | TCP | 80 | **Anywhere-IPv4** (`0.0.0.0/0`) | So people can open BillBuddy |

   - You do **not** need HTTPS (443) unless you add a domain later

6. **Configure storage:**
   - **20–30 GB** gp3 is enough and stays within typical free-tier disk
   - Do not add extra volumes

7. Click **Launch instance**

Wait until **Instance state** = **Running** (usually under a minute). Click the instance ID to open its page.

### Step 7: Get your server's public IP

On the instance page, copy **Public IPv4 address** (e.g. `3.15.42.67`).

Your `BASE_URL` will be:

```
http://3.15.42.67
```

(replace with your actual IP)

**Note:** This IP can change if you **stop** then **start** the instance (reboot usually keeps it). See Part 6 for a free Elastic IP if you want it to stay the same.

### Step 8: Connect to your server (SSH)

**Option A — Browser (easiest for beginners):**

1. Select the instance
2. Click **Connect**
3. Choose **EC2 Instance Connect**
4. Username should be `ubuntu`
5. Click **Connect**

A browser terminal opens — you're now on the server.

**Option B — From Windows PowerShell:**

1. Put `billbuddy-key.pem` in a folder, e.g. `C:\Users\YOURNAME\Downloads\`
2. Restrict the key permissions (Windows requires this):

```powershell
icacls "C:\Users\YOURNAME\Downloads\billbuddy-key.pem" /inheritance:r
icacls "C:\Users\YOURNAME\Downloads\billbuddy-key.pem" /grant:r "$($env:USERNAME):(R)"
```

3. Connect (replace the path and IP):

```powershell
ssh -i "C:\Users\YOURNAME\Downloads\billbuddy-key.pem" ubuntu@YOUR_PUBLIC_IP
```

The first time, type `yes` when asked to trust the host.

---

## Part 4 — Install and deploy BillBuddy

Run these commands **on the server** (in the SSH terminal).

### Step 9: Install Docker and Git

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2
```

### Step 10: Add swap if you have 1 GB RAM

Skip this step if you launched **`t3.small` (2 GB)**.

On **`t2.micro` / `t3.micro` (1 GB)**, add 2 GB swap so PDF generation is less likely to crash:

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Check it:

```bash
free -h
```

You should see a **Swap** line of about 2 GB.

### Step 11: Clone your project

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/billbuddy.git
cd billbuddy
```

Replace `YOUR_USERNAME/billbuddy` with your actual GitHub repo.

**If the repo is private**, you'll need to authenticate:
- Use a [GitHub Personal Access Token](https://github.com/settings/tokens) as the password when prompted
- Or set up an SSH key on the server

If your files live in a subfolder (for example the repo root contains `Expense Splitter Web/`), `cd` into the folder that has `docker-compose.yml` before the next steps.

### Step 12: Create and edit `.env`

```bash
cp .env.example .env
openssl rand -hex 32
nano .env
```

Copy the `openssl` output into `SECRET_KEY`. Edit the file to look like this (use your real values):

```env
SECRET_KEY=paste-the-openssl-output-here
BASE_URL=http://YOUR_PUBLIC_IP
POSTGRES_PASSWORD=your-strong-db-password

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=your@gmail.com
SMTP_PASSWORD=your-gmail-app-password
SMTP_FROM=your@gmail.com

ACCESS_TOKEN_EXPIRE_DAYS=7
```

On **1 GB** instances only, add this line so the app uses less RAM:

```env
WEB_CONCURRENCY=1
```

**Save in nano:** `Ctrl+O`, Enter, then `Ctrl+X`.

### Step 13: Build and start the app

```bash
sudo docker compose up -d --build
```

First run takes **5–10 minutes** (downloads images, builds the app).

Check it's running:

```bash
sudo docker compose ps
```

Both `app` and `db` should show **running** (or `healthy` for `db`).

View logs if something goes wrong:

```bash
sudo docker compose logs -f app
```

Press `Ctrl+C` to exit logs.

**Shortcut:** if you cloned the repo and are already in the project folder, you can run the helper script instead of Steps 9–13:

```bash
chmod +x deploy/ec2-free-tier-setup.sh
./deploy/ec2-free-tier-setup.sh
```

It installs Docker, adds swap, creates `.env`, and starts the app. You still need to put your `POSTGRES_PASSWORD` and `SMTP_*` in `.env`.

### Step 14: Open BillBuddy in your browser

On your laptop, go to:

```
http://YOUR_PUBLIC_IP
```

You should see the BillBuddy login page. Create an account and test it.

---

## Part 5 — Post-deployment checklist

### Step 15: Test these features

- [ ] Register / login
- [ ] Create a group
- [ ] Add an expense
- [ ] Invite someone (email or copy invite link)
- [ ] Forgot password (needs SMTP configured)
- [ ] Receipt scan (add your API key in **Settings**)
- [ ] **Reports → Generate Report** — on 1 GB this is the feature most likely to struggle

### Step 16: Fix invite links if needed

Invite links use `BASE_URL`. If links show `localhost`, edit `.env`:

```bash
nano .env
# Set BASE_URL=http://YOUR_PUBLIC_IP
sudo docker compose up -d
```

You do not need `--build` for an env-only change, but a recreate is required:

```bash
sudo docker compose up -d
```

If the app still shows the old URL, force recreate:

```bash
sudo docker compose up -d --force-recreate
```

### Step 17: Stay inside free limits

1. Run **only this one** instance
2. AWS Console → search **Billing** → **Free Tier** (legacy) or **Credits** (new plan)
3. Do **not** create RDS, extra EBS volumes, or a load balancer on the free trial unless you know they are covered
4. **Stop** vs **Terminate:**
   - **Stop** = VM is off (legacy hours pause; new-plan credits pause for compute). Disk still exists. Public IP may change on start.
   - **Terminate** = instance is deleted. Data on that instance is gone unless you backed it up.

Month-end PDF emails only send if the instance is **running** at 11:55 PM on the last day of the month.

---

## Part 6 — Optional but recommended

### Elastic IP (so the IP doesn't change on stop/start)

1. EC2 → **Network & Security** → **Elastic IPs** → **Allocate Elastic IP address**
2. Select it → **Actions** → **Associate Elastic IP address** → pick your instance
3. Update `.env` `BASE_URL` if the IP changed, then `sudo docker compose up -d --force-recreate`

On the **legacy** 12-month plan, one attached Elastic IP is typically free. An Elastic IP that is **allocated but not attached** is billed. On the **new Free plan**, this uses credits; keep it associated.

### Custom domain + HTTPS

EC2 does not include Lightsail's one-click load balancer SSL.

Typical beginner path:

1. Buy a domain
2. Create a DNS **A record** pointing to your public IP
3. On the server, install a reverse proxy (Caddy or Nginx) and a Let's Encrypt certificate
4. Set `BASE_URL=https://yourdomain.com` and recreate containers
5. Open port **443** in the security group

If you mainly want HTTPS without learning certificates, Oracle Always Free (more RAM, never expires) or Lightsail's load balancer are simpler later upgrades.

---

## Part 7 — Ongoing maintenance

### Update the app after code changes

```bash
cd ~/billbuddy
git pull
sudo docker compose up -d --build
```

### View logs

```bash
sudo docker compose logs -f app
```

### Restart everything

```bash
sudo docker compose restart
```

### Stop the app (instance still exists and may still count toward hours/credits)

```bash
sudo docker compose down
```

To stop **compute** charges / hour usage: EC2 → instance → **Instance state** → **Stop instance**.

To stop AWS charges entirely for this server: **Terminate** the instance, then release any Elastic IP that is no longer attached.

### After the trial ends

| Result | Next step |
|--------|-----------|
| You want to keep it free forever | Move to **Oracle Always Free** (see `deploy/ORACLE-ALWAYS-FREE.md`) |
| PDFs fail often on 1 GB | Move to Lightsail **$10 / 2 GB**, or Oracle A1 (12 GB) |
| New Free plan hitting 6 months | Upgrade to paid, or migrate before the account closes |
| Don't need 24/7 | Stop the instance when idle; generate reports manually |

---

## Common problems

| Problem | Fix |
|---------|-----|
| Site won't load | Confirm security group allows **HTTP port 80** from `0.0.0.0/0`. Confirm instance is **Running**. |
| `Connection refused` | `sudo docker compose ps` — is `app` running? Check `sudo docker compose logs app` |
| Browser SSH won't open | Use Option B (PowerShell) with the `.pem` file. Username must be `ubuntu`, not `root` or `ec2-user`. |
| Permission denied (publickey) | Wrong key, or key file permissions too open on Windows — rerun the `icacls` commands |
| Login doesn't work | Check `SECRET_KEY` is set; `sudo docker compose logs app` |
| No emails | Verify Gmail app password and `SMTP_*` in `.env`, then `sudo docker compose up -d --force-recreate` |
| PDF generation fails | Use swap on 1 GB; set `WEB_CONCURRENCY=1`; retry when idle. Prefer `t3.small` or Oracle / Lightsail |
| Wrong invite links | Set `BASE_URL` to `http://YOUR_PUBLIC_IP`, recreate containers |
| Unexpected AWS bill | Confirm you launched only one free-eligible instance; stop/terminate extras; check Elastic IPs not left unattached |
| Out of disk | `df -h` — 20–30 GB is usually enough |

---

## Cost summary

| Item | New Free plan (2026) | Legacy 12-month plan |
|------|----------------------|----------------------|
| EC2 instance | Covered by credits / free plan; no charges until you upgrade | $0 for 750 hrs/month of one micro |
| 24/7 for 31 days | Fits; watch credits | 744 hours — fits in 750 |
| Public IPv4 | Uses credits | Often included in free-tier hours |
| Elastic IP (attached) | Uses credits | Typically free while attached |
| Domain (optional) | ~$10–15/year | Same |
| Gmail SMTP | Free | Free |
| After the free period | Upgrade to paid, or migrate | Standard EC2 rates (~$8–15/month for a micro + IP) |

---

## Quick reference — minimum `.env` for AWS EC2

```env
SECRET_KEY=<openssl rand -hex 32>
BASE_URL=http://YOUR_PUBLIC_IP
POSTGRES_PASSWORD=<strong password>

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_TLS=true
SMTP_USER=your@gmail.com
SMTP_PASSWORD=<gmail app password>
SMTP_FROM=your@gmail.com
```

On 1 GB micros, also set `WEB_CONCURRENCY=1`.

---

## Recommended order of operations

1. GitHub repo ready
2. AWS account created — **Free account plan** if you are a new customer
3. EC2 instance (Ubuntu 22.04, free-eligible type, security group 22 + 80)
4. SSH in → install Docker → add swap if 1 GB → clone repo
5. Configure `.env`
6. `sudo docker compose up -d --build`
7. Test at `http://YOUR_IP`
8. (Later) Elastic IP, then custom domain + HTTPS — or migrate to Oracle / Lightsail
