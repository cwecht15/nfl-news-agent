# NFL News Agent — Documentation

A daily NFL news aggregation, summarization, and reporting system. It collects
from ~70 sources (RSS, web scrapers, Reddit, beat writers, FantasyPoints,
YouTube press conferences), deduplicates stories, summarizes them with an LLM,
tracks fantasy projections and depth-chart movement, and publishes a daily
report through a Streamlit dashboard.

> This `docs/` folder is the long-form companion to the root [`CLAUDE.md`](../CLAUDE.md),
> which is the terse operator quick-reference. Start here for "how does it
> work"; go to `CLAUDE.md` for "what command do I run".

## What it does, in one paragraph

Every morning a pipeline pulls the last ~28 hours of NFL news from dozens of
feeds and pages, throws out fluff, merges stories that multiple outlets reported,
and asks an LLM to write a structured briefing: transactions, injuries,
depth-chart changes, projection movers, per-team notes, and league-wide notes.
In parallel it snapshots fantasy projections from a Google Sheet and scrapes all
32 teams' depth charts from OurLads, diffing both against the prior day. The
output is saved as JSON + HTML and surfaced in a multi-page Streamlit dashboard.
The pipeline runs two ways: locally on a Windows PC via Task Scheduler, and in
the cloud via GitHub Actions (which commits results back to the repo so a free
Streamlit Cloud dashboard can serve them). YouTube transcript collection is a
separate, local-only tool because yt-dlp is blocked on CI IPs.

## Documentation map

| Doc | Covers |
|-----|--------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System overview, the 7-step daily pipeline, data flow, deployment topology, the local/cloud split |
| [COLLECTORS.md](COLLECTORS.md) | Every source collector: RSS, web scraper, Reddit, beat writers, FantasyPoints, YouTube, depth charts |
| [PROCESSING.md](PROCESSING.md) | Quality filter, dedup (embeddings + transaction-aware), cross-day filter, the LLM summarizer, section builders, source health, sheet reconciliation |
| [REPORTS_AND_DASHBOARD.md](REPORTS_AND_DASHBOARD.md) | Report assembly, HTML/PDF rendering, citations, flagging, and all 12 dashboard pages |
| [SCRIPTS_AND_DEPLOYMENT.md](SCRIPTS_AND_DEPLOYMENT.md) | Entry-point scripts, projection snapshots, scheduling, GitHub Actions CI, Streamlit Cloud / VM deployment |
| [CONFIGURATION.md](CONFIGURATION.md) | `config/*.yaml`, `.env` / secrets, the data directory layout, retention |
| [TESTING.md](TESTING.md) | The `pytest` suite — what's covered, how to run it, CI |

## Quick start

```bash
# Run the news pipeline manually (no YouTube)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py

# Collect YouTube transcripts (local-only)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\collect_youtube.py

# Local report with YouTube section attached
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py --include-yt-section

# Launch the dashboard
Launch_Dashboard.bat
```

See [SCRIPTS_AND_DEPLOYMENT.md](SCRIPTS_AND_DEPLOYMENT.md) for the full command
catalog and [CONFIGURATION.md](CONFIGURATION.md) for environment setup.

## Tech stack at a glance

- **Language:** Python 3.12 (conda env `nfl_agent`)
- **LLM:** OpenAI `gpt-5.4-mini` by default (pluggable: Anthropic, Ollama)
- **Dedup:** `sentence-transformers` (`all-MiniLM-L6-v2`), with a `SequenceMatcher` fallback
- **Dashboard:** Streamlit (multi-page)
- **Data store:** flat JSON/CSV files under `data/` (no database)
- **External data:** Google Sheets (projections), OurLads (depth charts), yt-dlp + Whisper (YouTube)
- **Hosting:** GitHub Actions (pipeline) + Streamlit Community Cloud (dashboard), both free tier
