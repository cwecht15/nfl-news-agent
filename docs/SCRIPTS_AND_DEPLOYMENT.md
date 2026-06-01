# Scripts, Scheduling & Deployment

## Entry-point scripts (`scripts/`)

| Script | Purpose | Key CLI flags |
|--------|---------|---------------|
| `run_daily.py` | The full 7-step news pipeline (see [ARCHITECTURE.md](ARCHITECTURE.md)). | `--lookback-hours N`, `--include-yt-section`, `--date YYYY-MM-DD` |
| `collect_youtube.py` | Local-only YouTube transcript collection → `youtube.json` + `transcripts/` + updates `youtube_seen.json`. | `--date`, `--lookback-hours` |
| `auto_backfill_youtube.py` | Unattended YT catch-up: detects the last covered date, fills the gap (captions-only by default), then git-pushes only the YouTube paths (rebase, never force). | `--dry-run`, `--no-push`, `--max-gap-days N` (default 14), `--enable-whisper` |
| `backfill_youtube.py` | One-off batch collection over a hardcoded date range (captions-only). | edit constants |
| `recover_backfill.py` | Rebuild `all_videos.json` / `all_transcripts.json` manifests from surviving `.vtt`/`.txt` when a dashboard backfill died before the final dump. | positional date label |
| `snapshot_projections.py` | Google Sheets → JSON snapshots + diffs + `changelog.csv`. | `--diff`, `--history PLAYER` |
| `transaction_reconciler.py` | Flag transactions whose team doesn't match the projection sheet (tracked: QB/RB/WR/TE/K/FB). Overrides in `transaction_overrides.json`. | — |
| `run_digest.py` | Synthesize the last N daily reports into a themed rollup `digest_*.json`. | `--days N` (default 7) |
| `setup_scheduler.py` | Create/delete/query Windows Task Scheduler tasks. | `create [HH:MM]`, `create-yt [HH:MM]`, `delete`, `status` |
| `_audit_source_mix.py`, `_flag_recovery_audit.py`, `_list_si_sources.py` | Maintenance/audit helpers (SI over-representation, flag recoverability from git, SI source listing). | — |

### Projection snapshots — `snapshot_projections.py`

Connects to Google Sheets via a service account (key path from
`GOOGLE_SERVICE_ACCOUNT_KEY` env, else `secrets/service_account.json`, else the
local Football/Keys path). Spreadsheet ID
`1bQtJKplmdOAEmKA1zCdSe8TeVFdOqO3fd-vUgtP1dH0`. Three sheets:

- **`snapshot_players`** (`PreSeas_Working_Plyr_Proj`) — QB/RB/WR/TE only;
  dynamically maps ~80 metric columns from the header, disambiguating the
  duplicate "YPA Adj" (Scramble vs Pass) and skipping historical/reference cols.
- **`snapshot_fantasy`** (`Preseason_Projections`) — Half-PPR/PPR/PPR-G + POS RANK.
- **`snapshot_teams`** (`Working_Tm_Proj`) — `CurSeas` rows, all metric columns.

`diff_snapshots` / `diff_fantasy` emit change records; `write_changelog` appends
to `data/projections/changelog.csv`. `_latest_snapshot(kind, before_date)` powers
same-day-safe diffing. In the daily pipeline, only fantasy rank changes for
players whose **Adj** columns actually changed become report "movers" (prevents
cascade-rank noise).

### Batch wrappers (`.bat`)

`run_daily.bat`, `auto_backfill_youtube.bat` — Task Scheduler wrappers that `cd`
to the project, derive the date, call the conda env `python.exe` **directly** (no
`conda activate`), and tee a wrapper log to `data/logs/<date>-*-task.log`.
`run_dashboard.bat`, `Launch_Dashboard.bat` (browser-opening launcher), and
`Launch_NotebookLM.bat` (Node MCP server) round out the launchers.

## Scheduling (local Windows)

`setup_scheduler.py` writes Task Scheduler XML and registers via `schtasks`:

- **`NFL_News_Agent_Daily`** — `run_daily.bat` at **06:00** (2h limit).
- **`NFL_News_Agent_YT_Backfill`** — `auto_backfill_youtube.bat` at **05:30** (1h
  limit), so transcripts are on disk before the news task.

Both use `StartWhenAvailable: true` (catches up on missed runs),
`RunOnlyIfNetworkAvailable`, and the interactive logon token (must be logged in;
screen lock is fine).

## Cloud CI — `.github/workflows/daily.yml`

- **Trigger:** cron `0 10 * * *` (10:00 UTC = 06:00 EDT / 05:00 EST) + manual
  dispatch. Concurrency group `daily-pipeline` with `cancel-in-progress: false`.
- **Runner:** `ubuntu-24.04`, 90-min timeout, `contents: write`.
- **Steps:** checkout (shallow) → set up Python 3.12 → `pip install -r
  requirements.txt` → cache HF/torch models → write secrets to disk (`.env`,
  `secrets/service_account.json`, `secrets/athletic_cookies.txt` from Actions
  secrets) → `python scripts/run_daily.py` (no `--include-yt-section`) tee'd to a
  log → commit `data/` artifacts back to `master`.
- **Commit-back:** force-adds each artifact path independently (a missing path
  won't abort), commits as `nfl-news-agent-bot` with `[skip ci]` (so the commit
  doesn't re-trigger the workflow), then a 3-attempt fetch/rebase/push loop.
- **No YouTube on CI** (yt-dlp blocked on datacenter IPs) and **cleanup skipped**
  on CI (preserves pushed YouTube data — see [ARCHITECTURE.md](ARCHITECTURE.md)).

## Hosting

### Streamlit Community Cloud (active) — `deploy/STREAMLIT_DEPLOY.md`

The live setup: GitHub Actions runs the pipeline and commits data; Streamlit
Cloud serves `dashboard/app.py` and auto-redeploys on each data commit. Secrets:
`dashboard_password` (the gate) plus a `gcp_service_account` block for the Depth
Chart Manager. Live at **nfl-news-agent.streamlit.app**. Repo grows ~5–10 MB/day;
reset manually at end of season.

### Oracle / generic VM (documented, abandoned) — `deploy/README.md`

Ubuntu 24.04 ARM64 path: `deploy/setup.sh` bootstraps, the dashboard runs under
`deploy/nfl-dashboard.service` (systemd, bound to `127.0.0.1:8502`), cron from
`deploy/crontab.example` runs the pipeline, and `deploy/cloudflared-config.example.yml`
+ Cloudflare Access expose it with email-PIN auth. This path was abandoned over
capacity issues in favor of Streamlit Cloud.

### Dev container — `.devcontainer/devcontainer.json`

Python 3.11 image that auto-runs the dashboard on attach (port 8501 forwarded).

## Dependencies — `requirements.txt`

`anthropic`, `openai`, `streamlit`, `feedparser`, `beautifulsoup4`, `requests`,
`yt-dlp`, `openai-whisper`, `pyyaml`, `python-dotenv`, `jinja2`,
`sentence-transformers` (optional — dedup falls back to `SequenceMatcher`),
`gspread`, `google-auth`, `reportlab`.
