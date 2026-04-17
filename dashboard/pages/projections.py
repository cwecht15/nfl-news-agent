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

tab_changes, tab_rankings, tab_weekly, tab_lookup, tab_history, tab_teams = st.tabs([
    "Today's Changes", "Fantasy Rankings", "Weekly Summary",
    "Player Lookup", "Player History", "Team Projections",
])

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


# ─── Tab 2: Fantasy Rankings ───

with tab_rankings:
    rank_date = st.selectbox("Snapshot date", dates, index=0, key="rank_date")
    fantasy = _load_snapshot(rank_date, "fantasy")

    if not fantasy:
        st.warning("No fantasy snapshot for this date. Run the pipeline to generate one.")
    else:
        # Load changelog for fantasy changes on this date
        fantasy_day = [c for c in _load_changelog()
                       if c.get("date") == rank_date and c.get("kind") == "fantasy"]

        # Show adjusted players with rank changes
        adjusted_changes = [c for c in fantasy_day if c.get("type") == "fantasy_change"]

        # We need to reconstruct rank changes from the changelog
        # The changelog stores fantasy changes as individual rows per field
        # Let's load the raw fantasy snapshots and compare directly
        prev_dates = [d for d in sorted(_get_snapshot_dates()) if d < rank_date]
        prev_fantasy = _load_snapshot(prev_dates[-1], "fantasy") if prev_dates else None

        if prev_fantasy:
            # Find players whose adjustments changed (check player snapshots)
            cur_players_snap = _load_snapshot(rank_date, "players")
            prev_players_snap = _load_snapshot(prev_dates[-1], "players") if prev_dates else None

            adjusted_ids = set()
            if cur_players_snap and prev_players_snap:
                for pid, cur_p in cur_players_snap.items():
                    prev_p = prev_players_snap.get(pid)
                    if not prev_p:
                        adjusted_ids.add(pid)
                        continue
                    cur_m = cur_p.get("metrics", {})
                    prev_m = prev_p.get("metrics", {})
                    for k in cur_m:
                        if "adj" in k.lower() and cur_m.get(k) != prev_m.get(k):
                            adjusted_ids.add(pid)
                            break

            if adjusted_ids:
                st.subheader("Rank Changes (Adjusted Players Only)")
                st.caption("Only showing rank movement for players whose projection adjustments changed.")

                for pid in adjusted_ids:
                    cur_f = fantasy.get(pid)
                    prev_f = prev_fantasy.get(pid)
                    if not cur_f or not prev_f:
                        continue

                    cur_rank = cur_f.get("pos_rank", "")
                    prev_rank = prev_f.get("pos_rank", "")
                    if cur_rank == prev_rank:
                        continue

                    name = cur_f.get("name", pid)
                    pos = cur_f.get("pos", "")
                    team = cur_f.get("team", "")
                    cur_ppr = cur_f.get("ppr")
                    prev_ppr = prev_f.get("ppr")
                    cur_pprg = cur_f.get("ppr_g")
                    prev_pprg = prev_f.get("ppr_g")

                    # Rank direction
                    import re
                    cur_num = int(m.group(1)) if (m := re.search(r"(\d+)$", cur_rank)) else None
                    prev_num = int(m.group(1)) if (m := re.search(r"(\d+)$", prev_rank)) else None
                    if cur_num and prev_num:
                        direction = prev_num - cur_num  # positive = moved up
                        arrow = f"{'up' if direction > 0 else 'down'} {abs(direction)}"
                    else:
                        arrow = ""

                    with st.expander(f"{name} ({team} {pos}) — {prev_rank} -> {cur_rank} ({arrow})", expanded=True):
                        cols = st.columns(4)
                        cols[0].metric("Rank", cur_rank, f"{direction:+d}" if cur_num and prev_num else None)
                        if cur_ppr is not None and prev_ppr is not None:
                            cols[1].metric("PPR", f"{cur_ppr:.1f}", f"{cur_ppr - prev_ppr:+.1f}")
                        if cur_pprg is not None and prev_pprg is not None:
                            cols[2].metric("PPR/G", f"{cur_pprg:.1f}", f"{cur_pprg - prev_pprg:+.1f}")
                        cur_half = cur_f.get("half_ppr")
                        prev_half = prev_f.get("half_ppr")
                        if cur_half is not None and prev_half is not None:
                            cols[3].metric("Half PPR", f"{cur_half:.1f}", f"{cur_half - prev_half:+.1f}")

                        # Show who they were ranked near before and after
                        def _neighbors(snap, rank_str, pos, exclude_pid, window=2):
                            rank_num = int(m.group(1)) if (m := re.search(r"(\d+)$", rank_str)) else None
                            if not rank_num:
                                return []
                            result = []
                            for p, d in snap.items():
                                if p == exclude_pid or d.get("pos", "") != pos:
                                    continue
                                other = int(m.group(1)) if (m := re.search(r"(\d+)$", d.get("pos_rank", ""))) else None
                                if other and abs(other - rank_num) <= window:
                                    result.append({"Name": d["name"], "Rank": d["pos_rank"],
                                                   "PPR": d.get("ppr"), "PPR/G": d.get("ppr_g")})
                            result.sort(key=lambda x: int(m.group(1)) if (m := re.search(r"(\d+)$", x["Rank"])) else 999)
                            return result

                        col_before, col_after = st.columns(2)
                        with col_before:
                            st.markdown(f"**Previously near ({prev_rank}):**")
                            neighbors_old = _neighbors(prev_fantasy, prev_rank, pos, pid)
                            if neighbors_old:
                                st.dataframe(neighbors_old, use_container_width=True, hide_index=True)
                        with col_after:
                            st.markdown(f"**Now near ({cur_rank}):**")
                            neighbors_new = _neighbors(fantasy, cur_rank, pos, pid)
                            if neighbors_new:
                                st.dataframe(neighbors_new, use_container_width=True, hide_index=True)

            else:
                st.info("No adjustment changes detected — no rank movements to show.")
        else:
            st.info("Need at least 2 snapshots to show rank changes.")

        # Full rankings table
        st.subheader("Current Rankings")
        pos_filter = st.multiselect("Filter by position", ["QB", "RB", "WR", "TE"], key="rank_pos")

        rank_data = []
        for pid, data in fantasy.items():
            if pos_filter and data.get("pos") not in pos_filter:
                continue
            rank_data.append({
                "Rank": data.get("pos_rank", ""),
                "Name": data.get("name", ""),
                "Team": data.get("team", ""),
                "Pos": data.get("pos", ""),
                "PPR": data.get("ppr"),
                "Half PPR": data.get("half_ppr"),
                "Games": data.get("games"),
                "PPR/G": data.get("ppr_g"),
            })

        import re as _re
        rank_data.sort(key=lambda x: int(m.group(1)) if (m := _re.search(r"(\d+)$", x.get("Rank", ""))) else 9999)
        st.dataframe(rank_data, use_container_width=True, hide_index=True)


