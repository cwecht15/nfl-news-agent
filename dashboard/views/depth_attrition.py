"""Depth Attrition page — which Week-1 depth-chart slots each team is down
this week, league-wide (in-season only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Depth Attrition", page_icon="🧱", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_teams
from dashboard import attrition_view as av
from dashboard import in_season_data as isd
from processing import season as season_mod
from processing.team_abbr import to_news

st.header("Depth Attrition")
isd.require_in_season()

_settings, ctx, schedule, week = isd.context()
if not week:
    st.info("No current week yet.")
    st.stop()

data = isd.attrition(ctx.season, int(week))
if data is None:
    st.info("No Week-1 baseline yet. Build it locally with "
            "`python scripts/build_attrition_baseline.py` and push `data/attrition/`.")
    st.stop()

st.caption(f"Week {week}. " + av.CAPTION)

names = {t["abbr"]: t["name"] for t in get_teams()}
byes = {to_news(t, "proj") for t in season_mod.teams_on_bye(schedule or [], int(week))}


def _opp(team: str) -> str:
    g = season_mod.opponent(schedule or [], team, int(week), source="news") if schedule else None
    return to_news(g["opp"], "proj") if g else ""


c1, c2 = st.columns([2, 3])
sort = c1.radio("Order", ["Most depleted", "Team"], horizontal=True, key="attr_sort")
hide_quiet = c2.checkbox("Only teams with a notable or severe unit", value=False, key="attr_quiet")

teams = sorted(data)
if hide_quiet:
    teams = [t for t in teams if any(u.get("level") in ("notable", "severe") for u in data[t].values())]
if sort == "Most depleted":
    teams.sort(key=lambda t: -av.team_score(data[t]))

text, level = av.heat_frame(data, teams)
text.insert(0, "Opp", [("bye" if t in byes else _opp(t)) for t in teams])
text.insert(1, "Total", [av.team_score(data[t]) for t in teams])
level.insert(0, "Opp", "none")
level.insert(1, "Total", "none")

st.markdown("Cells list the slots that are down; **(Q)** / **(D)** / **(DNP)** mark partial ones. "
            "Click a row for the detail.")
event = st.dataframe(
    av.styled_heat(text, level).format({"Total": "{:.2f}"}),
    use_container_width=True, height=min(38 + 35 * len(teams), 1160),
    on_select="rerun", selection_mode="single-row", key="attr_heat",
)
sel = (event.selection.rows if event and hasattr(event, "selection") else []) or []

st.divider()
team_opts = sorted(data)
default = teams[sel[0]] if sel else (teams[0] if teams else team_opts[0])
pick = st.selectbox("Team", team_opts, index=team_opts.index(default),
                    format_func=lambda a: f"{names.get(a, a)} ({a})", key=f"attr_team_{default}")
show_opp = st.toggle("Show this week's opponent side by side", value=True, key="attr_opp")
show_departed = st.checkbox("Include players who left the roster (greyed)", value=True, key="attr_dep")

opp = _opp(pick) if pick not in byes else ""
cols = st.columns(2) if show_opp and opp in data else [st.container()]
for col, t in zip(cols, [pick, opp]):
    with col:
        st.subheader(f"{names.get(t, t)}" + (" — on bye" if t in byes else ""))
        summary = av.unit_summary(data[t])
        if summary:
            st.markdown(summary)
        av.render_detail(data[t], key=f"attr_detail_{t}", include_departed=show_departed)
