"""Projections Page — track projection changes and player trends."""

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import streamlit as st

PROJ_DIR = Path(__file__).parent.parent.parent / "data" / "projections"

st.header("Projections")


# --- Helpers ---

def _get_snapshot_dates() -> list[str]:
    """Return available snapshot dates, newest first."""
    if not PROJ_DIR.exists():
        return []
    return sorted(
        [d.name for d in PROJ_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )


def _load_snapshot(date: str, kind: str) -> dict | None:
    path = PROJ_DIR / date / f"{kind}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_changelog() -> list[dict]:
    path = PROJ_DIR / "changelog.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def _find_player(snapshot: dict, query: str) -> list[tuple[str, dict]]:
    """Find players matching a search query. Keys are player IDs."""
    query_lower = query.lower()
    matches = []
    for key, data in snapshot.items():
        name = data.get("name", "").lower()
        team = data.get("team", "").lower()
        slot = data.get("slot", "").lower()
        pid = data.get("player_id", "").lower()
        if (query_lower in name or query_lower in team
                or query_lower in slot or query_lower == key.lower()
                or query_lower == pid):
            matches.append((key, data))
    return matches


# --- Page layout ---

dates = _get_snapshot_dates()

if not dates:
    st.warning("No projection snapshots yet. Run `python scripts/snapshot_projections.py` to create one.")
    st.stop()

tab_changes, tab_lookup, tab_history = st.tabs(["Today's Changes", "Player Lookup", "Player History"])

# ─── Tab 1: Today's Changes ───

with tab_changes:
    selected_date = st.selectbox("Snapshot date", dates, index=0, key="changes_date")

    # Load changelog for this date
    changelog = _load_changelog()
    day_changes = [c for c in changelog if c.get("date") == selected_date]

    if not day_changes:
        st.info(f"No changes recorded for {selected_date}. This may be the baseline snapshot.")
    else:
        # Split into categories
        adj_changes = [c for c in day_changes if "Adj" in c.get("metric", "") and c.get("kind") == "player"]
        player_proj = [c for c in day_changes if c.get("kind") == "player" and c.get("type") == "metric_change" and "Adj" not in c.get("metric", "")]
        team_changes = [c for c in day_changes if c.get("kind") == "team"]
        roster_changes = [c for c in day_changes if c.get("type") in ("added", "removed", "team_change", "games_change")]

        # Adjustment tweaks
        if adj_changes:
            st.subheader(f"Your Adjustment Tweaks ({len(adj_changes)})")
            adj_data = []
            for c in adj_changes:
                adj_data.append({
                    "Player": c.get("label", ""),
                    "Metric": c.get("metric", ""),
                    "Old": c.get("old_value", ""),
                    "New": c.get("new_value", ""),
                })
            st.dataframe(adj_data, use_container_width=True, hide_index=True)

        # Roster / games changes
        if roster_changes:
            st.subheader(f"Roster & Games Changes ({len(roster_changes)})")
            for c in roster_changes:
                if c["type"] == "added":
                    st.success(f"**Added:** {c.get('label', c.get('key', ''))}")
                elif c["type"] == "removed":
                    st.error(f"**Removed:** {c.get('label', c.get('key', ''))}")
                elif c["type"] == "team_change":
                    st.info(f"**{c.get('label', '')}** moved: {c.get('old_value', '')} -> {c.get('new_value', '')}")
                elif c["type"] == "games_change":
                    st.info(f"**{c.get('label', '')}** games: {c.get('old_value', '')} -> {c.get('new_value', '')}")

        # Biggest projection movers (top 20 by absolute change)
        if player_proj:
            st.subheader(f"Projection Shifts ({len(player_proj)})")

            # Try to compute magnitude for sorting
            def _change_magnitude(c):
                try:
                    return abs(float(c.get("new_value", 0)) - float(c.get("old_value", 0)))
                except (ValueError, TypeError):
                    return 0

            sorted_proj = sorted(player_proj, key=_change_magnitude, reverse=True)

            proj_data = []
            for c in sorted_proj[:30]:
                try:
                    delta = float(c.get("new_value", 0)) - float(c.get("old_value", 0))
                    delta_str = f"{delta:+.3g}"
                except (ValueError, TypeError):
                    delta_str = ""
                proj_data.append({
                    "Player": c.get("label", ""),
                    "Metric": c.get("metric", ""),
                    "Old": c.get("old_value", ""),
                    "New": c.get("new_value", ""),
                    "Delta": delta_str,
                })
            st.dataframe(proj_data, use_container_width=True, hide_index=True)

            if len(player_proj) > 30:
                st.caption(f"Showing top 30 of {len(player_proj)} projection changes.")

        # Team changes
        if team_changes:
            st.subheader(f"Team Projection Changes ({len(team_changes)})")
            team_data = []
            for c in team_changes:
                try:
                    delta = float(c.get("new_value", 0)) - float(c.get("old_value", 0))
                    delta_str = f"{delta:+.3g}"
                except (ValueError, TypeError):
                    delta_str = ""
                team_data.append({
                    "Team": c.get("label", c.get("key", "")),
                    "Metric": c.get("metric", ""),
                    "Old": c.get("old_value", ""),
                    "New": c.get("new_value", ""),
                    "Delta": delta_str,
                })
            st.dataframe(team_data, use_container_width=True, hide_index=True)


# ─── Tab 2: Player Lookup ───

with tab_lookup:
    lookup_date = st.selectbox("Snapshot date", dates, index=0, key="lookup_date")
    snapshot = _load_snapshot(lookup_date, "players")

    if not snapshot:
        st.warning("No player snapshot for this date.")
        st.stop()

    query = st.text_input("Search by player name, team, or slot", placeholder="e.g. Bijan, KC QB1, Chase")

    if query:
        matches = _find_player(snapshot, query)
        if not matches:
            st.warning(f"No players found matching '{query}'")
        else:
            for key, player in matches[:10]:
                name = player.get("name", key)
                team = player.get("team", "")
                pos = player.get("pos", "")
                games = player.get("games", "")
                slot = player.get("slot", "")

                with st.expander(f"{name} ({team} {pos}, {games}G) — {slot}", expanded=len(matches) == 1):
                    metrics = player.get("metrics", {})

                    # Split adjustments vs projections
                    adjs = {k: v for k, v in metrics.items() if "adj" in k.lower() and v is not None and v != 0}
                    projs = {k: v for k, v in metrics.items() if "adj" not in k.lower() and v is not None}

                    if adjs:
                        st.markdown("**Active Adjustments:**")
                        adj_cols = st.columns(min(len(adjs), 4))
                        for i, (k, v) in enumerate(adjs.items()):
                            adj_cols[i % len(adj_cols)].metric(k, v)

                    # Show key stats in columns based on position
                    st.markdown("**Projections (per game):**")
                    if pos == "QB":
                        key_stats = ["PYDS/G", "PTD/G", "INT/G", "CMP/G", "YPA",
                                     "Scrm/G", "Scrm Yds/G", "Sacks/G",
                                     "Des RuYds/G", "RuTD/G", "Fumbles Lost"]
                    elif pos == "RB":
                        key_stats = ["Des RuYds/G", "RuTD/G", "Ru/G", "YPC",
                                     "RecYds/G", "Rec TD/G", "TGT/G", "REC/G",
                                     "Fumbles Lost"]
                    elif pos == "WR":
                        key_stats = ["RecYds/G", "Rec TD/G", "TGT/G", "REC/G",
                                     "YPRR", "Catch Rate", "TGT Share",
                                     "Des RuYds/G", "Fumbles Lost"]
                    else:  # TE
                        key_stats = ["RecYds/G", "Rec TD/G", "TGT/G", "REC/G",
                                     "YPRR", "Catch Rate", "TGT Share",
                                     "Fumbles Lost"]

                    cols = st.columns(min(len(key_stats), 4))
                    shown = 0
                    for stat in key_stats:
                        val = projs.get(stat)
                        if val is not None:
                            cols[shown % len(cols)].metric(stat, f"{val:.2f}" if isinstance(val, float) else val)
                            shown += 1

                    # Full metrics in an expandable table
                    with st.expander("All metrics"):
                        all_data = [{"Metric": k, "Value": v} for k, v in sorted(metrics.items()) if v is not None]
                        st.dataframe(all_data, use_container_width=True, hide_index=True)

            if len(matches) > 10:
                st.caption(f"Showing first 10 of {len(matches)} matches.")


# ─── Tab 3: Player History ───

with tab_history:
    hist_snapshot = _load_snapshot(dates[0], "players") if dates else {}
    if not hist_snapshot:
        st.warning("No snapshots available.")
        st.stop()

    hist_query = st.text_input("Player name", placeholder="e.g. Bijan Robinson", key="hist_query")

    if hist_query:
        # Find the player across all snapshots
        history = []
        player_key = None
        for date in sorted(dates):
            snap = _load_snapshot(date, "players")
            if not snap:
                continue
            if player_key is None:
                # Find the key from first match
                matches = _find_player(snap, hist_query)
                if matches:
                    player_key = matches[0][0]
                else:
                    continue

            if player_key in snap:
                history.append((date, snap[player_key]))

        if not history:
            st.warning(f"No history found for '{hist_query}'")
        else:
            player_name = history[0][1].get("name", hist_query)
            pos = history[0][1].get("pos", "")
            st.subheader(f"{player_name} — Projection History")

            if len(history) < 2:
                st.info("Only one snapshot available. Run the pipeline again tomorrow to start tracking changes.")

            # Pick metrics to chart based on position
            if pos == "QB":
                chart_metrics = ["PYDS/G", "PTD/G", "INT/G", "CMP/G", "Sacks/G", "Scrm Yds/G"]
            elif pos == "RB":
                chart_metrics = ["Des RuYds/G", "RuTD/G", "Ru/G", "RecYds/G", "TGT/G", "Rec TD/G"]
            elif pos == "WR":
                chart_metrics = ["RecYds/G", "Rec TD/G", "TGT/G", "REC/G", "YPRR", "TGT Share"]
            else:
                chart_metrics = ["RecYds/G", "Rec TD/G", "TGT/G", "REC/G", "YPRR", "Catch Rate"]

            # Build history table
            hist_data = {"Date": [d for d, _ in history]}
            hist_data["Team"] = [p.get("team", "") for _, p in history]
            hist_data["Games"] = [p.get("games", "") for _, p in history]
            for metric in chart_metrics:
                hist_data[metric] = [p.get("metrics", {}).get(metric) for _, p in history]

            st.dataframe(hist_data, use_container_width=True, hide_index=True)

            # Show adjustment history
            all_adj_keys = set()
            for _, p in history:
                for k, v in p.get("metrics", {}).items():
                    if "adj" in k.lower() and v is not None and v != 0:
                        all_adj_keys.add(k)

            if all_adj_keys:
                st.markdown("**Adjustment History:**")
                adj_hist = {"Date": [d for d, _ in history]}
                for k in sorted(all_adj_keys):
                    adj_hist[k] = [p.get("metrics", {}).get(k) for _, p in history]
                st.dataframe(adj_hist, use_container_width=True, hide_index=True)

            # Line charts if we have multiple dates
            if len(history) >= 2:
                selected_metrics = st.multiselect(
                    "Chart metrics",
                    chart_metrics,
                    default=chart_metrics[:3],
                )
                if selected_metrics:
                    import pandas as pd
                    chart_df = pd.DataFrame({
                        m: [p.get("metrics", {}).get(m) for _, p in history]
                        for m in selected_metrics
                    }, index=[d for d, _ in history])
                    st.line_chart(chart_df)
