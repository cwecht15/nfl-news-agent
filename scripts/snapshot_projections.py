"""Projection Snapshot Tracker.

Pulls player and team projections from the Google Sheet, saves daily
snapshots, and logs changes to adjustment columns and key projections.

Usage:
    python scripts/snapshot_projections.py           # Take snapshot + log changes
    python scripts/snapshot_projections.py --diff     # Show changes since last snapshot
    python scripts/snapshot_projections.py --history PLAYER  # Show a player's history
"""

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# --- Config ---

SPREADSHEET_ID = "1bQtJKplmdOAEmKA1zCdSe8TeVFdOqO3fd-vUgtP1dH0"
SERVICE_ACCOUNT_KEY = Path(r"C:\Users\cwech\Documents\Football\Keys\fp-data-357113-a6174bb87054.json")
SNAPSHOT_DIR = PROJECT_ROOT / "data" / "projections"

# Player sheet config (PreSeas_Working_Plyr_Proj)
PLAYER_SHEET = "PreSeas_Working_Plyr_Proj"
PLAYER_HEADER_ROW = 8
PLAYER_DATA_START = 9
PLAYER_ID_COL = 3       # 0-indexed: col D = Player ID
PLAYER_NAME_COL = 5      # col F = Name
PLAYER_TEAM_COL = 0      # col A = Team
PLAYER_POS_COL = 6       # col G = Position
PLAYER_GAMES_COL = 7     # col H = Games
PLAYER_SLOT_COL = 4      # col E = Slot (e.g. "ARZ QB1")

# Player metric columns are built dynamically from the header row.
# Range: col H (idx 7) through col GW (idx 204) — Games to Fumbles Lost.
PLAYER_COL_START = 7    # col H (0-indexed)
PLAYER_COL_END = 205    # col GW + 1 (exclusive)

# Team sheet config (Working_Tm_Proj)
TEAM_SHEET = "Working_Tm_Proj"
# Team columns are read dynamically from the header row (row 1).
# Columns "Team" and "Year" are used for identification, all others are tracked.


SKIP_HEADERS = {"2025", "2024", "Career", "L10", "L5", "Career Expected"}


def _build_player_col_map(header_row: list[str]) -> list[tuple[int, str, bool]]:
    """Build a map of (col_index, unique_label, is_adjustment) from the header row.

    Skips historical/reference columns (2025, Career, L10, L5, 2024)
    and Career Expected. Disambiguates duplicate header names (e.g.
    two "YPA Adj" columns) by prefixing with the preceding named column.
    """
    from collections import Counter

    # First pass: collect non-skipped names and find duplicates
    raw_names = []
    current_group = ""
    entries = []

    for i in range(PLAYER_COL_START, min(PLAYER_COL_END, len(header_row))):
        raw = header_row[i].strip()
        if not raw or raw in SKIP_HEADERS:
            # Still update group tracker for non-skip named columns
            continue

        is_adj = "adj" in raw.lower()

        # Track the current "group" (most recent non-generic named column)
        # Adj columns belong to the group before them
        if not is_adj:
            current_group = raw

        raw_names.append(raw)
        entries.append((i, raw, is_adj, current_group))

    dupes = {k for k, v in Counter(raw_names).items() if v > 1}

    # Second pass: build unique labels
    # Also track which broader section we're in for deeper disambiguation
    col_map = []
    section = ""  # higher-level section name (e.g. "Scramble", "Pass")
    for i, raw, is_adj, group in entries:
        # Track broad sections from per-game output columns
        raw_lower = raw.lower()
        if "scramble" in raw_lower or "scrm" in raw_lower:
            section = "Scramble"
        elif "aimed" in raw_lower or "cmp" in raw_lower or "int " in raw_lower or raw_lower == "ypa" or "pyds" in raw_lower or "ptd" in raw_lower:
            section = "Pass"
        elif "rr/" in raw_lower or "tgt" in raw_lower or "tprr" in raw_lower or "catch" in raw_lower or "yprr" in raw_lower or "rec" in raw_lower:
            section = "Rec"
        elif "des rush" in raw_lower or "des ru" in raw_lower or raw_lower == "ypc" or "rutd" in raw_lower:
            section = "Rush"
        elif "qbsnk" in raw_lower or "qb sneak" in raw_lower:
            section = "QBSNK"
        elif "fum" in raw_lower:
            section = "Fum"

        if raw in dupes and group and group != raw:
            raw_words = set(raw.lower().split())
            prefix_words = [w for w in group.split() if w.lower() not in raw_words]
            if prefix_words:
                label = " ".join(prefix_words) + " " + raw
            else:
                # Group name is fully contained in raw — use section instead
                label = f"{section} {raw}" if section else raw
        else:
            label = raw
        col_map.append((i, label, is_adj))

    return col_map


