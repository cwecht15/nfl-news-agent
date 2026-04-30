"""Transcripts & YouTube Links — Browse videos, push to NotebookLM, backfill."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Transcripts", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_data_dir, get_teams

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
# Tabs
# ──────────────────────────────────────────────────────────

tab_browse, tab_backfill, tab_export = st.tabs(
    ["📺 Browse by date", "🔍 Backfill", "📋 Multi-day export"]
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
                scan_channel, download_captions, _parse_subtitle_file,
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

                    caption_path = download_captions(
                        v["video_id"], v["url"], bf_temp_dir, bf_settings,
                    )
                    transcript_text = None
                    if caption_path:
                        transcript_text = _parse_subtitle_file(caption_path)

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
                            "method": "captions",
                        })
                    time.sleep(delay)
                return team_videos, team_transcripts

            all_videos = []
            all_transcripts = []
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
                        team_videos, team_transcripts = fut.result()
                    except Exception as e:
                        st.warning(f"{team['abbr']} failed: {e}")
                        team_videos, team_transcripts = [], []
                    all_videos.extend(team_videos)
                    all_transcripts.extend(team_transcripts)
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
            }

    # Render results (from this run or previous run within the session)
    bf_results = st.session_state.get("bf_results")
    if bf_results:
        st.success(
            f"Backfill `{bf_results['date_str']}`: "
            f"{bf_results['video_count']} videos found, "
            f"{len(bf_results['transcripts'])} transcripts saved."
        )

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
