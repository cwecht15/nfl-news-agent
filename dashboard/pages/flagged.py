"""Flagged Findings — long-term collection of items flagged from daily reports."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Flagged Findings", page_icon="🚩", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_teams_by_abbr
from reports.flagged_findings import (
    CATEGORIES,
    MODE_DAILY,
    MODE_HANDBOOK,
    load_flags,
    remove_flag,
    update_flag_fields,
)
from reports.report_builder import load_report

ALL_TEAMS = sorted(get_teams_by_abbr().keys())
NONE_TEAM = "(none)"
AUTHOR_UNKNOWN = "(unknown)"

_CITE_RE = re.compile(r"\[(\d+(?:[,\s]+\d+)*)\]")


@st.cache_data(show_spinner=False)
def _section_numbered_sources(report_date: str, section: str) -> list[dict]:
    """Load numbered_sources for a section from the original report JSON."""
    try:
        report = load_report(report_date)
    except Exception:
        return []
    sections = getattr(report, "sections", {}) or {}
    sd = sections.get(section, {}) or {}
    return sd.get("numbered_sources") or []


def _resolve_sources(flag: dict) -> list[dict]:
    """Return stored sources, or fall back to the report's numbered_sources."""
    stored = flag.get("sources") or []
    if stored:
        return stored
    section = flag.get("section", "")
    if not section or section.startswith("team:"):
        return []
    fallback = _section_numbered_sources(flag.get("report_date", ""), section)
    if not fallback:
        return []
    nums = set()
    for m in _CITE_RE.finditer(flag.get("content", "")):
        for n in re.split(r"[,\s]+", m.group(1)):
            n = n.strip()
            if n:
                nums.add(n)
    if not nums:
        return []
    return [s for s in fallback if str(s.get("num", "")) in nums]


def _linkify_citations(content: str, sources: list[dict]) -> str:
    """Replace [N] markers in content with markdown links using stored sources."""
    if not sources:
        return content
    url_map: dict[str, str] = {}
    for s in sources:
        num = str(s.get("num", "") or "")
        url = s.get("url", "") or ""
        if num and url:
            url_map[num] = url
    if not url_map:
        return content

    def _sub(m):
        parts = []
        for n in re.split(r"[,\s]+", m.group(1)):
            n = n.strip()
            if n in url_map:
                parts.append(f"[[{n}]]({url_map[n]})")
            elif n:
                parts.append(f"[{n}]")
        return " ".join(parts)

    return _CITE_RE.sub(_sub, content)

st.header("Flagged Findings")

# Two modes share the same store: long-running Internal FP Handbook
# flags vs per-day Daily Site Report flags. Tabs let visitors switch
# without losing filter state cross-mode (each mode's filter widgets
# carry their own keys).
all_flags = load_flags()


def _flag_mode(f: dict) -> str:
    return f.get("mode") or MODE_HANDBOOK


handbook_flags = [f for f in all_flags if _flag_mode(f) == MODE_HANDBOOK]
daily_flags = [f for f in all_flags if _flag_mode(f) == MODE_DAILY]

mode_label = st.radio(
    "Flag set",
    options=[
        f"🚩 Internal FP Handbook ({len(handbook_flags)})",
        f"📋 Daily Site Report ({len(daily_flags)})",
    ],
    horizontal=True,
    label_visibility="collapsed",
)
selected_mode = MODE_HANDBOOK if mode_label.startswith("🚩") else MODE_DAILY
flags = handbook_flags if selected_mode == MODE_HANDBOOK else daily_flags

if not flags:
    if selected_mode == MODE_HANDBOOK:
        st.info(
            "No Internal FP Handbook flags yet. Open a Daily Report, "
            "set Flag mode to **🚩 Internal FP Handbook**, and click the "
            "flag icon next to any bullet to start collecting persistent, "
            "categorized findings across all reports."
        )
    else:
        st.info(
            "No Daily Site Report flags yet. Open today's Daily Report, "
            "set Flag mode to **📋 Daily Site Report**, and flag items "
            "you want to bundle into a one-day PDF export."
        )
    st.stop()

# Filters — same widgets for both modes, but Categories only renders
# for handbook (DSR flags don't carry a category).
dates_seen = sorted(
    {f.get("report_date", "") for f in flags if f.get("report_date")},
    reverse=True,
)
teams_seen_in_flags = sorted({f.get("team", "") for f in flags if f.get("team")})
team_options = [NONE_TEAM] + sorted(set(ALL_TEAMS) | set(teams_seen_in_flags))