# ─── Tab 3: Weekly Summary ───

with tab_weekly:
    st.subheader("Weekly Projection Summary")

    if len(dates) < 2:
        st.info("Need at least 2 snapshots to generate a weekly summary.")
    else:
        # Find dates in the last 7 days
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.now() - _td(days=7)).strftime("%Y-%m-%d")
        week_dates = sorted([d for d in dates if d >= cutoff])

        if len(week_dates) < 2:
            st.info("Not enough snapshots in the last 7 days for a weekly summary.")
        else:
            oldest = week_dates[0]
            newest = week_dates[-1]
            st.markdown(f"Comparing **{oldest}** to **{newest}** ({len(week_dates)} snapshots)")

            old_fantasy = _load_snapshot(oldest, "fantasy")
            new_fantasy = _load_snapshot(newest, "fantasy")
            old_players = _load_snapshot(oldest, "players")
            new_players = _load_snapshot(newest, "players")

            if old_fantasy and new_fantasy:
                # Find all adjustment changes across the week
                adjusted_ids = set()
                if old_players and new_players:
                    for pid, cur_p in new_players.items():
                        prev_p = old_players.get(pid)
                        if not prev_p:
                            continue
                        cur_m = cur_p.get("metrics", {})
                        prev_m = prev_p.get("metrics", {})
                        for k in cur_m:
                            if "adj" in k.lower() and cur_m.get(k) != prev_m.get(k):
                                adjusted_ids.add(pid)
                                break

                # Biggest PPR movers (adjusted players only)
                movers = []
                for pid in adjusted_ids:
                    cur_f = new_fantasy.get(pid)
                    old_f = old_fantasy.get(pid)
                    if not cur_f or not old_f:
                        continue
                    cur_ppr = cur_f.get("ppr")
                    old_ppr = old_f.get("ppr")
                    if cur_ppr is None or old_ppr is None:
                        continue
                    delta = cur_ppr - old_ppr
                    if abs(delta) < 0.1:
                        continue

                    import re as _re2
                    cur_rank = cur_f.get("pos_rank", "")
                    old_rank = old_f.get("pos_rank", "")
                    cur_num = int(m.group(1)) if (m := _re2.search(r"(\d+)$", cur_rank)) else None
                    old_num = int(m.group(1)) if (m := _re2.search(r"(\d+)$", old_rank)) else None
                    rank_delta = old_num - cur_num if cur_num and old_num else None

                    movers.append({
                        "Name": cur_f.get("name", pid),
                        "Pos": cur_f.get("pos", ""),
                        "Team": cur_f.get("team", ""),
                        "PPR (old)": f"{old_ppr:.1f}",
                        "PPR (new)": f"{cur_ppr:.1f}",
                        "PPR Delta": f"{delta:+.1f}",
                        "PPR/G": f"{cur_f.get('ppr_g', 0):.1f}",
                        "Rank": f"{old_rank} -> {cur_rank}",
                        "Rank Delta": f"{rank_delta:+d}" if rank_delta else "",
                        "_sort": abs(delta),
                    })

                if movers:
                    movers.sort(key=lambda x: x["_sort"], reverse=True)

                    # Risers
                    risers = [m for m in movers if float(m["PPR Delta"]) > 0]
                    fallers = [m for m in movers if float(m["PPR Delta"]) < 0]

                    if risers:
                        st.subheader(f"Risers ({len(risers)})")
                        display_risers = [{k: v for k, v in m.items() if k != "_sort"} for m in risers]
                        st.dataframe(display_risers, use_container_width=True, hide_index=True)

                    if fallers:
                        st.subheader(f"Fallers ({len(fallers)})")
                        display_fallers = [{k: v for k, v in m.items() if k != "_sort"} for m in fallers]
                        st.dataframe(display_fallers, use_container_width=True, hide_index=True)

                    if not risers and not fallers:
                        st.info("No significant PPR changes among adjusted players this week.")
                else:
                    st.info("No adjustment changes this week.")

                # Adjustment summary
                if adjusted_ids and old_players and new_players:
                    st.subheader("Adjustments Made This Week")
                    adj_summary = []
                    for pid in adjusted_ids:
                        cur_p = new_players.get(pid, {})
                        prev_p = old_players.get(pid, {})
                        cur_m = cur_p.get("metrics", {})
                        prev_m = prev_p.get("metrics", {})
                        for k in sorted(cur_m.keys()):
                            if "adj" in k.lower() and cur_m.get(k) != prev_m.get(k):
                                adj_summary.append({
                                    "Player": cur_p.get("name", pid),
                                    "Pos": cur_p.get("pos", ""),
                                    "Team": cur_p.get("team", ""),
                                    "Adjustment": k,
                                    "Old": prev_m.get(k),
                                    "New": cur_m.get(k),
                                })
                    if adj_summary:
                        st.dataframe(adj_summary, use_container_width=True, hide_index=True)
            else:
                st.warning("Missing fantasy snapshots for weekly comparison.")


