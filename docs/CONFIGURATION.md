# Configuration, Secrets & Data Layout

## Config files (`config/`)

Loaded by [`config_loader.py`](../config_loader.py), which also calls
`load_dotenv()`. YAML loads are `@lru_cache`d, so the dashboard's Config page
clears the cache after editing.

### `settings.yaml` — runtime tuning

| Section | Key settings |
|---------|-------------|
| `summarization` | `provider` (`openai` default / `anthropic` / `ollama`), `max_output_tokens`, `daily_token_budget` (500k cost cap) |
| `openai` / `anthropic` / `ollama` | per-provider model + options (e.g. `openai.model: gpt-5.4-mini`, `reasoning_effort: high`; `anthropic.use_prompt_caching`). `openai.sections.team_news` tunes the player-news extractor (`model` / `reasoning_effort` / `max_output_tokens`) — set `model: "gpt-5.4"` to upgrade just that section. |
| `content_filter` | `enabled`, `drop_patterns` (regex), `mock_draft_keep_years` (update annually) |
| `injury_classifier` | `enabled`; optional `patterns` / `exclude_patterns` override the built-in title regexes that retag injury-status news into the Injuries section (offseason coverage — NFL.com's game-week pages are empty then) |
| `cross_day_dedup` | `enabled`, `lookback_days` (2), `threshold` (0.82), `skip_categories` (`[transaction]`), `skip_title_patterns` (regexes exempting rolling articles like ESPN's "training camp: Latest intel") |
| `league_wide` | `twitter_exclude_patterns` — regexes making untagged other-sport tweets (MLB/NBA names) ineligible for League-Wide Notes |
| `team_notes` | `item_limit` (12; per-team pool for the Team Notes LLM call, code default 8), `source_limit` (10; per-team flat source list in the report, code default 6) |
| `collection` | `lookback_hours` (28), `youtube_max_per_channel`, `youtube_workers`, `request_delay`, `request_timeout`, `user_agent` |
| `fantasypoints` | `enabled`, `lookback_hours` (24), `max_articles` |
| `nitter` | disabled (instances dead) |
| `transcription` | `whisper_model` (`small`), `delete_audio_after`, `whisper_max_duration_seconds` (1800 gate) |
| `storage` | `reports_to_keep` (90), `raw_data_to_keep` (7) |
| `dashboard`, `schedule`, `logging` | port/host, run time/timezone, log level |

### `sources.yaml` — feeds & sources (no code changes needed to edit)

- `rss_feeds` — national outlets (ESPN, PFT, CBS, The Athletic) + 32 SB Nation
  team blogs, each pre-tagged with `teams:` so single-team posts attribute
  correctly.
- `web_sources` — typed entries (`transactions`, `injuries`, `athletic_nfl`,
  `si_team`) with `enabled` flags.
- `beat_writers` — per-writer RSS/JSON/YouTube/podcast config with declared
  `teams`, `team_focus`, keyword/duration filters.
- `youtube_keywords` — press-conference match terms.

### `teams.yaml` — the 32 teams

Each team: `abbr`, `name`, `conference`, `division`, and `youtube_channels`
(list of `{id, handle, scan_streams?, keyword_filter?}`). Accessed via
`get_teams()`, `get_teams_by_abbr()`. Team-abbreviation mapping between news and
projection styles (ARI↔ARZ, BAL↔BLT, CLE↔CLV, HOU↔HST, LAR↔LA) is handled in the
reconciler/snapshot code, not here.

## Secrets & environment (`.env`, `secrets/`)

API keys go in `.env` (never in YAML); cookies/keys go in `secrets/`.
`config_loader` exposes typed getters that raise a clear error if missing:

| Variable / file | Used for |
|-----------------|----------|
| `OPENAI_API_KEY` | OpenAI summarization (default provider) |
| `ANTHROPIC_API_KEY` | Anthropic summarization |
| `FANTASYPOINTS_AUTH`, `FANTASYPOINTS_SOURCE` | FantasyPoints API headers |
| `ATHLETIC_COOKIES_PATH` → `secrets/athletic_cookies.txt` | The Athletic (Netscape cookies) |
| `GOOGLE_SERVICE_ACCOUNT_KEY` → `secrets/service_account.json` | Google Sheets (projections + reconciliation) |
| `HF_TOKEN` | optional, sentence-transformers downloads |

On **Streamlit Cloud** there is no `.env`; `dashboard.helpers.bootstrap_secrets()`
bridges `st.secrets` into `os.environ`, and `gcp_service_account` /
`dashboard_password` / `GITHUB_PAT` live in Streamlit secrets. On **GitHub
Actions** the workflow writes `.env` and `secrets/*` from Actions secrets at the
start of each run. Templates: `deploy/.env.example`,
`.streamlit/secrets.toml.example`.

## Data layout (`data/`)

No database — everything is flat files. The cloud repo treats `data/` as the
transport layer between pipeline (writes) and dashboard (reads).

```
data/
  raw/YYYY-MM-DD/          rss.json, web.json, reddit.json, beat_writers.json,
                           fantasypoints.json, youtube.json   (per-collector raw output)
  reports/                 YYYY-MM-DD.json + .html  (daily reports)
                           digest_<start>_to_<end>.json  (rollups)
  yt_reports/<key>.json    (durable cache of generated YouTube Report sections;
                            gitignored, never committed)
  projections/YYYY-MM-DD/  players.json, fantasy.json, teams.json
  projections/changelog.csv            (every projection change, all days)
  projections/transaction_overrides.json  (dismissed reconciler alerts)
  depth_charts/YYYY-MM-DD.json          (OurLads snapshots)
  transcripts/YYYY-MM-DD/  *.txt        (one per YouTube transcript)
  logs/YYYY-MM-DD*.log                  (pipeline, -task, -actions, -youtube, -yt-backfill)
  pipeline_status.json     (live step state for the dashboard progress bar)
  source_health.json       (per-source success/failure history)
  youtube_seen.json        (processed video IDs — dedup across runs)
  flagged_findings.json    (visitor flags, both modes; pushed back to repo)
  sheet_recon_dismissals.json  (dismissed depth-chart-manager discrepancies)
  notebooklm_pushed.json   (transcripts already pushed to NotebookLM)
```

### Retention

`run_daily.cleanup_old_data` (step 7) prunes by `storage.*`: reports & logs older
than 90 days, raw & transcript dirs older than 7 days. **On GitHub Actions the
raw/transcript prune is skipped** (detected via the `GITHUB_ACTIONS` env var) so
locally-pushed YouTube data survives for the cloud dashboard; the cloud repo is
reset manually at end of season.

> **Git-push guardrail:** when committing, stage specific code files — never
> `git add -A` or `git add data/`. Visitor-written files like
> `flagged_findings.json` can be silently clobbered otherwise. The CI workflow
> and the dashboard repo-sync stage only their specific paths for this reason.

## Environment baseline

- Conda env `nfl_agent` at `C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe`
  (Python 3.12). Scripts call this exe directly — **never** `conda activate`.
- `collectors/__init__.py` prepends the env's `Library/bin` to PATH so yt-dlp and
  Whisper find `ffmpeg`.
