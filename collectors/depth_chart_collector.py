"""OurLads Depth Chart Collector.

Scrapes current NFL depth charts from ourlads.com to get player
positions and depth chart status. Used by the transaction reconciler
to determine if a transaction involves a skill position player.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DEPTH_CHART_DIR = Path(__file__).parent.parent / "data" / "depth_charts"
BASE_URL = "https://www.ourlads.com/nfldepthcharts/depthchart"
USER_AGENT = "NFL-News-Agent/1.0 (personal news aggregator)"

# OurLads position codes -> generic position
POS_MAP = {
    "LWR": "WR", "RWR": "WR", "SWR": "WR", "WR": "WR",
    "TE": "TE", "QB": "QB", "RB": "RB", "FB": "FB",
    "PK": "K", "K": "K",
    "LT": "OL", "LG": "OL", "C": "OL", "RG": "OL", "RT": "OL",
    "DE": "DL", "NT": "DL", "DT": "DL",
    "LOLB": "LB", "ROLB": "LB", "LILB": "LB", "RILB": "LB",
    "MLB": "LB", "WLB": "LB", "SLB": "LB", "MIKE": "LB",
    "SAM": "LB", "WILL": "LB", "ILB": "LB", "OLB": "LB",
    "LCB": "CB", "RCB": "CB", "SCB": "CB", "CB": "CB", "NB": "CB",
    "SS": "S", "FS": "S",
    "PT": "P", "LS": "LS", "H": "P",
    "KR": "RET", "PR": "RET",
    "LDE": "DL", "RDE": "DL", "WDE": "DL", "SDE": "DL",
}

TEAM_SLUGS = [
    "ARZ", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
    "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
    "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG",
    "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS",
]


def _parse_player_name(raw: str) -> str | None:
    """Parse 'Last, First SUFFIX' into 'First Last'."""
    match = re.match(r"([^,]+),\s*(\S+)", raw)
    if match:
        last = match.group(1).strip()
        first = match.group(2).strip()
        return f"{first} {last}"
    return None


def scrape_team(team_slug: str, delay: float = 2.0) -> list[dict]:
    """Scrape depth chart for a single team.

    Returns list of dicts with: name, pos, generic_pos, depth, team
    """
    url = f"{BASE_URL}/{team_slug}"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to scrape %s depth chart: %s", team_slug, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    players = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Check if this looks like a depth chart table (has Pos header)
        header_cells = [th.get_text(strip=True) for th in rows[0].find_all(["td", "th"])]
        if not header_cells or header_cells[0] != "Pos":
            continue

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            pos_raw = cells[0].get_text(strip=True)
            generic_pos = POS_MAP.get(pos_raw, pos_raw)

            # Players in cells 2, 4, 6, ... (1-indexed jersey numbers in between)
            for i in range(2, len(cells), 2):
                name_raw = cells[i].get_text(strip=True)
                if not name_raw:
                    continue

                name = _parse_player_name(name_raw)
                if not name:
                    continue

                depth = i // 2  # 1 = starter, 2 = backup, etc.
                players.append({
                    "name": name,
                    "pos": pos_raw,
                    "generic_pos": generic_pos,
                    "depth": depth,
                    "team": team_slug,
                })

    return players


def scrape_all_teams(delay: float = 2.0) -> dict:
    """Scrape depth charts for all 32 teams.

    Returns dict keyed by normalized player name -> player info.
    """
    all_players = {}
    for team in TEAM_SLUGS:
        logger.info("Scraping %s depth chart...", team)
        players = scrape_team(team, delay)
        for p in players:
            key = p["name"].lower()
            # If player appears on multiple teams, keep the latest
            all_players[key] = p
        time.sleep(delay)

    return all_players


def save_depth_charts(players: dict, date_str: str | None = None):
    """Save depth chart data to a JSON file."""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    DEPTH_CHART_DIR.mkdir(parents=True, exist_ok=True)
    path = DEPTH_CHART_DIR / f"{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(players, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d depth chart entries to %s", len(players), path)
    return path


def load_latest_depth_charts() -> dict | None:
    """Load the most recent depth chart data."""
    if not DEPTH_CHART_DIR.exists():
        return None
    files = sorted(DEPTH_CHART_DIR.glob("*.json"), reverse=True)
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        return json.load(f)


def load_depth_chart_by_date(date_str: str) -> dict | None:
    """Load depth chart data for a specific date."""
    path = DEPTH_CHART_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_depth_chart_dates() -> list[str]:
    """Return available depth chart snapshot dates, newest first."""
    if not DEPTH_CHART_DIR.exists():
        return []
    return sorted(
        [f.stem for f in DEPTH_CHART_DIR.glob("*.json")],
        reverse=True,
    )


def diff_depth_charts(current: dict, previous: dict) -> list[dict]:
    """Compare two depth chart snapshots and return changes.

    Tracks: promotions, demotions, additions, removals, team changes.
    """
    changes = []

    for name, cur in current.items():
        prev = previous.get(name)

        if prev is None:
            # New player on a depth chart
            changes.append({
                "type": "added",
                "name": cur["name"],
                "team": cur["team"],
                "pos": cur["pos"],
                "generic_pos": cur["generic_pos"],
                "depth": cur["depth"],
                "message": f"{cur['name']} added to {cur['team']} depth chart at {cur['pos']} #{cur['depth']}",
            })
            continue

        # Team change
        if cur["team"] != prev["team"]:
            changes.append({
                "type": "team_change",
                "name": cur["name"],
                "pos": cur["pos"],
                "generic_pos": cur["generic_pos"],
                "old_team": prev["team"],
                "new_team": cur["team"],
                "old_depth": prev["depth"],
                "new_depth": cur["depth"],
                "message": f"{cur['name']} ({cur['pos']}) moved from {prev['team']} to {cur['team']} (#{prev['depth']} -> #{cur['depth']})",
            })
            continue

        # Same team — check position or depth change
        if cur["pos"] != prev["pos"]:
            changes.append({
                "type": "position_change",
                "name": cur["name"],
                "team": cur["team"],
                "old_pos": prev["pos"],
                "new_pos": cur["pos"],
                "generic_pos": cur["generic_pos"],
                "depth": cur["depth"],
                "message": f"{cur['name']} ({cur['team']}) position change: {prev['pos']} -> {cur['pos']}",
            })

        if cur["depth"] != prev["depth"] and cur["pos"] == prev["pos"]:
            direction = "promoted" if cur["depth"] < prev["depth"] else "demoted"
            changes.append({
                "type": direction,
                "name": cur["name"],
                "team": cur["team"],
                "pos": cur["pos"],
                "generic_pos": cur["generic_pos"],
                "old_depth": prev["depth"],
                "new_depth": cur["depth"],
                "message": f"{cur['name']} ({cur['team']} {cur['pos']}) {direction}: #{prev['depth']} -> #{cur['depth']}",
            })

    # Removed players
    for name, prev in previous.items():
        if name not in current:
            changes.append({
                "type": "removed",
                "name": prev["name"],
                "team": prev["team"],
                "pos": prev["pos"],
                "generic_pos": prev["generic_pos"],
                "depth": prev["depth"],
                "message": f"{prev['name']} removed from {prev['team']} depth chart ({prev['pos']} #{prev['depth']})",
            })

    return changes


def lookup_player(name: str, depth_charts: dict | None = None) -> dict | None:
    """Look up a player in the depth chart data.

    Returns dict with pos, generic_pos, depth, team or None.
    """
    if depth_charts is None:
        depth_charts = load_latest_depth_charts()
    if not depth_charts:
        return None

    key = name.lower().strip()
    if key in depth_charts:
        return depth_charts[key]

    # Try last name + first initial match
    parts = key.split()
    if len(parts) >= 2:
        for dc_name, data in depth_charts.items():
            dc_parts = dc_name.split()
            if (len(dc_parts) >= 2
                    and parts[-1] == dc_parts[-1]
                    and parts[0][0] == dc_parts[0][0]):
                return data

    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", help="Scrape a single team (e.g. PIT)")
    parser.add_argument("--lookup", help="Look up a player by name")
    args = parser.parse_args()

    if args.lookup:
        info = lookup_player(args.lookup)
        if info:
            print(f"{info['name']} | {info['team']} {info['generic_pos']} ({info['pos']}) | Depth: {info['depth']}")
        else:
            print(f"Player '{args.lookup}' not found in depth charts.")
    elif args.team:
        players = scrape_team(args.team)
        for p in players:
            print(f"  {p['team']} {p['pos']:5} #{p['depth']} {p['name']}")
        print(f"\nTotal: {len(players)} players")
    else:
        print("Scraping all 32 teams...")
        all_players = scrape_all_teams()
        path = save_depth_charts(all_players)
        skill = [p for p in all_players.values() if p["generic_pos"] in ("QB", "RB", "WR", "TE", "K")]
        print(f"Done: {len(all_players)} total, {len(skill)} skill position players")
