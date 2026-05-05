"""Depth Chart Manager logic.

Compares the user's hand-maintained "2026 Depth Chart" Google Sheet
against the agent's view of the world (latest OurLads scrape +
last N days of NFL.com transactions) and surfaces discrepancies.

The Streamlit page in dashboard/pages/depth_chart_manager.py wraps
these functions and renders the results. Logic is kept pure here so
the tool can also be smoke-tested from the CLI:

    python -m processing.sheet_reconciliation

In-season work is partially deferred — see
docs/depth_chart_manager_in_season.md for the changes needed when the
season starts (game-day inactives, practice-squad distinction, weekly
elevations).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from collectors.depth_chart_collector import load_latest_depth_charts
from scripts.transaction_reconciler import (
    NEWS_TO_PROJ_TEAM,
    PROJ_TO_NEWS_TEAM,
    _collect_recent_transactions,
    _extract_player_name,
)

# A stricter name normalizer than transaction_reconciler._normalize_name —
# handles Jr/Sr (with or without period) and Roman-numeral generational
# suffixes (II/III/IV/...). OurLads stores names like "Kyle Pitts Sr." and
# "Cortez Braham Jr.", while the master sheet often stores them without
# the suffix; without consistent stripping, every player like that
# produces a spurious "Not in OurLads" status mismatch.
_NAME_SUFFIX_RE = re.compile(
    r"\s+("
    r"[IVX]{1,4}"          # Roman numerals I, II, III, IV, V, VI, VII, VIII, IX
    r"|j[rR]\.?"          # Jr / Jr. (any case for the r)
    r"|s[rR]\.?"          # Sr / Sr.
    r")$",
    re.IGNORECASE,
)


def _normalize_name(name: str) -> str:
    """Lowercase + strip generational suffix + strip dots + strip trailing
    all-caps team abbrev. Used as the cross-system join key.

    OurLads strips periods from initial-style names ("J.J. Jansen" stored
    as "jj jansen"); the master sheet keeps them. Stripping dots on both
    sides eliminates that source of false-positive mismatches.
    """
    name = name.strip()
    # Loop in case of "Foo Bar Jr. III" (rare).
    for _ in range(3):
        stripped = _NAME_SUFFIX_RE.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    name = re.sub(r"\s+[A-Z]{2,3}$", "", name)
    name = name.replace(".", "")
    name = re.sub(r"\s+", " ", name)
    return name.lower().strip()

logger = logging.getLogger(__name__)

MASTER_SHEET_ID = "1XHXiR__p7h2JVLKNkS-F9aiKZjhar78YubQklW_baQA"

DEPTHCHART_TAB = "DepthCharts"
TRANSACTIONS_TAB = "Transactions_New"

# Header row + first data row in DepthCharts
_DC_HEADER_ROW = 3
_DC_DATA_START = 4

# Column letters in DepthCharts (1-indexed positions from probe)
_DC_COL_GSIS = 8        # gsisId
_DC_COL_NAME = 6        # Display Name
_DC_COL_POS = 13        # POSITION
_DC_COL_TEAM = 23       # TEAM
_DC_COL_STATUS = 27     # STATUS DESC
_DC_COL_DEPTH_POS = 30  # DEPTH POSITION
_DC_COL_ORDER = 31      # ORDER

# Statuses recognised by the master sheet for the active-roster slot.
# Anything outside this set means the player is *not* simply "Active".
ACTIVE_STATUSES = {"Active"}

# Transaction types that should clear an "Active" status
DEACTIVATING_TX_TYPES = {
    "released",
    "waived",
    "reserve-list",
    "reserve/injured",
    "retired",
    "terminated",
}

DISMISSALS_PATH = PROJECT_ROOT / "data" / "sheet_recon_dismissals.json"


# ---------------------------------------------------------------------------
# Team abbreviation helpers
# ---------------------------------------------------------------------------


def to_proj_team(news_abbr: str) -> str:
    """Convert news/Transactions_New style (ARI) to projection style (ARZ).

    Master DepthCharts and OurLads both use projection style.
    """
    return NEWS_TO_PROJ_TEAM.get(news_abbr, news_abbr)


def to_news_team(proj_abbr: str) -> str:
    return PROJ_TO_NEWS_TEAM.get(proj_abbr, proj_abbr)


# ---------------------------------------------------------------------------
# Master sheet loaders
# ---------------------------------------------------------------------------


def _get_gspread_client():
    """Build a gspread client using the same key resolver as the projections
    pipeline."""
    from scripts.snapshot_projections import _resolve_service_account_key
    from google.oauth2.service_account import Credentials
    import gspread

    creds = Credentials.from_service_account_file(
        str(_resolve_service_account_key()),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    return gspread.authorize(creds)


def load_master_depthchart(client=None) -> dict[str, dict]:
    """Read the DepthCharts tab and return a dict keyed by gsisId.

    Each value: {gsis_id, name, pos, team, status_desc, depth_pos, order, name_key}
    where name_key is _normalize_name() of the display name (used as fallback
    when matching against OurLads which has no gsisId).
    Rows without a gsisId are skipped — they can't be reliably matched.
    """
    if client is None:
        client = _get_gspread_client()

    sh = client.open_by_key(MASTER_SHEET_ID)
    ws = sh.worksheet(DEPTHCHART_TAB)
    rows = ws.get_all_values()

    out: dict[str, dict] = {}
    for row in rows[_DC_DATA_START - 1:]:
        if len(row) < _DC_COL_STATUS:
            continue
        gsis = row[_DC_COL_GSIS - 1].strip()
        if not gsis or gsis.upper() == "ROOKIE001":
            # Skip placeholder/rookie rows without real GSIS IDs
            continue
        name = row[_DC_COL_NAME - 1].strip()
        if not name:
            continue
        out[gsis] = {
            "gsis_id": gsis,
            "name": name,
            "name_key": _normalize_name(name),
            "pos": row[_DC_COL_POS - 1].strip(),
            "team": row[_DC_COL_TEAM - 1].strip(),
            "status_desc": row[_DC_COL_STATUS - 1].strip(),
            "depth_pos": row[_DC_COL_DEPTH_POS - 1].strip() if len(row) >= _DC_COL_DEPTH_POS else "",
            "order": row[_DC_COL_ORDER - 1].strip() if len(row) >= _DC_COL_ORDER else "",
        }
    logger.info("Loaded %d players from master DepthCharts", len(out))
    return out


def load_master_transactions(client=None) -> set[tuple[str, str, str]]:
    """Read Transactions_New tab; return set of (date, gsisId, type) tuples.

    Used to detect transactions our agent has seen that the master
    sheet hasn't imported yet.
    """
    if client is None:
        client = _get_gspread_client()

    sh = client.open_by_key(MASTER_SHEET_ID)
    ws = sh.worksheet(TRANSACTIONS_TAB)
    rows = ws.get_all_values()
    if not rows:
        return set()

    headers = rows[0]
    # Find columns by header — the layout could shift if the sheet owner edits it.
    def _col(name: str) -> int:
        for i, h in enumerate(headers):
            if h.strip().lower() == name.lower():
                return i
        return -1

    date_col = _col("date")
    type_col = _col("transactionType")
    gsis_col = _col("person_gsisId")
    if min(date_col, type_col, gsis_col) < 0:
        logger.warning("Transactions_New is missing one of date/transactionType/person_gsisId columns")
        return set()

    out: set[tuple[str, str, str]] = set()
    for row in rows[1:]:
        if len(row) <= max(date_col, type_col, gsis_col):
            continue
        date = row[date_col].strip()
        gsis = row[gsis_col].strip()
        ttype = row[type_col].strip().lower()
        if date and gsis:
            out.add((date, gsis, ttype))
    logger.info("Loaded %d (date,gsisId,type) keys from master Transactions_New", len(out))
    return out


# ---------------------------------------------------------------------------
# Agent-side helpers
# ---------------------------------------------------------------------------


_TX_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\(.*released\b", re.I), "released"),
    (re.compile(r"\(.*waived\b", re.I), "waived"),
    (re.compile(r"\bplaced on (?:ir|injured reserve|reserve/injured)", re.I), "reserve/injured"),
    (re.compile(r"\(.*reserve/injured\b", re.I), "reserve/injured"),
    (re.compile(r"\(.*reserve\b", re.I), "reserve-list"),
    (re.compile(r"\(.*retired\b", re.I), "retired"),
    (re.compile(r"\(.*terminated\b", re.I), "terminated"),
    (re.compile(r"\(.*traded\b", re.I), "traded"),
    (re.compile(r"\(.*free agent signing\b", re.I), "free-agent-signing"),
    (re.compile(r"\(.*signing\b", re.I), "signing"),
    (re.compile(r"\(.*promoted\b", re.I), "promotion"),
]


def _classify_transaction(title: str) -> str:
    """Return a coarse type for an agent-side transaction title."""
    for pat, label in _TX_TYPE_PATTERNS:
        if pat.search(title):
            return label
    return "other"


def _build_ourlads_by_name(depth_charts: dict | None = None) -> dict[str, dict]:
    """Return {normalized_name: dc_record}.

    Both sides (master + OurLads) are keyed via _normalize_name so suffixes
    like "III" / "Jr." don't cause spurious not-found results."""
    if depth_charts is None:
        depth_charts = load_latest_depth_charts() or {}
    out: dict[str, dict] = {}
    for name, rec in depth_charts.items():
        out[_normalize_name(name)] = rec
    return out


