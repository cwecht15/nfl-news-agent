# Deploying via Streamlit Community Cloud + GitHub Actions

A fully free deployment path. The daily pipeline runs on GitHub Actions
(free 2,000 min/mo, plenty for ~30 min/day) and the dashboard runs on
Streamlit Community Cloud reading data committed back to the repo by
the pipeline workflow.

Trade-offs vs. the Oracle/Hetzner + Cloudflare path:

| | This path | Cloudflare path |
|---|---|---|
| Cost | $0/yr (or $10/yr if you front the domain at the dashboard) | $10–$70/yr |
| Auth | Shared password | Per-user email PIN |
| Always-on | Yes | Yes |
| Pipeline server | GitHub Actions | Your VM |
| Dashboard server | Streamlit Cloud | Your VM |
| Custom domain | Optional | Required |

## Part 1 — GitHub Actions secrets

In https://github.com/cwecht15/nfl-news-agent/settings/secrets/actions
add the following repository secrets:

| Secret name | Value |
|---|---|
| `OPENAI_API_KEY` | Your existing OpenAI key (from local `.env`) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of `C:\Users\cwech\Documents\Football\Keys\fp-data-357113-a6174bb87054.json` (open in Notepad, copy all, paste) |
| `ANTHROPIC_API_KEY` | (Optional) only if you actually use Anthropic |
| `ATHLETIC_COOKIES` | (Optional) entire contents of your athletic cookies file |
| `YOUTUBE_COOKIES` | (Optional, but required for press-conference transcripts on CI — YouTube blocks unauthenticated datacenter IPs) See "YouTube cookies" section below |

The workflow at `.github/workflows/daily.yml` reads these and reconstructs
`.env` and `secrets/service_account.json` on the runner.

## Part 2 — Verify the workflow runs

1. Go to https://github.com/cwecht15/nfl-news-agent/actions
2. Click **"Daily NFL News pipeline"** in the left sidebar
3. Click **"Run workflow"** button → **Run workflow** (manual trigger)
4. Watch it run end-to-end. Should take ~15–30 min.
5. On success, the workflow creates a commit `Daily pipeline run YYYY-MM-DD [skip ci]` to master with all the pipeline outputs under `data/`.

The first run will be slowest (downloads Whisper + sentence-transformer
models). Subsequent runs reuse the cache and are faster.

## Part 3 — Deploy the dashboard on Streamlit Community Cloud

1. Sign in at https://share.streamlit.io with your GitHub account.
2. Click **"New app"** → **"From existing repo"**.
3. Configure:
   - Repository: `cwecht15/nfl-news-agent`
   - Branch: `master`
   - Main file path: `dashboard/app.py`
   - App URL: pick anything — e.g. `nfl-news-agent` → gives you `nfl-news-agent.streamlit.app`
4. Click **"Advanced settings"** → **"Secrets"** → paste:

   ```toml
   dashboard_password = "<pick-a-long-passphrase-here>"
   ```

   This is the password every visitor must enter. Only share with invited people. To rotate later, edit it in Streamlit Cloud's app settings.

5. **(For Depth Chart Manager only)** In the same Secrets editor, append the Google service-account credentials below the password line:

   ```toml
   [gcp_service_account]
   type = "service_account"
   project_id = "fp-data-357113"
   private_key_id = "..."
   private_key = """-----BEGIN PRIVATE KEY-----
   <paste the full multi-line key from your fp-data-357113-*.json file>
   -----END PRIVATE KEY-----
   """
   client_email = "fp-data@fp-data-357113.iam.gserviceaccount.com"
   client_id = "..."
   auth_uri = "https://accounts.google.com/o/oauth2/auth"
   token_uri = "https://oauth2.googleapis.com/token"
   auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
   client_x509_cert_url = "..."
   universe_domain = "googleapis.com"
   ```

   Paste the values from your local service-account JSON file. The `private_key` must be a TOML triple-quoted string so the embedded newlines survive. Without this section, the Depth Chart Manager tab fails with `[Errno 2] No such file or directory`. The other dashboard pages don't need this and will keep working without it.

