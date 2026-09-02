# Deploy BillBuddy on Oracle Cloud (Always Free)

A complete beginner-friendly guide, starting from no Oracle account at all.

BillBuddy runs on a small cloud server (a **virtual machine**) using **Docker**. Oracle Cloud's **Always Free** Ampere A1 VM is the strongest free option for BillBuddy: **2 CPUs and 12 GB RAM**, and it does **not** expire after 6 or 12 months.

**What you'll end up with:**
- BillBuddy live at `http://YOUR_SERVER_IP`
- PostgreSQL database (managed inside Docker)
- Emails for invites, password resets, and monthly reports (if you configure Gmail SMTP)

This is a better free home for PDF reports than AWS's 1 GB micros.

---

## Always Free vs the 30-day trial

New Oracle accounts get **both**:

| Piece | What it is | Expires? |
|-------|------------|----------|
| **Always Free** | A set of resources that stay $0 for the life of the account (in your **home region**) | No |
| **Free Trial** | Extra **$300** credits for 30 days to try paid services | Yes — after 30 days, only Always Free remains |

This guide uses **only Always Free** resources so nothing surprises you when the trial ends.

**Recommended Always Free shape for BillBuddy:**

| Setting | Value |
|---------|--------|
| Shape | **VM.Standard.A1.Flex** (Ampere / ARM) |
| OCPUs | **2** |
| Memory | **12 GB** |
| OS | **Canonical Ubuntu 22.04** (Always Free eligible) |
| Disk | **50 GB** boot volume (counts toward 200 GB Always Free storage) |

Do **not** use **VM.Standard.E2.1.Micro** (AMD, 1 GB) unless A1 is out of capacity. 1 GB is tight for PDFs — same problem as AWS micro.

**ARM is fine for BillBuddy.** Docker builds Linux/ARM images automatically. The first build can take a bit longer than on AWS x86.

---

## Part 1 — Before Oracle (do this on your computer)

### Step 1: Put your code on GitHub

Oracle will pull your code from GitHub (you clone it on the server). If it's not there yet:

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

**You do NOT need AI API keys on the server** — users add their own in the app's Settings.

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

### Step 4: Create an SSH key on your computer (for Option B login)

Skip this if you plan to use Oracle's **browser Cloud Shell connection** only.

**Windows PowerShell:**

```powershell
ssh-keygen -t rsa -b 4096 -f $env:USERPROFILE\.ssh\billbuddy-oracle -N '""'
```

This creates:

