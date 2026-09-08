# NFL News Agent

## Quick Start

```bash
# Run news pipeline manually (no YouTube — that's a separate tool now)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_daily.py

# Collect YouTube transcripts (local-only — yt-dlp doesn't work on CI)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\collect_youtube.py

# Collect NFL podcast episodes (RSS; transcript-tag first, show-notes fallback —
# no Whisper, CI-safe). Feeds in config/sources.yaml under `podcasts:`.
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\collect_podcasts.py
# Re-resolve podcast feed URLs from the iTunes API (after editing the name list)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\resolve_podcast_feeds.py

# Collect X/Twitter insider-list tweets (TwitterAPI.io; CI-safe). Routine
# collection is cloud-only via the daily pipeline — this is for manual backfill.
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\collect_twitter.py

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

# --- In-season only (config/settings.yaml -> season.phase: in_season) ---
# Weekly projection sheets: dry run (season/week/counts/active sheet) or real snapshot
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\snapshot_weekly_projections.py --dry-run --sheet both
# nflverse roster snapshot + diff vs previous
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe collectors\nflverse_roster_collector.py
# Injury report tracker (team sites -> RotoWire -> NFL.com), merges into data/injuries/<season>/wkNN.json
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe collectors\injury_report_collector.py
# Rebuild roster state from the event ledger; classify a headline
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m processing.roster_events --rebuild
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m processing.roster_events --classify "Bills placed RB Ray Davis on injured reserve"
# Projection audit ("right guys projected?") against the active weekly sheet
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m processing.projection_audit
# Afternoon update (transactions + nflverse + OurLads + injuries + inactives + audit, report updated in place; no LLM)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_afternoon.py
# Game-day mode: ESPN inactives + audit + report refresh only (what .github/workflows/inactives.yml runs)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe scripts\run_afternoon.py --inactives-only
# Poll inactives directly (--all polls every game of the week; --season/--week/--event for debugging)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe collectors\inactives_collector.py --all
# Season context (phase, week, working sheet)
C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe -m processing.season
```

## Environment

- **Conda env:** `nfl_agent` at `C:\Users\cwech\anaconda3\envs\nfl_agent\python.exe`
- **Do NOT use `conda activate`** in scripts — call the Python exe directly
- **Key packages:** openai, streamlit, feedparser, beautifulsoup4, gspread, google-auth, sentence-transformers, yt-dlp, openai-whisper
- **Secrets:** `.env` (API keys), `secrets/` (cookies), Google service account key at `C:\Users\cwech\Documents\Football\Keys\fp-data-357113-a6174bb87054.json`

## Architecture

7-step daily pipeline (build-report runs late so depth-chart and projection diffs flow into the report):
1. **Collect** — RSS (ESPN, PFT, CBS, Athletic, plus 32 SB Nation team blogs), web (NFL.com transactions/injuries, Athletic team pages, 32 SI.com team pages), Reddit (r/nfl), beat writers, plus — on the cloud run only — X/Twitter insider lists via TwitterAPI.io — all parallel. **No YouTube** — transcripts are produced by `scripts/collect_youtube.py` running locally and consumed only by the YouTube Report dashboard tab and (optionally) the local daily report's YT section. Then `processing.quality_filter` strips obvious fluff (voting articles, off-cycle mock drafts, jersey reveals, trivia) before dedup, and `reclassify_injury_items` retags title-obvious injury-status news to `category="injury"` so the Injuries section works outside game weeks (NFL.com's injury page is empty all offseason).
2. **Deduplicate** — Embedding-based (sentence-transformers) with transaction-aware name matching and a same-team guard (team-tagged non-transaction items with disjoint team sets never merge — keeps the 32 per-team ESPN camp-intel articles from collapsing into one group). Cross-day filter (2-day lookback, 0.82 cosine) suppresses repeats from previous days; `cross_day_dedup.skip_title_patterns` exempts rolling daily-republished articles (ESPN "training camp: Latest intel").
3. **Summarize** — OpenAI gpt-5.4-mini, single call per section. Article bodies (3.5k–8k chars each) flow into the team-notes prompt so depth-chart and post-draft pieces yield real player-level bullets, not just title-level paraphrase.
4. **Snapshot projections** — Google Sheets: player metrics (80 cols), fantasy points (PPR/rank), team metrics (66 cols). Compares against most-recent snapshot strictly *before today's date* (otherwise same-day re-runs would diff today against itself).
5. **Scrape depth charts** — OurLads, all 32 teams, all positions; tracks promotions / demotions / adds / removes / team changes / position changes. Same prior-day comparison logic as projections.
6. **Build report** — JSON + HTML with: Transactions (position-tagged), Injuries, Depth Chart Movement, Today's Projection Movers, Team Notes (per-team bulleted with `[N]` citations), League-Wide Notes (cross-team items only). When invoked with `--include-yt-section` (local only), `processing.yt_section.build_yt_section` reads `data/raw/<date>/youtube.json` and appends a YouTube subsection (press-conf summary + per-team transcript bullets).
7. **Cleanup** — Old data pruning per `storage.{reports_to_keep, raw_data_to_keep}` in settings.yaml.

