# Deploying NFL News Agent to Oracle Cloud Always Free

End-to-end guide to run the pipeline + dashboard on a free Oracle Cloud VM
with invite-only access via Cloudflare Tunnel + Cloudflare Access.

Target: Ubuntu 24.04 LTS, ARM64 (Ampere A1), **always free**, no credit
card charges as long as you stay inside the Always Free shapes.

## Part 1 — Provision the VM

1. Sign up at https://cloud.oracle.com/ (credit card required for
   identity verification; Always Free shapes are not billed).
2. Create a compute instance:
   - Image: **Canonical Ubuntu 24.04** (ARM64 / aarch64)
   - Shape: **VM.Standard.A1.Flex**, 2 OCPU / 12 GB RAM (within Always
     Free limits — you can go up to 4 OCPU / 24 GB across the tenancy)
   - Boot volume: 50 GB is enough (Always Free includes 200 GB)
   - Generate or upload an SSH public key — save the private key
3. On the Networking panel, the instance gets a public IP. You do
   **not** need to open ingress ports, because Cloudflare Tunnel dials
   outbound. Leave the default security list alone.
4. SSH in: `ssh ubuntu@<public-ip> -i ~/.ssh/<your-key>`

> **Note on ARM region capacity.** Some Oracle regions exit of A1 stock.
> If "Out of Capacity" errors block you, pick a less-busy region
> (us-sanjose-1, uk-london-1, eu-frankfurt-1 tend to have availability)
> or retry at off-hours.

## Part 2 — Bootstrap the server

On the VM:

```bash
sudo apt update && sudo apt upgrade -y
git clone <your-private-repo-url> /home/ubuntu/nfl_agent
cd /home/ubuntu/nfl_agent
bash deploy/setup.sh
```

`setup.sh` installs Python 3.12, ffmpeg, creates a venv at `.venv/`,
installs `requirements.txt`, and creates the `data/` and `secrets/`
subdirectories.

## Part 3 — Drop in your secrets

The pipeline needs three secrets. Place them on the VM (not in git):

```bash
# 1. API keys + config
cp .env.example .env     # then edit in your OPENAI_API_KEY, etc.

# 2. Google service account (for Sheets projection snapshots)
#    scp from your Windows box:
#    scp "C:\Users\cwech\Documents\Football\Keys\fp-data-357113-a6174bb87054.json" \
#        ubuntu@<public-ip>:/home/ubuntu/nfl_agent/secrets/service_account.json

# 3. Athletic cookies (optional, for Athletic NFL scraping)
#    scp ubuntu@<public-ip>:.../athletic_cookies.txt secrets/
```

Lock down the secrets directory:

```bash
chmod 700 secrets .env
chmod 600 secrets/* .env
```

## Part 4 — Verify the pipeline runs

```bash
cd /home/ubuntu/nfl_agent
source .venv/bin/activate
python scripts/run_daily.py
```

It should produce `data/reports/$(date +%F).json` and `.html`. If
Whisper transcription errors out, ffmpeg installation is the usual
cause — re-run `apt install -y ffmpeg`.

## Part 5 — Run the dashboard under systemd

```bash
sudo cp deploy/nfl-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nfl-dashboard
sudo systemctl status nfl-dashboard   # should show "active (running)"
```

Streamlit is now listening on `127.0.0.1:8502`. It's not yet reachable
from outside the VM — that's Cloudflare Tunnel's job.

## Part 6 — Schedule the daily pipeline via cron

```bash
crontab -e
# paste the line from deploy/crontab.example
```

This runs `scripts/run_daily.py` at 06:00 America/New_York every day.
The `TZ=` prefix is important — the VM is likely in UTC.

## Part 7 — Expose the dashboard via Cloudflare Tunnel

Prereqs: a domain you own, added to a (free) Cloudflare account.

On the VM:

```bash
# Install cloudflared (ARM64 deb)
curl -L -o cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared.deb

# Authenticate — this opens a URL you visit in your browser
cloudflared tunnel login

# Create the tunnel
cloudflared tunnel create nfl-agent

# Copy the credentials file path that create printed
# (e.g. /home/ubuntu/.cloudflared/<UUID>.json)
# Place it next to the config:
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
# edit config.yml: set tunnel UUID, credentials-file, hostname

# Route DNS — points nfl.yourdomain.com at the tunnel
cloudflared tunnel route dns nfl-agent nfl.yourdomain.com

# Run as a service so it survives reboots
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Visit `https://nfl.yourdomain.com` — you should see the dashboard.

## Part 8 — Gate access to invited emails (Cloudflare Access)

In the Cloudflare dashboard → Zero Trust → Access → Applications:

1. Add an application → type **Self-hosted**.
2. Application domain: `nfl.yourdomain.com`.
3. Create an **Access policy**:
   - Name: "Invited users"
   - Action: Allow
   - Include rule: **Emails** → add every invited address, one per
     line. (Or use an email domain rule for whole-org access.)
4. Identity providers: enable **One-time PIN** (users enter their
   email, receive a 6-digit code) — no Google setup required.

First-time visitors now hit a Cloudflare login page, enter their
email, type the PIN from the email, and are through. Free plan
covers up to 50 users.

## Part 9 — Nightly backup of `data/` (recommended)

Because the VM can be reclaimed if unused, back up to free Cloudflare
R2 (10 GB free) or Backblaze B2 (10 GB free). Simplest option is
rclone + R2 — add another cron line:

```
15 7 * * *  rclone sync /home/ubuntu/nfl_agent/data r2:nfl-agent-backup
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: sentence_transformers` | It's optional; project falls back to SequenceMatcher dedup. Re-install with `pip install sentence-transformers` if you want embedding dedup. |
| Whisper OOMs | The A1 Flex instance has enough RAM at 12 GB. If you shrank it, either raise shape or set `transcription.whisper_model: tiny` in `config/settings.yaml`. |
| `ffprobe not found` | `sudo apt install -y ffmpeg` — fixed by setup.sh but confirm. |
| Tunnel can't connect | Check `sudo systemctl status cloudflared`; re-run `cloudflared tunnel list` to confirm the tunnel is live. |
| Access page doesn't appear | The DNS record must be proxied (orange cloud) in Cloudflare DNS. |

## Ongoing ops

- Pipeline logs: `journalctl -u cron` and `data/logs/YYYY-MM-DD.log`
- Dashboard logs: `journalctl -u nfl-dashboard -f`
- Tunnel logs: `journalctl -u cloudflared -f`
- Updates: `cd /home/ubuntu/nfl_agent && git pull && sudo systemctl restart nfl-dashboard`