st.caption("Filters — leave empty to include all.")
if selected_mode == MODE_HANDBOOK:
    all_categories = sorted({f.get("category", "") for f in flags if f.get("category")})
    known_categories = list(dict.fromkeys(CATEGORIES + all_categories))

    # Build the Flagged-by options: unique non-empty names + the
    # "(unknown)" sentinel for legacy/anonymous entries.
    authors_seen = sorted(
        {(f.get("flagged_by") or "").strip() for f in flags
         if (f.get("flagged_by") or "").strip()}
    )
    author_options = authors_seen + [AUTHOR_UNKNOWN]

    filter_cols = st.columns([2, 2, 2, 2, 2, 2])
    with filter_cols[0]:
        cat_filter = st.multiselect(
            "Categories", known_categories, default=[], key="hb_cat_filter",
        )
    with filter_cols[1]:
        date_filter = st.multiselect(
            "Report dates", dates_seen, default=[], key="hb_date_filter",
        )
    with filter_cols[2]:
        team_filter = st.multiselect(
            "Teams", team_options, default=[], key="hb_team_filter",
        )
    with filter_cols[3]:
        author_filter = st.multiselect(
            "Flagged by", author_options, default=[], key="hb_author_filter",
            help="Pick one or more authors. (unknown) covers anonymous "
                 "entries and legacy flags from before attribution was added.",
        )
    with filter_cols[4]:
        search_q = st.text_input(
            "Search", placeholder="content, note, or section...",
            key="hb_search",
        )
    with filter_cols[5]:
        sort_order = st.selectbox(
            "Sort",
            ["Newest first", "Oldest first", "Date (newest)", "Date (oldest)"],
            index=0, key="hb_sort",
        )
else:
    # Daily Site Report scope: pick a single report date by default
    # (most recent). The user can broaden via the multiselect.
    cat_filter = []
    known_categories = []
    author_filter = []
    filter_cols = st.columns([3, 2, 3, 2])
    default_date = [dates_seen[0]] if dates_seen else []
    with filter_cols[0]:
        date_filter = st.multiselect(
            "Report date(s)", dates_seen, default=default_date,
            help="Daily Site Report flags are per-day. Defaults to the "
                 "most recent date with flags. Pick more than one if you "
                 "want a multi-day export.",
            key="dsr_date_filter",
        )
    with filter_cols[1]:
        team_filter = st.multiselect(
            "Teams", team_options, default=[], key="dsr_team_filter",
        )
    with filter_cols[2]:
        search_q = st.text_input(
            "Search", placeholder="content, note...", key="dsr_search",
        )
    with filter_cols[3]:
        sort_order = st.selectbox(
            "Sort",
            ["Newest first", "Oldest first", "Date (newest)", "Date (oldest)"],
            index=0, key="dsr_sort",
        )

filtered = []
q_lower = search_q.lower()
for f in flags:
    if selected_mode == MODE_HANDBOOK and cat_filter and f.get("category") not in cat_filter:
        continue
    if date_filter and f.get("report_date") not in date_filter:
        continue
    flag_team = f.get("team") or NONE_TEAM
    if team_filter and flag_team not in team_filter:
        continue
    if author_filter:
        flag_author = (f.get("flagged_by") or "").strip() or AUTHOR_UNKNOWN
        if flag_author not in author_filter:
            continue
    if q_lower:
        haystack = " ".join([
            f.get("content", ""), f.get("note", ""),
            f.get("section_label", ""), f.get("section", ""),
            f.get("team", ""),
        ]).lower()
        if q_lower not in haystack:
            continue
    filtered.append(f)

if sort_order == "Newest first":
    filtered.sort(key=lambda x: x.get("flagged_at", ""), reverse=True)
elif sort_order == "Oldest first":
    filtered.sort(key=lambda x: x.get("flagged_at", ""))
elif sort_order == "Date (newest)":
    filtered.sort(key=lambda x: x.get("report_date", ""), reverse=True)
else:
    filtered.sort(key=lambda x: x.get("report_date", ""))

st.caption(f"{len(filtered)} of {len(flags)} flagged items in this mode")

# PDF export — different builder + filename per mode.
export_col, _ = st.columns([1, 5])
with export_col:
    if st.button("Build PDF", type="primary"):
        try:
            if selected_mode == MODE_HANDBOOK:
                from reports.pdf_exporter import build_flagged_pdf
                pdf_bytes = build_flagged_pdf(filtered)
            else:
                from reports.pdf_exporter import build_daily_site_pdf
                pdf_bytes = build_daily_site_pdf(filtered)
            st.session_state["_flagged_pdf_bytes"] = pdf_bytes
            st.session_state["_flagged_pdf_mode"] = selected_mode
        except Exception as e:
            st.error(f"PDF build failed: {e}")

