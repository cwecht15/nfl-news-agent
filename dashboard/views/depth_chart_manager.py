"""Depth Chart Manager page.

Compares the user's hand-maintained "2026 Depth Chart" Google Sheet against
the agent's view of the world (latest OurLads scrape + recent NFL.com
transactions) and surfaces three classes of discrepancy. Read-only — does
not write back to the master sheet. Dismissals are stored locally in
data/sheet_recon_dismissals.json.

In-season behavior is partially deferred — see
docs/depth_chart_manager_in_season.md for the work needed when the season
starts (game-day inactives, practice-squad distinction, weekly elevations).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Depth Chart Manager", page_icon="📋", layout="wide")

from dashboard.auth import require_password

require_password()

from processing.sheet_reconciliation import (
    _build_ourlads_by_name,
    _collect_recent_transactions,
    _index_recent_transactions_by_player,
    compute_status_mismatches,
    compute_team_mismatches,
    compute_untracked_transactions,
    dismiss,
    filter_dismissed,
    load_dismissals,
    load_master_depthchart,
    load_master_transactions,
    undismiss,
    _get_gspread_client,
)

st.header("Depth Chart Manager")
st.caption(
    "Compares the **2026 Depth Chart** master sheet against the latest "
    "OurLads scrape and the last 30 days of NFL.com transactions. Read-only — "
    "fix flagged rows directly in the sheet."
)


# ---------------------------------------------------------------------------
# Cached loaders (1h TTL; refresh button busts cache)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=3600, show_spinner="Loading master DepthCharts...")
def _cached_master_depthchart() -> dict:
    return load_master_depthchart(_get_gspread_client())


@st.cache_data(ttl=3600, show_spinner="Loading master Transactions_New...")
def _cached_master_transactions() -> set:
    return load_master_transactions(_get_gspread_client())


@st.cache_data(ttl=600, show_spinner="Loading agent depth chart + transactions...")
def _cached_agent_data(lookback_days: int):
    ourlads = _build_ourlads_by_name()
    txs = _collect_recent_transactions(days=lookback_days)
    tx_by_player = _index_recent_transactions_by_player(txs)
    return ourlads, txs, tx_by_player


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.subheader("Depth Chart Manager")
    lookback_days = st.slider(
        "Agent transaction lookback (days)",
        min_value=7,
        max_value=120,
        value=30,
        step=1,
        help="How far back to scan agent-side transactions when checking "
        "for staleness in the master sheet.",
    )
    if st.button("🔄 Refresh data", use_container_width=True):
        _cached_master_depthchart.clear()
        _cached_master_transactions.clear()
        _cached_agent_data.clear()
        st.rerun()

    show_dismissed = st.checkbox(
        "Show dismissed rows", value=False,
        help="Toggle to review previously-dismissed discrepancies.",
    )

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

try:
    master = _cached_master_depthchart()
    master_tx = _cached_master_transactions()
    ourlads, agent_tx, tx_by_player = _cached_agent_data(lookback_days)
except Exception as e:
    st.error(f"Failed to load data: {e}")
    st.stop()

team_rows = compute_team_mismatches(master, ourlads, tx_by_player)
status_rows = compute_status_mismatches(master, ourlads, tx_by_player)
untracked_rows = compute_untracked_transactions(master_tx, agent_tx, master)

dismissals = load_dismissals()
team_active, team_dismissed = filter_dismissed(team_rows, dismissals)
status_active, status_dismissed = filter_dismissed(status_rows, dismissals)
untracked_active, untracked_dismissed = filter_dismissed(untracked_rows, dismissals)

# ---------------------------------------------------------------------------
# Top metrics
# ---------------------------------------------------------------------------

m1, m2, m3, m4 = st.columns(4)
m1.metric("Team Mismatches", len(team_active), help="Open (non-dismissed)")
m2.metric("Status Mismatches", len(status_active))
m3.metric("Untracked Transactions", len(untracked_active))
m4.metric("Dismissed", len(dismissals))

st.divider()

# ---------------------------------------------------------------------------
# Helper: render a tab with selectable dataframe + dismiss button
# ---------------------------------------------------------------------------


def _render_tab(
    tab_key: str,
    rows: list[dict],
    dismissed_rows: list[dict],
    columns: list[tuple[str, str]],  # (display_name, source_field)
    team_field: str | None = None,
):
    """Render a discrepancy tab with team filter, selectable dataframe,
    and dismiss button.
    """
    if not rows and not (show_dismissed and dismissed_rows):
        st.success("No discrepancies in this category. Nice.")
        return

    if team_field:
        teams = sorted({r[team_field] for r in rows if r.get(team_field)})
        if teams:
            picked = st.selectbox(
                "Filter by team",
                ["(all)"] + teams,
                key=f"{tab_key}_team_filter",
            )
            if picked != "(all)":
                rows = [r for r in rows if r.get(team_field) == picked]

    if not rows:
        st.info("No discrepancies in this category for the chosen filter.")
    else:
        df = pd.DataFrame([
            {disp: r.get(field, "") for disp, field in columns}
            for r in rows
        ])
        sel = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="multi-row",
            key=f"{tab_key}_df",
        )
        selected_idxs = sel.selection.rows if sel and sel.selection else []
        c1, _ = st.columns([1, 4])
        with c1:
            if st.button(
                f"Dismiss {len(selected_idxs)} selected",
                disabled=not selected_idxs,
                key=f"{tab_key}_dismiss_btn",
                type="primary",
            ):
                for idx in selected_idxs:
                    dismiss(rows[idx]["key"])
                st.rerun()

    if show_dismissed and dismissed_rows:
        with st.expander(f"Dismissed ({len(dismissed_rows)})", expanded=False):
            df_d = pd.DataFrame([
                {disp: r.get(field, "") for disp, field in columns} | {"_key": r["key"]}
                for r in dismissed_rows
            ])
            sel_d = st.dataframe(
                df_d.drop(columns=["_key"]),
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key=f"{tab_key}_dismissed_df",
            )
            selected_d = sel_d.selection.rows if sel_d and sel_d.selection else []
            if st.button(
                f"Un-dismiss {len(selected_d)} selected",
                disabled=not selected_d,
                key=f"{tab_key}_undismiss_btn",
            ):
                for idx in selected_d:
                    undismiss(dismissed_rows[idx]["key"])
                st.rerun()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3 = st.tabs([
    f"Team Mismatches ({len(team_active)})",
    f"Status Mismatches ({len(status_active)})",
    f"Untracked Transactions ({len(untracked_active)})",
])


def _format_recent_tx(row: dict) -> str:
    tx = row.get("recent_tx")
    if not tx:
        return ""
    return f"{tx.get('_date', '')}: {tx.get('title', '')[:80]}"


with tab1:
    st.markdown(
        "Players whose **master DepthCharts.TEAM** disagrees with the latest "
        "OurLads scrape (after normalizing BAL↔BLT etc.). Most recent agent "
        "transaction shown if available."
    )
    # Build display rows with formatted recent_tx
    display_team_rows = [
        dict(r, recent_tx_str=_format_recent_tx(r))
        for r in team_active
    ]
    _render_tab(
        "team",
        display_team_rows,
        team_dismissed,
        columns=[
            ("Player", "player"),
            ("Pos", "pos"),
            ("Master Team", "master_team"),
            ("OurLads Team", "ourlads_team"),
            ("OurLads Depth", "ourlads_depth"),
            ("Recent Transaction", "recent_tx_str"),
        ],
        team_field="master_team",
    )

with tab2:
    st.markdown(
        "Players whose **master STATUS DESC = Active** but agent data implies "
        "otherwise — either a recent deactivating transaction or absence from "
        "the latest OurLads depth chart."
    )
    _render_tab(
        "status",
        status_active,
        status_dismissed,
        columns=[
            ("Player", "player"),
            ("Pos", "pos"),
            ("Master Team", "master_team"),
            ("Master Status", "master_status"),
            ("Suggested Status", "suggested_status"),
            ("Evidence", "evidence"),
            ("Evidence Date", "evidence_date"),
        ],
        team_field="master_team",
    )

with tab3:
    st.markdown(
        f"Transactions our agent saw in the last {lookback_days} days that "
        "don't appear in the master sheet's **Transactions_New** tab "
        "(matched by date + gsisId). Players not in the master DepthCharts "
        "are excluded — those are a separate problem your *Missing Players* "
        "tab already handles."
    )
    _render_tab(
        "untracked",
        untracked_active,
        untracked_dismissed,
        columns=[
            ("Date", "date"),
            ("Player", "player"),
            ("Type", "agent_type"),
            ("Title", "title"),
            ("URL", "url"),
        ],
        team_field=None,
    )

st.divider()
st.caption(
    f"Master sheet ID: 1XHXiR... · Lookback: {lookback_days}d · "
    f"OurLads source: data/depth_charts/<latest>.json"
)
