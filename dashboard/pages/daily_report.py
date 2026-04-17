"""Daily Report Page."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st
from dashboard.helpers import highlight_summary, highlight_sources, render_sources
from reports.report_builder import list_available_reports, load_report

st.header("Daily Report")


def _format_count(value) -> str:
    """Format integer-like metrics for display."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value or 0)


def _matches_search(text: str, query: str) -> bool:
    """Check if text contains the search query (case-insensitive)."""
    return query.lower() in text.lower()


def _filter_paragraphs(summary: str, query: str) -> str:
    """For long summaries, return only paragraphs containing the query.

    Short summaries (under 5 paragraphs) are returned in full.
    """
    paragraphs = summary.split("\n\n")
    if len(paragraphs) <= 4:
        return summary

    query_lower = query.lower()
    matching = [p for p in paragraphs if query_lower in p.lower()]
    if not matching:
        return summary
    return "\n\n".join(matching)


# Date selector
available = list_available_reports()

if not available:
    st.warning(
        "No reports available yet. Run `python scripts/run_daily.py` to generate one."
    )
    st.stop()

selected_date = st.selectbox("Select date", available, index=0)

try:
    report = load_report(selected_date)
except Exception as e:
    st.error(f"Failed to load report: {e}")
    st.stop()

# Search box
search_query = st.text_input("Search report", placeholder="Player, team, or keyword...")

if report.alerts:
    for alert in report.alerts:
        source_name = alert.get("source", "Source")
        message = alert.get("message", "")
        latest_expiry = alert.get("latest_expiry")
        suffix = (
            f" Latest cookie expiry: {latest_expiry}."
            if latest_expiry else ""
        )
        severity = str(alert.get("severity", "warning")).lower()
        full_message = f"{source_name}: {message}{suffix}"
        if severity == "error":
            st.error(full_message)
        else:
            st.warning(full_message)

# Section titles
TITLES = {
    "transactions": "Transactions & Signings",
    "injuries": "Injury Reports",
    "press_conferences": "Press Conference Highlights",
    "analysis": "Analysis & What to Watch",
}

# Render sections
for key, section_data in report.sections.items():
    title = TITLES.get(key, key.replace("_", " ").title())
    summary = section_data.get("summary", "No data.")
    sources = section_data.get("sources", [])

    # If searching, skip sections that don't match
    if search_query:
        source_text = " ".join(
            s.get("title", "") + " " + s.get("source", "")
            for s in sources
        )
        if not _matches_search(summary + " " + source_text, search_query):
            continue

    count = section_data.get("count", "")
    badge = f" ({count} items)" if count else ""

    with st.expander(f"{title}{badge}", expanded=True):
        display_summary = _filter_paragraphs(summary, search_query) if search_query else summary
        st.markdown(display_summary)

        # Numbered sources (analysis section with inline citations)
        numbered_sources = section_data.get("numbered_sources")
        if numbered_sources:
            st.caption("Sources")
            lines = []
            for src in numbered_sources:
                num = src.get("num", "")
                title_text = src.get("title", "Source")
                source_name = src.get("source", "")
                url = src.get("url", "")
                suffix = f" ({source_name})" if source_name else ""
                if url:
                    lines.append(f"[{num}] [{title_text}]({url}){suffix}")
                else:
                    lines.append(f"[{num}] {title_text}{suffix}")
            st.markdown("\n\n".join(lines))
        else:
            render_sources(sources)

# Team highlights
if report.team_highlights:
    st.subheader("Team Highlights")
    # Let user filter teams
    teams = sorted(report.team_highlights.keys())
    selected_teams = st.multiselect(
        "Filter teams (leave empty for all)", teams
    )
    show_teams = selected_teams if selected_teams else teams

    matched = 0
    for team in show_teams:
        highlight = report.team_highlights[team]
        summary_text = highlight_summary(highlight)

        # If searching, skip teams that don't match
        if search_query:
            source_text = " ".join(
                s.get("title", "") + " " + s.get("source", "")
                for s in highlight_sources(highlight)
            )
            if not _matches_search(
                team + " " + summary_text + " " + source_text,
                search_query,
            ):
                continue

        matched += 1
        st.markdown(f"**{team}**")
        st.markdown(summary_text)
        render_sources(highlight_sources(highlight))
        st.divider()

    if search_query and matched == 0:
        st.info(f"No team highlights match '{search_query}'.")

# Collection stats
if report.collection_stats:
    st.subheader("Collection Stats")
    cols = st.columns(len(report.collection_stats))
    for i, (source, count) in enumerate(report.collection_stats.items()):
        cols[i].metric(source.upper(), count)

if report.llm_usage:
    usage = report.llm_usage
    st.subheader("LLM Usage")
    st.markdown(
        f"**Provider:** {usage.get('provider', 'unknown')}  \n"
        f"**Model:** {usage.get('model', 'unknown')}  \n"
        f"**Service tier:** {usage.get('service_tier', 'default')}"
    )

    usage_cols = st.columns(5)
    usage_cols[0].metric("Calls", _format_count(usage.get("request_count", 0)))
    usage_cols[1].metric("Input", _format_count(usage.get("input_tokens", 0)))
    usage_cols[2].metric("Output", _format_count(usage.get("output_tokens", 0)))
    usage_cols[3].metric(
        "Reasoning",
        _format_count(usage.get("reasoning_tokens", 0)),
    )
    usage_cols[4].metric(
        "Est. Cost",
        f"${float(usage.get('estimated_cost_usd', 0.0)):.4f}",
    )

    if usage.get("tracking_note"):
        st.caption(usage["tracking_note"])

# Transaction reconciliation
try:
    from scripts.transaction_reconciler import reconcile, add_override

    txn_alerts = reconcile()
    if txn_alerts:
        st.subheader(f"Projection Alerts ({len(txn_alerts)})")
        st.caption("Transactions not yet reflected in your projections. Dismiss if intentionally not projecting.")
        for i, alert in enumerate(txn_alerts):
            col_msg, col_btn = st.columns([5, 1])
            with col_msg:
                icon = "MISSING" if alert["type"] == "missing" else "WRONG TEAM"
                pos = alert.get("pos", "")
                depth = alert.get("depth", 0)
                depth_label = f" | Depth #{depth}" if depth else ""
                st.warning(f"**[{icon}]** {alert['message']}  \n*{alert['transaction']}* ({alert['date']})")
            with col_btn:
                if st.button("Dismiss", key=f"dismiss_txn_{i}"):
                    add_override(alert["player"], alert.get("transaction", ""), reason="Dismissed from daily report")
                    st.rerun()
except Exception:
    pass  # Non-fatal — don't break the report page

st.caption(f"Generated: {report.generated_at}")
