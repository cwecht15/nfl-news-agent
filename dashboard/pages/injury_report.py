"""Injury Report page — the full weekly practice grid (Wed/Thu/Fri) with
game status per listed player (in-season only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Injury Report", page_icon="🩹", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import in_season_data as isd

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
    f"Updated {data.get('updated_at', '')} · sources "
    f"{', '.join(f'{k}={v}' for k, v in sorted((data.get('sources_used') or {}).items()))}"
)
teams = data.get("teams") or {}
team_pick = st.multiselect("Team", sorted(teams), key="ir_team")
practice_dates = sorted({d for t in teams.values() for p in (t.get("players") or {}).values() for d in (p.get("practice") or {})})
rows = []
for team, t in sorted(teams.items()):
    if team_pick and team not in team_pick:
        continue
    for nk, p in (t.get("players") or {}).items():
        row = {"Team": team, "Opp": t.get("opp") or "bye", "Player": p.get("name", ""),
               "Pos": p.get("pos", ""), "Injury": p.get("injury", "")}
        for d in practice_dates:
            row[d[5:]] = (p.get("practice") or {}).get(d, "")
        row["Game status"] = p.get("game_status", "")
        row["Source"] = p.get("source", "")
        rows.append(row)
st.markdown(f"**{len(rows)}** listed players")
st.dataframe(rows, use_container_width=True, hide_index=True)
conflicts = data.get("conflicts") or []
if conflicts:
    with st.expander(f"Source conflicts ({len(conflicts)})"):
        st.dataframe(conflicts, use_container_width=True, hide_index=True)
