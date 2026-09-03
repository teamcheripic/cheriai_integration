# Deploying the CheriPic AI backend to AWS EC2

Goal: run the FastAPI service 24/7 on a free-tier EC2 instance, behind HTTPS, so
the frontend can hit it from anywhere.

End state: a public URL like `https://cheri-api.yourdomain.com` (or the raw EC2
public DNS for testing) that responds to `GET /health`.

---

## 0. What you'll need (5 min)

- AWS account with billing enabled (free tier covers t3.micro for 12 months)
- An SSH key pair you control (you'll create one in step 1 if you don't have one)
- A domain or subdomain you can point at the server (optional — needed for real HTTPS, not for testing)
- Your **prod** Supabase URL + secret key + OpenAI key handy

---

## 1. Launch the EC2 instance

1. AWS Console → **EC2** → **Launch instance**
2. **Name**: `cheripic-ai`
3. **AMI**: Ubuntu Server 24.04 LTS (free tier eligible)
4. **Instance type**: `t3.micro` (free tier; if your account only has `t2.micro`, that's fine too)
5. **Key pair**:
   - If you don't have one, click **Create new key pair** → Type: RSA → Format: `.pem` → name `cheripic-key` → Download
   - Save the `.pem` to `~/.ssh/cheripic-key.pem`
   - Lock it: `chmod 400 ~/.ssh/cheripic-key.pem`
6. **Network**: default VPC is fine. Under **Firewall (security groups)**:
   - Allow **SSH** from **My IP** (port 22)
   - Allow **HTTP** from anywhere (port 80)
   - Allow **HTTPS** from anywhere (port 443)
7. **Storage**: 8 GB gp3 (default, free tier)
8. Click **Launch instance**

Wait ~30 s, then copy the **Public IPv4 DNS** (looks like `ec2-3-94-xx-xx.compute-1.amazonaws.com`).

---

## 2. SSH in

```bash
ssh -i ~/.ssh/cheripic-key.pem ubuntu@<your-ec2-public-dns>
```

First time: type `yes` to accept the host key.

---

## 3. Install OS-level dependencies

Run these on the EC2 box (all in one block):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.12 python3.12-venv python3-pip git curl
# Caddy for free HTTPS + reverse proxy
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

---

## 4. Get your code on the server

Two options.

### Option A — Clone from GitHub (cleanest, but you need a public/private repo)

```bash
cd /home/ubuntu
git clone https://github.com/<your-user>/cheripic_pwa.git app
cd app/ai_integration/fastapi
```

For a private repo, use a [GitHub deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys) or a fine-grained PAT.

### Option B — Copy from your laptop (one-shot, no git)

From your **laptop** (not on EC2):

```bash
cd /Users/saiswaroop/Documents/Projects/cheripic/pwa/cheripic_pwa
rsync -avz --exclude '__pycache__' --exclude 'venv' --exclude '.DS_Store' \
  -e "ssh -i ~/.ssh/cheripic-key.pem" \
  ai_integration/fastapi/ \
  ubuntu@<your-ec2-public-dns>:/home/ubuntu/cheri-api/
```

Then SSH back in and:

```bash
cd /home/ubuntu/cheri-api
```

---

## 5. Set up the Python environment

On the EC2 box, inside the fastapi/ directory:

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 6. Configure `.env`

```bash
nano .env
```

Paste this template, replacing the placeholders:

```
SUPABASE_URL=https://zkyutnnlskmltiukdmtm.supabase.co
SUPABASE_KEY=sb_secret_xxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxx
OPENAI_MODEL=gpt-4o-mini
HOST=127.0.0.1
PORT=8000
FRONTEND_URLS=https://your-frontend-domain.com,http://localhost:5173
RATE_LIMIT=30/minute
LOG_LEVEL=INFO
ENVIRONMENT=production
```

Save with Ctrl+O, Enter, Ctrl+X.

Important:
- `HOST=127.0.0.1` — only listen on localhost. Caddy proxies the public traffic in.
- `FRONTEND_URLS` — include every origin that'll call this API (CORS).
- `SUPABASE_KEY` — the **secret** key, not the anon/publishable one.

---

## 7. Smoke test — does it run at all?

```bash
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000
```

You should see `Uvicorn running on http://127.0.0.1:8000`. Open a **second SSH** session and verify:

```bash
curl http://127.0.0.1:8000/health
```

Should return `{"status":"ok",...}`. Ctrl+C the first session to stop.

---

## 8. Run it as a service (systemd) so it survives reboots

```bash
sudo nano /etc/systemd/system/cheri-api.service
```

Paste:

```ini
[Unit]
Description=CheriPic AI Backend (FastAPI)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/cheri-api
EnvironmentFile=/home/ubuntu/cheri-api/.env
ExecStart=/home/ubuntu/cheri-api/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

> If your code is at `/home/ubuntu/app/ai_integration/fastapi` (Option A), change all three paths above accordingly.

Enable + start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cheri-api
sudo systemctl start cheri-api
sudo systemctl status cheri-api   # should show "active (running)"
```

Tail the live logs anytime with:

```bash
sudo journalctl -u cheri-api -f
```

---

## 9. Caddy — public HTTPS, zero-config certs

Caddy reverse-proxies port 443/80 → your local uvicorn on 8000, and gets a Let's Encrypt cert for free.

### If you have a domain pointed at the EC2 IP:

```bash
sudo nano /etc/caddy/Caddyfile
```

Replace the file's contents with:

```
cheri-api.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
    encode gzip
}
```

Point an `A` record from `cheri-api.yourdomain.com` → EC2 public IPv4 (in your DNS provider).

### If you just want to test without a domain:

```
:80 {
    reverse_proxy 127.0.0.1:8000
}
```

(HTTPS won't work without a domain — that's fine for testing only.)

Then:

```bash
sudo systemctl reload caddy
sudo systemctl status caddy
```

If you set a domain, Caddy will auto-fetch an SSL cert on first request. Test:

```bash
curl https://cheri-api.yourdomain.com/health
```

---

## 10. Tell the frontend where the API lives

On your **laptop**, edit `cheripic_pwa/.env`:

```
VITE_AI_BACKEND_URL=https://cheri-api.yourdomain.com
```

Rebuild/redeploy the frontend. The chat page will now hit the EC2 box instead of localhost.

---

## 11. Updates (every time you change code)

If you used **Option B** (rsync), re-run the rsync from your laptop, then on EC2:

```bash
cd /home/ubuntu/cheri-api
source venv/bin/activate
pip install -r requirements.txt    # only if requirements changed
sudo systemctl restart cheri-api
```

If you used **Option A** (git), on EC2:

```bash
cd /home/ubuntu/app
git pull
cd ai_integration/fastapi
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart cheri-api
```

---

## Common gotchas

| Symptom | Fix |
|---|---|
| `curl: (7) Failed to connect to ... port 443` | Security group missing the inbound HTTPS rule. Add it. |
| `cheri-api.service: failed` | `sudo journalctl -u cheri-api -n 50` shows the real error. Usually a missing `.env` value or a typo in the `WorkingDirectory` path. |
| Caddy says `permission denied` binding ports | Caddy needs to run via systemd (which it does by default), not directly. `sudo systemctl restart caddy`. |
| CORS error from the frontend | `FRONTEND_URLS` in `.env` doesn't include the prod frontend origin. Update + `sudo systemctl restart cheri-api`. |
| Cert renewal | Caddy auto-renews. Nothing to do. |
| Free tier expiring | t3.micro becomes ~$8/month after 12 months. Consider a Lightsail $5 plan as a swap. |

---

## Estimated monthly cost

- Year 1: **$0** (t3.micro free tier + 1 GB egress free + Caddy free)
- After Year 1: **~$8** for the EC2, +$0–$2 egress unless you're streaming a lot
- OpenAI costs are separate (depends on chat volume; current settings cap each reply at ~120 tokens)

---

## Security notes (don't skip)

1. Restrict SSH to your IP only in the security group — never `0.0.0.0/0` on port 22.
2. Never commit `.env`. The EC2 copy stays on the server.
3. `SUPABASE_KEY` on the server is the **secret** key — it bypasses RLS. If it ever leaks (or you suspect it has), rotate it in the Supabase dashboard and update `.env`, then `sudo systemctl restart cheri-api`.
4. Set up CloudWatch billing alarms (`Billing → Alarms`) so AWS can't surprise you.