if "_flagged_pdf_bytes" in st.session_state:
    from datetime import datetime as _dt
    pdf_mode = st.session_state.get("_flagged_pdf_mode", MODE_HANDBOOK)
    if pdf_mode == MODE_DAILY:
        # Stamp the file with the date(s) it covers when possible.
        if date_filter:
            stamp = "_".join(sorted(date_filter))
        else:
            stamp = _dt.now().strftime("%Y%m%d")
        fname = f"daily_site_report_{stamp}.pdf"
    else:
        fname = f"flagged_findings_{_dt.now().strftime('%Y%m%d')}.pdf"
    st.download_button(
        "Download PDF",
        data=st.session_state["_flagged_pdf_bytes"],
        file_name=fname,
        mime="application/pdf",
    )

st.divider()

# Render entries
for f in filtered:
    fid = f.get("id", "")
    report_date = f.get("report_date", "")
    section_label = f.get("section_label", f.get("section", ""))
    category = f.get("category", "")
    team = f.get("team", "")
    content = f.get("content", "")
    note = f.get("note", "")
    sources = _resolve_sources(f)
    flagged_at = f.get("flagged_at", "")

    entry_mode_for_header = _flag_mode(f)
    team_chunk = f" · **{team}**" if team else ""
    flagger = (f.get("flagged_by") or "").strip()
    if entry_mode_for_header == MODE_HANDBOOK:
        flagger_chunk = f" · :grey[by **{flagger}**]" if flagger else ""
        head_left = (
            f"**[{category}]**{team_chunk} · {report_date} · *{section_label}*  \n"
            f":grey[flagged {flagged_at}]{flagger_chunk}"
        )
    else:
        head_left = (
            f"**📋 Daily Site**{team_chunk} · {report_date} · *{section_label}*  \n"
            f":grey[flagged {flagged_at}]"
        )

    with st.container():
        head_l, head_r = st.columns([6, 1])
        with head_l:
            st.markdown(head_left)
        with head_r:
            with st.popover("Edit"):
                entry_mode = _flag_mode(f)
                if entry_mode == MODE_HANDBOOK:
                    cat_idx = (
                        known_categories.index(category)
                        if category in known_categories else 0
                    )
                    new_cat = st.selectbox(
                        "Category", known_categories, index=cat_idx,
                        key=f"edit_cat_{fid}",
                    )
                else:
                    st.caption("📋 Daily Site Report")
                    new_cat = ""

                team_choice_options = [NONE_TEAM] + ALL_TEAMS
                cur_team_label = team if team else NONE_TEAM
                team_idx = (
                    team_choice_options.index(cur_team_label)
                    if cur_team_label in team_choice_options else 0
                )
                new_team_choice = st.selectbox(
                    "Team", team_choice_options, index=team_idx, key=f"edit_team_{fid}",
                )
                new_team = "" if new_team_choice == NONE_TEAM else new_team_choice
                new_note = st.text_area(
                    "Note", value=note, height=100, key=f"edit_note_{fid}",
                )

                update_kwargs: dict = {
                    "team": new_team,
                    "note": new_note,
                }
                if entry_mode == MODE_HANDBOOK:
                    update_kwargs["category"] = new_cat
                    # Allow editing the attribution — useful if someone
                    # flagged anonymously and now wants to claim it, or
                    # to fix a typo. Empty string clears attribution.
                    new_flagger = st.text_input(
                        "Flagged by",
                        value=flagger,
                        key=f"edit_flagger_{fid}",
                        placeholder="leave blank to clear attribution",
                    )
                    update_kwargs["flagged_by"] = (new_flagger or "").strip()

                if st.button("Save", key=f"edit_save_{fid}", type="primary"):
                    update_flag_fields(fid, **update_kwargs)
                    st.rerun()
                if st.button("Delete", key=f"edit_del_{fid}"):
                    remove_flag(fid)
                    st.rerun()

        st.markdown(_linkify_citations(content, sources))
        if note:
            st.markdown(f"> **Note:** {note}")
        if sources:
            src_lines = []
            for s in sources:
                title_text = s.get("title", "Source") or "Source"
                source_name = s.get("source", "") or ""
                url = s.get("url", "") or ""
                num = s.get("num", "")
                prefix = f"**[{num}]** " if num else "- "
                suffix = f" ({source_name})" if source_name else ""
                if url:
                    src_lines.append(f"{prefix}[{title_text}]({url}){suffix}")
                else:
                    src_lines.append(f"{prefix}{title_text}{suffix}")
            st.caption("Sources")
            st.markdown("\n\n".join(src_lines))
        st.divider()