def _index_recent_transactions_by_player(
    transactions: list[dict],
) -> dict[str, list[dict]]:
    """Return {normalized_player_name: [tx, ...]} sorted newest-first."""
    out: dict[str, list[dict]] = {}
    for tx in transactions:
        title = tx.get("title", "")
        name = _extract_player_name(title)
        if not name or len(name.split()) < 2:
            continue
        key = _normalize_name(name)
        out.setdefault(key, []).append(tx)
    for key in out:
        out[key].sort(key=lambda t: t.get("_date", ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Discrepancy computations
# ---------------------------------------------------------------------------


def compute_team_mismatches(
    master: dict[str, dict],
    ourlads: dict[str, dict],
    tx_by_player: dict[str, list[dict]],
) -> list[dict]:
    """Find players whose master TEAM disagrees with OurLads.

    Master DepthCharts uses projection-style abbreviations (ARZ/BLT/CLV/HST/LA);
    OurLads uses news-style abbreviations except for ARZ. Both sides are
    normalized to projection style before compare so BAL↔BLT etc. don't
    produce false positives.
    """
    results: list[dict] = []
    for gsis, m in master.items():
        master_team = m["team"]
        if not master_team:
            continue
        dc = ourlads.get(m["name_key"])
        if dc is None:
            continue
        dc_team_raw = dc.get("team", "")
        if not dc_team_raw:
            continue
        dc_team_normalized = to_proj_team(dc_team_raw)
        if dc_team_normalized == master_team:
            continue
        recent_tx = tx_by_player.get(m["name_key"], [])
        results.append({
            "gsis_id": gsis,
            "player": m["name"],
            "pos": m["pos"],
            "master_team": master_team,
            "ourlads_team": dc_team_raw,  # show what OurLads literally said
            "ourlads_depth": f"{dc.get('pos', '')} #{dc.get('depth', '')}".strip(" #"),
            "recent_tx": recent_tx[0] if recent_tx else None,
            "key": _stable_key("team", gsis, master_team, dc_team_normalized),
        })
    return results


def compute_status_mismatches(
    master: dict[str, dict],
    ourlads: dict[str, dict],
    tx_by_player: dict[str, list[dict]],
) -> list[dict]:
    """Find players whose master STATUS DESC says 'Active' but the agent's
    data implies otherwise."""
    results: list[dict] = []
    for gsis, m in master.items():
        if m["status_desc"] not in ACTIVE_STATUSES:
            continue
        recent_tx = tx_by_player.get(m["name_key"], [])
        deactivating = next(
            (
                tx for tx in recent_tx
                if _classify_transaction(tx.get("title", "")) in DEACTIVATING_TX_TYPES
            ),
            None,
        )
        in_ourlads = m["name_key"] in ourlads
        # Skip players we can't make a confident judgement on. Without a
        # deactivating transaction AND with no OurLads record at all, we
        # might just be missing them in the OurLads scrape (rookies etc.)
        if deactivating is None and in_ourlads:
            continue
        suggested = (
            _classify_transaction(deactivating["title"]).replace("-", " ").title()
            if deactivating else "Released / Inactive"
        )
        results.append({
            "gsis_id": gsis,
            "player": m["name"],
            "pos": m["pos"],
            "master_team": m["team"],
            "master_status": m["status_desc"],
            "suggested_status": suggested,
            "evidence": (
                deactivating["title"] if deactivating
                else "Not present in latest OurLads depth chart"
            ),
            "evidence_date": (
                deactivating.get("_date") if deactivating else ""
            ),
            "in_ourlads": in_ourlads,
            "key": _stable_key("status", gsis, m["status_desc"], suggested),
        })
    return results


def compute_untracked_transactions(
    master_tx: set[tuple[str, str, str]],
    agent_transactions: list[dict],
    master: dict[str, dict],
) -> list[dict]:
    """Agent-side transactions in the last 30 days that aren't in master Transactions_New.

    Matching is approximate: we map the agent's title-extracted player name
    back to a gsisId via the master DepthCharts (name -> gsisId index).
    Players with no master row are dropped — we can't match them.
    """
    name_to_gsis: dict[str, str] = {m["name_key"]: gsis for gsis, m in master.items()}

    # Index master_tx by gsisId for cheap "do we have any tx for this player on this date?" check
    master_dates_by_player: dict[str, set[str]] = {}
    for date, gsis, _t in master_tx:
        master_dates_by_player.setdefault(gsis, set()).add(date)

    results: list[dict] = []
    for tx in agent_transactions:
        title = tx.get("title", "")
        name = _extract_player_name(title)
        if not name or len(name.split()) < 2:
            continue
        gsis = name_to_gsis.get(_normalize_name(name))
        if not gsis:
            continue
        date = tx.get("_date", "")
        ttype = _classify_transaction(title)
        # Match generously: if the master sheet has *any* transaction for this
        # player on the same date, we treat it as covered. Type strings differ
        # between feeds, so date+player is the practical gate.
        if date in master_dates_by_player.get(gsis, set()):
            continue
        results.append({
            "gsis_id": gsis,
            "player": name,
            "date": date,
            "agent_type": ttype,
            "title": title,
            "url": tx.get("url", ""),
            "key": _stable_key("missing_tx", gsis, date, ttype),
        })
    return results


# ---------------------------------------------------------------------------
# Dismissals
# ---------------------------------------------------------------------------


def _stable_key(category: str, gsis: str, expected: str, actual: str) -> str:
    return f"{category}|{gsis}|{expected}|{actual}"


def load_dismissals() -> dict[str, dict]:
    if not DISMISSALS_PATH.exists():
        return {}
    try:
        with open(DISMISSALS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_dismissals(dismissals: dict[str, dict]) -> None:
    DISMISSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DISMISSALS_PATH, "w", encoding="utf-8") as f:
        json.dump(dismissals, f, indent=2, ensure_ascii=False)


def dismiss(key: str, note: str = "") -> None:
    d = load_dismissals()
    d[key] = {
        "dismissed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    }
    save_dismissals(d)


def undismiss(key: str) -> None:
    d = load_dismissals()
    if key in d:
        del d[key]
        save_dismissals(d)


def filter_dismissed(rows: list[dict], dismissals: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """Split rows into (active, dismissed)."""
    active, dismissed = [], []
    for r in rows:
        if r["key"] in dismissals:
            dismissed.append(r)
        else:
            active.append(r)
    return active, dismissed


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


def reconcile(lookback_days: int = 30, client=None) -> dict[str, list[dict]]:
    """Run all three discrepancy computations and return them keyed by category."""
    if client is None:
        client = _get_gspread_client()

    master = load_master_depthchart(client)
    master_tx = load_master_transactions(client)
    ourlads = _build_ourlads_by_name()
    agent_tx = _collect_recent_transactions(days=lookback_days)
    tx_by_player = _index_recent_transactions_by_player(agent_tx)

    return {
        "team_mismatches": compute_team_mismatches(master, ourlads, tx_by_player),
        "status_mismatches": compute_status_mismatches(master, ourlads, tx_by_player),
        "untracked_transactions": compute_untracked_transactions(master_tx, agent_tx, master),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    results = reconcile()
    for category, rows in results.items():
        print(f"\n=== {category}: {len(rows)} ===")
        for r in rows[:8]:
            if category == "team_mismatches":
                print(f"  {r['player']:30s} master={r['master_team']:4s} ourlads={r['ourlads_team']:4s}")
            elif category == "status_mismatches":
                evidence = r['evidence'][:60] + ("..." if len(r['evidence']) > 60 else "")
                print(f"  {r['player']:30s} master={r['master_status']:8s} -> {r['suggested_status']}  ({evidence})")
            else:
                print(f"  {r['date']} {r['player']:30s} {r['agent_type']}  {r['title'][:60]}")
        if len(rows) > 8:
            print(f"  ... +{len(rows) - 8} more")
