"""Inactives page — game-day inactives polled from ESPN's per-game rosters
(in-season only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Inactives", page_icon="🚫", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import in_season_data as isd

st.header("Game-Day Inactives")
isd.require_in_season()

_settings, ctx, _schedule, _week = isd.context()

inact_weeks = isd.inactives_weeks(ctx.season)
if not inact_weeks:
    st.info("No inactives yet. They are polled from ESPN game rosters right after each "
            "inactives window (about 90 minutes before kickoff) by the game-day workflow.")
    st.stop()

iw = st.selectbox("Week", inact_weeks, index=0, key="inact_week")
idata = isd.inactives_week(ctx.season, iw) or {}
st.caption(f"Updated {idata.get('updated_at', '')}")
skill = {"QB", "RB", "FB", "WR", "TE", "K"}
only_skill = st.checkbox("Skill positions only", value=True, key="inact_skill")
rows = []
for g in sorted((idata.get("games") or {}).values(), key=lambda x: (x.get("date", ""), x.get("short_name", ""))):
    for team, t in (g.get("teams") or {}).items():
        for p in t.get("inactives") or []:
            if only_skill and p.get("pos") not in skill:
                continue
            rows.append({
                "Game": g.get("short_name", ""), "Kickoff (UTC)": g.get("date", ""), "Team": team,
                "Player": p.get("name", ""), "Pos": p.get("pos", ""), "Phase": t.get("phase", ""),
                "Published": (t.get("published_at") or "")[:16],
            })
st.markdown(f"**{len(rows)}** inactive players listed")
st.dataframe(rows, use_container_width=True, hide_index=True)
