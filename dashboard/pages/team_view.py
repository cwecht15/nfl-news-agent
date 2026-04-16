"""Team View Page — Per-team news drilldown."""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st
from config_loader import get_teams, get_data_dir
from dashboard.helpers import highlight_summary, highlight_sources, render_sources
from reports.report_builder import list_available_reports, load_report
from models import NewsItem

st.header("Team View")

teams = get_teams()
team_names = {t["abbr"]: t["name"] for t in teams}
team_options = [f"{t['abbr']} — {t['name']}" for t in sorted(teams, key=lambda x: x["name"])]

selected = st.selectbox("Select a team", team_options)
team_abbr = selected.split(" — ")[0]

st.subheader(team_names.get(team_abbr, team_abbr))

# Show recent reports for this team
available = list_available_reports()

if not available:
    st.warning("No reports available yet.")
    st.stop()

# How many days back to show
days_back = st.slider("Days to show", 1, 30, 7)
dates_to_show = available[:days_back]

for date_str in dates_to_show:
    try:
        report = load_report(date_str)
    except Exception:
        continue

    highlight = report.team_highlights.get(team_abbr)
    if highlight:
        with st.expander(f"{date_str}", expanded=(date_str == dates_to_show[0])):
            st.markdown(highlight_summary(highlight))
            render_sources(highlight_sources(highlight))

    # Also check raw data for this team
    raw_dir = get_data_dir("raw", date_str)
    for source_file in ("rss.json", "web.json"):
        path = raw_dir / source_file
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                team_items = [
                    i for i in items if team_abbr in i.get("teams", [])
                ]
                if team_items:
                    for item in team_items:
                        title = item.get("title", "Source")
                        url = item.get("url", "")
                        source_name = item.get("source", "Source")

                        if url:
                            st.markdown(
                                f"- **[{source_name}]** "
                                f"[{title}]({url})"
                            )
                        else:
                            st.markdown(f"- **[{source_name}]** {title}")
            except Exception:
                pass

if not any(
    highlight_summary(report.team_highlights.get(team_abbr))
    for date_str in dates_to_show
    if (report := load_report(date_str)) is not None
):
    st.info(f"No recent news for {team_names.get(team_abbr, team_abbr)}.")
