"""YouTube Report — on-demand summary of transcripts in a date range.

Pairs with `scripts/collect_youtube.py`: that tool produces transcripts
locally and the user pushes them; this page summarizes whatever is in
`data/raw/<date>/youtube.json` for the selected range, on demand. The
public daily report deliberately does NOT include YouTube content — that
work happens here.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="YouTube Report", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard.helpers import bootstrap_secrets

# Bridge st.secrets → os.environ before importing pipeline modules that
# read OPENAI_API_KEY at call time. Locally this is a no-op (the .env
# file already populated os.environ); on Streamlit Cloud it pulls keys
# from the app's Secrets panel.
bootstrap_secrets()

from config_loader import get_data_dir, get_teams_by_abbr
from dashboard.citations import build_citation_linker
from dashboard.flagging import (
    FLAG_MODE_LABELS,
    FLAG_MODE_VALUES,
    render_flaggable,
)
from models import Transcript
from processing import yt_cache
from processing.summarizer import _press_relevance_score
from processing.yt_section import build_yt_section
from reports.flagged_findings import MODE_HANDBOOK

# "NFL" is the league-wide bucket for national channels (no single team);
# it isn't in teams.yaml, so add it explicitly for filtering / flagging.
ALL_TEAMS = sorted(set(get_teams_by_abbr().keys()) | {"NFL"})


st.header("YouTube Report")

if not os.environ.get("OPENAI_API_KEY"):
    st.error(
        "**OpenAI API key is not configured for this dashboard.** The "
        "YouTube Report tab calls OpenAI on demand to summarize "
        "transcripts, so a key has to be reachable to the Streamlit "
        "process.\n\n"
        "**To fix on Streamlit Cloud:** open *Manage app → Settings → "
        "Secrets* and add a line:\n\n"
        "```\nOPENAI_API_KEY = \"sk-...\"\n```\n\n"
        "Save and let the app reboot. This page will work on the next "
        "page load."
    )
    st.stop()
st.caption(
    "Summarized press conferences and per-team notes drawn from YouTube "
    "transcripts. Pick a date range, click Generate. Each click runs an "
    "LLM call — results are cached for the session."
)

raw_base = get_data_dir("raw")


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _list_dated_dirs() -> list[str]:
    """Return YYYY-MM-DD folders under data/raw/ that contain youtube.json."""
    dirs: list[str] = []
    if not raw_base.exists():
        return dirs
    for d in raw_base.iterdir():
        if not d.is_dir():
            continue
        if len(d.name) != 10 or d.name[4] != "-":
            continue
        if (d / "youtube.json").exists():
            dirs.append(d.name)
    return sorted(dirs)


def _parse_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _load_transcripts_in_range(start: date, end: date) -> list[Transcript]:
    """Load every transcript whose folder date falls in [start, end]."""
    out: list[Transcript] = []
    for dir_name in _list_dated_dirs():
        dir_date = _parse_date(dir_name)
        if dir_date is None or dir_date < start or dir_date > end:
            continue
        path = raw_base / dir_name / "youtube.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        for record in raw:
            try:
                out.append(Transcript.from_dict(record))
            except Exception:
                continue
    return out


def _summary_payload(
    start_iso: str,
    end_iso: str,
    team_filter: tuple,
    selected_video_ids: tuple,
) -> dict:
    """Pure cache key surface for `@st.cache_data` — small, hashable inputs.

    Lives outside the cached function so the cache key is identical
    across reruns of this page even though Transcript objects aren't
    hashable. `selected_video_ids` is the explicit user pick from the
    table; the score-based pre-filter is bypassed downstream so every
    selected video contributes regardless of its title score.
    """
    start = _parse_date(start_iso)
    end = _parse_date(end_iso)
    if start is None or end is None or end < start:
        return {}

    transcripts = _load_transcripts_in_range(start, end)
    if team_filter:
        wanted = set(team_filter)
        transcripts = [t for t in transcripts if t.team in wanted]

    if selected_video_ids:
        wanted_ids = set(selected_video_ids)
        transcripts = [t for t in transcripts if t.video_id in wanted_ids]

    if not transcripts:
        return {"empty": True, "reason": "no_transcripts"}

    # Durable disk cache keyed by the exact inputs. Survives Streamlit
    # redeploys (which wipe the in-memory @st.cache_data), so a range that
    # was generated before loads instantly and spends no tokens.
    cache_key = yt_cache.make_key(start_iso, end_iso, team_filter, selected_video_ids)
    cached = yt_cache.load(cache_key)
    if cached and cached.get("section"):
        section = cached["section"]
        section["_cache"] = {"hit": True, "generated_at": cached.get("generated_at")}
        return section

    label = start_iso if start_iso == end_iso else f"{start_iso} → {end_iso}"
    # User explicitly picked these videos — skip the title-score filter
    # so every selection is honored.
    section = build_yt_section(transcripts, date_label=label, pre_filtered=True)
    yt_cache.save(
        cache_key,
        section,
        meta={
            "start": start_iso,
            "end": end_iso,
            "teams": sorted(team_filter or []),
            "video_count": len(selected_video_ids or []),
        },
    )
    section["_cache"] = {"hit": False}
    return section


# `build_yt_section` calls OpenAI; cache so a reload doesn't re-spend tokens.
# 24h is generous given the user controls the inputs by pushing files.
@st.cache_data(ttl=86400, show_spinner=False)
def _generate_cached(
    start_iso: str,
    end_iso: str,
    team_filter: tuple,
    selected_video_ids: tuple,
) -> dict:
    return _summary_payload(start_iso, end_iso, team_filter, selected_video_ids)


# ──────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────

available_dirs = _list_dated_dirs()
if not available_dirs:
    st.warning(
        "No transcripts available yet. Run `python scripts/collect_youtube.py` "
        "locally and push the resulting `data/raw/<date>/youtube.json` files."
    )
    st.stop()

earliest = _parse_date(available_dirs[0]) or date.today() - timedelta(days=30)
latest = _parse_date(available_dirs[-1]) or date.today()

today = date.today()
default_end = min(latest, today)
default_start = max(earliest, default_end - timedelta(days=6))

col_l, col_r = st.columns(2)
with col_l:
    start_d = st.date_input(
        "Start date", value=default_start,
        min_value=earliest, max_value=latest,
    )
with col_r:
    end_d = st.date_input(
        "End date", value=default_end,
        min_value=earliest, max_value=latest,
    )

teams_by_abbr = get_teams_by_abbr()
# Include the league-wide "NFL" bucket (national channels) as a filter option.
all_team_abbrs = sorted(set(teams_by_abbr.keys()) | {"NFL"})
team_filter_list = st.multiselect(
    "Filter teams (leave empty for all)",
    all_team_abbrs,
    help="Restricts both press-conf summary scope and per-team output.",
)

st.caption(
    f"Transcripts available: {available_dirs[0]} → {available_dirs[-1]} "
    f"({len(available_dirs)} days)"
)

if start_d > end_d:
    st.error("Start date must be on or before end date.")
    st.stop()


# ──────────────────────────────────────────────────────────
# Per-video selection table
# ──────────────────────────────────────────────────────────
# Lets the user pick exactly which transcripts go into the summary.
# Default selection is "would have passed the press-relevance filter"
# (score > 0) so the default Generate output matches pre-selector
# behavior. The user can check noisy items they actually want included
# (e.g. the LV "Presser" the score dictionary missed) or uncheck low-
# value picks to keep the summary tight.
candidate_transcripts = _load_transcripts_in_range(start_d, end_d)
if team_filter_list:
    wanted = set(team_filter_list)
    candidate_transcripts = [
        t for t in candidate_transcripts if t.team in wanted
    ]

if not candidate_transcripts:
    st.info(
        "No transcripts in this date range / team filter. "
        "Push more transcripts or widen the range."
    )
    st.stop()

import pandas as pd

candidate_rows = []
for t in candidate_transcripts:
    score = _press_relevance_score(t.title)
    candidate_rows.append({
        "Select": score > 0,
        "Team": t.team,
        "Date": t.published.date().isoformat() if t.published else "",
        "Score": score,
        "Title": t.title,
        "video_id": t.video_id,
        "url": t.url,
    })
candidate_df = pd.DataFrame(candidate_rows)

# "Clear all" empties every checkbox. The data_editor seeds its checkbox
# state from the dataframe only when its `key` changes, so we bump a nonce
# (forcing a fresh widget) and zero out the Select column before render.
editor_nonce = st.session_state.get("yt_editor_nonce", 0)
if st.session_state.pop("yt_force_clear", False):
    candidate_df["Select"] = False

st.caption(
    f"{len(candidate_df)} transcripts available in range. "
    f"Default selection includes the {int(candidate_df['Select'].sum())} "
    f"that score > 0 on the press-conference relevance filter — "
    f"check additional rows to include them, or uncheck to drop them."
)

if st.button("Clear all selections"):
    st.session_state["yt_force_clear"] = True
    st.session_state["yt_editor_nonce"] = editor_nonce + 1
    st.rerun()

edited = st.data_editor(
    candidate_df,
    hide_index=True,
    use_container_width=True,
    column_config={
        "Select": st.column_config.CheckboxColumn(required=True),
        "Team": st.column_config.TextColumn(width="small"),
        "Date": st.column_config.TextColumn(width="small"),
        "Score": st.column_config.NumberColumn(
            width="small",
            help="Press-conference relevance score from title keywords. "
                 "Positive = scored as a presser by default; check the "
                 "row to force-include a zero or negative one.",
        ),
        "Title": st.column_config.TextColumn(width="large"),
        "video_id": None,
        "url": st.column_config.LinkColumn("Open", width="small"),
    },
    disabled=["Team", "Date", "Score", "Title", "url"],
    key=(
        f"yt_video_editor_{start_d}_{end_d}_"
        f"{','.join(team_filter_list)}_{editor_nonce}"
    ),
)

selected_video_ids = tuple(
    edited.loc[edited["Select"], "video_id"].astype(str).tolist()
)

if not selected_video_ids:
    st.warning("No videos selected — pick at least one to generate.")
    st.stop()

generate = st.button(
    f"Generate Report ({len(selected_video_ids)} videos)",
    type="primary",
)

# Cache the rendered report in session_state so unrelated reruns (e.g.
# toggling the flag-mode dropdown below) don't blank it out. Only the
# Generate button or a real input change re-runs the LLM call.
session_key = "yt_report_section"
session_inputs_key = "yt_report_inputs"
current_inputs = (
    start_d.isoformat(),
    end_d.isoformat(),
    tuple(team_filter_list),
    selected_video_ids,
)

if generate:
    with st.spinner("Summarizing transcripts..."):
        section = _generate_cached(*current_inputs)
    st.session_state[session_key] = section
    st.session_state[session_inputs_key] = current_inputs
else:
    section = st.session_state.get(session_key)
    cached_inputs = st.session_state.get(session_inputs_key)
    # Drop the cached render when the user changed the date range or
    # team filter — otherwise a stale report would render against
    # currently-displayed inputs.
    if section is not None and cached_inputs != current_inputs:
        section = None
        st.session_state.pop(session_key, None)
        st.session_state.pop(session_inputs_key, None)

if section is None:
    st.info(
        "Pick a range and click **Generate Report**. Each generation runs "
        "an LLM call across the transcripts in the selected window."
    )
    st.stop()

if not section:
    st.error("Could not load transcripts for that range.")
    st.stop()

if section.get("empty"):
    st.info("No transcripts in that range. Push more before regenerating.")
    st.stop()


# ──────────────────────────────────────────────────────────
# Render
# ──────────────────────────────────────────────────────────

count = section.get("transcript_count", 0)
label = section.get("date_label", "")
st.success(f"Summarized {count} transcripts ({label}).")

cache_info = section.get("_cache", {}) or {}
if cache_info.get("hit"):
    ts = cache_info.get("generated_at", "")
    st.caption(
        f"⚡ Loaded from saved cache (generated {ts} UTC) — no tokens spent on this view."
    )

# Per-bullet flagging — same UX as the Daily Report. report_date is the
# range label so all flags from one Generate click stay together; section
# IDs are `yt:` prefixed so the same content can be flagged here without
# colliding with daily-report flags.
flag_mode = st.selectbox(
    "Flag mode",
    options=FLAG_MODE_VALUES,
    format_func=lambda v: FLAG_MODE_LABELS[v],
    key="yt_flag_mode",
    help=(
        "Off: just read the report. "
        "Internal FP Handbook: long-running, persistent flags with "
        "category + author. "
        "Daily Site Report: range-scoped flags (team + note only)."
    ),
)
if flag_mode == MODE_HANDBOOK:
    page_flagger = st.text_input(
        "Your name (saved with each flag for attribution)",
        value=st.session_state.get("flagger_name_remembered", ""),
        key="yt_page_flagger_name",
        placeholder="optional — leave blank to flag anonymously",
        help="Other visitors will see this name next to flags you create.",
    )
    # Mirror into the non-widget canonical key so popovers can read it
    # as their default without hitting Streamlit's widget-key write ban.
    st.session_state["flagger_name_remembered"] = (page_flagger or "").strip()

flag_report_date = label or f"{start_d.isoformat()} → {end_d.isoformat()}"

pc = section.get("press_conferences", {}) or {}
pc_summary = (pc.get("summary") or "").strip()
if pc_summary:
    st.subheader(f"Press Conference Highlights ({pc.get('count', 0)} transcripts)")
    if flag_mode:
        render_flaggable(
            pc_summary, flag_report_date, "yt:press_conferences",
            "YT: Press Conference Highlights",
            key_prefix="yt_pc",
            all_teams=ALL_TEAMS,
            mode=flag_mode,
        )
    else:
        st.markdown(pc_summary)

    pc_sources = pc.get("sources", []) or []
    if pc_sources:
        links = [
            f"[{s.get('team', '')} — {s.get('title', 'video')}]({s.get('url', '')})"
            for s in pc_sources if s.get("url")
        ]
        if links:
            st.caption("Videos in this summary")
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
                note_summary, flag_report_date, f"yt:team:{team}",
                f"YT Team: {team}",
                key_prefix=f"yt_team_{team}",
                all_teams=ALL_TEAMS,
                default_team=team, numbered_sources=numbered,
                linkify=note_linkify,
                mode=flag_mode,
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
    cols[4].metric(
        "Est. Cost",
        f"${float(usage.get('estimated_cost_usd', 0.0)):.4f}",
    )
