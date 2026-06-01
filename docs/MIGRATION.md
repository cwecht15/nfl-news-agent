# Migrating off Streamlit → FastAPI + HTMX

## Why

Streamlit Cloud's ephemeral containers force the awkward parts of the current
design: visitor flags have to be pushed back to `master` via the GitHub Contents
API (`dashboard/_repo_sync.py`), the pipeline has to commit `data/` so the
dashboard can read it, and every data commit triggers a redeploy with a cold
start off a growing repo (`.git` is already ~143 MB).

The constraint that decides the target: the site must stay **publicly reachable,
interactive, and persistent** (flagging + on-demand LLM summaries). A pure static
site can't do public writes, so we need a small always-on server with a real
data store.

## Target architecture

```
GitHub Actions (UNCHANGED)
   run_daily.py ──> commits data/reports, projections, depth_charts to repo

FastAPI app (NEW, always-on host with a disk)
   ├─ git pull on a timer ──> read-only data (reports/projections/…)
   ├─ Jinja2 server-rendered pages + HTMX for in-page interactivity
   ├─ reuses processing/ + reports/ (report_builder, pdf_exporter) directly
   └─ writes (flags, overrides, dismissals) ──> local store on the disk
                                                  (no more git-push-back)
```

The clean separation: **pipeline-produced data stays in git** (read-only,
pulled by the app); **visitor-generated writes go to the app's own disk**. This
deletes `_repo_sync.py` entirely.

### Why FastAPI + Jinja + HTMX

- It's Python, so `processing/`, `reports/report_builder`, and
  `reports/pdf_exporter` (reportlab) are imported and reused unchanged.
- Server-rendered templates + HTMX give per-element interactivity (flag a bullet,
  generate a summary) without a separate SPA/JS build.
- Full control over auth, routing, and mobile layout.

### Data store

The current flag store (`reports/flagged_findings.py`) is already a clean
module-level API over a JSON file. On a **persistent disk it works as-is** — the
POC reuses it untouched, just without the git push. For a public multi-writer
site, hardening step: move flags/overrides/dismissals into **SQLite** (same API
surface, row-level writes, no whole-file rewrite races). One-time JSON→SQLite
migration script.

### Hosting

Any always-on host with a small persistent disk: Fly.io (free persistent
volume), Railway, Render, or the already-documented Oracle Always-Free VM. The
app is just `uvicorn webapp.main:app` + a data directory, so it's host-portable.

## Page-by-page mapping

| Streamlit page | New route | Notes |
|----------------|-----------|-------|
| Daily Report | `GET /report/{date}` | **POC built.** Sections + team notes; HTMX flag toggle per bullet. |
| Flagged | `GET /flagged` | Reuses `load_flags_by_mode`; PDF via `pdf_exporter` unchanged. |
| YouTube Report | `GET /yt` + `POST /yt/generate` | On-demand `build_yt_section`; HTMX-triggered. |
| Digest | `GET /digest` + `POST /digest/generate` | On-demand `run_digest`. |
| Team View | `GET /team/{abbr}` | Read-only. |
| Projections | `GET /projections` | Read-only tables over snapshots/changelog. |
| Depth Charts | `GET /depth-charts` | Read-only diff/browse. |
| FantasyPoints | `GET /fantasypoints` | Read-only search. |
| Trends | `GET /trends` | Charts → server-rendered (Chart.js) or pre-rendered. |
| Depth Chart Manager | `GET /reconcile` | Reuses `processing.sheet_reconciliation`. |
| Transcripts / Config / Pipeline control | **stay local-only** | Operator tools — keep as a thin local Streamlit app or CLI; they don't need to be public. |

## Auth

Replace the homegrown `st.secrets` password gate with Starlette
`SessionMiddleware` + a signed cookie. Password from `DASHBOARD_PASSWORD` env;
when unset the app is passwordless (mirrors today's local-dev behavior).

## Phased plan

1. **POC (done)** — app skeleton, session auth, Daily Report page, persistent
   flag toggle on a local disk. Proves *public + interactive + persistent*.
2. Port the remaining read-only pages (Team, Projections, Depth Charts,
   FantasyPoints, Trends).
3. Port the interactive pages (Flagged + PDF, YouTube Report, Digest, Reconcile).
4. Swap the JSON flag store for SQLite; write the migration script.
5. Add a deploy target (Dockerfile + host config); add a timer that `git pull`s
   the data; retire `_repo_sync.py` and the Streamlit deploy.

## What's in the repo now (POC)

- `webapp/main.py` — FastAPI app (auth, index, daily report, flag toggle)
- `webapp/templates/` — `base.html`, `login.html`, `index.html`, `report.html`
- Run: `uvicorn webapp.main:app --reload --port 8600` (passwordless unless
  `DASHBOARD_PASSWORD` is set). See the header of `webapp/main.py`.
