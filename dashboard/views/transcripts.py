"""Transcripts & YouTube Links — Browse videos, push to NotebookLM, backfill,
and publish locally-collected transcripts to the live site."""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Transcripts", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard.helpers import stop_if_not_local
stop_if_not_local("Transcripts")

from config_loader import get_data_dir, get_teams

PROJECT_ROOT = Path(__file__).parent.parent.parent

st.header("YouTube Videos & Transcripts")

teams = get_teams()
team_names = {t["abbr"]: t["name"] for t in teams}

transcripts_base = get_data_dir("transcripts")
raw_base = get_data_dir("raw")

all_transcript_dirs = sorted(
    [d.name for d in transcripts_base.iterdir() if d.is_dir()],
    reverse=True,
)
daily_dirs = [d for d in all_transcript_dirs if len(d) == 10 and d[4] == "-"]
backfill_dirs = [d for d in all_transcript_dirs if d.startswith("backfill-")]

if not all_transcript_dirs:
    st.warning(
        "No transcripts available yet. Run `python scripts/run_daily.py` "
        "to collect videos."
    )
    st.stop()


# ──────────────────────────────────────────────────────────
# Data helpers
# ──────────────────────────────────────────────────────────

def _parse_date_safe(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_youtube_meta(dir_name: str) -> dict:
    meta = {}
    for json_name in ("youtube.json", "all_transcripts.json", "all_videos.json"):
        path = raw_base / dir_name / json_name
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for item in json.load(f):
                        vid_id = item.get("video_id", "")
                        if vid_id and vid_id not in meta:
                            meta[vid_id] = item
            except (json.JSONDecodeError, TypeError):
                continue
    return meta


def _video_date(item: dict):
    upload = str(item.get("upload_date", "") or "").strip()
    if upload:
        try:
            return datetime.strptime(upload, "%Y%m%d").date()
        except ValueError:
            pass
    pub = str(item.get("published_at", "") or "").strip()
    if pub:
        try:
            return datetime.fromisoformat(pub).date()
        except ValueError:
            pass
    return None


def _get_transcript_files(dir_name: str) -> list[Path]:
    d = transcripts_base / dir_name
    if d.exists():
        return sorted(d.glob("*.txt"))
    return []


def _all_known_dates() -> tuple:
    dates = []
    for d in daily_dirs:
        parsed = _parse_date_safe(d)
        if parsed:
            dates.append(parsed)
    for d in backfill_dirs:
        import re as _re
        m = _re.search(r"(\d{4}-\d{2}-\d{2})-to-(\d{4}-\d{2}-\d{2})", d)
        if m:
            start = _parse_date_safe(m.group(1))
            end = _parse_date_safe(m.group(2))
            if start:
                dates.append(start)
            if end:
                dates.append(end)
    if not dates:
        today = datetime.now().date()
        return today - timedelta(days=30), today
    return min(dates), max(dates)


# ──────────────────────────────────────────────────────────
# NotebookLM HTTP client
# ──────────────────────────────────────────────────────────

NOTEBOOKLM_HTTP = "http://localhost:3000"


def _nb_notebooks() -> tuple[list[dict], str | None]:
    import requests
    try:
        r = requests.get(f"{NOTEBOOKLM_HTTP}/notebooks", timeout=5)
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            return [], str(body)
        return body.get("data", {}).get("notebooks", []) or [], None
    except requests.ConnectionError:
        return [], "server_down"
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _nb_list_sources(notebook_url: str) -> tuple[list[dict], str | None]:
    """List sources in a notebook. Server launches a browser session, so 60s+."""
    import requests
    try:
        r = requests.get(
            f"{NOTEBOOKLM_HTTP}/content",
            params={"notebook_url": notebook_url},
            timeout=120,
        )
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            return [], str(body)
        return body.get("sources") or [], None
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def _nb_add_youtube(notebook_url: str, url: str) -> tuple[bool, str]:
    """POST a YouTube source. Returns (ok, message).

    Treats the known false-negative `'Source not found after upload'` as
    success — the source actually was added but the server's verifier
    can't see it in the list (stale selector after a NotebookLM UI
    change). See roomi-fields v1.5.8 changelog and Falcons notebook
    diagnosis from 2026-04-26.
    """
    import requests
    try:
        r = requests.post(
            f"{NOTEBOOKLM_HTTP}/content/sources",
            json={
                "source_type": "youtube",
                "url": url,
                "notebook_url": notebook_url,
            },
            timeout=120,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("success"):
            return True, body.get("status", "ok")
        # Known false-negative: server submitted the source successfully
        # but couldn't verify it in the post-upload source list.
        err_text = str(body.get("error") or body)
        if "source not visible in list" in err_text.lower() or \
           "source not found after upload" in err_text.lower():
            return True, "added (verifier false-negative; assumed success)"
        return False, err_text
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _nb_register_notebook(url: str, name: str) -> tuple[bool, str]:
    import requests
    try:
        r = requests.post(
            f"{NOTEBOOKLM_HTTP}/notebooks",
            json={
                "url": url,
                "name": name,
                "description": f"Added via dashboard on {datetime.now().date()}",
                "topics": ["nfl"],
            },
            timeout=120,
        )
        body = r.json()
        if body.get("success"):
            return True, "added"
        return False, str(body.get("error") or body)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _title_matches(youtube_title: str, source_name: str) -> bool:
    def norm(s: str) -> str:
        return "".join(c.lower() for c in s if c.isalnum())[:40]
    a, b = norm(youtube_title), norm(source_name)
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


# ──────────────────────────────────────────────────────────
# Local push history sidecar — keyed by notebook URL → set of video_ids
# we've successfully pushed from this dashboard. Authoritative for
# dashboard-initiated pushes; server-side title matching still catches
# sources added outside the dashboard.
# ──────────────────────────────────────────────────────────

_PUSHED_HISTORY_PATH = (
    Path(__file__).parent.parent.parent / "data" / "notebooklm_pushed.json"
)


def _load_pushed_history() -> dict[str, list[str]]:
    if not _PUSHED_HISTORY_PATH.exists():
        return {}
    try:
        with open(_PUSHED_HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _record_pushed(notebook_url: str, video_ids: list[str]) -> None:
    if not video_ids:
        return
    history = _load_pushed_history()
    existing = set(history.get(notebook_url, []))
    existing.update(video_ids)
    history[notebook_url] = sorted(existing)
    _PUSHED_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PUSHED_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


# ──────────────────────────────────────────────────────────
# Notebook picker (sticky at top, used by every tab)
# ──────────────────────────────────────────────────────────

notebooks, nb_err = _nb_notebooks()

with st.container(border=True):
    nb_cols = st.columns([4, 2])
    chosen_nb_url = None
    chosen_nb_name = None

    with nb_cols[0]:
        if nb_err == "server_down":
            st.warning(
                "📕 NotebookLM server not running. Start `Launch_NotebookLM.bat` "
                "to enable push. (Browsing/copying still works.)"
            )
        elif nb_err:
            st.error(f"📕 NotebookLM server error: {nb_err}")
        elif not notebooks:
            st.info(
                "📕 No notebooks registered. Add one below to enable push "
                "(auto-scrape from NotebookLM is broken upstream)."
            )
        else:
            nb_options = {nb.get("id", ""): nb for nb in notebooks}
            chosen_id = st.selectbox(
                "📕 Push target",
                list(nb_options.keys()),
                format_func=lambda nid: nb_options[nid].get("name", nid) or nid,
                key="nb_target",
            )
            chosen_nb = nb_options[chosen_id]
            chosen_nb_url = chosen_nb.get("url") or (
                f"https://notebooklm.google.com/notebook/{chosen_id}"
            )
            chosen_nb_name = chosen_nb.get("name", chosen_id)

    with nb_cols[1]:
        if nb_err != "server_down":
            with st.expander("➕ Add notebook by URL", expanded=not notebooks):
                st.caption(
                    "Open a notebook in NotebookLM, copy the URL "
                    "(`notebooklm.google.com/notebook/<id>`)."
                )
                with st.form("nb_add_form", clear_on_submit=True):
                    new_url = st.text_input("Notebook URL")
                    new_name = st.text_input("Nickname")
                    submitted = st.form_submit_button("Add", type="primary")
                    if submitted:
                        if not new_url.strip() or not new_name.strip():
                            st.error("Both URL and nickname are required.")
                        elif "notebooklm.google.com/notebook/" not in new_url:
                            st.error("That doesn't look like a NotebookLM URL.")
                        else:
                            with st.spinner(
                                f"Adding '{new_name}' (server validates against "
                                "NotebookLM, 30-60s)..."
                            ):
                                ok, msg = _nb_register_notebook(
                                    new_url.strip(), new_name.strip()
                                )
                            if ok:
                                st.success(f"Added '{new_name}'.")
                                st.rerun()
                            else:
                                st.error(f"Add failed: {msg}")


# ──────────────────────────────────────────────────────────
# Reusable video selection + action footer
# ──────────────────────────────────────────────────────────

def _video_selector(items: list[dict], key_prefix: str) -> list[dict]:
    """Render checkbox grid; return list of selected items.

    Each item should have: team, date, title, url. Optional: method.
    """
    if not items:
        st.info("No videos in this view yet.")
        return []

    sel_cols = st.columns([1, 1, 6])
    with sel_cols[0]:
        if st.button("Select all", key=f"{key_prefix}_sel_all"):
            for i in range(len(items)):
                st.session_state[f"{key_prefix}_pick_{i}"] = True
            st.rerun()
    with sel_cols[1]:
        if st.button("Clear", key=f"{key_prefix}_sel_clear"):
            for i in range(len(items)):
                st.session_state[f"{key_prefix}_pick_{i}"] = False
            st.rerun()
    with sel_cols[2]:
        st.caption(f"{len(items)} candidates")

    selected: list[dict] = []
    for i, d in enumerate(items):
        team = d.get("team", "")
        team_label = team_names.get(team, team)
        date = d.get("date") or d.get("upload_date", "")
        if date and len(date) == 8 and date.isdigit():
            date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        method = d.get("method", "")
        suffix = f" ({method})" if method else ""
        label = f"**{team_label}** {date} — {d.get('title','?')}{suffix}"
        if st.checkbox(label, key=f"{key_prefix}_pick_{i}"):
            selected.append(d)
    return selected


def _push_footer(
    selected: list[dict],
    key_prefix: str,
    nb_url: str | None,
    nb_name: str | None,
):
    """Action footer: copy URLs + push to notebook."""
    if not selected:
        st.caption("Select videos above to enable copy/push actions.")
        return

    st.divider()
    foot = st.columns([2, 3, 4])
    with foot[0]:
        with st.popover(f"📋 Copy {len(selected)} URLs"):
            st.code("\n".join(d["url"] for d in selected), language=None)
    with foot[1]:
        do_push = st.button(
            f"📕 Push to '{nb_name}'" if nb_url else "📕 Push (no notebook)",
            type="primary",
            disabled=(nb_url is None),
            key=f"{key_prefix}_push",
        )
    with foot[2]:
        if not nb_url:
            st.caption("Pick or add a notebook above to push.")

    if do_push and nb_url:
        _execute_push(selected, nb_url)


def _execute_push(selected: list[dict], nb_url: str):
    """Run the dedup → push → record flow, with status output.

    Dedup uses the local push-history sidecar (data/notebooklm_pushed.json),
    which is authoritative for anything pushed via this dashboard.
    Server-side `/content` reads are unreliable in current versions of
    notebooklm-mcp (selectors stale after NotebookLM UI changes), so we
    don't depend on them for dedup or post-push verification.
    """
    import time

    history = _load_pushed_history()
    pushed_ids = set(history.get(nb_url, []))

    skipped_local = [d for d in selected if d.get("video_id") in pushed_ids]
    candidates = [d for d in selected if d.get("video_id") not in pushed_ids]

    if skipped_local:
        with st.expander(
            f"Skipping {len(skipped_local)} already pushed from this dashboard",
            expanded=False,
        ):
            for d in skipped_local:
                st.markdown(f"- {d['title']}")

    if not candidates:
        st.info("Nothing new to push.")
        return

    progress = st.progress(0.0, text=f"Pushing 0/{len(candidates)}...")
    results = []
    for i, d in enumerate(candidates, start=1):
        ok, msg = _nb_add_youtube(nb_url, d["url"])
        results.append((d, ok, msg))
        progress.progress(
            i / len(candidates),
            text=f"Pushing {i}/{len(candidates)} — {d['title'][:50]}",
        )
        time.sleep(2.0)

    ok_pushes = [d for d, ok, _ in results if ok]
    fail = [(d, msg) for d, ok, msg in results if not ok]

    # Record successful pushes to the local sidecar for future dedup
    _record_pushed(nb_url, [d["video_id"] for d in ok_pushes if d.get("video_id")])

    if ok_pushes:
        st.success(
            f"Pushed {len(ok_pushes)} of {len(candidates)} videos. "
            f"Recorded in local push history."
        )
    if fail:
        with st.expander(f"⚠ {len(fail)} failures", expanded=True):
            for d, msg in fail:
                st.markdown(f"- **{d['title']}** — {msg}")

    st.caption(
        "Note: server-side source verification is broken in current "
        "notebooklm-mcp (NotebookLM UI changed). Sources marked 'success' "
        "above are reliably added; 'failed' may also have succeeded — check "
        "the notebook in NotebookLM to confirm. Local push history prevents "
        "re-pushing the same video next time regardless."
    )


# ──────────────────────────────────────────────────────────
# Publish to live site — git stage/commit/push helpers
# ──────────────────────────────────────────────────────────

YOUTUBE_PATH_PREFIXES = (
    "data/transcripts/",
)
YOUTUBE_LITERAL_PATHS = {
    "data/youtube_seen.json",
}


def _is_youtube_path(path: str) -> bool:
    if path in YOUTUBE_LITERAL_PATHS:
        return True
    if path.startswith("data/raw/") and path.endswith("/youtube.json"):
        return True
    if any(path.startswith(p) for p in YOUTUBE_PATH_PREFIXES):
        return True
    return False


def _git_pending_youtube_paths() -> list[tuple[str, str]]:
    """Return [(status_code, path), ...] for unpushed YouTube files.

    Status code is the porcelain XY (e.g. " M", "??", "M ", "A ").
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return []
    except Exception:
        return []

    if result.returncode != 0:
        return []

    out: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        path = line[3:].strip()
        # Strip surrounding double quotes that git uses for paths with spaces.
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if _is_youtube_path(path):
            out.append((code, path))
    return out


def _git_run(args: list[str]) -> tuple[bool, str]:
    """Run a git subcommand; return (ok, combined_stdout_stderr)."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except FileNotFoundError:
        return False, "git executable not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git command timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    output = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, output.strip()


def _summarize_pending_videos(
    pending: list[tuple[str, str]],
) -> tuple[list[dict], list[tuple[str, int]], list[str]]:
    """Turn pending file paths into something user-meaningful.

    Returns:
        videos: list of {date, team, title, method, url} for every video in
            a queued `data/raw/<date>/youtube.json`.
        loose_dirs: list of (transcripts_dir_name, txt_count) for transcript
            directories whose youtube.json isn't itself queued — usually
            means the metadata file was already committed earlier.
        other_paths: file paths that didn't match either bucket
            (e.g. `data/youtube_seen.json` itself).
    """
    queued_youtube_jsons: list[Path] = []
    queued_txt_dirs: dict[str, int] = {}
    other: list[str] = []

    for _code, path in pending:
        if path.startswith("data/raw/") and path.endswith("/youtube.json"):
            queued_youtube_jsons.append(PROJECT_ROOT / path)
        elif path.startswith("data/transcripts/") and path.endswith(".txt"):
            # group by parent dir name
            dir_name = path.split("/")[2] if path.count("/") >= 2 else "?"
            queued_txt_dirs[dir_name] = queued_txt_dirs.get(dir_name, 0) + 1
        else:
            other.append(path)

    videos: list[dict] = []
    yt_dirs_with_metadata: set[str] = set()
    for json_path in queued_youtube_jsons:
        date_label = json_path.parent.name
        yt_dirs_with_metadata.add(date_label)
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            continue
        for item in items or []:
            videos.append({
                "date": date_label,
                "team": item.get("team", "?"),
                "title": item.get("title", "(untitled)"),
                "method": item.get("method", ""),
                "url": item.get("url", ""),
            })

    # Sort by (date, team, title) so the rendered list is stable + scannable.
    videos.sort(key=lambda v: (v["date"], v["team"], v["title"]))

    loose_dirs = [
        (name, count)
        for name, count in sorted(queued_txt_dirs.items())
        if name not in yt_dirs_with_metadata
    ]
    return videos, loose_dirs, other


def _publish_to_cloud(
    paths: list[str],
    commit_msg: str,
) -> tuple[bool, list[str]]:
    """Stage, commit, and push the listed paths. Returns (success, log_lines)."""
    log: list[str] = []

    # data/ is gitignored; force-add each path so the commit picks them up.
    add_cmd = ["add", "-f"] + paths
    log.append(f"$ git {' '.join(add_cmd)}")
    ok, out = _git_run(add_cmd)
    if out:
        log.append(out)
    if not ok:
        return False, log

    log.append("$ git commit -m <msg>")
    ok, out = _git_run(["commit", "-m", commit_msg])
    if out:
        log.append(out)
    if not ok:
        # Most likely "nothing to commit" — surface but don't fail the push.
        if "nothing to commit" in out.lower():
            log.append("(no staged YouTube changes; skipping commit + push)")
            return True, log
        return False, log

    log.append("$ git push origin master")
    ok, out = _git_run(["push", "origin", "master"])
    if out:
        log.append(out)
    return ok, log


# ──────────────────────────────────────────────────────────
# Tabs
# ──────────────────────────────────────────────────────────

tab_browse, tab_backfill, tab_export, tab_publish = st.tabs(
    [
        "📺 Browse by date",
        "🔍 Backfill",
        "📋 Multi-day export",
        "🚀 Publish to cloud",
    ]
)


# ─── Tab 1: Browse by date ───────────────────────────────
with tab_browse:
    st.markdown(
        "Single-day view of videos collected by the daily run "
        "(or a previous backfill)."
    )

    selected_date = st.selectbox(
        "Date / dataset",
        all_transcript_dirs,
        key="browse_date",
    )
    youtube_meta = _load_youtube_meta(selected_date)
    txt_files = _get_transcript_files(selected_date)

    if not txt_files and not youtube_meta:
        st.info(f"No videos collected for {selected_date}.")
    else:
        # Team filter
        browse_teams = set()
        for item in youtube_meta.values():
            browse_teams.add(item.get("team", ""))
        for f in txt_files:
            abbr = f.name.split("_")[0]
            if abbr in team_names:
                browse_teams.add(abbr)

        team_filter = st.multiselect(
            "Filter by team (empty = all)",
            sorted(browse_teams),
            format_func=lambda x: f"{x} — {team_names.get(x, x)}",
            key="browse_team_filter",
        )

        # Build candidate list for selection
        browse_items: list[dict] = []
        for vid_id, meta in youtube_meta.items():
            team = meta.get("team", "")
            if team_filter and team not in team_filter:
                continue
            url = meta.get("url", "")
            if not url:
                continue
            browse_items.append({
                "video_id": vid_id,
                "team": team,
                "title": meta.get("title", "Unknown"),
                "url": url,
                "date": str(_video_date(meta) or selected_date),
                "method": meta.get("method", ""),
            })
        browse_items.sort(key=lambda x: (x["team"], x["title"]))

        st.markdown(f"**{len(browse_items)} YouTube videos**")
        sel = _video_selector(browse_items, key_prefix=f"browse_{selected_date}")
        _push_footer(sel, f"browse_{selected_date}", chosen_nb_url, chosen_nb_name)

        # Transcript previews + bulk download
        if txt_files:
            st.divider()
            st.markdown(f"**📝 {len(txt_files)} transcripts**")
            with st.expander("Browse / download transcripts"):
                for txt_file in txt_files:
                    abbr = txt_file.name.split("_")[0]
                    if team_filter and abbr not in team_filter:
                        continue
                    team_name_display = team_names.get(abbr, abbr)
                    display_name = txt_file.stem.replace("_", " ")
                    with st.expander(f"{team_name_display}: {display_name}"):
                        content = txt_file.read_text(encoding="utf-8")
                        preview_len = 500
                        if len(content) > preview_len:
                            st.markdown(content[:preview_len] + "...")
                        else:
                            st.markdown(content)
                        st.caption(f"Length: {len(content):,} characters")
                        st.download_button(
                            label=f"Download {txt_file.name}",
                            data=content,
                            file_name=txt_file.name,
                            mime="text/plain",
                            key=f"dl_{selected_date}_{txt_file.name}",
                        )

            if st.button("Download all transcripts as ZIP", key="browse_zip_btn"):
                import io
                import zipfile
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                    for txt_file in txt_files:
                        abbr = txt_file.name.split("_")[0]
                        if team_filter and abbr not in team_filter:
                            continue
                        zf.writestr(
                            txt_file.name,
                            txt_file.read_text(encoding="utf-8"),
                        )
                st.download_button(
                    label="📦 Download ZIP",
                    data=zip_buffer.getvalue(),
                    file_name=f"transcripts_{selected_date}.zip",
                    mime="application/zip",
                    key="browse_zip_dl",
                )


# ─── Tab 2: Backfill ─────────────────────────────────────
with tab_backfill:
    st.markdown(
        "Scan team YouTube channels for press conferences in a custom date range. "
        "Results stay loaded so you can push them to a notebook below."
    )

    bf_cols = st.columns([1, 1, 2])
    with bf_cols[0]:
        bf_start = st.date_input(
            "Start date",
            value=datetime.now().date() - timedelta(days=7),
            key="bf_start",
        )
    with bf_cols[1]:
        bf_end = st.date_input(
            "End date",
            value=datetime.now().date(),
            key="bf_end",
        )
    with bf_cols[2]:
        bf_team_options = ["All Teams"] + sorted(team_names.keys())
        bf_team = st.selectbox(
            "Team(s)",
            bf_team_options,
            format_func=lambda x: f"{x} — {team_names[x]}" if x in team_names else x,
            key="bf_team",
        )

    run_backfill = st.button("Run Backfill", type="primary", key="bf_run")

    # Load a previous backfill's saved results without re-running.
    # Useful when the page lost session state, or to avoid hitting
    # YouTube rate-limits by re-scanning the same channels.
    existing_backfills = sorted(
        [d.name for d in (raw_base.iterdir() if raw_base.exists() else [])
         if d.is_dir() and d.name.startswith("backfill-")
         and (d / "all_transcripts.json").exists()],
        reverse=True,
    )
    if existing_backfills:
        with st.expander("📂 Load previous backfill (skip re-scan)", expanded=False):
            chosen_prev = st.selectbox(
                "Pick a saved backfill",
                existing_backfills,
                key="bf_prev_choice",
            )
            if st.button("Load into push UI", key="bf_load_prev"):
                prev_path = raw_base / chosen_prev / "all_transcripts.json"
                prev_videos_path = raw_base / chosen_prev / "all_videos.json"
                try:
                    with open(prev_path, encoding="utf-8") as f:
                        prev_transcripts = json.load(f)
                    video_count = 0
                    if prev_videos_path.exists():
                        with open(prev_videos_path, encoding="utf-8") as f:
                            video_count = len(json.load(f))
                    st.session_state["bf_results"] = {
                        "date_str": chosen_prev,
                        "transcripts": prev_transcripts,
                        "video_count": video_count,
                    }
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to load: {e}")

    if run_backfill:
        if bf_start > bf_end:
            st.error("Start date must be before end date.")
        else:
            from collectors.youtube_collector import (
                _parse_subtitle_file,
                _transcribe_with_whisper,
                _whisper_lock,
                download_audio,
                download_captions,
                scan_channel,
            )
            from config_loader import get_settings, get_youtube_keywords
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import re
            import time

            settings = get_settings()
            keywords = get_youtube_keywords()

            delta = datetime.now(tz=None) - datetime.combine(
                bf_start, datetime.min.time()
            )
            lookback_hours = int(delta.total_seconds() / 3600) + 24

            bf_settings = dict(settings)
            bf_settings["collection"] = dict(settings["collection"])
            bf_settings["collection"]["lookback_hours"] = lookback_hours
            # Channels can pile up 40+ uploads during draft week before
            # Day-1 pressers fall off the visible window — scan deeper for
            # backfill so press conferences buried under reaction clips
            # still get caught.
            bf_settings["collection"]["youtube_max_per_channel"] = 100

            date_str = f"backfill-{bf_start}-to-{bf_end}"
            bf_transcripts_dir = get_data_dir("transcripts", date_str)
            bf_temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
            bf_temp_dir.mkdir(exist_ok=True)

            scan_teams = (
                list(teams) if bf_team == "All Teams"
                else [t for t in teams if t["abbr"] == bf_team]
            )

            delay = settings["collection"].get("request_delay", 2.0)
            workers = int(settings["collection"].get("youtube_workers", 6))

            # YouTube's `upload_date` is UTC-day. A team uploading after
            # ~8pm Eastern lands in the *next* UTC day, so a strict
            # `<= bf_end` filter silently drops the entire late-evening
            # press-conference burst on the user's selected end date.
            # Extend the upper bound by 1 day to absorb that offset.
            bf_end_inclusive = bf_end + timedelta(days=1)

            def _backfill_team(team):
                abbr = team["abbr"]
                videos = scan_channel(team, keywords, bf_settings, date_str)
                team_videos = []
                team_transcripts = []
                team_skipped: list[dict] = []
                for v in videos:
                    upload = v.get("upload_date", "")
                    if upload:
                        try:
                            v_date = datetime.strptime(upload, "%Y%m%d").date()
                            if v_date < bf_start or v_date > bf_end_inclusive:
                                continue
                        except ValueError:
                            pass

                    team_videos.append(
                        {**v, "team": abbr, "team_name": team["name"]}
                    )

                    # Resumability: if a transcript .txt for this video
                    # already exists in the backfill's transcripts dir,
                    # the prior run got this one — skip caption + Whisper
                    # and reuse the existing text. Lets an interrupted
                    # backfill re-run pick up from where it left off in
                    # seconds rather than re-doing every video.
                    existing_slug = re.sub(
                        r"[^\w\s-]", "", v["title"]
                    )[:60].strip().replace(" ", "_")
                    existing_txt = (
                        bf_transcripts_dir / f"{abbr}_{existing_slug}.txt"
                    )
                    if existing_txt.exists() and existing_txt.stat().st_size > 0:
                        team_transcripts.append({
                            "video_id": v["video_id"],
                            "title": v["title"],
                            "team": abbr,
                            "team_name": team["name"],
                            "url": v["url"],
                            "upload_date": v.get("upload_date", ""),
                            "method": "cached",
                        })
                        continue

                    # 1) Try auto-generated captions first.
                    caption_path = download_captions(
                        v["video_id"], v["url"], bf_temp_dir, bf_settings,
                    )
                    transcript_text = None
                    method = "captions"
                    if caption_path:
                        transcript_text = _parse_subtitle_file(caption_path)

                    # 2) Whisper fallback when captions are missing/empty —
                    #    matches the daily collector's behavior so backfill
                    #    doesn't silently drop videos that lack
                    #    auto-generated captions (live-stream archives,
                    #    very recent uploads).
                    if not (transcript_text and len(transcript_text.strip()) > 50):
                        duration = int(v.get("duration") or 0)
                        max_dur = int(
                            bf_settings.get("transcription", {})
                            .get("whisper_max_duration_seconds", 1800)
                        )
                        if max_dur > 0 and duration > max_dur:
                            team_skipped.append({
                                "title": v.get("title", ""),
                                "url": v.get("url", ""),
                                "reason": (
                                    f"too long for Whisper ({duration//60}m"
                                    f" > {max_dur//60}m cap)"
                                ),
                            })
                        else:
                            audio_path = download_audio(
                                v["video_id"], v["url"], bf_temp_dir, bf_settings,
                            )
                            if audio_path:
                                with _whisper_lock:
                                    transcript_text = _transcribe_with_whisper(
                                        audio_path, bf_settings,
                                    )
                                method = "whisper"
                                if bf_settings.get("transcription", {}).get(
                                    "delete_audio_after", True
                                ):
                                    audio_path.unlink(missing_ok=True)
                            else:
                                team_skipped.append({
                                    "title": v.get("title", ""),
                                    "url": v.get("url", ""),
                                    "reason": "no captions and audio download failed",
                                })

                    if transcript_text and len(transcript_text.strip()) > 50:
                        slug = re.sub(r"[^\w\s-]", "", v["title"])[:60].strip().replace(" ", "_")
                        txt_path = bf_transcripts_dir / f"{abbr}_{slug}.txt"
                        txt_path.write_text(transcript_text, encoding="utf-8")

                        team_transcripts.append({
                            "video_id": v["video_id"],
                            "title": v["title"],
                            "team": abbr,
                            "team_name": team["name"],
                            "url": v["url"],
                            "upload_date": v.get("upload_date", ""),
                            "method": method,
                        })
                    elif not team_skipped or team_skipped[-1].get("title") != v.get("title", ""):
                        # Whisper ran but produced empty/short text.
                        team_skipped.append({
                            "title": v.get("title", ""),
                            "url": v.get("url", ""),
                            "reason": "transcription returned empty/very short text",
                        })
                    time.sleep(delay)
                return team_videos, team_transcripts, team_skipped

            all_videos = []
            all_transcripts = []
            all_skipped: list[dict] = []
            total = len(scan_teams)
            completed = 0
            progress = st.progress(
                0.0, text=f"Scanning {total} teams (workers={workers})..."
            )

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(_backfill_team, t): t for t in scan_teams
                }
                for fut in as_completed(futures):
                    team = futures[fut]
                    try:
                        team_videos, team_transcripts, team_skipped = fut.result()
                    except Exception as e:
                        st.warning(f"{team['abbr']} failed: {e}")
                        team_videos, team_transcripts, team_skipped = [], [], []
                    all_videos.extend(team_videos)
                    all_transcripts.extend(team_transcripts)
                    all_skipped.extend(team_skipped)
                    completed += 1
                    progress.progress(
                        completed / total,
                        text=f"{completed}/{total} teams complete — "
                             f"{len(all_transcripts)} transcripts so far",
                    )

            out_dir = get_data_dir("raw", date_str)
            with open(out_dir / "all_videos.json", "w", encoding="utf-8") as f:
                json.dump(all_videos, f, indent=2, ensure_ascii=False)
            with open(out_dir / "all_transcripts.json", "w", encoding="utf-8") as f:
                json.dump(all_transcripts, f, indent=2, ensure_ascii=False)

            progress.progress(1.0, text="Backfill complete!")

            # Persist results in session state so they survive reruns
            st.session_state["bf_results"] = {
                "date_str": date_str,
                "transcripts": all_transcripts,
                "video_count": len(all_videos),
                "skipped": all_skipped,
            }

    # Render results (from this run or previous run within the session)
    bf_results = st.session_state.get("bf_results")
    if bf_results:
        st.success(
            f"Backfill `{bf_results['date_str']}`: "
            f"{bf_results['video_count']} videos found, "
            f"{len(bf_results['transcripts'])} transcripts saved."
        )

        skipped = bf_results.get("skipped") or []
        if skipped:
            with st.expander(
                f"⚠ {len(skipped)} found videos produced no transcript",
                expanded=False,
            ):
                st.caption(
                    "Captions weren't available and the Whisper fallback "
                    "didn't produce usable text for these videos. They're "
                    "still in `all_videos.json` if you want to inspect them."
                )
                for s in skipped:
                    title = s.get("title", "(untitled)")
                    url = s.get("url", "")
                    reason = s.get("reason", "")
                    if url:
                        st.markdown(f"- [{title}]({url}) — *{reason}*")
                    else:
                        st.markdown(f"- {title} — *{reason}*")

        # Build candidate list in the shape _video_selector expects
        bf_items = []
        for t in bf_results["transcripts"]:
            ud = t.get("upload_date", "")
            date = (
                f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
                if (len(ud) == 8 and ud.isdigit())
                else ud
            )
            bf_items.append({
                "video_id": t["video_id"],
                "team": t["team"],
                "title": t["title"],
                "url": t["url"],
                "date": date,
                "method": t.get("method", ""),
            })
        bf_items.sort(key=lambda x: (x["team"], x["title"]))

        sel = _video_selector(bf_items, key_prefix=f"bf_{bf_results['date_str']}")
        _push_footer(
            sel, f"bf_{bf_results['date_str']}", chosen_nb_url, chosen_nb_name
        )

        if st.button("Clear backfill results from view", key="bf_clear"):
            del st.session_state["bf_results"]
            st.rerun()


# ─── Tab 3: Multi-day Export ────────────────────────────
with tab_export:
    st.markdown(
        "Pull videos across **all** stored dates (daily + backfill) for a team and date range."
    )

    min_date, max_date = _all_known_dates()
    export_cols = st.columns([2, 1, 1])
    with export_cols[0]:
        export_team = st.selectbox(
            "Team",
            ["All Teams"] + sorted(team_names.keys()),
            format_func=lambda x: f"{x} — {team_names[x]}" if x in team_names else x,
            key="export_team",
        )
    with export_cols[1]:
        default_start = max(min_date, max_date - timedelta(days=7))
        ex_start = st.date_input(
            "From",
            value=default_start,
            min_value=min_date,
            max_value=max_date,
            key="export_start",
        )
    with export_cols[2]:
        ex_end = st.date_input(
            "To",
            value=max_date,
            min_value=min_date,
            max_value=max_date,
            key="export_end",
        )

    export_items = []
    seen_vid_ids = set()
    for dir_name in all_transcript_dirs:
        meta = _load_youtube_meta(dir_name)
        for vid_id, item in meta.items():
            if vid_id in seen_vid_ids:
                continue
            team = item.get("team", "")
            if export_team != "All Teams" and team != export_team:
                continue
            v_date = _video_date(item) or _parse_date_safe(dir_name)
            if v_date is None:
                continue
            if v_date < ex_start or v_date > ex_end:
                continue
            url = item.get("url", "")
            if not url:
                continue
            seen_vid_ids.add(vid_id)
            export_items.append({
                "video_id": vid_id,
                "team": team,
                "title": item.get("title", "Unknown"),
                "url": url,
                "date": str(v_date),
                "method": item.get("method", ""),
            })
    export_items.sort(key=lambda x: x["date"])

    sel = _video_selector(export_items, key_prefix="export")
    _push_footer(sel, "export", chosen_nb_url, chosen_nb_name)


# ─── Tab 4: Publish to cloud ─────────────────────────────
with tab_publish:
    st.markdown(
        "Stage, commit, and push the YouTube transcripts you've collected "
        "locally so they're visible to the **YouTube Report** tab on "
        "[the live site](https://nfl-news-agent.streamlit.app/).\n\n"
        "Paths included: `data/raw/<date>/youtube.json`, "
        "`data/transcripts/<date>/`, and `data/youtube_seen.json`. "
        "Other modified files are left alone."
    )

    # The scan + per-video parse hits `git status` and reads every queued
    # youtube.json from disk. Streamlit re-evaluates every tab body on
    # every rerun, so during a long-running Backfill (which writes new
    # files and reruns the page repeatedly to update progress) doing this
    # work unconditionally would slow each rerun. Gate it behind a
    # button and cache the result in session_state.
    scan_btn_label = (
        "🔄 Re-scan working tree"
        if "publish_scan" in st.session_state
        else "🔍 Scan for pending YouTube files"
    )
    if st.button(scan_btn_label, key="publish_scan_btn"):
        with st.spinner("Scanning git working tree..."):
            scan_pending = _git_pending_youtube_paths()
            scan_videos, scan_loose, scan_other = _summarize_pending_videos(
                scan_pending
            )
            st.session_state["publish_scan"] = {
                "pending": scan_pending,
                "videos": scan_videos,
                "loose_dirs": scan_loose,
                "other_paths": scan_other,
                "ts": datetime.now(),
            }

    scan = st.session_state.get("publish_scan")
    if scan is None:
        st.info(
            "Click the button above to inspect the working tree. The scan "
            "is gated behind this button so it doesn't slow down a "
            "long-running Backfill on the previous tab."
        )
        st.stop()

    st.caption(
        f"Scan from {scan['ts'].strftime('%I:%M:%S %p')}. "
        "Press the button again after a backfill or a fresh "
        "`collect_youtube.py` run."
    )

    pending = scan["pending"]

    if not pending:
        st.success(
            "✓ Up to date — no untracked or modified YouTube files. Run "
            "`scripts/collect_youtube.py` to gather fresh transcripts first."
        )
    else:
        # Reuse the parse from the button-triggered scan above.
        videos = scan["videos"]
        loose_dirs = scan["loose_dirs"]
        other_paths = scan["other_paths"]

        # Per-video table — what the user really cares about.
        if videos:
            videos_by_date: dict[str, list[dict]] = {}
            for v in videos:
                videos_by_date.setdefault(v["date"], []).append(v)

            st.markdown(
                f"**{len(videos)} video(s) across "
                f"{len(videos_by_date)} date(s) will be published:**"
            )
            for date_label in sorted(videos_by_date.keys()):
                day_videos = videos_by_date[date_label]
                with st.expander(
                    f"📅 {date_label} — {len(day_videos)} video(s)",
                    expanded=True,
                ):
                    rows = [
                        {
                            "Team": v["team"],
                            "Title": v["title"],
                            "Method": v["method"],
                            "URL": v["url"],
                        }
                        for v in day_videos
                    ]
                    st.dataframe(
                        rows,
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "URL": st.column_config.LinkColumn(
                                "URL", display_text="watch ↗"
                            ),
                        },
                    )

        # Loose transcript directories (their .txt files are queued but the
        # parent youtube.json was already committed in a previous push).
        if loose_dirs:
            loose_total = sum(count for _, count in loose_dirs)
            st.markdown(
                f"**{loose_total} loose transcript file(s)** in "
                f"{len(loose_dirs)} dir(s) — metadata already committed:"
            )
            for name, count in loose_dirs:
                st.text(f"  data/transcripts/{name}/  ({count} .txt files)")

        # Anything that wasn't a video or transcript file (typically just
        # data/youtube_seen.json).
        if other_paths:
            with st.expander(
                f"Other files ({len(other_paths)})",
                expanded=False,
            ):
                for path in other_paths:
                    st.text(path)

        # Raw porcelain list for users who want it.
        with st.expander(
            f"Raw git status ({len(pending)} files)",
            expanded=False,
        ):
            for code, path in pending:
                label = code.strip() or "??"
                st.text(f"[{label}] {path}")

        default_msg = (
            f"YouTube transcripts {datetime.now().strftime('%Y-%m-%d')}"
        )
        commit_msg = st.text_input(
            "Commit message",
            value=default_msg,
            key="publish_commit_msg",
            help="One-line message used for the git commit.",
        )

        push_clicked = st.button(
            "🚀 Push to live site",
            type="primary",
            disabled=not commit_msg.strip(),
            help=(
                "Stages only the YouTube paths above, commits with the "
                "message, and pushes to origin/master. Streamlit Cloud "
                "auto-redeploys ~1 minute later."
            ),
        )

        if push_clicked:
            paths_only = [p for _, p in pending]
            with st.spinner("Staging, committing, pushing..."):
                ok, log = _publish_to_cloud(paths_only, commit_msg.strip())

            if ok:
                # Stale scan after a successful push — the staged paths
                # now live on origin/master and won't show up in the next
                # `git status`. Drop the cached scan so the next render
                # nudges the user to rescan.
                st.session_state.pop("publish_scan", None)
                st.success(
                    "✓ Pushed to origin/master. The live site will "
                    "redeploy in roughly a minute. Open the **YouTube "
                    "Report** tab there to confirm the new transcripts "
                    "are visible."
                )
            else:
                st.error(
                    "Push failed. See the git output below — most likely "
                    "either no upstream credentials, or master moved while "
                    "the commit was being prepared. You can retry from a "
                    "terminal: `git push origin master`."
                )

            with st.expander("Git output", expanded=not ok):
                st.code("\n".join(log), language="text")


# ──────────────────────────────────────────────────────────
# Reference: monitored channels
# ──────────────────────────────────────────────────────────
with st.expander("YouTube channels being monitored"):
    st.markdown(
        "Videos are collected from **all 32 official NFL team YouTube channels**:"
    )
    cols = st.columns(4)
    for i, team in enumerate(sorted(teams, key=lambda t: t["name"])):
        channels = team.get("youtube_channels", [])
        cid = (
            channels[0]["id"]
            if channels
            else team.get("youtube_channel_id", "")
        )
        cols[i % 4].markdown(
            f"[{team['abbr']}](https://www.youtube.com/channel/{cid}) "
            f"— {team['name']}"
        )
