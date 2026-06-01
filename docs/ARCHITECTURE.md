# Architecture

## System overview

The NFL News Agent is a file-based batch pipeline with a read-mostly web UI on
top. There is no database — every artifact is a JSON/CSV/TXT file under `data/`,
which doubles as the transport layer between the cloud pipeline (writes) and the
cloud dashboard (reads): GitHub Actions commits results to the repo, and
Streamlit Cloud redeploys off those commits.

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         COLLECT  (parallel, ~6 workers)                    │
│  RSS · ESPN team API · web scraper (NFL.com, Athletic, SI) · Reddit ·      │
│  beat writers · FantasyPoints                  [YouTube is a separate tool]│
└───────────────┬────────────────────────────────────────────────────────--┘
                │  list[NewsItem]              raw JSON → data/raw/<date>/
                ▼
┌──────────────────────────┐   drops fluff (regex on titles)
│  quality_filter          │
└───────────────┬──────────┘
                ▼
┌──────────────────────────┐   embeddings (0.75) + transaction-aware name match
│  deduplicate             │   → story groups, one "primary" representative each
└───────────────┬──────────┘
                ▼
┌──────────────────────────┐   suppress titles ≥0.82 similar to prior 2 days
│  cross_day_filter        │
└───────────────┬──────────┘
                ▼
┌──────────────────────────┐   OpenAI / Anthropic / Ollama
│  summarizer              │   transactions · injuries · league-wide ·
│                          │   per-team notes  (+ token/cost tracking)
└───────────────┬──────────┘
                │
   ┌────────────┴───────────────┬─────────────────────┐
   ▼                            ▼                     ▼
┌─────────────────┐   ┌────────────────────┐   ┌──────────────────┐
│ snapshot         │  │ scrape depth charts │   │ build_report     │
│ projections      │  │ (OurLads, 32 teams) │   │ → JSON + HTML    │
│ (Google Sheets)  │  │ diff vs prior day   │   │ → data/reports/  │
│ diff vs prior day│  └────────────────────┘   └────────┬─────────┘
└─────────────────┘                                      │
                                                         ▼
                                          ┌──────────────────────────┐
                                          │ cleanup (retention prune) │
                                          └──────────────────────────┘
                                                         │
                                                         ▼
                                        Streamlit dashboard reads data/reports/
