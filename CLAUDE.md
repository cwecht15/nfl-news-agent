# NFL News Agent

## Quick Start

```bash
# Run news pipeline manually (no YouTube — that's a separate tool now)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py

# Collect YouTube transcripts (local-only — yt-dlp doesn't work on CI)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\collect_youtube.py

# Auto YT catch-up: fills in missing days since last run, captions-only,
# then git-pushes to master. Driven by NFL_News_Agent_YT_Backfill task.
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\auto_backfill_youtube.py
# Register the auto-catch-up task (default 05:30 daily; admin shell)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\setup_scheduler.py create-yt

# Local report with the YouTube section attached (run collect_youtube first)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py --include-yt-section

# Re-stamp a prior day's report after a logic change
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py --date 2026-04-30

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

7-step daily pipeline (build-report runs late so depth-chart and projection diffs flow into the report):
1. **Collect** — RSS (ESPN, PFT, CBS, Athletic, plus 32 SB Nation team blogs), web (NFL.com transactions/injuries, Athletic team pages, 32 SI.com team pages), Reddit (r/nfl), beat writers — all parallel. **No YouTube** — transcripts are produced by `scripts/collect_youtube.py` running locally and consumed only by the YouTube Report dashboard tab and (optionally) the local daily report's YT section. Then `processing.quality_filter` strips obvious fluff (voting articles, off-cycle mock drafts, jersey reveals, trivia) before dedup.
2. **Deduplicate** — Embedding-based (sentence-transformers) with transaction-aware name matching. Cross-day filter (2-day lookback, 0.82 cosine) suppresses repeats from previous days.
3. **Summarize** — OpenAI gpt-5.4-mini, single call per section. Article bodies (3.5k–8k chars each) flow into the team-notes prompt so depth-chart and post-draft pieces yield real player-level bullets, not just title-level paraphrase.
4. **Snapshot projections** — Google Sheets: player metrics (80 cols), fantasy points (PPR/rank), team metrics (66 cols). Compares against most-recent snapshot strictly *before today's date* (otherwise same-day re-runs would diff today against itself).
5. **Scrape depth charts** — OurLads, all 32 teams, all positions; tracks promotions / demotions / adds / removes / team changes / position changes. Same prior-day comparison logic as projections.
6. **Build report** — JSON + HTML with: Transactions (position-tagged), Injuries, Depth Chart Movement, Today's Projection Movers, Team Notes (per-team bulleted with `[N]` citations), League-Wide Notes (cross-team items only). When invoked with `--include-yt-section` (local only), `processing.yt_section.build_yt_section` reads `data/raw/<date>/youtube.json` and appends a YouTube subsection (press-conf summary + per-team transcript bullets).
7. **Cleanup** — Old data pruning per `storage.{reports_to_keep, raw_data_to_keep}` in settings.yaml.

## YouTube — separate tool

YouTube collection is decoupled from the news pipeline because yt-dlp doesn't work on GitHub Actions CI. The split:

- **`scripts/collect_youtube.py`** runs locally; calls `collect_youtube` for the chosen date, writes `data/raw/<date>/youtube.json`, `data/transcripts/<date>/*.txt`, and updates `data/youtube_seen.json`.
- **Local daily report** (`run_daily.py --include-yt-section`) reuses the saved file via `_load_existing_transcripts` and calls `processing.yt_section.build_yt_section` to attach press-conf + per-team subsections to that day's report.
- **Cloud daily report** (GHA cron, no flag) ignores transcripts entirely. Transactions / Injuries / Depth Chart / Projections / Team Notes / League-Wide only.
- **Cloud `YouTube Report` dashboard tab** (`dashboard/pages/yt_report.py`) takes a date range, reads matching `youtube.json` files in repo, and calls the same `build_yt_section` on demand. The user pushes transcripts; visitors trigger summarization with one click each. Per-team bullets linkify their `[N]` citations to the source video and the press-conference block lists its source videos (`build_citation_linker` in `dashboard/citations.py`, shared with the Daily Report). Two cache layers: `@st.cache_data` (in-memory) over a durable disk cache (`processing/yt_cache.py` → `data/yt_reports/<key>.json`, keyed by date-range + selected video IDs) so a previously generated range reloads instantly and spends no tokens even after a Streamlit redeploy. Cache files are gitignored and never committed.

## Google Sheets

- **Spreadsheet ID:** `1bQtJKplmdOAEmKA1zCdSe8TeVFdOqO3fd-vUgtP1dH0`
- **Service account:** `fp-data@fp-data-357113.iam.gserviceaccount.com`
- **Sheets tracked:** `PreSeas_Working_Plyr_Proj` (player metrics), `Preseason_Projections` (fantasy points/rank), `Working_Tm_Proj` (team metrics)
- **Team abbrev mapping:** ARI↔ARZ, BAL↔BLT, CLE↔CLV, HOU↔HST, LAR↔LA
- **Adj columns** = user's manual adjustments. The column preceding each Adj is the projection for that stat.
- **Duplicate header:** "YPA Adj" appears twice (Scramble and Pass) — code disambiguates as "Scramble YPA Adj" / "Pass YPA Adj"

## Key Design Decisions

- **Quality pre-filter:** `processing/quality_filter.py` drops items whose titles match configurable regexes (voting/trivia/uniform-reveal/off-cycle-mock-draft) before dedup. Tuning lives in `config/settings.yaml` under `content_filter:`. Keeps fluff out of every downstream stage including LLM cost.
- **Transaction dedup:** Requires first+last name match. Team names + transaction verbs stripped to prevent false merges of structurally similar titles.
- **Dedup group representative:** `pick_primary` in `processing/deduplicator.py` ranks original-reporting outlets (ESPN, Pro Football Talk, CBS Sports, NFL.com, The Athletic, named beat writers, etc.) above aggregator/blog coverage (SBN team blogs, SI team pages, Reddit). When SBN is just commenting on an ESPN scoop, the cited representative is ESPN even if SBN's body is longer. Within a tier, longest summary wins, then earliest published.
- **Transaction position tagging:** `summarize_transactions` builds a name→position map from the latest depth chart and pre-tags lines as `[TEAM / POS]` so the LLM produces bullets like "Lions signed LB Joe Bachie". Side-tagged positions (LDT, MLB, etc.) are normalized to generic ones (DT, LB).
- **Press conference count:** Reports count of summarized (not collected) transcripts. Low-signal content filtered by keyword scoring.
- **Team Notes (renamed from Team Highlights):** One bullet per development, bold named subject, ends with `[N]` citation. Single-source teams still pass through an LLM "SKIP" quality gate. Multi-source teams get bulleted output, not a paragraph synthesis. Transactions and injuries are filtered out of per-team pools (covered by their own sections).
- **Article body fetching:** Athletic, SI, ESPN (national + team API), CBS Sports articles get their `<p>` body extracted at scrape time and stored in `NewsItem.full_text`. SBN feeds expose `content:encoded`, parsed in the RSS collector. Team-notes prompt uses a 5000-char window for "deep" articles (depth charts, post-draft recaps, every-pick breakdowns) and 1200 for ordinary news, so the LLM can reach beyond QB notes into RB/WR/TE/OL coverage.
- **Source diversity in team pools:** Per-team selection runs a relevance score (deep article > ordinary, then body length, then recency) with a soft cap of `limit // 2` items per source. SI items pseudo-stamped at scrape time can't crowd out SBN/Athletic items with real timestamps, but a single source can still take up to half the pool when warranted.
- **Team-notes prompt rules:** Surface non-obvious takeaways (skip "the franchise QB is still the starter"); prioritize offensive skill players (QB/RB/FB/WR/TE) over OL over defense / special teams; never invent a player's first name — if the source only gives a last name, the bullet uses only the last name.
- **League-Wide Notes:** Items with empty `teams` list and `category not in {transaction, injury}`. Capped at 8 bullets, inline `[N]` citations, sources never cited inline are trimmed from the rendered list.
- **Projection rank changes:** Only shown for players whose Adj columns actually changed — prevents noise from cascading rank shifts. Records use `rank_old`/`rank_new` (strings like `"RB12"`); the section renderer parses the numeric tail for sorting and arrow direction.
- **Transaction reconciliation:** OurLads depth charts provide position. Only alerts on QB/RB/WR/TE/K. Dismissals are per-transaction, not per-player.
- **Depth chart diffs:** Track ALL positions for promotions/demotions/adds/removes/team changes/position changes. The daily report uses `before_date=today` when looking up the prior snapshot so multiple same-day runs don't compare today against itself (zero diff).

## Dashboard Pages

| Page | Purpose |
|------|---------|
| Daily Report | Six sections + Team Notes with clickable `[N]` citations; search, transaction alerts. YouTube subsection appears only on locally-generated reports (`run_daily.py --include-yt-section`). |
| YouTube Report | Date-range picker → on-demand LLM summary of pushed transcripts (press-conf summary + per-team bullets). Cached per-session. |
| Team View | Per-team historical drilldown |
| Projections | 7 tabs: Today's Changes, Fantasy Rankings, Weekly Summary, Transactions, Player Lookup, Player History, Team Projections |
| Depth Charts | Changes (promotions/demotions/position-changes/etc.) and team browser |
| Transcripts | Raw press-conference transcripts with bulk-ZIP download, NotebookLM push, backfill |
| Trends | Historical patterns & cost tracking |
| Digest | Weekly rollup reports |
| Flagged | Items you've flagged across reports |
| Config | Edit sources.yaml, settings.yaml |

## Scheduling

- Windows Task Scheduler: `NFL_News_Agent_Daily` at 6:00 AM (news pipeline)
- Windows Task Scheduler: `NFL_News_Agent_YT_Backfill` at 5:30 AM (YouTube catch-up; runs first so transcripts are on disk before the news task). Captions-only by default for fast unattended runs; pushes new YouTube files to master via `git push`.
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
