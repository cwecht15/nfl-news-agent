# Reports & Dashboard

`reports/` assembles and renders the daily report; `dashboard/` is the Streamlit
UI that displays it and lets visitors flag findings.

## Report building — `reports/report_builder.py`

**`build_report(date_str, sections, team_highlights, news_items, llm_usage=None,
alerts=None, depth_chart_changes=None, projection_movers=None, yt_section=None,
fp_section=None) -> DailyReport`** normalizes every section payload to a
consistent `{summary, sources}` shape and attaches sources:

- `_build_section_sources(news_items)` — concrete sources for top-level sections
  (transactions / injuries / league-wide), deduped and recency-ordered.
- `_build_team_sources(news_items)` — per-team sources via round-robin across
  distinct labels then recency fill, so one high-volume outlet doesn't crowd out
  others.
- `_build_depth_chart_section(changes)` — renders the diff as per-team grouped
  bullets (Promotions / Demotions / Added / Removed / Team / Position changes).
- `_build_projection_movers_section(movers)` — top 15 rank movers by delta, with
  ↑/↓ arrows.

`save_report(report)` writes `data/reports/<date>.json` (via `DailyReport.to_json`)
and a styled HTML file from a Jinja2 `HTML_TEMPLATE` (NFL navy/red branding,
per-section cards, Team Notes, optional YouTube subsection, collection stats, and
an LLM-usage footer with token counts and estimated cost). `load_report(date_str)`
and `list_available_reports()` (newest-first, excludes `digest_*`) round out the
API. Sections render in `SECTION_ORDER`: transactions, injuries,
depth_chart_movement, projection_movers, league_wide, fantasypoints.

**Citations** — sections may carry `numbered_sources`
(`{num, title, url, source}`); inline `[N]` markers in the markdown link to them.
The same schema drives clickable links in the dashboard and PDF.

## Flag store — `reports/flagged_findings.py`

A JSON store (`data/flagged_findings.json`) of user-flagged findings, with two
coexisting modes distinguished by a `mode` field:

- **`handbook`** (default/legacy) — the long-running "Internal FP Handbook";
  flags persist forever across all reports, with category + author attribution.
- **`daily`** — per-report-date "Daily Site Report"; the Flagged tab and PDF
  filter by `report_date`. Captures team + note only.

Flag IDs are deterministic hashes of `(report_date, section, content)`; daily IDs
prepend `daily|` so the same content can be flagged in both modes independently.
Key functions: `add_or_update_flag(...)`, `is_flagged(...)`, `get_flag(id)`,
`remove_flag(id)`, `update_flag_fields(id, **fields)`, `load_flags_by_mode(mode)`.

## PDF export — `reports/pdf_exporter.py`

ReportLab PDFs in FantasyPoints brand styling (Kanit ExtraBold Italic headlines,
Mulish body, red ticker footer). Three builders:

- **`build_daily_pdf(date_str)`** — full daily report: sections (markdown → bold/
  italic/`[N]` links), team highlights, a projection-changes table (from
  `changelog.csv`), depth-chart diff with news context, stats.
- **`build_flagged_pdf(flags=None)`** — the Handbook, grouped by category.
- **`build_daily_site_pdf(flags=None)`** — daily-mode flags, grouped by team.

`_render_markdown` parses heading/bullet/paragraph blocks into ReportLab
flowables and linkifies `[N]` citations against a `source_url_map`.

## Dashboard

Streamlit multi-page app, entry point **`dashboard/app.py`**
(`streamlit run dashboard/app.py`, default port 8502). Every page calls
`require_password()` ([`auth.py`](../dashboard/auth.py)) right after
`st.set_page_config` — the cloud deploy sets `dashboard_password` in Streamlit
secrets; local dev is passwordless. When running locally, `app.py` also renders a
**Run Pipeline** sidebar that launches `scripts/run_daily.py` as a subprocess
(with a catch-up lookback expander), reads `data/pipeline_status.json` to detect
an already-running pipeline, and streams the 7-step progress. A cloud-safe PDF
export widget is always available.

