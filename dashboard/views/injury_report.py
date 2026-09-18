"""Injury Report page — the full weekly practice grid (Wed/Thu/Fri) with
game status per listed player (in-season only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Injury Report", page_icon="🩹", layout="wide")

from dashboard.auth import require_password
require_password()

from collectors.injury_report_collector import split_trailing_pos
from dashboard import in_season_data as isd
from dashboard.helpers import to_et_display
from processing.season import weekday_name

# Sources spell the same position several ways (SAF/S/FS, OT/T, DE/EDGE), so the
# filter works on a group rather than the raw label, which stays in the table.
_POS_GROUPS = {
    "QB": {"QB"},
    "RB": {"RB", "HB", "FB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "OL": {"C", "G", "OG", "T", "OT", "OL", "T/G", "G/C", "LT", "RT", "LG", "RG"},
    "DL": {"DT", "DE", "NT", "DL", "EDGE"},
    "LB": {"LB", "OLB", "ILB", "MLB"},
    "DB": {"CB", "S", "SAF", "FS", "SS", "DB", "NB"},
    "ST": {"K", "PK", "P", "LS"},
}
_POS_TO_GROUP = {p: g for g, members in _POS_GROUPS.items() for p in members}
_SKILL = {"QB", "RB", "FB", "WR", "TE", "K"}
_SKILL_OPTION = "Skill (QB/RB/WR/TE/K)"


def _name_and_pos(p: dict) -> tuple[str, str]:
    """Some team sites publish no `pos` and append it to the name instead
    ("Shelby Harris, DT"). The collector splits that at write time now; this
    keeps week files collected before that fix readable — and filterable."""
    name = (p.get("name") or "").strip()
    pos = (p.get("pos") or "").strip()
    if not pos:
        name, pos = split_trailing_pos(name)
    return name, pos.upper()


st.header("Injury Report")
isd.require_in_season()

_settings, ctx, _schedule, _week = isd.context()

weeks = isd.injury_weeks(ctx.season)
if not weeks:
    st.info("No injury report files yet (data/injuries/<season>/wkNN.json). "
            "The first practice report of the week lands on Wednesday.")
    st.stop()

wk = st.selectbox("Week", weeks, index=0, key="ir_week")
data = isd.injury_week(ctx.season, wk) or {}
st.caption(
    f"Updated {to_et_display(data.get('updated_at'))} · sources "
    f"{', '.join(f'{k}={v}' for k, v in sorted((data.get('sources_used') or {}).items()))}"
)
teams = data.get("teams") or {}
col_team, col_pos, col_cleared = st.columns([2, 1, 1])
show_cleared = col_cleared.checkbox("Show players dropped from the report", value=False, key="ir_cleared")


def _listed(p: dict) -> bool:
    return show_cleared or not p.get("cleared")


practice_dates = sorted({d for t in teams.values() for p in (t.get("players") or {}).values()
                         if _listed(p) for d in (p.get("practice") or {})})
# "Wed 09-16" rather than "09-16": a Thursday game's Mon/Tue columns sit
# next to everyone else's Wed/Thu/Fri, and a bare date hides which is which.
date_labels = {d: f"{weekday_name(d)} {d[5:]}" for d in practice_dates}
all_rows = []
for team, t in sorted(teams.items()):
    for nk, p in (t.get("players") or {}).items():
        if not _listed(p):
            continue
        name, pos = _name_and_pos(p)
        row = {"Team": team, "Opp": t.get("opp") or "bye", "Player": name,
               "Pos": pos, "Injury": p.get("injury", "")}
        for d in practice_dates:
            row[date_labels[d]] = (p.get("practice") or {}).get(d, "")
        row["Game status"] = p.get("game_status", "")
        if show_cleared:
            row["Dropped"] = p.get("cleared") or ""
        row["Source"] = p.get("source", "")
        all_rows.append(row)

team_pick = col_team.multiselect("Team", sorted(teams), key="ir_team")
pos_options = [_SKILL_OPTION]
pos_options += [g for g in _POS_GROUPS
                if any(_POS_TO_GROUP.get(r["Pos"]) == g for r in all_rows)]
if any(_POS_TO_GROUP.get(r["Pos"]) is None for r in all_rows):
    pos_options.append("Other")
pos_pick = col_pos.multiselect("Position", pos_options, key="ir_pos")
picked_groups = {g for g in pos_pick if g != _SKILL_OPTION}

rows = []
for r in all_rows:
    if team_pick and r["Team"] not in team_pick:
        continue
    if pos_pick:
        group = _POS_TO_GROUP.get(r["Pos"], "Other")
        if not (group in picked_groups
                or (_SKILL_OPTION in pos_pick and r["Pos"] in _SKILL)):
            continue
    rows.append(r)

count = f"**{len(rows)}** listed players"
if len(rows) != len(all_rows):
    count += f" (of {len(all_rows)})"
st.markdown(count)
st.dataframe(rows, use_container_width=True, hide_index=True)
conflicts = data.get("conflicts") or []
if conflicts:
    with st.expander(f"Source conflicts ({len(conflicts)})"):
        st.dataframe(conflicts, use_container_width=True, hide_index=True)