## In-season mode (season.phase switch)

`config/settings.yaml → season.phase` is the switch: `offseason` runs the pipeline exactly as
described above (preseason sheet snapshot, six sections); `in_season` adds the pieces below.
**Nothing offseason is removed** — flip back to `offseason` next spring. All in-season code is
additive and gated on `processing.season.is_in_season()`; `tests/test_offseason_parity.py` pins
the offseason path.

- **Week + working sheet:** `processing/season.py` reads each weekly sheet's `Working_Game_Proj!C2`
  (Current Week). The **secondary** sheet (`projections.in_season.sheets.secondary`) is the working
  copy **only on Tuesday** (`season.secondary_weekdays`) while MNF finishes; the active sheet is the
  one showing the higher week, tie → main/primary. Schedule (games, byes, opponents, IR return
  math) is cached from the primary sheet's `Schedule` tab into `data/schedule/<year>.json`.
- **Weekly snapshots (Step 4 in-season):** `processing/weekly_projections.py` parses
  `Working_Player_Proj` (32 team blocks, each with its own header row where `G=="ID"`; Status
  col Active/PS/IR; the `#` col is a global row number, so a per-team `depth` is derived), `Working_Game_Proj`, `Player_Projections` (PPR + POS Rank) and
  `Working_Kicker_Proj` into `data/weekly_projections/<season>/wk<NN>/<sheet>/<date>/` +
  `active.json` pointer and its own `changelog.csv`. Separate tree on purpose: `data/projections/`
  and the Projections page stay preseason-only. Reuses `_build_player_col_map` (now takes
  `col_start`/`col_end`; defaults unchanged) and `diff_snapshots`/`diff_fantasy`.
- **Roster events + state (Step 5b):** `collectors/nflverse_roster_collector.py` (GSIS-keyed daily
  baseline, `data/roster/nflverse/<date>.json`) + `processing/roster_events.py` merge four sources
  into `data/roster/events.jsonl` and `data/roster/state.json` — NFL.com transactions (now carry
  structured fields in `NewsItem.extra`; official), nflverse status flips, OurLads reserve-bucket
  crossings (`depth_chart_collector.split_reserve_changes` — IR/PUP/NFI/SUS are status in-season,
  not positions, so within-IR "promotions" are dropped), and insider tweets/news via a
  precision-first regex classifier (`confidence: reported` until confirmed). State tracks
  IR date → `earliest_return_week` (4 games, byes skipped) and practice-squad `elevations_used`
  (max 3). NFL.com's feed has no elevation / IR-activation rows — those come from nflverse flips
  and news.
- **Injury report tracker (Step 5c):** `collectors/injury_report_collector.py` — team sites
  (`https://www.<site_domain>/team/injury-report/`, `site_domain` per team in `config/teams.yaml`;
  official, full Wed/Thu/Fri grid + game status, both clubs per page) → RotoWire league-wide JSON
  (`/football/tables/practice-report.php`) → NFL.com `/injuries/` fallback. Accumulates the week
  in `data/injuries/<season>/wk<NN>.json`, diffs day-over-day into the "Injury Report Changes"
  section (new listing, practice up/downgrade, designation, cleared). The offseason blob
  `scrape_injuries` in `web_scraper.py` is untouched.