# ─── Tab 4: Player Lookup ───

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

                    # Fantasy points from Preseason_Projections
                    fantasy_snap = _load_snapshot(lookup_date, "fantasy")
                    if fantasy_snap and key in fantasy_snap:
                        fp = fantasy_snap[key]
                        st.markdown("**Fantasy Points:**")
                        fp_cols = st.columns(5)
                        fp_cols[0].metric("PPR", f"{fp['ppr']:.1f}" if fp.get("ppr") is not None else "-")
                        fp_cols[1].metric("Half PPR", f"{fp['half_ppr']:.1f}" if fp.get("half_ppr") is not None else "-")
                        fp_cols[2].metric("PPR/G", f"{fp['ppr_g']:.1f}" if fp.get("ppr_g") is not None else "-")
                        fp_cols[3].metric("Games", fp.get("games", "-"))
                        fp_cols[4].metric("Pos Rank", fp.get("pos_rank", "-"))

                    # Full metrics in an expandable table
                    with st.expander("All metrics"):
                        all_data = [{"Metric": k, "Value": v} for k, v in sorted(metrics.items()) if v is not None]
                        st.dataframe(all_data, use_container_width=True, hide_index=True)

            if len(matches) > 10:
                st.caption(f"Showing first 10 of {len(matches)} matches.")


