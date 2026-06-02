"""Podcast Report — on-demand summary of podcast episodes in a date range.

Pairs with `scripts/collect_podcasts.py`: that tool collects episode
transcripts (or show notes) locally and the user pushes them; this page
summarizes whatever is in `data/raw/<date>/podcast.json` for the selected
range, on demand. Mirrors the YouTube Report tab and reuses the same
summarizer (`build_yt_section` operates on Transcript lists) — episodes are
stored as Transcript records with the episode GUID in `video_id` and the show
title in `channel_name`.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Podcast Report", page_icon="🎙️", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard.helpers import bootstrap_secrets
bootstrap_secrets()

import pandas as pd

from config_loader import get_data_dir, get_teams_by_abbr
from dashboard.citations import build_citation_linker
from dashboard.flagging import (
    FLAG_MODE_LABELS,
    FLAG_MODE_VALUES,
    render_flaggable,
)
from models import Transcript
from processing import podcast_cache
from processing.yt_section import build_yt_section
from reports.flagged_findings import MODE_HANDBOOK

ALL_TEAMS = sorted(set(get_teams_by_abbr().keys()) | {"NFL"})

st.header("Podcast Report")

if not os.environ.get("OPENAI_API_KEY"):
    st.error(
        "**OpenAI API key is not configured for this dashboard.** The "
        "Podcast Report tab calls OpenAI on demand to summarize episodes, so "
        "a key has to be reachable to the Streamlit process.\n\n"
        "**On Streamlit Cloud:** *Manage app → Settings → Secrets*, add "
        "`OPENAI_API_KEY = \"sk-...\"`, save, and reload."
    )
    st.stop()

st.caption(
    "Summarized NFL podcast episodes (published transcript when a show offers "
    "one, otherwise show notes). Pick a date range, choose episodes, click "
    "Generate. Each click runs an LLM call — results are cached."
)

raw_base = get_data_dir("raw")


def _list_dated_dirs() -> list[str]:
    dirs: list[str] = []
    if not raw_base.exists():
        return dirs
    for d in raw_base.iterdir():
        if not d.is_dir() or len(d.name) != 10 or d.name[4] != "-":
            continue
        if (d / "podcast.json").exists():
            dirs.append(d.name)
    return sorted(dirs)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _all_episodes() -> list[Transcript]:
    """Load every episode across all collection folders, deduped by GUID.

    A single collect_podcasts run (with its multi-day lookback) writes every
    episode it found into one date folder, so episodes are filtered by their
    own published date — NOT the collection-folder date — for the range
    picker to be meaningful.
    """
    out: list[Transcript] = []
    seen: set[str] = set()
    for dir_name in _list_dated_dirs():
        path = raw_base / dir_name / "podcast.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        for record in raw:
            try:
                ep = Transcript.from_dict(record)
            except Exception:
                continue
            if ep.video_id and ep.video_id in seen:
                continue
            if ep.video_id:
                seen.add(ep.video_id)
            out.append(ep)
    return out


def _load_in_range(start: date, end: date) -> list[Transcript]:
    """Episodes whose published date falls in [start, end]."""
    out: list[Transcript] = []
    for ep in _all_episodes():
        d = ep.published.date() if ep.published else None
        if d is None or d < start or d > end:
            continue
        out.append(ep)
    return out


def _published_span() -> tuple[date, date] | None:
    dates = [ep.published.date() for ep in _all_episodes() if ep.published]
    if not dates:
        return None
    return min(dates), max(dates)


def _summary_payload(start_iso, end_iso, team_filter, selected_ids) -> dict:
    start = _parse_date(start_iso)
    end = _parse_date(end_iso)
    if start is None or end is None or end < start:
        return {}

    episodes = _load_in_range(start, end)
    if team_filter:
        wanted = set(team_filter)
        episodes = [e for e in episodes if e.team in wanted]
    if selected_ids:
        wanted_ids = set(selected_ids)
        episodes = [e for e in episodes if e.video_id in wanted_ids]
    if not episodes:
        return {"empty": True, "reason": "no_episodes"}

    cache_key = podcast_cache.make_key(start_iso, end_iso, team_filter, selected_ids)
    cached = podcast_cache.load(cache_key)
    if cached and cached.get("section"):
        section = cached["section"]
        section["_cache"] = {"hit": True, "generated_at": cached.get("generated_at")}
        return section

    label = start_iso if start_iso == end_iso else f"{start_iso} → {end_iso}"
    section = build_yt_section(episodes, date_label=label, pre_filtered=True)
    podcast_cache.save(
        cache_key, section,
        meta={"start": start_iso, "end": end_iso,
              "teams": sorted(team_filter or []), "episode_count": len(selected_ids or [])},
    )
    section["_cache"] = {"hit": False}
    return section


@st.cache_data(ttl=86400, show_spinner=False)
def _generate_cached(start_iso, end_iso, team_filter, selected_ids) -> dict:
    return _summary_payload(start_iso, end_iso, team_filter, selected_ids)


# ──────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────

available_dirs = _list_dated_dirs()
if not available_dirs:
    st.warning(
        "No podcast episodes available yet. Run "
        "`python scripts/collect_podcasts.py` locally and push the resulting "
        "`data/raw/<date>/podcast.json` files."
    )
    st.stop()

# Range bounds come from episodes' own published dates (see _all_episodes).
span = _published_span()
if span is not None:
    earliest, latest = span
else:
    earliest = _parse_date(available_dirs[0]) or date.today() - timedelta(days=30)
    latest = _parse_date(available_dirs[-1]) or date.today()
today = date.today()
# Clamp into [earliest, latest] — collection stamps the UTC date, which can
# be a day ahead of the local `today`, so min(latest, today) could fall below
# `earliest` (the only available day) and crash st.date_input.
default_end = max(earliest, min(latest, today))
default_start = max(earliest, min(latest, default_end - timedelta(days=13)))

# Purge any stored date left over from a previous run that now falls outside
# the available window (e.g. new data shifted the range) so the clamped
# default re-applies instead of failing st.date_input's range validation.
for _k, _dflt in (("pod_start_date", default_start), ("pod_end_date", default_end)):
    _v = st.session_state.get(_k)
    if isinstance(_v, date) and not (earliest <= _v <= latest):
        del st.session_state[_k]

col_l, col_r = st.columns(2)
with col_l:
    start_d = st.date_input("Start date", value=default_start,
                            min_value=earliest, max_value=latest, key="pod_start_date")
with col_r:
    end_d = st.date_input("End date", value=default_end,
                          min_value=earliest, max_value=latest, key="pod_end_date")

all_team_abbrs = sorted(set(get_teams_by_abbr().keys()) | {"NFL"})
team_filter_list = st.multiselect(
    "Filter teams (leave empty for all)", all_team_abbrs,
    help="Restricts both the episode-highlights scope and per-team output.",
)

st.caption(
    f"Episodes available (by published date): {earliest.isoformat()} → "
    f"{latest.isoformat()}"
)

if start_d > end_d:
    st.error("Start date must be on or before end date.")
    st.stop()

candidate = _load_in_range(start_d, end_d)
if team_filter_list:
    wanted = set(team_filter_list)
    candidate = [e for e in candidate if e.team in wanted]
if not candidate:
    st.info("No episodes in this date range / team filter. Push more or widen the range.")
    st.stop()

# Build the candidate pool; each row carries its episode GUID (video_id).
rows: list[dict] = []
all_ids: list[str] = []
for e in candidate:
    date_str = e.published.date().isoformat() if e.published else ""
    rows.append({
        "video_id": e.video_id,
        "Scope": "National" if e.team == "NFL" else "Team",
        "Team": e.team,
        "Show": e.channel_name,
        "Date": date_str,
        "Src": "transcript" if e.method == "transcript" else "notes",
        "Title": e.title,
        "Open": e.url,
    })
    all_ids.append(e.video_id)

# Selection persisted as a set of GUIDs — immune to sorting/filtering. Podcasts
# are curated feeds, so everything is checked by default. Re-seeded when the
# range / team filter changes.
sel_token = f"{start_d}_{end_d}_{','.join(team_filter_list)}"
if st.session_state.get("pod_sel_token") != sel_token:
    st.session_state["pod_sel_token"] = sel_token
    st.session_state["pod_sel_ids"] = set(all_ids)
st.session_state["pod_sel_ids"] &= set(all_ids)
selected_ids: set = st.session_state["pod_sel_ids"]

st.caption(
    f"{len(rows)} episodes in range — all checked by default. Set **Sort** / "
    f"**Filter** first, then tick/untick rows (ticking won't reload the page), "
    f"then click **Apply selection & Generate Report**. The bulk buttons and "
    f"sort/filter reload the page, so use them before ticking."
)

editor_nonce = st.session_state.setdefault("pod_editor_nonce", 0)
b_all, b_clear = st.columns(2)
if b_all.button("Select all"):
    st.session_state["pod_sel_ids"] = set(all_ids)
    st.session_state["pod_editor_nonce"] = editor_nonce + 1
    st.rerun()
if b_clear.button("Clear all"):
    st.session_state["pod_sel_ids"] = set()
    st.session_state["pod_editor_nonce"] = editor_nonce + 1
    st.rerun()

# Sort + filter in pandas so the displayed order matches the data passed to the
# editor (avoids the data_editor sort-desync bug #11345/#10015). Use these
# controls rather than the table's own header sort.
f_search, f_sort, f_dir = st.columns([2, 1, 1])
search = f_search.text_input("Filter (title / team / show)", placeholder="e.g. Garrett, NYG, Schefter")
sort_by = f_sort.selectbox("Sort by", ["Date", "Scope", "Team", "Show", "Title"], index=0)
descending = f_dir.selectbox("Order", ["Asc", "Desc"], index=0) == "Desc"

df = pd.DataFrame(rows)
if search.strip():
    q = search.strip().lower()
    mask = (
        df["Title"].str.lower().str.contains(q, regex=False, na=False)
        | df["Team"].str.lower().str.contains(q, regex=False, na=False)
        | df["Show"].str.lower().str.contains(q, regex=False, na=False)
        | df["Scope"].str.lower().str.contains(q, regex=False, na=False)
    )
    df = df[mask]
df = df.sort_values(by=sort_by, ascending=not descending, kind="stable").reset_index(drop=True)
df.insert(0, "Select", df["video_id"].isin(selected_ids))

# Checkbox edits live inside a form so ticking boxes does NOT rerun the page
# (which would redraw the table and wipe a column-header sort). Edits apply
# only on Generate. Sort/filter are OUTSIDE the form, so set those BEFORE
# ticking — changing them reruns the page and resets un-applied checkboxes.
session_key = "pod_report_section"
session_inputs_key = "pod_report_inputs"
selected_episode_ids = tuple(sorted(selected_ids))
current_inputs = (
    start_d.isoformat(), end_d.isoformat(),
    tuple(team_filter_list), selected_episode_ids,
)

with st.form("pod_episode_form", clear_on_submit=False):
    edited = st.data_editor(
        df, hide_index=True, use_container_width=True, height=360,
        column_config={
            "Select": st.column_config.CheckboxColumn(required=True, width="small"),
            "video_id": None,
            "Scope": st.column_config.TextColumn(width="small", help="National = league-wide show; Team = single-team show."),
            "Team": st.column_config.TextColumn(width="small"),
            "Show": st.column_config.TextColumn(width="medium"),
            "Date": st.column_config.TextColumn(width="small"),
            "Src": st.column_config.TextColumn(width="small", help="transcript = published transcript; notes = show notes fallback."),
            "Title": st.column_config.TextColumn(width="large"),
            "Open": st.column_config.LinkColumn("Open", width="small"),
        },
        disabled=["Scope", "Team", "Show", "Date", "Src", "Title", "Open"],
        key=(
            f"pod_episode_editor_{sel_token}_{sort_by}_{descending}_"
            f"{len(search.strip())}_{search.strip().lower()}_{editor_nonce}"
        ),
    )
    submitted = st.form_submit_button("Apply selection & Generate Report", type="primary")

if submitted:
    # Reconcile the persisted set from the rows shown (keyed by GUID, so it's
    # correct regardless of sort order); rows filtered out keep their state.
    shown_ids = set(edited["video_id"])
    checked_ids = set(edited.loc[edited["Select"], "video_id"])
    st.session_state["pod_sel_ids"] = (selected_ids - shown_ids) | checked_ids
    selected_episode_ids = tuple(sorted(st.session_state["pod_sel_ids"]))
    if not selected_episode_ids:
        st.warning("No episodes selected — tick at least one and Generate again.")
        st.stop()
    current_inputs = (
        start_d.isoformat(), end_d.isoformat(),
        tuple(team_filter_list), selected_episode_ids,
    )
    with st.spinner("Summarizing episodes..."):
        section = _generate_cached(*current_inputs)
    st.session_state[session_key] = section
    st.session_state[session_inputs_key] = current_inputs
else:
    section = st.session_state.get(session_key)
    cached_inputs = st.session_state.get(session_inputs_key)
    # Drop a stale render when the range / team filter (and thus the default
    # selection) changed since it was generated.
    if section is not None and cached_inputs != current_inputs:
        section = None
        st.session_state.pop(session_key, None)
        st.session_state.pop(session_inputs_key, None)

if section is None:
    st.info("Pick a range and click **Generate Report**. Each generation runs an LLM call.")
    st.stop()
if not section:
    st.error("Could not load episodes for that range.")
    st.stop()
if section.get("empty"):
    st.info("No episodes in that range. Push more before regenerating.")
    st.stop()


# ──────────────────────────────────────────────────────────
# Render
# ──────────────────────────────────────────────────────────

count = section.get("transcript_count", 0)
label = section.get("date_label", "")
st.success(f"Summarized {count} episodes ({label}).")

cache_info = section.get("_cache", {}) or {}
if cache_info.get("hit"):
    st.caption(
        f"⚡ Loaded from saved cache (generated {cache_info.get('generated_at', '')} "
        f"UTC) — no tokens spent on this view."
    )

flag_mode = st.selectbox(
    "Flag mode", options=FLAG_MODE_VALUES,
    format_func=lambda v: FLAG_MODE_LABELS[v], key="pod_flag_mode",
)
if flag_mode == MODE_HANDBOOK:
    page_flagger = st.text_input(
        "Your name (saved with each flag for attribution)",
        value=st.session_state.get("flagger_name_remembered", ""),
        key="pod_page_flagger_name", placeholder="optional",
    )
    st.session_state["flagger_name_remembered"] = (page_flagger or "").strip()

flag_report_date = label or f"{start_d.isoformat()} → {end_d.isoformat()}"

# build_yt_section names the per-item block "press_conferences"; for podcasts
# it's the per-episode highlights.
pc = section.get("press_conferences", {}) or {}
pc_summary = (pc.get("summary") or "").strip()
if pc_summary:
    st.subheader(f"Episode Highlights ({pc.get('count', 0)} episodes)")
    if flag_mode:
        render_flaggable(
            pc_summary, flag_report_date, "pod:episodes", "Podcast: Episode Highlights",
            key_prefix="pod_ep", all_teams=ALL_TEAMS, mode=flag_mode,
        )
    else:
        st.markdown(pc_summary)

    pc_sources = pc.get("sources", []) or []
    links = [
        f"[{s.get('team', '')} — {s.get('title', 'episode')}]({s.get('url', '')})"
        for s in pc_sources if s.get("url")
    ]
    if links:
        st.caption("Episodes in this summary")
        st.markdown(" · ".join(links))

team_notes = section.get("team_notes", {}) or {}
if team_notes:
    st.subheader("Per-Team Notes")
    for team in sorted(team_notes.keys()):
        note = team_notes[team]
        note_summary = (note.get("summary") or "").strip()
        if not note_summary:
            continue
        numbered = note.get("numbered_sources", []) or []
        note_linkify = build_citation_linker(numbered)

        st.markdown(f"**{team}**")
        if flag_mode:
            render_flaggable(
                note_summary, flag_report_date, f"pod:team:{team}", f"Podcast Team: {team}",
                key_prefix=f"pod_team_{team}", all_teams=ALL_TEAMS,
                default_team=team, numbered_sources=numbered,
                linkify=note_linkify, mode=flag_mode,
            )
        else:
            st.markdown(note_linkify(note_summary) if note_linkify else note_summary)

        if numbered:
            lines = []
            for src in numbered:
                num = src.get("num", "")
                title_text = src.get("title", "Source")
                source_name = src.get("source", "")
                url = src.get("url", "")
                suffix = f" ({source_name})" if source_name else ""
                if url:
                    lines.append(f"**[{num}]** [{title_text}]({url}){suffix}")
                else:
                    lines.append(f"**[{num}]** {title_text}{suffix}")
            st.caption("Sources")
            st.markdown("\n\n".join(lines))
        st.divider()

usage = section.get("llm_usage", {})
if usage:
    st.subheader("LLM Usage")
    cols = st.columns(5)
    cols[0].metric("Calls", usage.get("request_count", 0))
    cols[1].metric("Input", usage.get("input_tokens", 0))
    cols[2].metric("Output", usage.get("output_tokens", 0))
    cols[3].metric("Reasoning", usage.get("reasoning_tokens", 0))
    cols[4].metric("Est. Cost", f"${float(usage.get('estimated_cost_usd', 0.0)):.4f}")