def _get_client() -> gspread.Client:
    """Authenticate with the Google Sheets API."""
    creds = Credentials.from_service_account_file(
        str(SERVICE_ACCOUNT_KEY),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    return gspread.authorize(creds)


def _safe_float(val: str):
    """Parse a cell value, returning the string if not numeric."""
    if not val or val.strip() in ("", "N/A", "-", "#N/A", "#REF!", "#DIV/0!"):
        return None
    cleaned = val.strip().replace("%", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return val.strip()


def _snapshot_path(date_str: str, kind: str) -> Path:
    """Return path for a snapshot file."""
    d = SNAPSHOT_DIR / date_str
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.json"


def _changelog_path() -> Path:
    """Return path for the running changelog CSV."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return SNAPSHOT_DIR / "changelog.csv"


def _latest_snapshot(kind: str) -> dict | None:
    """Load the most recent snapshot of the given kind."""
    if not SNAPSHOT_DIR.exists():
        return None
    dates = sorted(
        [d.name for d in SNAPSHOT_DIR.iterdir() if d.is_dir()],
        reverse=True,
    )
    for date in dates:
        path = SNAPSHOT_DIR / date / f"{kind}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return None


def snapshot_players(gc: gspread.Client, date_str: str) -> dict:
    """Take a snapshot of player projections."""
    sheet = gc.open_by_key(SPREADSHEET_ID)
    ws = sheet.worksheet(PLAYER_SHEET)
    all_rows = ws.get_all_values()

    # Build column map from header row
    header_row = all_rows[PLAYER_HEADER_ROW - 1]
    col_map = _build_player_col_map(header_row)

    players = {}
    for row in all_rows[PLAYER_DATA_START - 1:]:
        if len(row) <= PLAYER_POS_COL:
            continue
        pos = row[PLAYER_POS_COL].strip()
        if pos not in ("QB", "RB", "WR", "TE"):
            continue

        player_id = row[PLAYER_ID_COL].strip() if len(row) > PLAYER_ID_COL else ""
        name = row[PLAYER_NAME_COL].strip() if len(row) > PLAYER_NAME_COL else ""
        team = row[PLAYER_TEAM_COL].strip()
        slot = row[PLAYER_SLOT_COL].strip() if len(row) > PLAYER_SLOT_COL else ""
        games = row[PLAYER_GAMES_COL].strip() if len(row) > PLAYER_GAMES_COL else ""

        if not name and not player_id:
            continue

        key = player_id or slot  # use slot as fallback key for unnamed players

        tracked = {}
        for col_idx, label, is_adj in col_map:
            if col_idx < len(row):
                tracked[label] = _safe_float(row[col_idx])

        players[key] = {
            "player_id": player_id,
            "name": name,
            "team": team,
            "pos": pos,
            "slot": slot,
            "games": _safe_float(games),
            "metrics": tracked,
        }

    path = _snapshot_path(date_str, "players")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(players, f, indent=2, ensure_ascii=False)

    logger.info("Saved player snapshot: %d players to %s", len(players), path)
    return players


def snapshot_teams(gc: gspread.Client, date_str: str) -> dict:
    """Take a snapshot of team projections (CurSeas row only)."""
    sheet = gc.open_by_key(SPREADSHEET_ID)
    ws = sheet.worksheet(TEAM_SHEET)
    all_rows = ws.get_all_values()

    # Build column map from header row
    headers = all_rows[0]
    team_col_map = []
    for i, h in enumerate(headers):
        h = h.strip()
        if h and h not in ("Team", "Year"):
            is_adj = "adj" in h.lower()
            team_col_map.append((i, h, is_adj))

    teams = {}
    for row in all_rows[1:]:  # skip header
        if len(row) < 3:
            continue
        team = row[0].strip()
        year = row[1].strip()
        if year != "CurSeas":
            continue

        tracked = {}
        for col_idx, label, is_adj in team_col_map:
            if col_idx < len(row):
                tracked[label] = _safe_float(row[col_idx])

        teams[team] = {"metrics": tracked}

    path = _snapshot_path(date_str, "teams")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(teams, f, indent=2, ensure_ascii=False)

    logger.info("Saved team snapshot: %d teams to %s", len(teams), path)
    return teams


def diff_snapshots(current: dict, previous: dict, kind: str) -> list[dict]:
    """Compare two snapshots and return a list of changes."""
    changes = []

    for key, cur_data in current.items():
        prev_data = previous.get(key)
        if prev_data is None:
            # New entry
            label = cur_data.get("name") or cur_data.get("team", key)
            changes.append({
                "key": key,
                "label": label,
                "type": "added",
                "details": f"New {kind} entry",
            })
            continue

        cur_metrics = cur_data.get("metrics", {})
        prev_metrics = prev_data.get("metrics", {})

        # Check for team change (players only)
        if kind == "player":
            if cur_data.get("team") != prev_data.get("team"):
                changes.append({
                    "key": key,
                    "label": cur_data.get("name", key),
                    "type": "team_change",
                    "metric": "Team",
                    "old": prev_data.get("team"),
                    "new": cur_data.get("team"),
                })
            if cur_data.get("games") != prev_data.get("games"):
                changes.append({
                    "key": key,
                    "label": cur_data.get("name", key),
                    "type": "games_change",
                    "metric": "Games",
                    "old": prev_data.get("games"),
                    "new": cur_data.get("games"),
                })

        for metric, cur_val in cur_metrics.items():
            prev_val = prev_metrics.get(metric)
            if cur_val != prev_val and cur_val is not None:
                changes.append({
                    "key": key,
                    "label": cur_data.get("name") or key,
                    "type": "metric_change",
                    "metric": metric,
                    "old": prev_val,
                    "new": cur_val,
                })

    # Check for removed entries
    for key in previous:
        if key not in current:
            prev_data = previous[key]
            changes.append({
                "key": key,
                "label": prev_data.get("name") or prev_data.get("team", key),
                "type": "removed",
                "details": f"Removed from {kind}",
            })

    return changes


def write_changelog(changes: list[dict], date_str: str, kind: str):
    """Append changes to the running changelog CSV."""
    if not changes:
        return

    path = _changelog_path()
    write_header = not path.exists()

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "date", "kind", "key", "label", "type",
                "metric", "old_value", "new_value", "details",
            ])
        for change in changes:
            writer.writerow([
                date_str,
                kind,
                change.get("key", ""),
                change.get("label", ""),
                change.get("type", ""),
                change.get("metric", ""),
                change.get("old", ""),
                change.get("new", ""),
                change.get("details", ""),
            ])

    logger.info("Logged %d %s changes to %s", len(changes), kind, path)


def run_snapshot():
    """Take snapshots and log changes."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    logger.info("Connecting to Google Sheets...")
    gc = _get_client()

    # Players
    prev_players = _latest_snapshot("players")
    cur_players = snapshot_players(gc, date_str)
    if prev_players:
        player_changes = diff_snapshots(cur_players, prev_players, "player")
        adj_changes = [c for c in player_changes if "Adj" in c.get("metric", "")]
        proj_changes = [c for c in player_changes if c.get("type") == "metric_change" and "Adj" not in c.get("metric", "")]
        other_changes = [c for c in player_changes if c.get("type") != "metric_change"]
        logger.info(
            "Player changes: %d adjustment tweaks, %d projection shifts, %d other (adds/removes/team)",
            len(adj_changes), len(proj_changes), len(other_changes),
        )
        write_changelog(player_changes, date_str, "player")
    else:
        logger.info("No previous player snapshot — this is the baseline.")

    # Teams
    prev_teams = _latest_snapshot("teams")
    cur_teams = snapshot_teams(gc, date_str)
    if prev_teams:
        team_changes = diff_snapshots(cur_teams, prev_teams, "team")
        logger.info("Team changes: %d", len(team_changes))
        write_changelog(team_changes, date_str, "team")
    else:
        logger.info("No previous team snapshot — this is the baseline.")


def show_diff():
    """Show changes since the last snapshot."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    date_str = datetime.now().strftime("%Y-%m-%d")
    gc = _get_client()

    prev_players = _latest_snapshot("players")
    if not prev_players:
        print("No previous snapshot to compare against.")
        return

    cur_players = snapshot_players(gc, date_str)
    changes = diff_snapshots(cur_players, prev_players, "player")

    if not changes:
        print("No player projection changes detected.")
        return

    adj_changes = [c for c in changes if "Adj" in c.get("metric", "")]
    proj_changes = [c for c in changes if c.get("type") == "metric_change" and "Adj" not in c.get("metric", "")]
    other_changes = [c for c in changes if c.get("type") != "metric_change"]

    if adj_changes:
        print("\n=== ADJUSTMENT TWEAKS (%d) ===" % len(adj_changes))
        for c in adj_changes:
            print("  %s | %s: %s -> %s" % (c["label"], c["metric"], c["old"], c["new"]))

    if other_changes:
        print("\n=== ROSTER / GAMES CHANGES (%d) ===" % len(other_changes))
        for c in other_changes:
            if c["type"] in ("team_change", "games_change"):
                print("  %s | %s: %s -> %s" % (c["label"], c["metric"], c["old"], c["new"]))
            else:
                print("  %s | %s" % (c["label"], c.get("details", c["type"])))

    if proj_changes:
        print("\n=== PROJECTION SHIFTS (%d) ===" % len(proj_changes))
        for c in proj_changes[:50]:
            print("  %s | %s: %s -> %s" % (c["label"], c["metric"], c["old"], c["new"]))
        if len(proj_changes) > 50:
            print("  ... and %d more" % (len(proj_changes) - 50))


def show_history(player_query: str):
    """Show a player's projection history across snapshots."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not SNAPSHOT_DIR.exists():
        print("No snapshots found.")
        return

    dates = sorted(d.name for d in SNAPSHOT_DIR.iterdir() if d.is_dir())
    query_lower = player_query.lower()

    # Find matching player across snapshots
    history = []
    for date in dates:
        path = SNAPSHOT_DIR / date / "players.json"
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key, player in data.items():
            name = player.get("name", "").lower()
            if query_lower in name or query_lower == key:
                history.append((date, player))
                break

    if not history:
        print("No snapshots found for '%s'" % player_query)
        return

    print("=== Projection History: %s ===" % history[0][1].get("name", player_query))
    print()

    # Show key metrics across dates
    key_metrics = [
        "PYDS/G", "PTD/G", "INT/G", "CMP/G",  # passing
        "RecYds/G", "Rec TD/G", "TGT/G", "REC/G",  # receiving
        "Des RuYds/G", "RuTD/G", "Ru/G",  # rushing
    ]

    # Header
    dates_str = [d for d, _ in history]
    print("%-15s " % "Metric" + "  ".join("%-10s" % d for d in dates_str))
    print("-" * (16 + 12 * len(dates_str)))

    for metric in key_metrics:
        values = []
        has_data = False
        for _, player in history:
            val = player.get("metrics", {}).get(metric)
            if val is not None:
                has_data = True
                values.append("%-10s" % str(val))
            else:
                values.append("%-10s" % "-")
        if has_data:
            print("%-15s %s" % (metric, "  ".join(values)))

    # Show adjustments
    print()
    print("Adjustments:")
    # Collect all adj metric names from the first snapshot
    all_metrics = list(history[0][1].get("metrics", {}).keys())
    adj_metrics = [m for m in all_metrics if "adj" in m.lower()]
    for metric in adj_metrics:
        values = []
        has_nonzero = False
        for _, player in history:
            val = player.get("metrics", {}).get(metric)
            if val is not None and val != 0:
                has_nonzero = True
            values.append("%-10s" % str(val if val is not None else "-"))
        if has_nonzero:
            print("  %-15s %s" % (metric, "  ".join(values)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Projection snapshot tracker")
    parser.add_argument("--diff", action="store_true", help="Show changes since last snapshot")
    parser.add_argument("--history", type=str, help="Show projection history for a player")
    args = parser.parse_args()

    if args.diff:
        show_diff()
    elif args.history:
        show_history(args.history)
    else:
        run_snapshot()
