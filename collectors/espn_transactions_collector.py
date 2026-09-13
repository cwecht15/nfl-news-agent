"""Practice-squad elevations from ESPN's league transaction feed (in-season).

Standard elevations are due 4:00 PM ET the day before a game, and an elevated
player is active for it — but no source this project already polls says so:

* NFL.com's transaction pages carry no elevation rows in any of their six
  categories (verified live; see ``processing/roster_events`` docstring).
* nflverse's roster CSV shows the flip a day or more later, and cannot tell an
  elevation from a promotion until the player reverts.
* The game-day inactives poll only runs ~90 minutes before kickoff.

ESPN's feed says it outright, the same afternoon:

    https://site.api.espn.com/apis/site/v2/sports/football/nfl/transactions

    {"date": "2026-09-12T07:00Z", "team": {"abbreviation": "ATL", ...},
     "description": "Elevated LB Bralen Trice and TE Nick Muse from the
                     practice squad. Placed OT Cam Williams on injured reserve."}

Three things about the payload drive the parsing here:

``date`` is day-granular (every row stamps 07:00Z), and ESPN dates Week 1's
Wednesday/Thursday opener elevations on the game day itself rather than the day
before — so rows are never filtered on "is this today", only on a lookback
window, and the ledger's own dedup absorbs the repeats.

A description bundles unrelated moves, and the elevation clause is not
necessarily first ("Placed TE Eli Stowers on injured reserve. Elevated WR
Britain Covey to the active roster."). Only the elevation clause is parsed;
the rest is left to the sources that already own it.

Player names are full of periods — "C.J. Donaldson", "Velus Jones Jr.",
"Rodney Thomas II" — so the clause cannot be found by splitting on sentences.

ESPN 403s browser-style User-Agents but answers a plain requests UA, so the
session here deliberately keeps the default one (same as the inactives
collector).

CLI:
    python collectors/espn_transactions_collector.py [--limit N] [--days N] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors.injury_report_collector import POSITION_TOKENS
from config_loader import get_settings
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/transactions"
DEFAULT_LIMIT = 100          # one page ≈ 10 days of league-wide transactions
DEFAULT_LOOKBACK_DAYS = 4
DEFAULT_TIMEOUT = 30

# ESPN uses a few position labels the injury-report tables never show.
_EXTRA_POSITIONS = {"EDGE", "DE", "DT", "NT", "OG", "OT", "SAF", "PK", "ATH", "DL", "OL"}
_POSITIONS = {p.upper() for p in POSITION_TOKENS} | _EXTRA_POSITIONS

# A period that ends the clause, as opposed to one inside a name. Excludes
# "Jr." / "Sr." and single-letter initials ("C.J."), and requires whitespace
# or end-of-string after it so "C.J." is safe from either side.
_CLAUSE_END = r"(?<!\bJr)(?<!\bSr)(?<!\b[A-Z])\.(?!\S)"

# Body characters: anything but a period, plus the periods a name may contain.
_BODY_CHAR = r"(?:[^.]|\.(?=\S)|(?<=\bJr)\.|(?<=\bSr)\.|(?<=\b[A-Z])\.)"

# "Elevated <players>" up to the practice-squad / active-roster tail, the end
# of the clause, or the end of the description.
ELEVATION_RE = re.compile(
    r"\bElevat(?:ed|ing|es|e)\s+(?P<body>" + _BODY_CHAR + r"+?)"
    r"(?=\s+from\s+(?:the\s+|their\s+|its\s+)?practice[- ]squad"
    r"|\s+to\s+(?:the\s+|their\s+|its\s+)?active\s+roster"
    r"|" + _CLAUSE_END + r"|$)",
    re.IGNORECASE,
)

_SPLIT_RE = re.compile(r"\s*,\s*|\s+and\s+", re.IGNORECASE)


def _strip_positions(entry: str) -> tuple[str, str]:
    """``"LB LB Mohamoud Diabate"`` -> ``("Mohamoud Diabate", "LB")``.

    ESPN occasionally doubles the position token, so every leading position is
    consumed and the first one is kept.
    """
    tokens = entry.split()
    pos = ""
    while tokens and tokens[0].upper().strip(".,") in _POSITIONS:
        if not pos:
            pos = tokens[0].upper().strip(".,")
        tokens = tokens[1:]
    return " ".join(tokens).strip(), pos


def parse_elevations(rows: list[dict]) -> list[dict]:
    """ESPN transaction rows -> ``[{date, team, name, pos, detail}]``.

    Rows without an elevation clause yield nothing, so the unrelated moves
    bundled into the same description are ignored rather than misread.
    """
    out: list[dict] = []
    for row in rows or []:
        desc = str(row.get("description") or "")
        abbr = str(((row.get("team") or {}).get("abbreviation") or "")).strip().upper()
        if not desc or not abbr:
            continue
        team = to_news(abbr) or abbr
        day = str(row.get("date") or "")[:10]
        for m in ELEVATION_RE.finditer(desc):
            for entry in _SPLIT_RE.split(m.group("body")):
                name, pos = _strip_positions(entry.strip())
                # A bare position with no name, or a stray fragment, is not a player.
                if not name or len(name.split()) < 2:
                    continue
                out.append({
                    "date": day,
                    "team": team,
                    "name": name,
                    "pos": pos,
                    "detail": f"ESPN: {m.group(0).strip()}",
                })
    return out


def fetch_transactions(url: str = DEFAULT_URL, limit: int = DEFAULT_LIMIT,
                       timeout: int = DEFAULT_TIMEOUT,
                       session: Optional[requests.Session] = None) -> list[dict]:
    """Page 1 of the league transaction feed.

    Deliberately a single page: ``limit`` alone returns the newest ``limit``
    rows, but combining ``limit`` with ``page`` returns a different (older)
    window than the page size implies, so paging would silently skip today.
    """
    sess = session or requests.Session()
    try:
        resp = sess.get(url, params={"limit": int(limit)}, timeout=timeout)
        resp.raise_for_status()
        rows = resp.json().get("transactions") or []
    except Exception as e:  # noqa: BLE001 — non-fatal, like every other collector
        logger.warning("ESPN transactions fetch failed: %s", e)
        return []
    logger.debug("ESPN transactions: %d rows", len(rows))
    return rows


def collect_elevations(date_str: Optional[str] = None, settings: Optional[dict] = None,
                       session: Optional[requests.Session] = None) -> list[dict]:
    """Elevations inside the lookback window, newest first.

    Returns ``[]`` — never raises — when the feed is unreachable or the
    ``roster.elevations`` block is disabled.
    """
    cfg = ((settings or get_settings()).get("roster", {}) or {}).get("elevations", {}) or {}
    if not cfg.get("enabled", True):
        logger.info("roster.elevations disabled — skipping ESPN transactions.")
        return []
    rows = fetch_transactions(
        url=cfg.get("url", DEFAULT_URL),
        limit=int(cfg.get("limit", DEFAULT_LIMIT)),
        timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT)),
        session=session,
    )
    elevations = parse_elevations(rows)
    days = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    today = date.fromisoformat(date_str) if date_str else datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    fresh = [e for e in elevations if e["date"] >= cutoff]
    logger.info("ESPN elevations: %d in the last %d days (%d parsed from %d rows)",
                len(fresh), days, len(elevations), len(rows))
    return sorted(fresh, key=lambda e: (e["date"], e["team"], e["name"]), reverse=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Practice-squad elevations from ESPN's transaction feed.")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--json", action="store_true", help="dump the parsed rows as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = get_settings()
    cfg = dict(((settings.get("roster", {}) or {}).get("elevations", {}) or {}))
    cfg.update({"enabled": True, "limit": args.limit, "lookback_days": args.days})
    settings = {**settings, "roster": {**settings.get("roster", {}), "elevations": cfg}}

    rows = collect_elevations(args.date, settings=settings)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    for day in sorted(by_day, reverse=True):
        print(f"\n{day} — {len(by_day[day])} elevations")
        for r in sorted(by_day[day], key=lambda x: (x["team"], x["name"])):
            print(f"  {r['team']:4} {r['pos']:5} {r['name']}")
    print(f"\n{len(rows)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
