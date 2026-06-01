# Collectors

Everything in `collectors/` turns an external source into `list[NewsItem]` (or
`list[Transcript]`), tags teams, and saves raw JSON to `data/raw/<date>/`. All
collectors accept an optional `lookback_hours` override and are polite (per-feed
delays, configurable user agent). Errors are logged and return partial results
rather than raising.

Common helpers: `_detect_teams(text, teams_by_abbr)` (in `rss_collector.py`, used
widely) matches full names, nicknames, cities, and **case-sensitive** uppercase
abbreviations (so "WAS" the abbreviation isn't matched in "was" the word). Date
parsing falls back through `feedparser` structs, RFC 2822, and ISO 8601.

## RSS — `rss_collector.py`

- **`collect_rss(lookback_hours=None, teams_by_abbr=None)`** — polls every feed
  in `config/sources.yaml → rss_feeds` (national outlets + 32 SB Nation team
  blogs). Single-team blogs are pre-tagged via the feed's `teams:` key so a post
  whose title omits the team name still gets attributed. For feeds that ship no
  body, it enriches ESPN/CBS/NBC/Yahoo articles via
  `web_scraper.fetch_article_body()`. SBN feeds expose `content:encoded`, parsed
  into `full_text`. Double-encoded HTML entities (cincyjungle, hogshaven) are
  fixed with `html.unescape`.
- **`collect_espn_team_news(lookback_hours=None, teams_by_abbr=None)`** — hits the
  ESPN team news API (`site.api.espn.com/.../news?team={id}`) for all 32 teams,
  dedupes articles that surface under multiple teams, and fetches bodies.
- Saves via `save_rss_results(items, date_str)` → `data/raw/<date>/rss.json`
  (ESPN team items are merged into the same file by the orchestrator).

## Web scraper — `web_scraper.py`

`collect_web(lookback_hours=None)` dispatches by the `type` of each enabled entry
in `config/sources.yaml → web_sources`:

- **NFL.com transactions** (`scrape_transactions_category_pages`) — scrapes the
  trades / signings / reserve-list / waivers / terminations / other category
  pages for the current and previous month, parses table rows (date, name, type,
  from/to team, position) into titles like `"Name: from Team -> to Team (Type)"`,
  category `transaction`.
- **NFL.com injuries** (`scrape_injuries`) — the league injuries page, by team,
  category `injury`.
- **The Athletic** (`scrape_athletic_nfl`) — **cookie-authenticated**
  (Netscape-format file; path resolved by `config_loader.get_athletic_cookies_path`).
  Discovers team pages from the NFL hub, extracts articles + bodies. Validates
  cookie freshness and surfaces a structured status (missing / expired / auth
  redirect) consumed by the dashboard.
- **SI.com team pages** (`scrape_si_team`) — 32 team indices; no RSS, so it
  extracts `/nfl/<team>/onsi/<slug>` links and bodies. Items are pseudo-stamped
  at scrape time (no real publish date), so freshness is governed by lookback +
  cross-day dedup, and they're rate-limited in team pools so they can't crowd out
  timestamped sources.
- **Generic body extraction** — `fetch_article_body(session, url, ...)` uses
  domain-specific CSS selectors (ESPN, CBS, NBC, Yahoo), retries once on request
  errors, caps at 8000 chars.
- Source status is tracked via `_set_source_status` / `get_last_web_source_status`.
- Saves via `save_web_results` → `data/raw/<date>/web.json`.

## Reddit — `reddit_collector.py`

`collect_reddit(lookback_hours=None)` reads `r/nfl/new/.rss`. Most insider
breaking news (Schefter, Rapoport) gets reposted to r/nfl within minutes, so this
is an effective proxy for Twitter, which is otherwise dead (see below).
`_should_skip` filters highlights, game threads, daily/weekly talk threads, and
meme threads. `_extract_reporter` pulls the `[Reporter]` bracket prefix, sets
`source` to `"r/nfl via @{reporter}"`, and `_clean_title` strips the prefix. The
external article URL is regex-extracted from the entry HTML (not the Reddit
comments link). Saves → `data/raw/<date>/reddit.json`.

## Beat writers — `beat_writer_collector.py`

`collect_beat_writers(date_str, lookback_hours=None, teams_by_abbr=None, skip_youtube=False)`
returns `(news_items, transcripts)` for the writers configured in
`config/sources.yaml → beat_writers`. Each writer can have RSS feeds, a custom
JSON feed, YouTube channels, and podcasts:

- **RSS / custom feeds** — `_collect_writer_rss`, plus a typed dispatcher
  (`_collect_writer_custom_feed`) that currently handles `neworleans.football`'s
  JSON API (`_collect_nof_feed`: long-form articles + short "quickPosts", author
  filtering, premium tagging).
- **YouTube** (`_collect_writer_youtube`) — filters by min duration (drops Shorts),
  optional `team_focus`, and keywords; downloads captions or audio and uses
  Whisper if needed. `skip_youtube=True` on CI (yt-dlp is blocked there).
- **Podcasts** (`_collect_writer_podcasts`) — RSS enclosures, optionally
  transcribed via `_transcription.transcribe_audio_url`, transcript stored in
  `full_text`.
- Auto-detected teams are merged with the writer's declared `teams`. Processed
  YouTube IDs are tracked in `data/youtube_seen.json`.
- Saves via `save_beat_writer_results` → `data/raw/<date>/beat_writers.json`.

## FantasyPoints — `fantasypoints_collector.py`

`collect_fantasypoints(lookback_hours=None, limit=None, teams_by_abbr=None)` hits
the FantasyPoints v2 API (`api.fantasypoints.com/v2/articles/nfl/recent`).
Requires `FANTASYPOINTS_AUTH` + `FANTASYPOINTS_SOURCE` env vars. These articles
**do not enter the general dedup pool** — they render in their own report section
(`processing.fp_section`). `_articles_from_payload` tolerates multiple API
shapes; `_build_article_url` reconstructs canonical public URLs from
site-relative paths; `_normalize_body` keeps Markdown paragraph breaks for the
LLM. Uses its own lookback (`settings.fantasypoints.lookback_hours`, default 24h)
so it mirrors "today's articles". Saves → `data/raw/<date>/fantasypoints.json`.

## YouTube — `youtube_collector.py` (local-only tool)

`collect_youtube(date_str, lookback_hours=None, enable_whisper=True)` scans all 32
team YouTube channels (parallel via `ThreadPoolExecutor`, serial per-team with
delays) for press conferences. Driven by `scripts/collect_youtube.py`, not the
news pipeline. Key behavior:

- **`scan_channel`** supports per-team `youtube_channels` config with per-channel
  `keyword_filter` (set false for dedicated presser channels) and `scan_streams`
  (most pressers are live streams, so both `/videos` and `/streams` tabs are
  scanned). Uses yt-dlp `extract_flat` to scan without downloading.
- **`_is_press_conference`** matches keyword hits or a player-quote title pattern
  (`Firstname Lastname: "…"`).
- **`download_captions`** (preferred) / **`download_audio`** (MP3 64kbps for
  Whisper) both **cache** existing files in the output dir to make backfill
  re-runs cheap. Whisper inference is serialized with a module-level lock
  (PyTorch isn't thread-safe) while caption downloads stay parallel.
- A `whisper_max_duration_seconds` gate (default 1800s) skips Whisper on long
  streams — CPU transcription of a 49-minute video once hung the daily run.
  Captions are always tried first regardless.
- Seen video IDs in `data/youtube_seen.json` prevent re-transcription.
- Writes one `.txt` per transcript to `data/transcripts/<date>/` and metadata to
  `data/raw/<date>/youtube.json` (`save_youtube_results`).

`collectors/__init__.py` prepends the conda env's `Library/bin` to PATH so yt-dlp
and Whisper find `ffmpeg` (the runner calls `python.exe` directly, never
`conda activate`). `_transcription.py` holds shared caption parsing + Whisper
helpers (model cached globally); `processing/transcriber.py` has a parallel set
of VTT/SRT parsers + `transcribe_audio`.

## Depth charts — `depth_chart_collector.py`

Not a news source — a structured-data scraper. `scrape_all_teams(delay, max_workers=6)`
pulls all 32 teams from `ourlads.com/nfldepthcharts/depthchart/{slug}` in
parallel; each player row becomes `{name, pos (OurLads code), generic_pos, depth,
team}`. `POS_MAP` normalizes side-tagged codes (LWR/RWR/SWR → WR, LOLB/ROLB → LB).
Snapshots are keyed by normalized lowercase name and saved to
`data/depth_charts/<date>.json`.

- **`load_latest_depth_charts(before_date=None)`** / `load_depth_chart_by_date` /
  `get_depth_chart_dates` — snapshot access.
- **`diff_depth_charts(current, previous)`** — emits `added` / `removed` /
  `team_change` / `position_change` / `promoted` / `demoted` for **all** positions.
- **`annotate_depth_changes(changes, news_items)`** — attaches a `context` string
  to promotions/demotions explaining concurrent moves and matching news items (by
  last name).

## Twitter — `twitter_collector.py` (disabled)

`collect_twitter` reads Twitter via Nitter RSS bridges, trying instances in order.
As of April 2026 all public Nitter instances are dead, so it's disabled
(`settings.nitter.enabled: false`, empty instance list). Reddit covers the same
breaking-news ground. To revive: self-host Nitter with your own session tokens or
use a paid RSS bridge.

## Summary table

| Collector | Source | Output file | Auth |
|-----------|--------|-------------|------|
| `rss_collector` | National RSS + 32 SBN blogs + ESPN team API | `rss.json` | none |
| `web_scraper` | NFL.com txns/injuries, The Athletic, 32 SI team pages | `web.json` | Athletic cookies |
| `reddit_collector` | r/nfl/new RSS | `reddit.json` | none |
| `beat_writer_collector` | Per-writer RSS/JSON/YouTube/podcasts | `beat_writers.json` | none |
| `fantasypoints_collector` | FantasyPoints v2 API | `fantasypoints.json` | API auth headers |
| `youtube_collector` | 32 team YouTube channels | `youtube.json` + `transcripts/` | residential IP |
| `depth_chart_collector` | OurLads | `depth_charts/<date>.json` | none |
