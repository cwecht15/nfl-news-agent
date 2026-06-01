# Processing

`processing/` turns raw collected items into the summarized sections of the daily
report. The flow is: **quality filter → deduplicate → cross-day filter →
summarize**, with several independent helpers (source health, sheet
reconciliation) on the side.

## Quality filter — `quality_filter.py`

`filter_news_items(items) -> (kept, dropped)` drops obvious fluff *before* dedup
and the LLM, saving tokens and keeping junk out of Team Notes. Patterns live in
`config/settings.yaml → content_filter.drop_patterns` (case-insensitive regex:
voting articles, "all-time", trivia, jersey/uniform reveals, rip-offs,
"best X player?"). Mock drafts get special handling: dropped unless the title
mentions a year in `content_filter.mock_draft_keep_years` (`["2026","2027"]`,
updated annually). If `content_filter.enabled` is false, everything passes.

## Deduplicator — `deduplicator.py`

Groups items covering the same story across sources.

- **`deduplicate(items) -> list[list[NewsItem]]`** — encodes titles with
  `sentence-transformers` (`all-MiniLM-L6-v2`) and groups by cosine similarity
  ≥ **0.75** (`EMBEDDING_SIMILARITY_THRESHOLD`). If the model can't load, it falls
  back to `SequenceMatcher` ratio ≥ **0.6** plus a keyword-overlap signal (≥0.5)
  when items share a team. Titles are normalized first (`_normalize_title`:
  lowercase, strip "breaking/report/sources/per/via" prefixes, drop punctuation).
- **Transaction-awareness** — transaction items only merge if they share player
  names (`_transaction_name_overlap`): transaction verbs and team words are
  stripped, then ≥2 shared name words are required (1 if a name is mononymous).
  This stops structurally identical lines ("X: Exclusive Rights Signing") from
  false-merging different players.
- **`flatten_groups(groups)`** — keeps one representative per group (via
  `pick_primary`) and appends `[Also reported by: …]` to its summary.
- **`pick_primary(group)`** — two-tier ranking. **Tier 1:** original-reporting
  outlets (anything *not* prefixed `SBN `, `SI `, `r/nfl`) outrank aggregators.
  **Tier 2:** within a tier, longest summary wins, then earliest published. So
  when an SBN blog merely comments on an ESPN scoop, ESPN is cited.

## Cross-day filter — `cross_day_filter.py`

`filter_recent_duplicates(items, raw_dir, current_date, lookback_days=2, threshold=0.82, skip_categories=None)`
suppresses today's items whose titles are ≥ **0.82** cosine-similar to any title
collected in the prior `lookback_days` (loaded from each day's `rss.json` /
`web.json` / `reddit.json`). The threshold is **stricter** than intra-day dedup
to avoid over-suppression across day boundaries. `skip_categories` exempts
`transaction` by default (roster moves recur legitimately in recaps/injury
reports). Reuses the deduplicator's embedding pass, so it's cheap after model
load. Runs before summarization, so suppressed items cost no tokens.

## Summarizer — `summarizer.py`

The LLM engine. Provider-agnostic across three backends, selected by
`settings.summarization.provider`:

| Provider | Default model | Notes |
|----------|---------------|-------|
| OpenAI (default) | `gpt-5.4-mini` | Responses API; `reasoning_effort` + `service_tier`; retries on `incomplete` (budget spent on reasoning) with a larger token cap |
| Anthropic | `claude-sonnet-4-20250514` | Prompt caching (ephemeral cache control) |
| Ollama | `gemma3:4b` | Local, no cost tracking |

**Section functions** (each is one LLM call):

- **`summarize_transactions(items, client, usage_tracker, position_lookup)`** —
  pre-tags lines `[TEAM / POS]` using a name→position map from the latest depth
  chart (side-tagged positions normalized to generic), then asks the LLM to list
  every transaction with no omissions and no invented positions.
- **`summarize_injuries(items, ...)`** — key players whose status changed or who
  are newly listed; notes returning players.
- **`summarize_league_wide(news_items, ...) -> (summary, numbered_sources)`** —
  cross-team / league-office items only. Top 25 by recency in, capped at 8
  bullets out, inline `[N]` citations; sources never cited are trimmed.