6. Click **Deploy**. First deploy takes ~5 min (installs requirements).

The dashboard will be live at `https://nfl-news-agent.streamlit.app/`.
Anyone visiting hits the password gate first; only matches let them in.

## Part 4 — Schedule confirmation

The workflow runs daily at **10:00 UTC** (06:00 EDT summer / 05:00 EST winter).
It will commit new data to master, which triggers Streamlit Cloud to redeploy automatically (~1 min). Visitors see the new daily report shortly after the pipeline finishes.

## Part 5 — Sharing the dashboard

Send invited users:
- The URL: `https://nfl-news-agent.streamlit.app/`
- The password (deliver out-of-band — text, signal, in-person, etc.)

Rotate the password if it leaks: Streamlit Cloud → app → Settings → Secrets → save → app restarts.

## Known limitations of this deployment

1. **Flagging is session-only.** The "Flagged" page works during a single
   session but resets when Streamlit Cloud redeploys (i.e., daily after
   the pipeline runs). If durable flagging matters, run the dashboard
   locally for that workflow.
2. **The dashboard's "Run Pipeline" button doesn't work** on Streamlit
   Cloud — there's no way to trigger an ad-hoc pipeline run from the
   cloud UI. Use the GitHub Actions "Run workflow" button instead, or
   wait for the next 06:00 cron.
3. **Repo size grows** ~5–10 MB/day from committed data. The pipeline's
   built-in cleanup (Step 7) prunes old data; expect 1–2 GB/year.
4. **GitHub Actions free tier:** 2,000 min/mo for private repos. You'll
   use ~600–900 min/mo (≈30 min × 30 days). Plenty of headroom.

## Custom domain (optional, $10/yr)

If you want `dash.nfl-news-agent.com` instead of `*.streamlit.app`:
1. Streamlit Cloud → app → Settings → **Custom subdomain** isn't
   supported on the free tier. Custom domains require Streamlit's paid
   plan.
2. **Workaround**: keep `*.streamlit.app` (free), or point your domain
   via a 301 redirect from a free service (Cloudflare Workers free tier
   can do this).

For most users, `nfl-news-agent.streamlit.app` is fine.

## YouTube cookies (for press-conference transcripts on CI)

YouTube blocks unauthenticated requests from datacenter IPs (all GitHub
Actions runners). Without cookies, the YouTube collector logs `ERROR: Sign
in to confirm you're not a bot` for each video and returns no transcripts
for the day. The rest of the pipeline still works.

To enable YouTube transcripts on CI:

**1. Export cookies from your browser (one-time):**
- Install the **"Get cookies.txt LOCALLY"** Chrome extension:
  https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc
- Visit https://www.youtube.com and make sure you're logged in
- Click the extension icon → "Export As → cookies.txt"
- Save the file (e.g., `C:\Users\cwech\Documents\youtube_cookies.txt`)

**2. Add as GitHub secret:**
- Open the cookies file in Notepad
- Copy the entire contents (including all lines starting with `#`)
- https://github.com/cwecht15/nfl-news-agent/settings/secrets/actions
- Click **New repository secret** → Name: `YOUTUBE_COOKIES`, Value: paste the contents
- Click **Add secret**

**3. Re-run the workflow** — YouTube transcripts will now download.

**Cookie expiry:** YouTube cookies typically last several months. If you start seeing the bot-check errors again in the workflow logs, repeat steps 1–2 with a fresh export.

**Privacy note:** these cookies authenticate as YOU. The repository secret is encrypted at rest and only readable by your workflows, but treat the cookies file with the same care as a password.

## Switching back to the Oracle/Hetzner path later

Everything in this deployment is additive — the `.github/workflows/daily.yml`
and `.streamlit/` directory don't conflict with the VM-based deploy in
`deploy/README.md`. You can run both simultaneously, or disable one by
deleting/disabling the workflow.