```

## The 7-step daily pipeline

The orchestrator is [`scripts/run_daily.py`](../scripts/run_daily.py),
function `run(lookback_hours, include_yt_section, date_override)`. It writes
progress to `data/pipeline_status.json` after every step so the dashboard can
show a live progress bar. The build-report step runs late so depth-chart and
projection diffs can flow into the report.

| # | Step | Module(s) | Notes |
|---|------|-----------|-------|
| 1 | **Collect** | `collectors/*` | Six collectors run in a `ThreadPoolExecutor` (max 6). A failing collector is caught, logged as an alert, and returns `[]` — it never aborts the run. Each result is saved to `data/raw/<date>/`. Source health recorded per source. |
| 2 | **Deduplicate** | `processing.quality_filter`, `processing.deduplicator` | Quality filter drops fluff first; then embedding-based grouping picks one primary per story. |
| 2b | **Cross-day filter** | `processing.cross_day_filter` | Optional (enabled in settings). Suppresses today's items that repeat the prior 2 days. Runs before summarization so suppressed items cost no tokens. |
| 3 | **Summarize** | `processing.summarizer` | One LLM call per section. Falls back to a "summarization unavailable" stub if the provider isn't configured (pipeline still completes). |
| 4 | **Snapshot projections** | `scripts.snapshot_projections` | Google Sheets → JSON snapshot, diffed against the most recent snapshot *strictly before today*. Non-fatal: failures are logged and skipped. |
| 5 | **Scrape depth charts** | `collectors.depth_chart_collector` | All 32 teams from OurLads, diffed against prior snapshot (`before_date=today`). Non-fatal. |
| 6 | **Build report** | `reports.report_builder` (+ optional `yt_section`, `fp_section`) | Assembles the `DailyReport` dataclass; writes `.json` + `.html`. |
| 7 | **Cleanup** | `run_daily.cleanup_old_data` | Prunes per `storage.*` in settings. **Skipped on GitHub Actions** so the cloud repo keeps raw/transcript data year-round. |

### Catch-up mode

`--lookback-hours N` widens the collection window for a single run, used after
the PC has been off for days (e.g. `--lookback-hours 96`). Windows Task
Scheduler's `StartWhenAvailable` also re-fires missed runs on wake. Default
window is `collection.lookback_hours` (28h — slightly over a day to avoid gaps).

## Core data models

Defined in [`models.py`](../models.py) as dataclasses with `to_dict`/`from_dict`
(ISO datetime round-tripping):

- **`NewsItem`** — the universal unit across all collectors. Fields: `title`,
  `url`, `source`, `source_type`, `published`, `summary`, `full_text` (scraped
  article body), `teams` (abbreviations), `author`, `category`
  (`transaction` / `injury` / `press_conference` / `news`), `ai_summary`.
- **`Transcript`** — a YouTube press-conference transcript: `video_id`, `title`,
  `team`, `channel_name`, `published`, `url`, `text`, `method`
  (`captions` | `whisper`), `duration_seconds`, `ai_summary`.
- **`DailyReport`** — the assembled report: `date`, `generated_at`, `sections`,
  `team_highlights`, `collection_stats`, `llm_usage`, `alerts`,
  `depth_chart_changes`, `projection_movers`, `yt_section`. `from_json` is
  defensive — missing fields fall back to defaults so old reports still load.

## Deployment topology — the local / cloud split

The single most important architectural fact: **the pipeline runs in two places,
and YouTube is deliberately excluded from the cloud one.**

```
   LOCAL (Windows PC)                         CLOUD
   ─────────────────────                      ─────────────────────────────
   Task Scheduler 05:30  ─ auto_backfill_youtube.py ─┐
                                                      │ git push (YouTube data)
   Task Scheduler 06:00  ─ run_daily.py              │
                                                      ▼
                                              GitHub repo (master)
                                                      ▲
   GitHub Actions 10:00 UTC ─ run_daily.py ──────────┘
        (no --include-yt-section)  commits data/ back with [skip ci]
                                                      │
                                                      ▼
                                       Streamlit Community Cloud
                                       (auto-redeploys on data commits)
                                       https://nfl-news-agent.streamlit.app
```

- **Why the split?** `yt-dlp` is rejected by YouTube from datacenter IPs
  ("Sign in to confirm you're not a bot"), so transcript collection only works
  on the user's residential connection. The cloud pipeline runs the news side
  only; the user collects transcripts locally and `git push`es them, and the
  dashboard's **YouTube Report** tab summarizes them on demand.
- **Why commit data back to the repo?** Streamlit Community Cloud has no
  persistent disk and redeploys from git. The repo *is* the database. GitHub
  Actions commits with `[skip ci]` so the commit doesn't re-trigger the workflow.
- **Retention asymmetry:** local runs prune raw/transcript data after 7 days;
  cloud runs skip that prune (detected via `GITHUB_ACTIONS` env var) so pushed
  YouTube data survives for the dashboard. The cloud repo is reset manually at
  end of season.

See [SCRIPTS_AND_DEPLOYMENT.md](SCRIPTS_AND_DEPLOYMENT.md) for the concrete
scheduler tasks, the GitHub Actions workflow, and alternative VM hosting.

## Visitor write-back (flagging)

The dashboard is mostly read-only, but visitors can **flag** findings. Since
Streamlit Cloud containers are ephemeral, flag writes to
`data/flagged_findings.json` are pushed back to `master` via the GitHub
Contents API ([`dashboard/_repo_sync.py`](../dashboard/_repo_sync.py)) with a
debounced auto-push (immediate on cold start, then 60s throttle). The daily
cron and a manual "Save to repo" button are fallbacks. The same mechanism
persists `data/projections/transaction_overrides.json`.

## Design principles that recur throughout

- **Non-fatal stages.** Collectors, projections, depth charts, and the YT/FP
  sections all degrade gracefully — a failure is logged as an alert and the
  pipeline finishes with whatever it has.
- **Token thrift.** Fluff is dropped and cross-day repeats suppressed *before*
  the LLM ever sees them. A `daily_token_budget` hard-caps spend.
- **Original reporting over aggregation.** Dedup's `pick_primary` ranks ESPN /
  PFT / The Athletic / beat writers above SBN blogs / SI pages / Reddit, so the
  cited source is the outlet that broke the story.
- **Never invent.** Summarizer prompts forbid inventing player first names,
  numbers, positions, or stats — last-name-only sources stay last-name-only.
- **Same-day re-run safety.** Projection and depth-chart diffs always compare
  against a snapshot *strictly before today*, so re-running the pipeline twice
  in one day doesn't diff today against itself (which would show zero changes).