- **Game-day inactives (Step 5e):** `collectors/inactives_collector.py` polls ESPN's per-game
  competitor roster (`sports.core.api.espn.com/.../events/<id>/competitions/<id>/competitors/<cid>/roster`)
  for games within 2.5h of kickoff or finished within 30h: `active: false` before kickoff
  (accepted only when 40–53 players are dressed, i.e. the list is really posted), `didNotPlay`
  after. ESPN's JSON APIs 403 browser User-Agents — the collector keeps requests' default UA.
  Identity via ESPN `playerId` → nflverse `espn_id`; unknown players fall back to the athlete
  record (cached in `data/inactives/espn_athletes.json`). Week file
  `data/inactives/<season>/wk<NN>.json`; report section **Game-Day Inactives** (skill positions
  bolded); audit alert `inactive_but_projected`; In Season page "Inactives" tab.
  `.github/workflows/inactives.yml` runs `scripts/run_afternoon.py --inactives-only` right after
  each inactives window (Thu/Sun/Mon evenings, Sun midday/afternoon, plus Wed/Fri/Sat crons that
  no-op without games).
- **Projection audit (Step 5d):** `processing/projection_audit.py` cross-checks the active sheet
  against roster state / nflverse / injuries / inactives / OurLads / schedule: `status_conflict`
  (projected but on IR/PS), `sheet_status_stale`, `wrong_team`, `missing_active` (QB only when
  the starter is missing; FB/returners/KO skipped; a team block with < 8 rows collapses to one
  `team_block_incomplete` warning), `out_but_projected`, `inactive_but_projected`,
  `elevated_not_projected`, `elevation_limit`, `opp_mismatch`, `bye_projected`,
  `ir_return_window`, `unconfirmed_report`, `stale_secondary`. Output `data/audit/<date>-<run>.json`;
  week-scoped dismissal keys in `data/projections/audit_dismissals.json` (cloud: "Save dismissals
  to repo" via `_repo_sync.push_audit_dismissals_to_repo`).
- **Team Notes in-season prompt:** `summarizer._team_note_prompt_multi/_single` return the
  historical prompt text byte-for-byte when `game_line` is None (offseason;
  `tests/test_team_notes_prompt.py` pins it). In-season, `_in_season_game_lines()` injects
  "Week N: BUF visits HOU on Sunday …" (or "on bye") and the bullets are ranked by impact on THIS
  week's projections: usage/role changes → injury-driven opportunity → game plan & matchup →
  elevations/returns → everything else; schedule restatements and betting chatter are excluded.
- **Report + dashboard:** four phase-gated sections (`roster_moves`, `injury_report_changes`,
  `game_day_inactives`, `projection_audit`), `DailyReport.season_meta` / `inactives` /
  `pm_updated_at`, and the **In Season** page (Week / Roster State / Injury Report / Inactives /
  Projection Audit).
- **Afternoon run:** `scripts/run_afternoon.py` (cloud cron `.github/workflows/in_season_pm.yml`,
  22:00 UTC, shares the `daily-pipeline` concurrency group; skips itself in the offseason) —
  transactions + nflverse + OurLads + injuries + audit, then updates `data/reports/<date>.json`
  **in place** (no LLM). `run_in_season_steps` in `run_daily.py` is shared by both runs.

## YouTube — separate tool

YouTube collection is decoupled from the news pipeline because yt-dlp doesn't work on GitHub Actions CI. The split:

- **`scripts/collect_youtube.py`** runs locally; calls `collect_youtube` for the chosen date, writes `data/raw/<date>/youtube.json`, `data/transcripts/<date>/*.txt`, and updates `data/youtube_seen.json`.
- **Local daily report** (`run_daily.py --include-yt-section`) reuses the saved file via `_load_existing_transcripts` and calls `processing.yt_section.build_yt_section` to attach press-conf + per-team subsections to that day's report.
- **Cloud daily report** (GHA cron, no flag) ignores transcripts entirely. Transactions / Injuries / Depth Chart / Projections / Team Notes / League-Wide only.
- **Cloud `YouTube Report` dashboard tab** (`dashboard/pages/yt_report.py`) takes a date range, reads matching `youtube.json` files in repo, and calls the same `build_yt_section` on demand. The user pushes transcripts; visitors trigger summarization with one click each. Per-team bullets linkify their `[N]` citations to the source video and the press-conference block lists its source videos (`build_citation_linker` in `dashboard/citations.py`, shared with the Daily Report). Two cache layers: `@st.cache_data` (in-memory) over a durable disk cache (`processing/yt_cache.py` → `data/yt_reports/<key>.json`, keyed by date-range + selected video IDs) so a previously generated range reloads instantly and spends no tokens even after a Streamlit redeploy. Cache files are gitignored and never committed.

## Podcasts — separate tool

Podcast collection mirrors the YouTube tool but reads RSS instead of YouTube, and needs **no Whisper/yt-dlp** (so it's CI-safe). Strategy: **transcript-tag first, show-notes fallback** — use a show's Podcasting 2.0 `<podcast:transcript>` (VTT/SRT/JSON, parsed by the same `_transcription.parse_subtitle_file`) when present, else the episode's show notes. In practice the big network feeds (Megaphone/Acast/Art19) rarely publish the tag, so most episodes summarize from show notes; flip a per-feed `transcribe: true` flag later to Whisper the audio if notes prove too thin.

- **Feeds:** `config/sources.yaml` under `podcasts:` — `{name, team (abbr or "NFL"), feed_url, itunes_id}`. Resolved from the iTunes Search API via `scripts/resolve_podcast_feeds.py` (re-run after editing the curated name→team list inside it). Loaded by `config_loader.get_podcast_feeds()`.
- **`scripts/collect_podcasts.py`** runs locally (or CI); `collect_podcasts` fetches all feeds in parallel, writes `data/raw/<date>/podcast.json` + `data/transcripts/<date>/pod_*.txt`, and dedups by episode GUID via `data/podcast_seen.json`. Episodes are stored as `models.Transcript` (GUID in `video_id`, show title in `channel_name`, `method` = `transcript`|`shownotes`).
- **`Podcast Report` dashboard tab** (`dashboard/pages/podcast_report.py`) mirrors the YouTube tab: date range → checkbox episode table (sort/filter in pandas to dodge the data_editor sort bug; all episodes checked by default) → on-demand `build_yt_section` (reused — episodes are Transcript lists) rendered as **Episode Highlights** + **Per-Team Notes** with `[N]` citations. Disk cache `processing/podcast_cache.py` → `data/podcast_reports/<key>.json` (gitignored).

## Twitter — cloud-collected source + on-demand report

X/Twitter insider lists are read via the **TwitterAPI.io** REST API (a cheap third-party scraper, ~$0.15/1k tweets — API-key only, CI-safe, unlike yt-dlp). Lists in `config/sources.yaml` under `twitter_lists:` (`{name, list_id, optional team}`); knobs in `config/settings.yaml` under `twitter:`; key in `.env` / GitHub secret `TWITTERAPI_IO_KEY`.

- **Collection is cloud-only.** `run_daily.py` collects tweets ONLY when `GITHUB_ACTIONS` is set (`collect_twitter_on_ci`), so the local scheduled task and the cloud run don't both pull and bill. `collectors/twitter_collector.py` maps each tweet to a `NewsItem` (`source_type="twitter"`, dedup via `data/twitter_seen.json`); they flow through quality-filter → dedup → Team Notes / League-Wide like RSS. Standalone `scripts/collect_twitter.py` + `.github/workflows/twitter.yml` (`workflow_dispatch`-only) are for manual backfill.
- **Fluff scrub:** Twitter-scoped promo/holiday regexes in `content_filter.drop_patterns_by_source_type.twitter` (`quality_filter`); an un-teamed tweet only reaches League-Wide if it carries a news signal or names a known player (`summarizer._league_wide_eligible` / `_TWITTER_LEAGUE_SIGNAL`).
- **`Twitter Report` dashboard tab** (`dashboard/pages/twitter_report.py` + `processing/twitter_section.py`): on-demand LLM report that (1) LLM-attributes each tweet to a team even with no team named (correcting keyword false positives), (2) clusters same-story tweets into one bullet with multi-`[N]` citations to the tweet **account** (`summarizer._citation_source`), and (3) offers a pop-open raw tweet list. Disk cache `processing/twitter_cache.py` → `data/twitter_reports/<key>.json` (gitignored).

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
- **Team Notes (renamed from Team Highlights):** One bullet per development, bold named subject, ends with `[N]` citation. Single-source teams still pass through an LLM "SKIP" quality gate. Multi-source teams get bulleted output, not a paragraph synthesis. Transactions and injuries are filtered out of per-team pools (covered by their own sections). Bullets are **ordered by fantasy impact**; qualitative role signals ("running with the first team", "in the mix for WR3") are kept (numbers are a bonus, not required), only contentless praise is dropped.
- **Player-news extractor is tunable + model-upgradeable:** the Team Notes news call is the highest-value extraction step. Its model / reasoning_effort / max_output_tokens are configurable in `settings.yaml` under `openai.sections.team_news` (ships at `gpt-5.4-mini` @ `reasoning_effort: medium`; set `model: "gpt-5.4"` to upgrade ONLY this section). `_call_model`/`_call_openai` accept an OpenAI-only per-call `model` override; `_record_openai_usage` prices off the call's actual model and labels the run `pricing_model: "mixed"` when sections differ, so cost stays correct and the report footer shows "(mixed models)".
- **ESPN bodies via content API:** `www.espn.com` HTML is bot-walled from GitHub Actions datacenter IPs (page scraping always returned 0 chars on CI). `collect_espn_team_news` fetches `type=="Story"` bodies from `content.core.api.espn.com/v1/sports/news/{id}` instead (not walled), tag-stripped, with page scrape as fallback and `Media` (video) skipped. Log line "ESPN bodies: N via content API, ..." makes failures visible.
- **Article body fetching:** Athletic, SI, ESPN (national + team API), CBS Sports articles get their `<p>` body extracted at scrape time and stored in `NewsItem.full_text`. SBN feeds expose `content:encoded`, parsed in the RSS collector. Team-notes prompt uses a 5000-char window for "deep" articles (depth charts, post-draft recaps, every-pick breakdowns) and 1200 for ordinary news, so the LLM can reach beyond QB notes into RB/WR/TE/OL coverage.
- **Source diversity in team pools:** Per-team selection runs a relevance score (deep article > ordinary, then body length, then recency) with a soft cap of `limit // 2` items per source. SI items pseudo-stamped at scrape time can't crowd out SBN/Athletic items with real timestamps, but a single source can still take up to half the pool when warranted.
- **Team-notes prompt rules:** Surface non-obvious takeaways (skip "the franchise QB is still the starter"); prioritize offensive skill players (QB/RB/FB/WR/TE) over OL over defense / special teams; never invent a player's first name — if the source only gives a last name, the bullet uses only the last name.
- **League-Wide Notes:** Items with empty `teams` list and `category not in {transaction, injury}`. Candidates ordered non-Twitter-outlets-first (then primary source, then recency — pure recency let the tweet firehose crowd real outlets out of the 25-item window); untagged tweets matching `league_wide.twitter_exclude_patterns` (other-sport chatter) are ineligible. Capped at 8 bullets, inline `[N]` citations, sources never cited inline are trimmed from the rendered list.
- **Team Notes pool size is configurable:** `team_notes.item_limit` in settings.yaml (ships at 12; code default 8) sets how many items per team reach the Team Notes LLM call — during camp a busy team can have 90+ candidates. `team_notes.source_limit` (10) sizes the report's flat per-team source list to match.
- **Projection rank changes:** Only shown for players whose Adj columns actually changed — prevents noise from cascading rank shifts. Records use `rank_old`/`rank_new` (strings like `"RB12"`); the section renderer parses the numeric tail for sorting and arrow direction.
- **Transaction reconciliation:** OurLads depth charts provide position. Only alerts on QB/RB/WR/TE/K. Dismissals are per-transaction, not per-player.
- **Depth chart diffs:** Track ALL positions for promotions/demotions/adds/removes/team changes/position changes. The daily report uses `before_date=today` when looking up the prior snapshot so multiple same-day runs don't compare today against itself (zero diff).

## Dashboard Pages

| Page | Purpose |
|------|---------|
| Daily Report | Six sections + Team Notes with clickable `[N]` citations; search, transaction alerts. YouTube subsection appears only on locally-generated reports (`run_daily.py --include-yt-section`). |
| YouTube Report | Date-range picker → on-demand LLM summary of pushed transcripts (press-conf summary + per-team bullets). Cached per-session. |
| Podcast Report | Date-range picker → checkbox episode table → on-demand LLM summary of pushed podcast episodes (Episode Highlights + per-team bullets). Transcript-tag-first, show-notes fallback. Cached. |
| Twitter Report | Date-range picker → on-demand LLM summary of insider-list tweets: LLM team attribution (places tweets even with no team named), same-story clustering, `[N]` citations to the tweet account, plus a pop-open raw tweet list. Cached. |
| Team View | Per-team historical drilldown |
| Projections | 7 tabs: Today's Changes, Fantasy Rankings, Weekly Summary, Transactions, Player Lookup, Player History, Team Projections. Phase-aware via `dashboard/projection_data.py`: preseason snapshots in the offseason, the weekly sheet snapshots in-season (players+kickers / Player_Projections output as "fantasy" / game rows as "teams"; Weekly Summary = this NFL week's first vs latest snapshot). |
| Depth Charts | Changes (promotions/demotions/position-changes/etc.) and team browser. In-season, reserve-list (IR/PUP/NFI/SUS) crossings are shown separately and within-bucket shuffles hidden. |
| In Season | Week overview (games/byes/working sheet), roster state + event feed, weekly injury report grid, projection audit with dismissals. Banner only in the offseason. |
| Transcripts | Raw press-conference transcripts with bulk-ZIP download, NotebookLM push, backfill |
| Trends | Historical patterns & cost tracking |
| Digest | Weekly rollup reports |
| Flagged | Items you've flagged across reports |
| Config | Edit sources.yaml, settings.yaml |

## Scheduling

- Windows Task Scheduler: `NFL_News_Agent_Daily` at 6:00 AM (news pipeline)
- Windows Task Scheduler: `NFL_News_Agent_YT_Backfill` at 5:30 AM (YouTube catch-up; runs first so transcripts are on disk before the news task). Captions-only by default for fast unattended runs; pushes new YouTube files to master via `git push`.
- GitHub Actions: `.github/workflows/in_season_pm.yml` cron 22:00 UTC (in-season only; reads `season.phase` first and exits when offseason). Runs `scripts/run_afternoon.py` and commits `data/roster data/injuries data/audit data/weekly_projections data/schedule data/reports data/depth_charts data/raw data/logs`. `daily.yml` force-adds the same new dirs.
- GitHub Actions: `.github/workflows/podcasts.yml` cron 11:00 UTC (1h after the daily pipeline). Runs `scripts/collect_podcasts.py` on CI — RSS-only, no Whisper/yt-dlp, so it needs no local machine and no API keys — then force-adds only `data/raw/<date>/podcast.json` + `data/podcast_seen.json` and pushes to master (`[skip ci]`, rebase-retry). `workflow_dispatch` allows a manual run with an optional `lookback_hours`. (Unlike YouTube, which can't run on CI, so it stays a local scheduled task.)
- Twitter: collected inside the **cloud** daily pipeline (`daily.yml`, 10:00 UTC) — `run_daily.py` gates it to CI-only (`GITHUB_ACTIONS`) so the local task doesn't also pull/bill. `.github/workflows/twitter.yml` is `workflow_dispatch`-only (manual backfill), NOT a scheduled cron. Needs the `TWITTERAPI_IO_KEY` repo secret.
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
  schedule/<year>.json                  in-season: cached Schedule tab
  weekly_projections/<season>/wk<NN>/<primary|secondary>/<date>/{players,games,output,kickers,meta}.json
  weekly_projections/<season>/active.json, weekly_projections/changelog.csv
  roster/nflverse/<date>.json, roster/events.jsonl, roster/state.json
  injuries/<season>/wk<NN>.json
  inactives/<season>/wk<NN>.json, inactives/espn_athletes.json
  audit/<date>-<am|pm|gameday>.json
  projections/audit_dismissals.json
  transcripts/
  logs/YYYY-MM-DD.log
  pipeline_status.json     written during runs for dashboard progress
```
