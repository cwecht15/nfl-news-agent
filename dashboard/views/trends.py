"""Historical Trends Page — Visualize patterns across daily reports."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Historical Trends", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from reports.report_builder import list_available_reports, load_report

st.header("Historical Trends")

available = list_available_reports()

if len(available) < 2:
    st.warning("Need at least 2 daily reports to show trends.")
    st.stop()

max_days = min(len(available), 90)
days_back = st.slider("Days to analyze", min(2, max_days), max_days, min(max_days, 30))
dates = available[:days_back]

# Load all reports in range
reports = {}
for date_str in dates:
    try:
        reports[date_str] = load_report(date_str)
    except Exception:
        continue

if not reports:
    st.error("Could not load any reports.")
    st.stop()

st.subheader(f"Analyzing {len(reports)} reports ({dates[-1]} to {dates[0]})")

# ── Collection Volume ──────────────────────────────────
st.subheader("Collection Volume Over Time")

volume_data = {"Date": [], "RSS": [], "Web": [], "YouTube": []}
for date_str in sorted(reports.keys()):
    report = reports[date_str]
    stats = report.collection_stats
    volume_data["Date"].append(date_str)
    volume_data["RSS"].append(stats.get("rss", 0))
    volume_data["Web"].append(stats.get("web", 0))
    volume_data["YouTube"].append(stats.get("youtube", 0))

st.line_chart(
    data={k: v for k, v in volume_data.items() if k != "Date"},
    use_container_width=True,
)

# Total items per day
total_per_day = {
    date_str: sum(
        reports[date_str].collection_stats.get(src, 0)
        for src in ("rss", "web", "youtube")
    )
    for date_str in sorted(reports.keys())
}
avg_items = sum(total_per_day.values()) / len(total_per_day) if total_per_day else 0
st.caption(f"Average: {avg_items:.0f} items/day across {len(reports)} days")

# ── Transaction Volume ─────────────────────────────────
st.subheader("Transaction Volume Over Time")

txn_data = {}
for date_str in sorted(reports.keys()):
    report = reports[date_str]
    txn_section = report.sections.get("transactions", {})
    txn_data[date_str] = txn_section.get("count", 0)

st.bar_chart(txn_data, use_container_width=True)

# ── Most Mentioned Teams ──────────────────────────────
st.subheader("Most Mentioned Teams")

team_counts = Counter()
for report in reports.values():
    for team in report.team_highlights.keys():
        team_counts[team] += 1

if team_counts:
    top_teams = dict(team_counts.most_common(15))
    st.bar_chart(top_teams, use_container_width=True, horizontal=True)
else:
    st.info("No team highlight data available.")

# ── LLM Cost Over Time ────────────────────────────────
st.subheader("LLM Cost Over Time")

cost_data = {}
token_data = {"Date": [], "Input": [], "Output": []}
total_cost = 0.0
total_requests = 0

for date_str in sorted(reports.keys()):
    report = reports[date_str]
    usage = report.llm_usage
    cost = float(usage.get("estimated_cost_usd", 0.0) or 0.0)
    cost_data[date_str] = cost
    total_cost += cost
    total_requests += int(usage.get("request_count", 0) or 0)
    token_data["Date"].append(date_str)
    token_data["Input"].append(int(usage.get("input_tokens", 0) or 0))
    token_data["Output"].append(int(usage.get("output_tokens", 0) or 0))

st.line_chart(cost_data, use_container_width=True)

cost_cols = st.columns(3)
cost_cols[0].metric("Total Cost", f"${total_cost:.2f}")
cost_cols[1].metric("Avg Cost/Day", f"${total_cost / len(reports):.4f}" if reports else "$0")
cost_cols[2].metric("Total LLM Calls", f"{total_requests:,}")

# ── Token Usage Over Time ─────────────────────────────
st.subheader("Token Usage Over Time")
st.line_chart(
    data={"Input": token_data["Input"], "Output": token_data["Output"]},
    use_container_width=True,
)

# ── Source Alerts Frequency ───────────────────────────
st.subheader("Source Alert Frequency")

alert_counts = Counter()
for report in reports.values():
    for alert in report.alerts:
        source = alert.get("source", "Unknown")
        alert_counts[source] += 1

if alert_counts:
    st.bar_chart(dict(alert_counts.most_common(10)), use_container_width=True)
else:
    st.success("No source alerts in this period.")

# ── Press Conference Coverage ─────────────────────────
st.subheader("Press Conference Coverage")

press_data = {}
for date_str in sorted(reports.keys()):
    report = reports[date_str]
    press_section = report.sections.get("press_conferences", {})
    press_data[date_str] = press_section.get("count", 0)

st.bar_chart(press_data, use_container_width=True)

total_press = sum(press_data.values())
st.caption(f"Total: {total_press} press conferences across {len(reports)} days")