- **`generate_team_highlights(news_items, ...) -> {team: {summary, numbered_sources}}`**
  — per-team bulleted notes. `_diversify_by_source` soft-caps any one outlet
  (primary ≤ `limit//2`, non-primary ≤ `limit//3`) so one blog can't dominate.
  `_is_deep_article` (depth charts, post-draft recaps, "every pick") gets the
  full body (~5000 chars) into the prompt; ordinary articles get 1200 chars, so
  the LLM can reach past QB notes into RB/WR/TE/OL.
- **`summarize_press_conferences(transcripts, ...) -> (summary, count)`** and
  **`summarize_team_highlights_from_transcripts(...)`** — YouTube side.
  `_press_relevance_score` rewards presser signals ("press conference" +5,
  "podium" +3) and penalizes non-pressers ("podcast" −5, "film" −4); top 12
  selected. The reported press-conference count is *summarized* transcripts, not
  collected.

**Prompt rules** (shared system prompt + per-section): bold the most-specific
named subject; one bullet per development; end bullets with `[N]`; prioritize
QB/RB/FB/WR/TE → OL/coaches → defense/ST; drop common knowledge and platitudes;
**never invent** a first name, number, position, or stat; respect word caps.

**Cost & budget** — `_init_usage_tracker` accumulates request count, input
(cached vs uncached), output, and reasoning tokens, plus estimated USD from
built-in per-model price tables for OpenAI and Anthropic. Each call checks
`settings.summarization.daily_token_budget` (default 500k) and returns a fallback
string rather than overspending. Rate limits retry up to 3× with 60s backoff.

`run_summarization(deduped_news)` is the entry point the orchestrator calls; it
returns `{sections, team_highlights, llm_usage}`.

## Section builders — `fp_section.py`, `yt_section.py`

These build optional report sections on top of the summarizer:

- **`build_fp_section(items, usage_tracker=None, date_label=...)`** — per-player
  rollup of FantasyPoints articles. One bullet per meaningfully-discussed player,
  `**Player** — takeaway. Author [N]`, skill players first. Article bodies capped
  at 5000 chars; `reasoning_effort="medium"`; output capped to finish a ~15-article
  rollup. Reuses the daily usage tracker so FP tokens roll into the daily total.
- **`build_yt_section(transcripts, date_label=..., pre_filtered=False)`** — two
  subsections: a press-conference summary (`summarize_press_conferences`) and
  per-team transcript notes (`summarize_team_highlights_from_transcripts`).
  `pre_filtered=True` skips the press-relevance gate (caller curated the list).
  Used by both `run_daily.py --include-yt-section` and the dashboard's YouTube
  Report tab.

## Source health — `source_health.py`

`record_source_result(source_name, item_count, error="", low_volume=False)` tracks
per-source success/failure history in `data/source_health.json` (last 30 runs).
`get_health_alerts()` warns after **3** consecutive failures. `low_volume=True`
(beat writers, FantasyPoints) treats a zero-item, error-free run as a quiet-day
success rather than a failure. `get_health_summary()` powers dashboard display.

## Sheet reconciliation — `sheet_reconciliation.py`

Independent of the news flow. `reconcile(lookback_days=30)` compares the user's
hand-maintained "2026 Depth Chart" Google Sheet (a *different* sheet from the
projections sheet) against the agent's data (latest OurLads scrape + recent
NFL.com transactions) and reports three discrepancy classes:

- **`compute_team_mismatches`** — sheet TEAM disagrees with OurLads.
- **`compute_status_mismatches`** — sheet says "Active" but a deactivating
  transaction (released/waived/reserve/retired/terminated) suggests otherwise.
- **`compute_untracked_transactions`** — recent agent transactions absent from the
  sheet's Transactions tab.

Both sides are normalized to projection-style abbreviations (ARZ/BLT/CLV/HST/LA)
and names are de-suffixed (`_normalize_name`). Dismissals persist by stable key
in `data/sheet_recon_dismissals.json`. Surfaced via the **Depth Chart Manager**
dashboard page.

## Thresholds cheat-sheet

| Stage | Value | Meaning |
|-------|-------|---------|
| Intra-day dedup (embeddings) | 0.75 | group same story |
| Intra-day dedup (fallback) | 0.60 ratio + 0.50 keyword | no sentence-transformers |
| Transaction name overlap | ≥2 shared name words | required to merge transactions |
| Cross-day suppression | 0.82 | drop prior-day repeat |
| Press-conference selection | top 12 by score | which transcripts get summarized |
| League-wide output | 8 bullets | hard cap |
| Source-health alert | 3 consecutive failures | warning |
| Daily token budget | 500,000 | hard spend cap |
