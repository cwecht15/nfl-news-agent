"""Daily Report Page."""

import re as _re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Daily Report", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_teams_by_abbr
from dashboard.helpers import (
    highlight_summary,
    highlight_sources,
    highlight_numbered_sources,
    render_sources,
)
from reports.flagged_findings import (
    CATEGORIES,
    MODE_DAILY,
    MODE_HANDBOOK,
    add_or_update_flag,
    get_flag,
    get_flag_id,
    remove_flag,
)

# Selectbox label → internal mode value. Empty string is "off" (no flag
# UI rendered alongside report content).
_FLAG_MODE_LABELS: dict[str, str] = {
    "": "Off",
    MODE_HANDBOOK: "🚩 Internal FP Handbook",
    MODE_DAILY: "📋 Daily Site Report",
}
_FLAG_MODE_VALUES: list[str] = list(_FLAG_MODE_LABELS.keys())
from reports.report_builder import list_available_reports, load_report

ALL_TEAMS = sorted(get_teams_by_abbr().keys())
NONE_TEAM = "(none)"

st.header("Daily Report")


def _format_count(value) -> str:
    """Format integer-like metrics for display."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or 0)


def _matches_search(text: str, query: str) -> bool:
    """Check if text contains the search query (case-insensitive)."""
    return query.lower() in text.lower()


def _filter_paragraphs(summary: str, query: str) -> str:
    """For long summaries, return only paragraphs containing the query.

    Short summaries (under 5 paragraphs) are returned in full.
    """
    paragraphs = summary.split("\n\n")
    if len(paragraphs) <= 4:
        return summary

    query_lower = query.lower()
    matching = [p for p in paragraphs if query_lower in p.lower()]
    if not matching:
        return summary
    return "\n\n".join(matching)


def _parse_flaggable_items(summary: str) -> list[tuple[str, str]]:
    """Split a markdown summary into (kind, content) units.

    kind is "heading" (not flaggable), "table", "bullet", or "paragraph".
    """
    items: list[tuple[str, str]] = []
    for block in summary.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        lines = block.split("\n")
        if any(line.lstrip().startswith("|") for line in lines):
            items.append(("table", block))
            continue

        prose: list[str] = []

        def _flush():
            if prose:
                items.append(("paragraph", " ".join(prose).strip()))
                prose.clear()

        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("#"):
                _flush()
                items.append(("heading", stripped))
                i += 1
                continue
            if stripped.startswith(("- ", "* ")):
                _flush()
                bullet = stripped[2:].strip()
                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    if not nxt or nxt.startswith(("- ", "* ", "#")):
                        break
                    bullet += " " + nxt
                    i += 1
                items.append(("bullet", bullet))
                continue
            prose.append(stripped)
            i += 1
        _flush()
    return items


def _build_citation_linker(numbered_sources):
    """Return a function that linkifies `[N]` citations in markdown text.

    Returns None when no numbered_sources are available.
    """
    if not numbered_sources:
        return None
    url_map: dict[str, str] = {}
    for src in numbered_sources:
        num = str(src.get("num", ""))
        url = src.get("url", "")
        if num and url:
            url_map[num] = url

    def _linkify(text: str) -> str:
        def _sub(m):
            parts = []
            for n in _re.split(r"[,\s]+", m.group(1)):
                n = n.strip()
                if n in url_map:
                    parts.append(f"[[{n}]]({url_map[n]})")
                elif n:
                    parts.append(f"[{n}]")
            return " ".join(parts)

        return _re.sub(r"\[(\d+(?:[,\s]+\d+)*)\]", _sub, text)

    return _linkify


def _citations_for(content: str, numbered_sources) -> list[dict]:
    """Return the subset of numbered_sources referenced by [N] in content."""
    if not numbered_sources:
        return []
    nums = set()
    for m in _re.finditer(r"\[(\d+(?:[,\s]+\d+)*)\]", content):
        for n in _re.split(r"[,\s]+", m.group(1)):
            n = n.strip()
            if n:
                nums.add(n)
    if not nums:
        return []
    return [s for s in numbered_sources if str(s.get("num", "")) in nums]


def _flag_control(content: str, report_date: str, section_id: str,
                   section_label: str, key_prefix: str,
                   default_team: str = "", attached_sources=None,
                   mode: str = MODE_HANDBOOK) -> None:
    """Render a popover flag control for a single content item.

    `mode` selects the flag schema:
      - MODE_HANDBOOK: Category + Team + Note + flagger name
      - MODE_DAILY: Team + Note (no category, no attribution)
    Each mode keys a distinct flag ID for the same content, so an item
    can carry one flag in each mode without collision.
    """
    fid = get_flag_id(report_date, section_id, content, mode=mode)
    existing = get_flag(fid)
    icon = "🚩" if existing else "⚐"
    with st.popover(icon, use_container_width=False):
        cur_note = existing.get("note", "") if existing else ""
        cur_team = existing.get("team") if existing else default_team

        if mode == MODE_HANDBOOK:
            cur_cat = existing.get("category") if existing else CATEGORIES[0]
            cat_idx = CATEGORIES.index(cur_cat) if cur_cat in CATEGORIES else 0
            cat = st.selectbox(
                "Category", CATEGORIES, index=cat_idx,
                key=f"{key_prefix}_cat",
            )
        else:
            st.caption("📋 Daily Site Report")
            cat = ""

        team_options = [NONE_TEAM] + ALL_TEAMS
        team_idx = team_options.index(cur_team) if cur_team in team_options else 0
        team_choice = st.selectbox(
            "Team", team_options, index=team_idx, key=f"{key_prefix}_team",
        )
        team = "" if team_choice == NONE_TEAM else team_choice

        note = st.text_area(
            "Note (optional)" if mode == MODE_HANDBOOK else "Note",
            value=cur_note, height=80, key=f"{key_prefix}_note",
        )

        flagger = ""
        if mode == MODE_HANDBOOK:
            # Per-popover name input. Defaults to whatever the visitor
            # set earlier this session (via this popover or the
            # page-level field at the top of the report). Editing here
            # also updates the session-wide value so subsequent
            # popovers pre-fill the new name.
            default_name = st.session_state.get("flagger_name", "")
            flagger_input = st.text_input(
                "Your name (saved with this flag)",
                value=default_name,
                key=f"{key_prefix}_flagger",
                placeholder="optional",
            )
            flagger = (flagger_input or "").strip()

        if st.button("Save flag", key=f"{key_prefix}_save", type="primary"):
            if mode == MODE_HANDBOOK and flagger:
                st.session_state["flagger_name"] = flagger
            add_or_update_flag(
                report_date, section_id, section_label, content,
                category=cat, note=note,
                team=team, sources=attached_sources or [],
                flagged_by=flagger, mode=mode,
            )
            st.rerun()
        if existing and st.button("Unflag", key=f"{key_prefix}_unflag"):
            remove_flag(fid)
            st.rerun()


def _render_flaggable(summary: str, report_date: str, section_id: str,
                      section_label: str, key_prefix: str,
                      linkify=None, default_team: str = "",
                      section_sources=None, numbered_sources=None,
                      mode: str = MODE_HANDBOOK) -> None:
    """Render a markdown summary as item-by-item flaggable units."""
    items = _parse_flaggable_items(summary)
    for idx, (kind, content) in enumerate(items):
        display = linkify(content) if linkify else content
        if kind == "heading":
            st.markdown(display)
            continue
        if numbered_sources:
            attached = _citations_for(content, numbered_sources)
        else:
            attached = list(section_sources or [])
        col_l, col_r = st.columns([24, 1])
        with col_l:
            if kind == "bullet":
                st.markdown(f"- {display}")
            else:
                st.markdown(display)
        with col_r:
            _flag_control(
                content, report_date, section_id, section_label,
                key_prefix=f"{key_prefix}_{idx}",
                default_team=default_team, attached_sources=attached,
                mode=mode,
            )


# Date selector
available = list_available_reports()

if not available:
    st.warning(
        "No reports available yet. Run `python scripts/run_daily.py` to generate one."
    )
    st.stop()

selected_date = st.selectbox("Select date", available, index=0)

try:
    report = load_report(selected_date)
except Exception as e:
    st.error(f"Failed to load report: {e}")
    st.stop()

# Search box + flag-mode selector (+ name field, only when handbook mode)
search_col, flag_col = st.columns([3, 2])
with search_col:
    search_query = st.text_input(
        "Search report", placeholder="Player, team, or keyword...",
    )
with flag_col:
    flag_mode = st.selectbox(
        "Flag mode",
        options=_FLAG_MODE_VALUES,
        format_func=lambda v: _FLAG_MODE_LABELS[v],
        key="flag_mode",
        help=(
            "Off: just read the report. "
            "Internal FP Handbook: long-running, persistent flags with "
            "category + author. "
            "Daily Site Report: one-day-scoped flags (team + note only) "
            "that you'll export to PDF."
        ),
    )

if flag_mode == MODE_HANDBOOK:
    st.text_input(
        "Your name (saved with each flag for attribution)",
        key="flagger_name",
        placeholder="optional — leave blank to flag anonymously",
        help="Other visitors will see this name next to flags you create. "
             "Stored only with the flag entry, not used for auth.",
    )

if report.alerts:
    for alert in report.alerts:
        source_name = alert.get("source", "Source")
        message = alert.get("message", "")
        latest_expiry = alert.get("latest_expiry")
        suffix = (
            f" Latest cookie expiry: {latest_expiry}."
            if latest_expiry else ""
        )
        severity = str(alert.get("severity", "warning")).lower()
        full_message = f"{source_name}: {message}{suffix}"
        if severity == "error":
            st.error(full_message)
        else:
            st.warning(full_message)

# Section titles
TITLES = {
    "transactions": "Transactions & Signings",
    "injuries": "Injury Reports",
    "depth_chart_movement": "Depth Chart Movement",
    "projection_movers": "Today's Projection Movers",
    "league_wide": "League-Wide Notes",
    # Legacy key kept so older reports on disk still render with the
    # right title rather than as "Analysis".
    "analysis": "Analysis & What to Watch",
}

# Legacy keys whose content has moved elsewhere — skip in render.
SKIP_SECTIONS = {"press_conferences"}

# Render sections
for key, section_data in report.sections.items():
    if key in SKIP_SECTIONS:
        continue
    title = TITLES.get(key, key.replace("_", " ").title())
    summary = section_data.get("summary", "No data.")
    sources = section_data.get("sources", [])

    # If searching, skip sections that don't match
    if search_query:
        source_text = " ".join(
            s.get("title", "") + " " + s.get("source", "")
            for s in sources
        )
        if not _matches_search(summary + " " + source_text, search_query):
            continue

    count = section_data.get("count", "")
    badge = f" ({count} items)" if count else ""

    with st.expander(f"{title}{badge}", expanded=True):
        display_summary = _filter_paragraphs(summary, search_query) if search_query else summary

        numbered_sources = section_data.get("numbered_sources")
        linkify = _build_citation_linker(numbered_sources)

        if flag_mode:
            _render_flaggable(
                display_summary, selected_date, key, title,
                key_prefix=f"sec_{key}", linkify=linkify,
                section_sources=sources, numbered_sources=numbered_sources,
                mode=flag_mode,
            )
        elif linkify:
            st.markdown(linkify(display_summary), unsafe_allow_html=True)
        else:
            st.markdown(display_summary)

        if numbered_sources:
            st.caption("Sources")
            lines = []
            for src in numbered_sources:
                num = src.get("num", "")
                title_text = src.get("title", "Source")
                source_name = src.get("source", "")
                url = src.get("url", "")
                suffix = f" ({source_name})" if source_name else ""
                if url:
                    lines.append(f"**[{num}]** [{title_text}]({url}){suffix}")
                else:
                    lines.append(f"**[{num}]** {title_text}{suffix}")
            st.markdown("\n\n".join(lines))
        else:
            render_sources(sources)

# Team notes
if report.team_highlights:
    st.subheader("Team Notes")
    # Let user filter teams
    teams = sorted(report.team_highlights.keys())
    selected_teams = st.multiselect(
        "Filter teams (leave empty for all)", teams
    )
    show_teams = selected_teams if selected_teams else teams

    matched = 0
    for team in show_teams:
        highlight = report.team_highlights[team]
        summary_text = highlight_summary(highlight)

        # If searching, skip teams that don't match
        if search_query:
            source_text = " ".join(
                s.get("title", "") + " " + s.get("source", "")
                for s in highlight_sources(highlight)
            )
            if not _matches_search(
                team + " " + summary_text + " " + source_text,
                search_query,
            ):
                continue

        matched += 1
        st.markdown(f"**{team}**")
        team_srcs = highlight_sources(highlight)
        team_numbered = highlight_numbered_sources(highlight)
        team_linkify = _build_citation_linker(team_numbered)
        if flag_mode:
            _render_flaggable(
                summary_text, selected_date, f"team:{team}", f"Team: {team}",
                key_prefix=f"team_{team}",
                default_team=team, section_sources=team_srcs,
                linkify=team_linkify, numbered_sources=team_numbered,
                mode=flag_mode,
            )
        elif team_linkify:
            st.markdown(team_linkify(summary_text), unsafe_allow_html=True)
        else:
            st.markdown(summary_text)

        if team_numbered:
            st.caption("Sources")
            lines = []
            for src in team_numbered:
                num = src.get("num", "")
                title_text = src.get("title", "Source")
                source_name = src.get("source", "")
                url = src.get("url", "")
                suffix = f" ({source_name})" if source_name else ""
                if url:
                    lines.append(f"**[{num}]** [{title_text}]({url}){suffix}")
                else:
                    lines.append(f"**[{num}]** {title_text}{suffix}")
            st.markdown("\n\n".join(lines))
        else:
            render_sources(team_srcs)
        st.divider()

    if search_query and matched == 0:
        st.info(f"No team highlights match '{search_query}'.")

# YouTube Highlights — only present when the report was generated locally
# with --include-yt-section. Cloud-generated reports won't have this block.
yt_section = getattr(report, "yt_section", None) or {}
if yt_section.get("transcript_count"):
    st.subheader(
        f"YouTube Highlights ({yt_section.get('transcript_count', 0)} transcripts)"
    )
    if yt_section.get("date_label"):
        st.caption(f"Range: {yt_section['date_label']}")

    pc = yt_section.get("press_conferences", {}) or {}
    pc_summary = pc.get("summary", "")
    if pc_summary and pc_summary.strip():
        with st.expander(
            f"Press Conference Highlights ({pc.get('count', 0)} transcripts)",
            expanded=True,
        ):
            st.markdown(pc_summary)

    yt_team_notes = yt_section.get("team_notes", {}) or {}
    if yt_team_notes:
        st.markdown("**Per-Team Notes**")
        yt_teams = sorted(yt_team_notes.keys())
        yt_selected = st.multiselect(
            "Filter teams (leave empty for all)", yt_teams,
            key="yt_team_filter",
        )
        yt_show = yt_selected if yt_selected else yt_teams

        for team in yt_show:
            note = yt_team_notes[team]
            note_summary = note.get("summary", "")
            if not note_summary.strip():
                continue

            # YT bullet citations link to YouTube URLs via numbered_sources
            note_numbered = note.get("numbered_sources", [])
            yt_linkify = _build_citation_linker(note_numbered)

            st.markdown(f"**{team}**")
            if yt_linkify:
                st.markdown(yt_linkify(note_summary), unsafe_allow_html=True)
            else:
                st.markdown(note_summary)

            if note_numbered:
                st.caption("Sources")
                lines = []
                for src in note_numbered:
                    num = src.get("num", "")
                    title_text = src.get("title", "Source")
                    source_name = src.get("source", "")
                    url = src.get("url", "")
                    suffix = f" ({source_name})" if source_name else ""
                    if url:
                        lines.append(f"**[{num}]** [{title_text}]({url}){suffix}")
                    else:
                        lines.append(f"**[{num}]** {title_text}{suffix}")
                st.markdown("\n\n".join(lines))
            st.divider()

# Collection stats
if report.collection_stats:
    st.subheader("Collection Stats")
    cols = st.columns(len(report.collection_stats))
    for i, (source, count) in enumerate(report.collection_stats.items()):
        cols[i].metric(source.upper(), count)

if report.llm_usage:
    usage = report.llm_usage
    st.subheader("LLM Usage")
    st.markdown(
        f"**Provider:** {usage.get('provider', 'unknown')}  \n"
        f"**Model:** {usage.get('model', 'unknown')}  \n"
        f"**Service tier:** {usage.get('service_tier', 'default')}"
    )

    usage_cols = st.columns(5)
    usage_cols[0].metric("Calls", _format_count(usage.get("request_count", 0)))
    usage_cols[1].metric("Input", _format_count(usage.get("input_tokens", 0)))
    usage_cols[2].metric("Output", _format_count(usage.get("output_tokens", 0)))
    usage_cols[3].metric(
        "Reasoning",
        _format_count(usage.get("reasoning_tokens", 0)),
    )
    usage_cols[4].metric(
        "Est. Cost",
        f"${float(usage.get('estimated_cost_usd', 0.0)):.4f}",
    )

    if usage.get("tracking_note"):
        st.caption(usage["tracking_note"])

# Transaction reconciliation
try:
    from scripts.transaction_reconciler import reconcile, add_override

    txn_alerts = reconcile()
    if txn_alerts:
        st.subheader(f"Projection Alerts ({len(txn_alerts)})")
        st.caption("Transactions not yet reflected in your projections. Dismiss if intentionally not projecting.")
        for i, alert in enumerate(txn_alerts):
            col_msg, col_btn = st.columns([5, 1])
            with col_msg:
                icon = "MISSING" if alert["type"] == "missing" else "WRONG TEAM"
                pos = alert.get("pos", "")
                depth = alert.get("depth", 0)
                depth_label = f" | Depth #{depth}" if depth else ""
                st.warning(f"**[{icon}]** {alert['message']}  \n*{alert['transaction']}* ({alert['date']})")
            with col_btn:
                if st.button("Dismiss", key=f"dismiss_txn_{i}"):
                    add_override(alert["player"], alert.get("transaction", ""), reason="Dismissed from daily report")
                    st.rerun()
except Exception:
    pass  # Non-fatal — don't break the report page

st.caption(f"Generated: {report.generated_at}")