- Private key: `C:\Users\YOURNAME\.ssh\billbuddy-oracle` (keep secret)
- Public key: `C:\Users\YOURNAME\.ssh\billbuddy-oracle.pub` (you'll paste this into Oracle)

---

## Part 2 — Create an Oracle Cloud account

### Step 5: Sign up

1. Go to [oracle.com/cloud/free](https://www.oracle.com/cloud/free/)
2. Click **Start for free** / **Sign up**
3. Enter email, verify it, then name, country, and address
4. Complete **phone verification**
5. Add a **credit/debit card** for identity verification
   - Oracle may place a temporary authorization hold (a few dollars). It should drop in 3–5 days
   - Always Free resources are **not charged**
   - Prepaid / virtual cards are often rejected — use a normal credit or debit card
6. **Home region — choose carefully. You cannot change this later.**
   - Always Free VMs can only be created in your **home region**
   - Pick the region closest to you (e.g. US East (Ashburn), US West (Phoenix), Canada Southeast (Toronto), UK South (London), Germany Central (Frankfurt))
7. Finish signup and sign in to the [Oracle Cloud Console](https://cloud.oracle.com)

Account approval can take a few minutes to a few hours. If the console shows a “verifying” banner, wait until it clears.

**Tip:** After login, confirm the region selector (top of the console) is your **home region**. Always Free will fail in other regions.

---

## Part 3 — Create your server (Compute instance)

### Step 6: Open Compute

1. In the console, open the hamburger menu (☰)
2. Go to **Compute** → **Instances**
3. Confirm the region (top right) is your home region
4. Click **Create instance**

### Step 7: Fill in the instance form

**Name:** `billbuddy`

**Placement:** leave the default availability domain. If create fails with “out of host capacity”, try another availability domain (see Common problems).

**Image:**

1. Click **Change image**
2. OS: **Ubuntu**
3. Choose **Canonical Ubuntu 22.04** (or 24.04) labeled **Always Free-eligible**
4. Click **Select image**

**Shape (this is the important part):**

1. Click **Change shape**
2. Specialty type: **Ampere**
3. Shape: **VM.Standard.A1.Flex** (Always Free-eligible)
4. Set:
   - **OCPUs:** `2`
   - **Memory:** `12` GB
5. Click **Select shape**

If A1 is greyed out or create fails later, see the capacity fallback in Common problems.

**Networking:**

1. **Primary VNIC**
2. If you have no VCN yet, choose **Create new virtual cloud network** (Oracle fills in a public subnet)
3. **Assign a public IPv4 address:** **Yes** (Ephemeral is fine; Reserved is nicer — see Part 6)
4. Leave IPv6 off

**SSH keys:**

- **Generate a key pair for me** (easiest): download the private key when offered, and store it like `billbuddy-oracle.pem`
- **Or** paste your public key from Step 4 (`billbuddy-oracle.pub` — one line starting with `ssh-rsa`)

**Boot volume:**

- Custom size: **50 GB** is enough
- Do not use all 200 GB on this one disk unless you need it — 50 GB leaves Always Free storage headroom

Click **Create**. Wait until **State** = **Running** (1–2 minutes).

### Step 8: Open port 80 in Oracle's cloud firewall

Oracle has **two** firewalls. This step is the cloud one. Without it, the site will never load.

1. Open your instance
2. Under **Instance access** / **Primary VNIC**, click the **Subnet** name
3. Open the **Security** tab
4. Click the **Default Security List** (or whichever list is attached)
5. **Add Ingress Rules** → add:

   | Field | Value |
   |-------|--------|
   | Stateless | **Unchecked** (leave stateful) |
   | Source Type | CIDR |
   | Source CIDR | `0.0.0.0/0` |
   | IP Protocol | TCP |
   | Destination Port Range | `80` |
   | Description | HTTP for BillBuddy |

6. Save

SSH port **22** is usually already open. You do not need 443 unless you add HTTPS later.

### Step 9: Get your server's public IP

On the instance page, copy **Public IP address** (e.g. `129.146.12.34`).

Your `BASE_URL` will be:

```
http://129.146.12.34
```

(replace with your actual IP)

### Step 10: Connect to your server (SSH)

**Option A — Browser Cloud Shell (easiest for beginners):**

1. On the instance page, click **Connect** / **Launch Cloud Shell connection**
2. A terminal opens in the browser
3. If you land as another user, switch:

```bash
sudo su - ubuntu
```

**Option B — From Windows PowerShell:**

The Ubuntu username is **`ubuntu`** (not `opc` — `opc` is for Oracle Linux).

```powershell
ssh -i "$env:USERPROFILE\.ssh\billbuddy-oracle" ubuntu@YOUR_PUBLIC_IP
```

If Oracle gave you a `.pem` download:

```powershell
ssh -i "C:\Users\YOURNAME\Downloads\ssh-key-....key" ubuntu@YOUR_PUBLIC_IP
```

If Windows says the key is too open:

```powershell
icacls "C:\path\to\your-key" /inheritance:r
icacls "C:\path\to\your-key" /grant:r "$($env:USERNAME):(R)"
```

Type `yes` the first time you connect.

---

## Part 4 — Install and deploy BillBuddy

Run these commands **on the server**.

### Step 11: Open port 80 on the *server* firewall (required on Ubuntu)

Oracle Ubuntu images block port 80 in **iptables** even after you opened the security list. If you skip this, the browser will hang or show “no route to host”.

Do **not** use `ufw` on Oracle Ubuntu — it fights Oracle's iptables rules.

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo netfilter-persistent save
```

If `netfilter-persistent` is missing:

```bash
sudo apt update
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

### Step 12: Install Docker and Git

```bash
sudo apt update && sudo apt install -y git docker.io docker-compose-v2
```

On ARM, this is normal — Ubuntu installs the ARM64 packages.

### Step 13: Clone your project

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

### Step 14: Create and edit `.env`

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

**Save in nano:** `Ctrl+O`, Enter, then `Ctrl+X`.

You do **not** need `WEB_CONCURRENCY=1` here — 12 GB is plenty. Leave the default (2 workers).

### Step 15: Build and start the app

```bash
sudo docker compose up -d --build
```

First run on ARM often takes **8–15 minutes** (downloads images, may compile some Python packages).

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

### Step 16: Open BillBuddy in your browser

On your laptop, go to:

```
http://YOUR_PUBLIC_IP
```

You should see the BillBuddy login page. Create an account and test it.

If the page never loads, you almost always missed **Step 8** (security list) or **Step 11** (iptables). Both are required.

---

## Part 5 — Post-deployment checklist

### Step 17: Test these features

- [ ] Register / login
- [ ] Create a group
- [ ] Add an expense
- [ ] Invite someone (email or copy invite link)
- [ ] Forgot password (needs SMTP configured)
- [ ] Receipt scan (add your API key in **Settings**)
- [ ] **Reports → Generate Report** — this should work comfortably on 12 GB

### Step 18: Fix invite links if needed

Invite links use `BASE_URL`. If links show `localhost`:

```bash
nano .env
# Set BASE_URL=http://YOUR_PUBLIC_IP
sudo docker compose up -d --force-recreate
```

### Step 19: Idle VM warning (Always Free only)

Oracle may **reclaim** Always Free VMs that look unused for **7 days**:

- CPU (95th percentile) under 20%, **and**
- Network under 20%, **and**
- Memory under 20% (A1 shapes)

A lightly used BillBuddy box with 12 GB RAM can sit under those memory/CPU lines. Using the app (login, add expenses, generate a report) keeps it obviously active. Check the instance weekly for the first month.

If Oracle stops it, you can start a new Always Free instance and redeploy (your Docker volumes are gone unless you backed them up — see Part 6).

---

## Part 6 — Optional but recommended

### Reserved public IP (so IP doesn't change)

1. Instance → **Attached VNICs** → the VNIC → **IPv4 Addresses**
2. Or: **Networking** → **Reserved public IPs** → **Reserve public IP** → attach to the instance
3. Update `BASE_URL` if the address changed, then recreate containers

Ephemeral public IPs can change if you stop/start the instance. Reserved IPs stay put and are included in Always Free networking when used this way.

### Custom domain (e.g. `billbuddy.yourdomain.com`)

1. Buy a domain (Namecheap, Google Domains, Cloudflare, etc.)
2. Add an **A record** pointing to your instance's public IP
3. Update `.env`: `BASE_URL=https://yourdomain.com` (after HTTPS) or `http://yourdomain.com` for now
4. `sudo docker compose up -d --force-recreate`

### HTTPS (secure `https://`)

Always Free includes a small **flexible load balancer** in some accounts; the beginner path is **Caddy** on the VM:

1. Open port **443** in the security list (same way as port 80)
2. Open 443 in iptables:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

3. Point the domain's A record at the VM
4. Install Caddy (or Nginx + Certbot) as a reverse proxy to `localhost:80`, **or** change docker-compose to publish 8000 internally and let Caddy bind 80/443
5. Set `BASE_URL=https://yourdomain.com` and recreate

### Back up the database

Oracle Always Free includes a few block-volume backups. Easier for beginners: dump Postgres from the server occasionally:

```bash
cd ~/billbuddy
sudo docker compose exec db pg_dump -U billbuddy billbuddy > ~/billbuddy-backup.sql
```

Copy `billbuddy-backup.sql` off the server (scp, OneDrive, etc.).

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

### Stop the app (VM still runs; Always Free still occupies the shape)

```bash
sudo docker compose down
```

To fully release the Always Free VM: instance → **More actions** → **Terminate**, and delete the boot volume if prompted. That **erases** the database unless you backed it up.

Oracle does **not** bill Always Free A1 usage. Terminating is only needed if you want the quota back or you are shutting the project down.

---

## Common problems

| Problem | Fix |
|---------|-----|
| **Out of host capacity** | Very common for A1. Try another **availability domain**, leave **fault domain** unassigned, wait hours/days and retry. Fallback: `VM.Standard.E2.1.Micro` (1 GB, AMD) and add swap like the AWS micro guide — PDFs may still struggle. |
| Site won't load / timeout | Do **both** firewalls: security list port **80** *and* iptables rule in Step 11 |
| `No route to host` | Classic missing **iptables** rule. Re-run Step 11. Do not enable `ufw`. |
| `Connection refused` | `sudo docker compose ps` — is `app` running? |
| SSH user rejected | Use `ubuntu` for Ubuntu images. `opc` is Oracle Linux only. |
| Login doesn't work | Check `SECRET_KEY`; `sudo docker compose logs app` |
| No emails | Verify Gmail app password and `SMTP_*`, then `sudo docker compose up -d --force-recreate` |
| Docker build fails on ARM | Wait and retry; first build is slow. Confirm image is Ubuntu ARM (A1), not a mismatched binary you copied from x86. |
| Wrong invite links | Set `BASE_URL` to your public IP/domain, recreate |
| Create instance wants a paid shape | You left home region, or picked a non–Always Free image/shape. Switch region; pick Ampere A1.Flex + Ubuntu Always Free. |
| Trial ended, instance vanished | You created a **paid** shape during the $300 trial. Recreate using **Always Free-eligible** A1.Flex only. |
| Instance stopped after a week idle | Oracle idle reclaim. Start/redeploy; use the app occasionally; consider a reserved IP + backups. |

---

## Cost summary

| Item | Cost |
|------|------|
| Ampere A1 VM (2 OCPU / 12 GB) | **$0** Always Free |
| 50 GB boot volume | **$0** (part of 200 GB Always Free) |
| Public IP (attached) | **$0** |
| Data transfer | Always Free includes a large monthly allowance (plenty for BillBuddy) |
| 30-day trial extras | $300 credits — do not use them for this VM |
| Domain (optional) | ~$10–15/year |
| Gmail SMTP | Free |
| Credit card on file | Verification only; not billed for Always Free |

Stay on Always Free by only creating resources labeled **Always Free-eligible**, in your **home region**.

---

## Quick reference — minimum `.env` for Oracle

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

---

## Recommended order of operations

1. GitHub repo ready  
2. Oracle account created — **home region** closest to you  
3. Always Free A1 instance (Ubuntu 22.04, 2 OCPU / 12 GB, public IP)  
4. Security list: port **80**  
5. SSH in → **iptables port 80** → install Docker → clone repo  
6. Configure `.env`  
7. `sudo docker compose up -d --build`  
8. Test at `http://YOUR_IP`  
9. (Later) reserved IP, custom domain + HTTPS  

Oracle is the best **long-term free** host for BillBuddy. Use AWS Free Tier if you already have AWS, or Lightsail (~$10/month) if you want the simplest paid setup with 2 GB and no capacity hunting.