### Shared modules

- **`helpers.py`** — `to_et_display` (UTC→ET timestamps), `running_locally()`
  (true when `dashboard_password` is unset) and `stop_if_not_local`,
  `bootstrap_secrets()` (bridges `st.secrets` → `os.environ` since cloud has no
  `.env`), and highlight/source extraction helpers.
- **`flagging.py`** — the shared flagging UI: `parse_flaggable_items` splits a
  summary into headings/tables/bullets/paragraphs; `flag_control` renders a 🚩
  popover (category/team/note/author) that **auto-saves on every change** and
  debounce-schedules a repo push; `render_flaggable` lays the summary out with
  per-item flag icons and citation linkification.
- **`_repo_sync.py`** — GitHub Contents API push of `flagged_findings.json` and
  `transaction_overrides.json` back to `master` (fine-grained PAT in
  `st.secrets["GITHUB_PAT"]`). `request_flag_autopush()` pushes immediately on
  cold start, then throttles to 60s; background-thread-safe because it caches the
  token (threads can't read `st.secrets`). Best-effort; cron + manual button are
  fallbacks.

### Pages (`dashboard/pages/`)

| Page | Purpose |
|------|---------|
| `daily_report.py` | The daily briefing: date picker, search (paragraph filtering on long summaries), source alerts, all sections rendered flaggable with `[N]` linkification, Team Notes, optional YouTube subsection. |
| `team_view.py` | Per-team drilldown across a 1–30 day window: team highlight per day plus matching raw news items. |
| `projections.py` | 7 tabs — Today's Changes, Fantasy Rankings, Weekly Summary, Transactions, Player Lookup, Player History, Team Projections — over `data/projections/` snapshots + `changelog.csv`. |
| `depth_charts.py` | Changes tab (diff two dates, annotated with news, filter by team/position) + Browse tab (latest snapshot by team/position). |
| `depth_chart_manager.py` | Read-only reconciliation of the hand-maintained depth-chart Google Sheet vs agent data (team/status mismatches, untracked transactions) with per-item dismissal. 1h cached. Local + cloud (needs `gcp_service_account` secret). |
| `fantasypoints.py` | Searchable archive of FantasyPoints articles, verbatim paragraphs, no LLM. Dedupes articles by URL slug. |
| `yt_report.py` | Date-range → on-demand `build_yt_section` summary of pushed transcripts (press-conf + per-team). Per-team `[N]` citations link to the source video and the press block lists its videos. Two-layer cache: `@st.cache_data` over a durable disk cache (`processing/yt_cache.py`) so a generated range reloads instantly/free even after a redeploy. Flaggable. |
| `transcripts.py` | Browse raw transcripts, bulk-ZIP download, push to NotebookLM, run backfill (local only). |
| `trends.py` | Historical charts: collection volume, transactions, injuries, league-wide, top teams, top flaggers, and daily LLM cost. |
| `digest.py` | Generate (LLM) and browse multi-day rollup reports (`digest_*.json`). |
| `flagged.py` | Browse/edit/delete flags (Handbook + Daily), export both PDFs, manual "Save to repo". |
| `config.py` | Edit `sources.yaml` / `settings.yaml` / `teams.yaml` in-browser with YAML validation, timestamped backups, and cache clearing. Local only. |

### Flagging write-back flow (cloud)

User edits a flag → `flag_control` auto-save callback → `add_or_update_flag`
writes `data/flagged_findings.json` (ephemeral container) →
`request_flag_autopush` → background thread pushes via GitHub Contents API (60s
throttle) → commit to `master` → Streamlit Cloud redeploys (~1 min). The daily
10 UTC cron and the manual "Save to repo" button are fallbacks for anything that
didn't land.
