"""Transaction Reconciler.

Cross-references collected transactions against projection data to flag
players whose team changes aren't reflected in the projections.

Only checks QB, RB, WR, TE, K positions. Allows persistent overrides
for players intentionally not projected.
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

OVERRIDES_PATH = PROJECT_ROOT / "data" / "projections" / "transaction_overrides.json"

# Map news pipeline team abbreviations to projection abbreviations
NEWS_TO_PROJ_TEAM = {
    "ARI": "ARZ",
    "BAL": "BLT",
    "CLE": "CLV",
    "HOU": "HST",
    "LAR": "LA",
}

PROJ_TO_NEWS_TEAM = {v: k for k, v in NEWS_TO_PROJ_TEAM.items()}

TRACKED_POSITIONS = {"QB", "RB", "WR", "TE", "K"}


def _normalize_team(news_abbr: str) -> str:
    """Convert a news pipeline team abbreviation to projection format."""
    return NEWS_TO_PROJ_TEAM.get(news_abbr, news_abbr)


def _news_team(proj_abbr: str) -> str:
    """Convert a projection team abbreviation to news format."""
    return PROJ_TO_NEWS_TEAM.get(proj_abbr, proj_abbr)


def _extract_player_name(title: str) -> str:
    """Extract player name from transaction title.

    Examples:
        'Cameron Sample: Bengals -> 49ers (Unrestricted Free Agent Signing)' -> 'Cameron Sample'
        'Kimani Vidal: Chargers (Exclusive Rights Signing)' -> 'Kimani Vidal'
    """
    # Strip everything after the colon
    name = title.split(":")[0].strip()
    # Remove brackets like [Schefter]
    name = re.sub(r"^\[.*?\]\s*", "", name)
    return name


def _extract_new_team(title: str, teams: list[str]) -> str | None:
    """Extract the destination team from a transaction.

    For 'Bengals -> 49ers', the new team is the last one in the teams list.
    For 'Chargers (Exclusive Rights Signing)', it's the only team.
    """
    if not teams:
        return None
    # The last team in the list is typically the destination
    return teams[-1]


def _normalize_name(name: str) -> str:
    """Normalize a name for fuzzy matching."""
    # Remove team suffix from projection names like "James Conner ARZ"
    name = re.sub(r"\s+[A-Z]{2,3}$", "", name.strip())
    return name.lower().strip()


def _find_player_in_projections(
    player_name: str, players: dict
) -> list[tuple[str, dict]]:
    """Find matching players in projection data by name.

    Only returns matches for tracked positions (QB, RB, WR, TE, K).
    Requires exact first+last name match to avoid false positives.
    """
    norm_query = _normalize_name(player_name)
    query_parts = norm_query.split()
    if len(query_parts) < 2:
        return []  # Skip single-word names (likely parsing errors)

    matches = []
    for pid, data in players.items():
        pos = data.get("pos", "")
        if pos not in TRACKED_POSITIONS:
            continue
        proj_name = _normalize_name(data.get("name", ""))
        if not proj_name:
            continue
        # Require exact first and last name match
        proj_parts = proj_name.split()
        if (len(proj_parts) >= 2
                and query_parts[0] == proj_parts[0]
                and query_parts[-1] == proj_parts[-1]):
            matches.append((pid, data))
    return matches


def load_overrides() -> dict:
    """Load transaction overrides (players intentionally not projected)."""
    if OVERRIDES_PATH.exists():
        with open(OVERRIDES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_overrides(overrides: dict):
    """Save transaction overrides."""
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, ensure_ascii=False)


def _override_key(player_name: str, transaction: str) -> str:
    """Build a unique key for a specific transaction dismissal."""
    return f"{_normalize_name(player_name)}::{transaction.strip()}"


def add_override(player_name: str, transaction: str, reason: str = ""):
    """Dismiss a specific transaction for a player."""
    overrides = load_overrides()
    key = _override_key(player_name, transaction)
    overrides[key] = {
        "player": player_name,
        "transaction": transaction,
        "reason": reason,
        "added": datetime.now().isoformat(),
    }
    save_overrides(overrides)


def remove_override(key: str):
    """Remove a specific transaction override by key."""
    overrides = load_overrides()
    if key in overrides:
        del overrides[key]
        save_overrides(overrides)


def _collect_recent_transactions(days: int = 30) -> list[dict]:
    """Gather all transactions from recent raw data."""
    raw_dir = PROJECT_ROOT / "data" / "raw"
    if not raw_dir.exists():
        return []

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    transactions = []

    for date_dir in sorted(raw_dir.iterdir()):
        if not date_dir.is_dir() or date_dir.name < cutoff:
            continue
        web_path = date_dir / "web.json"
        if not web_path.exists():
            continue
        with open(web_path, encoding="utf-8") as f:
            items = json.load(f)
        for item in items:
            if item.get("category") == "transaction":
                item["_date"] = date_dir.name
                transactions.append(item)

    return transactions


def reconcile(days: int = 30) -> list[dict]:
    """Check recent transactions against projection data.

    Returns a list of unresolved transaction alerts:
    - player moved to a new team but projections still show old team
    - player signed but not in projections at all
    - player in projections but on wrong team

    Excludes overridden players.
    """
    from scripts.snapshot_projections import _latest_snapshot

    players = _latest_snapshot("players")
    if not players:
        logger.warning("No player projection snapshot available.")
        return []

    transactions = _collect_recent_transactions(days)
    overrides = load_overrides()

    # Load depth chart data for position filtering
    try:
        from collectors.depth_chart_collector import lookup_player as dc_lookup
    except Exception:
        dc_lookup = None
        logger.warning("Depth chart collector not available — position filtering disabled.")

    alerts = []
    seen_players = set()  # dedupe by player name

    for txn in transactions:
        player_name = _extract_player_name(txn.get("title", ""))
        if not player_name or _normalize_name(player_name) in seen_players:
            continue
        # Skip parsing artifacts (repeated team names, single words, etc.)
        name_parts = player_name.strip().split()
        if len(name_parts) < 2:
            continue
        seen_players.add(_normalize_name(player_name))

        # Check override (per-transaction, not per-player)
        txn_title = txn.get("title", "")
        if _override_key(player_name, txn_title) in overrides:
            continue

        new_team_news = _extract_new_team(txn.get("title", ""), txn.get("teams", []))
        new_team_proj = _normalize_team(new_team_news) if new_team_news else None

        dc_info = dc_lookup(player_name) if dc_lookup else None
        dc_pos = dc_info.get("generic_pos") if dc_info else None

        # Skip non-skill positions using depth chart data
        if dc_pos and dc_pos not in TRACKED_POSITIONS:
            continue

        # Find in projections
        matches = _find_player_in_projections(player_name, players)

        if not matches:
            # Player not in projections at all — only alert for skill positions
            # If we don't have depth chart data, we can't confirm position, so include it
            pos_label = f" ({dc_pos})" if dc_pos else ""
            dc_team = dc_info.get("team", "") if dc_info else ""
            dc_depth = dc_info.get("depth", 0) if dc_info else 0

            alerts.append({
                "type": "missing",
                "player": player_name,
                "pos": dc_pos or "unknown",
                "depth": dc_depth,
                "dc_team": dc_team,
                "transaction": txn.get("title", ""),
                "new_team": new_team_news,
                "date": txn.get("_date", ""),
                "message": (
                    f"{player_name}{pos_label} signed with {new_team_news} "
                    f"but not found in projections"
                    + (f" (depth chart: {dc_team} #{dc_depth})" if dc_team else "")
                ),
            })
            continue

        for pid, proj_data in matches:
            proj_team = proj_data.get("team", "")
            pos = proj_data.get("pos", "")

            if pos not in TRACKED_POSITIONS:
                continue

            if new_team_proj and proj_team != new_team_proj and proj_team != "FA":
                alerts.append({
                    "type": "wrong_team",
                    "player": player_name,
                    "player_id": pid,
                    "pos": pos,
                    "depth": dc_info.get("depth", 0) if dc_info else 0,
                    "dc_team": dc_info.get("team", "") if dc_info else "",
                    "transaction": txn.get("title", ""),
                    "proj_team": proj_team,
                    "new_team": new_team_news,
                    "new_team_proj": new_team_proj,
                    "date": txn.get("_date", ""),
                    "message": (
                        f"{player_name} ({pos}) moved to {new_team_news} "
                        f"but projections show {_news_team(proj_team)}"
                    ),
                })

    return alerts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    alerts = reconcile()
    if not alerts:
        print("All recent transactions are accounted for in projections.")
    else:
        print(f"{len(alerts)} unresolved transaction(s):\n")
        for alert in alerts:
            print(f"  [{alert['type'].upper()}] {alert['message']}")
            print(f"    Transaction: {alert['transaction']}")
            print(f"    Date: {alert['date']}")
            print()
