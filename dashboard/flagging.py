"""Shared flag-mode UI used by the Daily Report and YouTube Report pages.

The flag store (`reports.flagged_findings`) keys entries by
`(mode, report_date, section_id, content)`. Each page passes its own
`report_date` (the daily report uses YYYY-MM-DD, the YT report uses the
range label) and `section_id` (page-prefixed so the same content in two
pages stays distinct).
"""

import re as _re
from typing import Callable, Optional, Sequence

import streamlit as st

from reports.flagged_findings import (
    CATEGORIES,
    MODE_DAILY,
    MODE_HANDBOOK,
    add_or_update_flag,
    get_flag,
    get_flag_id,
    remove_flag,
)

NONE_TEAM = "(none)"

# Selectbox label → internal mode value. Empty string = Off.
FLAG_MODE_LABELS: dict[str, str] = {
    "": "Off",
    MODE_HANDBOOK: "🚩 Internal FP Handbook",
    MODE_DAILY: "📋 Daily Site Report",
}
FLAG_MODE_VALUES: list[str] = list(FLAG_MODE_LABELS.keys())


def parse_flaggable_items(summary: str) -> list[tuple[str, str]]:
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


def citations_for(content: str, numbered_sources) -> list[dict]:
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


def flag_control(
    content: str,
    report_date: str,
    section_id: str,
    section_label: str,
    key_prefix: str,
    all_teams: Sequence[str],
    default_team: str = "",
    attached_sources=None,
    mode: str = MODE_HANDBOOK,
) -> None:
    """Render a popover flag control for a single content item."""
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

        team_options = [NONE_TEAM] + list(all_teams)
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


def render_flaggable(
    summary: str,
    report_date: str,
    section_id: str,
    section_label: str,
    key_prefix: str,
    all_teams: Sequence[str],
    linkify: Optional[Callable[[str], str]] = None,
    default_team: str = "",
    section_sources=None,
    numbered_sources=None,
    mode: str = MODE_HANDBOOK,
) -> None:
    """Render a markdown summary as item-by-item flaggable units."""
    items = parse_flaggable_items(summary)
    for idx, (kind, content) in enumerate(items):
        display = linkify(content) if linkify else content
        if kind == "heading":
            st.markdown(display)
            continue
        if numbered_sources:
            attached = citations_for(content, numbered_sources)
        else:
            attached = list(section_sources or [])
        col_l, col_r = st.columns([24, 1])
        with col_l:
            if kind == "bullet":
                st.markdown(f"- {display}")
            else:
                st.markdown(display)
        with col_r:
            flag_control(
                content, report_date, section_id, section_label,
                key_prefix=f"{key_prefix}_{idx}",
                all_teams=all_teams,
                default_team=default_team, attached_sources=attached,
                mode=mode,
            )
