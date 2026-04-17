# NFL News Agent

## Quick Start

```bash
# Run pipeline manually
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py

# Launch dashboard
Launch_Dashboard.bat

# Snapshot projections only
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\snapshot_projections.py

# Scrape depth charts only
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe collectors\depth_chart_collector.py

# Check transaction reconciliation
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\transaction_reconciler.py
```

## Environment

- **Conda env:** `nfl_agent` at `C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe`
- **Do NOT use `conda activate`** in scripts — call the Python exe directly
- **Key packages:** openai, streamlit, feedparser, beautifulsoup4, gspread, google-auth, sentence-transformers, yt-dlp, openai-whisper
- **Secrets:** `.env` (API keys), `secrets/` (cookies), Google service account key at `C:\Users\cwech\Documents\Football\Keys\fp-data-357113-a6174bb87054.json`

## Architecture

7-step daily pipeline:
1. **Collect** — RSS (ESPN, PFT, CBS, Yahoo, Athletic), web (NFL.com), Reddit (r/nfl), YouTube (32 team channels) — all parallel
2. **Deduplicate** — Embedding-based (sentence-transformers) with transaction-aware name matching
3. **Summarize** — OpenAI gpt-5.4-mini with inline source citations [1], [2], etc.
4. **Build report** — JSON + HTML with sections, team highlights (quality-filtered), press conferences
5. **Snapshot projections** — Google Sheets: player metrics (80 cols), fantasy points (PPR/rank), team metrics (66 cols)
6. **Scrape depth charts** — OurLads, all 32 teams, all positions, with change tracking
7. **Cleanup** — Old data pruning

## Google Sheets

- **Spreadsheet ID:** `1bQtJKplmdOAEmKA1zCdSe8TeVFdOqO3fd-vUgtP1dH0`
- **Service account:** `fp-data@fp-data-357113.iam.gserviceaccount.com`
- **Sheets tracked:** `PreSeas_Working_Plyr_Proj` (player metrics), `Preseason_Projections` (fantasy points/rank), `Working_Tm_Proj` (team metrics)
- **Team abbrev mapping:** ARI↔ARZ, BAL↔BLT, CLE↔CLV, HOU↔HST, LAR↔LA
- **Adj columns** = user's manual adjustments. The column preceding each Adj is the projection for that stat.
- **Duplicate header:** "YPA Adj" appears twice (Scramble and Pass) — code disambiguates as "Scramble YPA Adj" / "Pass YPA Adj"

## Key Design Decisions

- **Transaction dedup:** Requires first+last name match. Team names + transaction verbs stripped to prevent false merges of structurally similar titles.
- **Press conference count:** Reports count of summarized (not collected) transcripts. Low-signal content filtered by keyword scoring.
- **Team highlights:** Single-source teams pass through LLM quality gate — filler (trivia, uniforms, mock draft lists) returns "SKIP" and is omitted.
- **Projection rank changes:** Only shown for players whose Adj columns actually changed — prevents noise from cascading rank shifts.
- **Transaction reconciliation:** OurLads depth charts provide position. Only alerts on QB/RB/WR/TE/K. Dismissals are per-transaction, not per-player.
- **Depth chart diffs:** Track ALL positions for promotions/demotions/adds/removes.

## Dashboard Pages

| Page | Purpose |
|------|---------|
| Daily Report | News sections with search, team highlights, transaction alerts |
| Team View | Per-team historical drilldown |
| Projections | 7 tabs: Today's Changes, Fantasy Rankings, Weekly Summary, Transactions, Player Lookup, Player History, Team Projections |
| Depth Charts | Changes (promotions/demotions/etc.) and team browser |
| Transcripts | Press conference transcripts with bulk download |
| Trends | Historical patterns & cost tracking |
| Digest | Weekly rollup reports |

## Scheduling

- Windows Task Scheduler: `NFL_News_Agent_Daily` at 6:00 AM
- `StartWhenAvailable: true` — catches up on missed runs
- `InteractiveToken` logon — must be logged in (screen lock OK)
- Dashboard has a manual run button with live step-by-step progress

## Data Layout

```
data/
  raw/YYYY-MM-DD/          rss.json, web.json, reddit.json, youtube.json
  reports/YYYY-MM-DD.*     .json + .html daily reports
  projections/YYYY-MM-DD/  players.json, fantasy.json, teams.json
  projections/changelog.csv
  projections/transaction_overrides.json
  depth_charts/YYYY-MM-DD.json
  transcripts/
  logs/YYYY-MM-DD.log
  pipeline_status.json     written during runs for dashboard progress
```
