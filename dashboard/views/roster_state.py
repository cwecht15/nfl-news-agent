"""Roster State page — current IR/PUP/NFI/SUS/PS standing per player plus the
recent roster-event feed (in-season only)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Roster State", page_icon="🔁", layout="wide")

from dashboard.auth import require_password
require_password()

from dashboard import in_season_data as isd

st.header("Roster State")
isd.require_in_season()

state = isd.roster_state()
if not state:
    st.info("No roster state yet (data/roster/state.json). It is built by the daily pipeline in-season.")
    st.stop()

players = state.get("players") or {}
st.caption(
    f"Updated {state.get('updated_at', '')} · baseline {state.get('baseline', {}).get('source', '')} "
    f"{state.get('baseline', {}).get('date', '')} · {len(players)} players"
)
all_teams = sorted({p.get("team", "") for p in players.values() if p.get("team")})
all_status = sorted({p.get("status", "") for p in players.values() if p.get("status")})
f1, f2, f3 = st.columns(3)
team_f = f1.multiselect("Team", all_teams, key="rs_team")
status_f = f2.multiselect("Status", all_status, default=[s for s in ("IR", "PUP", "NFI", "SUS", "PS") if s in all_status], key="rs_status")
pos_f = f3.multiselect("Position", ["QB", "RB", "WR", "TE", "K"], default=["QB", "RB", "WR", "TE", "K"], key="rs_pos")

rows = []
for gid, p in players.items():
    if team_f and p.get("team") not in team_f:
        continue
    if status_f and p.get("status") not in status_f:
        continue
    if pos_f and p.get("pos") not in pos_f:
        continue
    rows.append({
        "Player": p.get("name", ""), "Team": p.get("team", ""), "Pos": p.get("pos", ""),
        "Status": p.get("status", ""), "Since": p.get("status_since", "") or "",
        "Source": p.get("status_source", "") or "",
        "IR date": p.get("ir_date", "") or "",
        "Eligible Wk": str(p.get("earliest_return_week") or ""),
        "Designated": (p.get("designated_return_date") or "")[:10],
        "Elev used": p.get("elevations_used", 0),
        "Pending": len(p.get("pending") or []),
    })
rows.sort(key=lambda r: (r["Team"], r["Status"], r["Player"]))
st.markdown(f"**{len(rows)}** players")
st.dataframe(rows, use_container_width=True, hide_index=True)

st.subheader("Recent events")
ev_rows = []
for e in isd.events(300):
    if team_f and e.get("team") not in team_f:
        continue
    ev_rows.append({
        "Date": e.get("date", ""), "Player": e.get("name", ""), "Team": e.get("team", ""),
        "Pos": e.get("pos", ""), "Event": (e.get("event_type") or "").replace("_", " "),
        "Detail": (e.get("detail") or "")[:60],
        "Source": (e.get("source") or "").replace("news:", ""),
        "Confidence": e.get("confidence", ""),
    })
st.dataframe(ev_rows[:200], use_container_width=True, hide_index=True)
