"""Depth Charts Page — track depth chart changes over time."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Depth Charts", page_icon="🏈", layout="wide")

from collectors.depth_chart_collector import (
    annotate_depth_changes,
    diff_depth_charts,
    get_depth_chart_dates,
    load_depth_chart_by_date,
    load_latest_depth_charts,
)
from reports.report_builder import load_report


def _news_items_for_date(date_str: str) -> list[dict]:
    """Pull a flat list of news-title dicts from the daily report on `date_str`.

    Used to annotate depth chart changes with related news context.
    Silently returns an empty list if the report is missing.
    """
    try:
        report = load_report(date_str)
    except Exception:
        return []

    items: list[dict] = []
    for section in (report.sections or {}).values():
        if isinstance(section, dict):
            items.extend(section.get("sources") or [])
            items.extend(section.get("numbered_sources") or [])
    for highlight in (report.team_highlights or {}).values():
        if isinstance(highlight, dict):
            items.extend(highlight.get("sources") or [])
    return items

st.header("Depth Charts")

dates = get_depth_chart_dates()

if not dates:
    st.warning("No depth chart snapshots yet. Run the pipeline or `python collectors/depth_chart_collector.py` to create one.")
    st.stop()

tab_changes, tab_browse = st.tabs(["Changes", "Browse"])

# ─── Tab 1: Changes ───

with tab_changes:
    if len(dates) < 2:
        st.info("Need at least 2 depth chart snapshots to show changes. The next pipeline run will capture a new one.")
        st.stop()

    col_from, col_to = st.columns(2)
    with col_from:
        compare_from = st.selectbox("From", sorted(dates), index=len(dates) - 1, key="dc_from")
    with col_to:
        compare_to = st.selectbox("To", dates, index=0, key="dc_to")

    if compare_from >= compare_to:
        st.warning("Select a 'From' date earlier than the 'To' date.")
        st.stop()

    old_dc = load_depth_chart_by_date(compare_from)
    new_dc = load_depth_chart_by_date(compare_to)

    if not old_dc or not new_dc:
        st.error("Could not load snapshots for the selected dates.")
        st.stop()

    changes = diff_depth_charts(new_dc, old_dc)
    annotate_depth_changes(changes, _news_items_for_date(compare_to))

    if not changes:
        st.success("No depth chart changes between these dates.")
        st.stop()

    # Filters
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        pos_filter = st.multiselect(
            "Filter by position group",
            sorted(set(c.get("generic_pos", "") for c in changes if c.get("generic_pos"))),
            key="dc_pos_filter",
        )
    with filter_col2:
        team_filter = st.multiselect(
            "Filter by team",
            sorted(set(
                c.get("team", c.get("new_team", ""))
                for c in changes
                if c.get("team") or c.get("new_team")
            )),
            key="dc_team_filter",
        )

    type_filter = st.multiselect(
        "Change type",
        ["promoted", "demoted", "added", "removed", "team_change", "position_change"],
        default=["promoted", "demoted", "team_change"],
        key="dc_type_filter",
    )

    filtered = changes
    if pos_filter:
        filtered = [c for c in filtered if c.get("generic_pos", "") in pos_filter]
    if team_filter:
        filtered = [c for c in filtered
                     if c.get("team", "") in team_filter
                     or c.get("new_team", "") in team_filter
                     or c.get("old_team", "") in team_filter]
    if type_filter:
        filtered = [c for c in filtered if c["type"] in type_filter]

    st.markdown(f"**{len(filtered)}** changes (of {len(changes)} total)")

    # Group by type
    promos = [c for c in filtered if c["type"] == "promoted"]
    demos = [c for c in filtered if c["type"] == "demoted"]
    team_moves = [c for c in filtered if c["type"] == "team_change"]
    adds = [c for c in filtered if c["type"] == "added"]
    removes = [c for c in filtered if c["type"] == "removed"]
    pos_changes = [c for c in filtered if c["type"] == "position_change"]

    if promos:
        st.subheader(f"Promotions ({len(promos)})")
        promo_data = []
        for c in promos:
            promo_data.append({
                "Player": c["name"],
                "Team": c["team"],
                "Position": c["pos"],
                "Old Depth": f"#{c['old_depth']}",
                "New Depth": f"#{c['new_depth']}",
                "Context": c.get("context", "") or "—",
            })
        st.dataframe(promo_data, use_container_width=True, hide_index=True)

    if demos:
        st.subheader(f"Demotions ({len(demos)})")
        demo_data = []
        for c in demos:
            demo_data.append({
                "Player": c["name"],
                "Team": c["team"],
                "Position": c["pos"],
                "Old Depth": f"#{c['old_depth']}",
                "New Depth": f"#{c['new_depth']}",
                "Context": c.get("context", "") or "—",
            })
        st.dataframe(demo_data, use_container_width=True, hide_index=True)

    if team_moves:
        st.subheader(f"Team Changes ({len(team_moves)})")
        move_data = []
        for c in team_moves:
            move_data.append({
                "Player": c["name"],
                "Position": c["pos"],
                "From": f"{c['old_team']} #{c['old_depth']}",
                "To": f"{c['new_team']} #{c['new_depth']}",
            })
        st.dataframe(move_data, use_container_width=True, hide_index=True)

    if adds:
        st.subheader(f"Added ({len(adds)})")
        add_data = [{"Player": c["name"], "Team": c["team"], "Position": c["pos"], "Depth": f"#{c['depth']}"} for c in adds]
        st.dataframe(add_data, use_container_width=True, hide_index=True)

    if removes:
        st.subheader(f"Removed ({len(removes)})")
        rem_data = [{"Player": c["name"], "Team": c["team"], "Position": c["pos"], "Depth": f"#{c['depth']}"} for c in removes]
        st.dataframe(rem_data, use_container_width=True, hide_index=True)

    if pos_changes:
        st.subheader(f"Position Changes ({len(pos_changes)})")
        pos_data = [{"Player": c["name"], "Team": c["team"], "Old Pos": c["old_pos"], "New Pos": c["new_pos"]} for c in pos_changes]
        st.dataframe(pos_data, use_container_width=True, hide_index=True)


# ─── Tab 2: Browse ───

with tab_browse:
    browse_date = st.selectbox("Snapshot date", dates, index=0, key="dc_browse_date")
    dc = load_depth_chart_by_date(browse_date)

    if not dc:
        st.warning("No data for this date.")
        st.stop()

    # Build team list
    teams = sorted(set(p["team"] for p in dc.values()))
    selected_team = st.selectbox("Team", teams, key="dc_browse_team")

    # Filter to selected team
    team_players = [p for p in dc.values() if p["team"] == selected_team]

    # Group by position
    pos_groups = {}
    for p in team_players:
        pos_groups.setdefault(p["pos"], []).append(p)

    # Sort within each position by depth
    for pos in pos_groups:
        pos_groups[pos].sort(key=lambda x: x["depth"])

    # Define position order for display
    POS_ORDER = [
        "QB", "RB", "FB",
        "LWR", "RWR", "SWR", "WR",
        "TE",
        "LT", "LG", "C", "RG", "RT",
        "LDE", "RDE", "SDE", "WDE", "DE", "NT", "DT",
        "LOLB", "ROLB", "LILB", "RILB", "MLB", "WLB", "SLB", "SAM", "WILL", "MIKE", "ILB", "OLB",
        "LCB", "RCB", "SCB", "CB", "NB",
        "SS", "FS",
        "PK", "PT", "LS", "H",
        "KR", "PR",
    ]

    ordered_positions = []
    seen = set()
    for pos in POS_ORDER:
        if pos in pos_groups and pos not in seen:
            ordered_positions.append(pos)
            seen.add(pos)
    for pos in sorted(pos_groups.keys()):
        if pos not in seen:
            ordered_positions.append(pos)

    # Offense / Defense / Special Teams grouping
    offense_pos = {"QB", "RB", "FB", "LWR", "RWR", "SWR", "WR", "TE", "LT", "LG", "C", "RG", "RT"}
    st_pos = {"PK", "PT", "LS", "H", "KR", "PR"}

    for section, label in [
        (offense_pos, "Offense"),
        (None, "Defense"),
        (st_pos, "Special Teams"),
    ]:
        section_positions = [
            p for p in ordered_positions
            if (section is not None and p in section)
            or (section is None and p not in offense_pos and p not in st_pos)
        ]
        if not section_positions:
            continue

        st.subheader(label)
        for pos in section_positions:
            players = pos_groups[pos]
            row_text = f"**{pos}**: " + " | ".join(
                f"#{p['depth']} {p['name']}" for p in players
            )
            st.markdown(row_text)