# ─── Tab 5: Player History ───

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


# ─── Tab 4: Team Projections ───

with tab_teams:
    team_date = st.selectbox("Snapshot date", dates, index=0, key="team_date")
    team_snapshot = _load_snapshot(team_date, "teams")

    if not team_snapshot:
        st.warning("No team snapshot for this date.")
        st.stop()

    team_list = sorted(team_snapshot.keys())
    selected_team = st.selectbox("Select team", team_list)

    if selected_team and selected_team in team_snapshot:
        metrics = team_snapshot[selected_team].get("metrics", {})

        # Show adjustments at the top
        adjs = {k: v for k, v in metrics.items() if "adj" in k.lower() and v is not None and v != 0}
        if adjs:
            st.subheader("Active Adjustments")
            adj_cols = st.columns(min(len(adjs), 4))
            for i, (k, v) in enumerate(adjs.items()):
                adj_cols[i % len(adj_cols)].metric(k, v)

        # Key stats
        st.subheader("Team Projections")
        key_team_stats = [
            "Plays/G", "Pass Rate", "DB/G", "Des RuAtt/G",
            "Proj Drives/G", "TDs/Drive",
            "Pass Yds/G", "Pass TDs/G", "Pass YPA", "CMP Rate", "INTs/G",
            "Des Rush Yds/G", "Des YPC", "Des Rush TDs/G",
            "Sacks Allowed/G", "Scrm/G", "Scrm Yds/G",
        ]

        cols = st.columns(4)
        shown = 0
        for stat in key_team_stats:
            val = metrics.get(stat)
            if val is not None:
                fmt = f"{val:.1f}%" if "rate" in stat.lower() else (f"{val:.2f}" if isinstance(val, float) else str(val))
                cols[shown % 4].metric(stat, fmt)
                shown += 1

        # Positional route/target shares
        share_stats = {
            k: v for k, v in metrics.items()
            if any(x in k for x in ["Routes/DB", "TGT Share", "Share"])
            and v is not None and "adj" not in k.lower()
        }
        if share_stats:
            st.subheader("Positional Shares")
            share_cols = st.columns(min(len(share_stats), 4))
            for i, (k, v) in enumerate(share_stats.items()):
                share_cols[i % len(share_cols)].metric(k, f"{v:.1f}%")

        # Full metrics
        with st.expander("All metrics"):
            all_data = [{"Metric": k, "Value": v} for k, v in sorted(metrics.items()) if v is not None]
            st.dataframe(all_data, use_container_width=True, hide_index=True)

    # Team history across snapshots
    if len(dates) >= 2 and selected_team:
        st.subheader(f"{selected_team} — History")

        team_history = []
        for d in sorted(dates):
            snap = _load_snapshot(d, "teams")
            if snap and selected_team in snap:
                team_history.append((d, snap[selected_team]))

        if len(team_history) >= 2:
            history_metrics = ["Plays/G", "Pass Rate", "Pass Yds/G", "Pass TDs/G",
                               "Des Rush Yds/G", "Des Rush TDs/G", "TDs/Drive"]

            hist_data = {"Date": [d for d, _ in team_history]}
            for m in history_metrics:
                hist_data[m] = [t.get("metrics", {}).get(m) for _, t in team_history]
            st.dataframe(hist_data, use_container_width=True, hide_index=True)

            # Adjustment history
            all_team_adj = set()
            for _, t in team_history:
                for k, v in t.get("metrics", {}).items():
                    if "adj" in k.lower() and v is not None and v != 0:
                        all_team_adj.add(k)
            if all_team_adj:
                st.markdown("**Adjustment History:**")
                adj_hist = {"Date": [d for d, _ in team_history]}
                for k in sorted(all_team_adj):
                    adj_hist[k] = [t.get("metrics", {}).get(k) for _, t in team_history]
                st.dataframe(adj_hist, use_container_width=True, hide_index=True)

            selected_team_metrics = st.multiselect(
                "Chart metrics",
                history_metrics,
                default=history_metrics[:3],
                key="team_chart",
            )
            if selected_team_metrics:
                import pandas as pd
                chart_df = pd.DataFrame({
                    m: [t.get("metrics", {}).get(m) for _, t in team_history]
                    for m in selected_team_metrics
                }, index=[d for d, _ in team_history])
                st.line_chart(chart_df)
