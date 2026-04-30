"""Weekly Digest Page — View multi-day rollup reports."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

st.set_page_config(page_title="Weekly Digest", page_icon="🏈", layout="wide")

from dashboard.auth import require_password
require_password()

from config_loader import get_data_dir
from dashboard.helpers import render_sources
from scripts.run_digest import run_digest

st.header("Weekly Digest")
st.markdown("Multi-day rollup reports synthesizing daily briefings into themes.")

# --- Generate new digest ---
with st.container(border=True):
    st.subheader("Generate new digest")
    col_days, col_btn = st.columns([1, 2])
    with col_days:
        days = st.number_input(
            "Days to cover",
            min_value=2,
            max_value=30,
            value=7,
            step=1,
            key="digest_days",
        )
    with col_btn:
        st.write("")  # vertical spacing
        generate = st.button(
            "Generate digest",
            type="primary",
            use_container_width=True,
            key="digest_generate",
        )

    if generate:
        try:
            with st.spinner(
                f"Synthesizing the last {int(days)} days... "
                "this calls the LLM and may take 30-90 seconds."
            ):
                path = run_digest(days=int(days))
            if path:
                st.success(f"Digest generated: {Path(path).name}")
                st.rerun()
            else:
                st.warning(
                    "Digest generator returned no path — check that at least "
                    "2 daily reports exist for the selected window."
                )
        except Exception as e:
            st.error(f"Digest failed: {e}")

st.divider()

# Find available digest files
reports_dir = get_data_dir("reports")
digest_files = sorted(
    [f for f in reports_dir.glob("digest_*.json")],
    key=lambda f: f.name,
    reverse=True,
)

if not digest_files:
    st.info(
        "No digest reports yet. Click **Generate digest** above to build one."
    )
    st.stop()

# Selector
digest_labels = []
for f in digest_files:
    name = f.stem.replace("digest_", "").replace("_to_", " to ")
    digest_labels.append(name)

selected_idx = st.selectbox(
    "Select digest",
    range(len(digest_files)),
    format_func=lambda i: digest_labels[i],
)

# Load digest
try:
    with open(digest_files[selected_idx], "r", encoding="utf-8") as f:
        digest = json.load(f)
except Exception as e:
    st.error(f"Failed to load digest: {e}")
    st.stop()

date_range = digest.get("date_range", {})
st.subheader(
    f"{digest.get('days', '?')}-Day Digest: {date_range.get('start', '?')} to {date_range.get('end', '?')}"
)

# Section titles
TITLES = {
    "transactions": "Transactions & Signings",
    "injuries": "Injury Reports",
    "press_conferences": "Press Conference Highlights",
    "analysis": "Analysis & What to Watch",
}

# Render sections
for key, section_data in digest.get("sections", {}).items():
    title = TITLES.get(key, key.replace("_", " ").title())
    summary = section_data.get("summary", "No data.")

    with st.expander(title, expanded=True):
        st.markdown(summary)

# Team highlights
team_highlights = digest.get("team_highlights", {})
if team_highlights:
    st.subheader("Team Highlights")

    # Show mention counts
    mention_counts = digest.get("team_mention_counts", {})

    for team, highlight in team_highlights.items():
        mentions = mention_counts.get(team, 0)
        badge = f" ({mentions} days)" if mentions else ""
        st.markdown(f"**{team}{badge}**")
        summary = highlight.get("summary", "") if isinstance(highlight, dict) else str(highlight)
        st.markdown(summary)
        st.divider()

# Stats
st.subheader("Digest Stats")
cols = st.columns(3)
cols[0].metric("Days Covered", digest.get("days", 0))
cols[1].metric("Teams Featured", len(team_highlights))
cols[2].metric(
    "Period LLM Cost",
    f"${digest.get('daily_cost_total', 0):.2f}",
)

# Digest LLM usage
usage = digest.get("llm_usage", {})
if usage.get("request_count"):
    st.caption(
        f"Digest generation: {usage.get('request_count', 0)} calls, "
        f"est. cost ${float(usage.get('estimated_cost_usd', 0)):.4f}"
    )

st.caption(f"Generated: {digest.get('generated_at', 'unknown')}")
