"""Practice-squad elevations and promotions from ESPN's league transaction feed (in-season).

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
import time
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
DEFAULT_ATTEMPTS = 3
_NO_CACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}

# ESPN uses a few position labels the injury-report tables never show.
_EXTRA_POSITIONS = {"EDGE", "DE", "DT", "NT", "OG", "OT", "SAF", "PK", "ATH", "DL", "OL"}
_POSITIONS = {p.upper() for p in POSITION_TOKENS} | _EXTRA_POSITIONS
# Typos seen in ESPN's feed. "Elevated OLD Barryl Peterson III" (2026-10-03)
# otherwise left "OLD" glued to the name, which then matched no roster.
_POSITION_TYPOS = {"OLD": "OLB"}

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

# Whose practice squad: the club's own, or another's ("from Atlanta's practice
# squad", "off New Orleans' practice squad") — either way he joins this club's 53.
_PS_OWNER = r"(?:(?:the|their|its)\s+|[A-Z][A-Za-z.'’ ]*?['’]s?\s+)?"
_ACTIVE_ROSTER = r"to\s+(?:the\s+|their\s+|its\s+)?(?:active|53-man)\s+roster"

# Practice squad -> 53-man roster. ESPN words it as a signing ("Signed WR
# Jamaal Pritchett from the practice squad", "... off the practice squad to the
# active roster", "... to the active roster from the practice squad"). A bare
# "Signed X to the active roster" is left alone: it is as often a street free
# agent. "Signed X to the practice squad" never matches — it needs from/off.
PROMOTION_RE = re.compile(
    r"\b(?:Sign(?:ed|ing)|Promot(?:ed|ing))\s+(?P<body>" + _BODY_CHAR + r"+?)"
    r"(?=\s+(?:" + _ACTIVE_ROSTER + r"\s+)?(?:from|off)\s+" + _PS_OWNER + r"practice[- ]squad)",
    re.IGNORECASE,
)
# "Promoted" alone is unambiguous, so it may name only the destination.
PROMOTED_TO_ROSTER_RE = re.compile(
    r"\bPromot(?:ed|ing)\s+(?P<body>" + _BODY_CHAR + r"+?)(?=\s+" + _ACTIVE_ROSTER + r")",
    re.IGNORECASE,
)

_SPLIT_RE = re.compile(r"\s*,\s*|\s+and\s+", re.IGNORECASE)
# A lazy body can still swallow an earlier move in the same sentence
# ("Signed A to the practice squad and signed B from the practice squad").
_INNER_VERB_RE = re.compile(r"\b(?:sign(?:ed|ing)|promot(?:ed|ing))\s+", re.IGNORECASE)
_BODY_REJECT_RE = re.compile(r"practice[- ]squad|\broster\b", re.IGNORECASE)


def _singular_position(token: str) -> str:
    """``"LBs"`` / ``"lb."`` -> ``"LB"``; anything else upper-cased as given."""
    t = token.upper().strip(".,")
    t = _POSITION_TYPOS.get(t, t)
    return t[:-1] if t.endswith("S") and t[:-1] in _POSITIONS else t


def _strip_positions(entry: str) -> tuple[str, str]:
    """``"LB LB Mohamoud Diabate"`` -> ``("Mohamoud Diabate", "LB")``.

    ESPN occasionally doubles the position token, so every leading position is
    consumed and the first one is kept. A plural introduces a list of players
    at that position ("Elevated LBs Curtis Robinson and Justin Barron"), so
    "LBs" / "EDGEs" count as the position too.
    """
    tokens = entry.split()
    pos = ""
    while tokens and _singular_position(tokens[0]) in _POSITIONS:
        if not pos:
            pos = _singular_position(tokens[0])
        tokens = tokens[1:]
    return " ".join(tokens).strip(), pos


def parse_elevations(rows: list[dict]) -> list[dict]:
    """ESPN transaction rows -> ``[{date, team, name, pos, detail, event_type}]``.

    Rows without an elevation clause yield nothing, so the unrelated moves
    bundled into the same description are ignored rather than misread.
    """
    return _parse_clauses(rows, (ELEVATION_RE,), "ps_elevated")


def parse_promotions(rows: list[dict]) -> list[dict]:
    """Practice-squad -> 53-man signings, same row shape as :func:`parse_elevations`.

    NFL.com lists these a day late and nflverse days late; ESPN has them the
    same afternoon (2026-09-26: "Signed WR Jamaal Pritchett from the practice
    squad" sat in this feed while the roster state still called him PS).
    """
    return _parse_clauses(rows, (PROMOTION_RE, PROMOTED_TO_ROSTER_RE), "ps_promoted")


def _parse_clauses(rows: list[dict], patterns: tuple[re.Pattern, ...], event_type: str) -> list[dict]:
    out: list[dict] = []
    for row in rows or []:
        desc = str(row.get("description") or "")
        abbr = str(((row.get("team") or {}).get("abbreviation") or "")).strip().upper()
        if not desc or not abbr:
            continue
        # ESPN's dialect, not news-style: it calls Washington WSH. Without the
        # "espn" source the map is never consulted and the club's elevations
        # land under a code nothing else uses, splitting it in two - the Roster
        # State team filter listed both WAS and WSH, "Clubs reported" counted
        # one club twice, and the Team page (which matches on the news abbr)
        # showed neither of Washington's 2026-09-19 elevations.
        team = to_news(abbr, "espn") or abbr
        day = str(row.get("date") or "")[:10]
        seen: set[str] = set()
        for pattern in patterns:
            for m in pattern.finditer(desc):
                body = _INNER_VERB_RE.split(m.group("body"))[-1]
                if _BODY_REJECT_RE.search(body):
                    continue
                for entry in _SPLIT_RE.split(body):
                    name, pos = _strip_positions(entry.strip())
                    # A bare position with no name, or a stray fragment, is not a player.
                    if not name or len(name.split()) < 2 or name in seen:
                        continue
                    seen.add(name)
                    out.append({
                        "date": day,
                        "team": team,
                        "name": name,
                        "pos": pos,
                        "detail": f"ESPN: {m.group(0).strip()}",
                        "event_type": event_type,
                    })
    return out


def fetch_transactions(url: str = DEFAULT_URL, limit: int = DEFAULT_LIMIT,
                       timeout: int = DEFAULT_TIMEOUT,
                       session: Optional[requests.Session] = None,
                       fresh_as_of: Optional[str] = None, attempts: int = DEFAULT_ATTEMPTS,
                       retry_sleep: float = 3.0) -> list[dict]:
    """Page 1 of the league transaction feed.

    Deliberately a single page: ``limit`` alone returns the newest ``limit``
    rows, but combining ``limit`` with ``page`` returns a different (older)
    window than the page size implies, so paging would silently skip today.

    With ``fresh_as_of`` (YYYY-MM-DD), a response whose newest row is older
    is refetched up to ``attempts`` times and the freshest one kept. On a day
    with no transactions yet that costs a few seconds and changes nothing.
    """
    sess = session or requests.Session()
    best: list[dict] = []
    for attempt in range(1, max(1, attempts) + 1):
        try:
            # ESPN does not serve every caller the same copy of this feed. On
            # 2026-10-03 the GitHub runners got a page ending before Saturday's
            # 18 elevations at 22:42 and 23:34 UTC (identical "38 parsed from
            # 100 rows" both times) while a local fetch had them all, so bust
            # any cache in the path and keep the freshest response.
            resp = sess.get(url, params={"limit": int(limit), "_": int(time.time() * 1000)},
                            headers=_NO_CACHE, timeout=timeout)
            resp.raise_for_status()
            rows = resp.json().get("transactions") or []
        except Exception as e:  # noqa: BLE001 — non-fatal, like every other collector
            logger.warning("ESPN transactions fetch failed (attempt %d/%d): %s", attempt, attempts, e)
            continue
        if not best or newest_row_date(rows) > newest_row_date(best):
            best = rows
        if not fresh_as_of or newest_row_date(best) >= fresh_as_of or attempt == attempts:
            break
        logger.info("ESPN transactions: newest row %s is older than %s — refetching (attempt %d/%d)",
                    newest_row_date(best) or "none", fresh_as_of, attempt, attempts)
        time.sleep(retry_sleep)
    logger.debug("ESPN transactions: %d rows", len(best))
    return best


def newest_row_date(rows: list[dict]) -> str:
    """YYYY-MM-DD of the newest row (ESPN stamps every row 07:00Z of its day)."""
    return max((str(r.get("date") or "")[:10] for r in rows or []), default="")


def collect_elevations(date_str: Optional[str] = None, settings: Optional[dict] = None,
                       session: Optional[requests.Session] = None) -> list[dict]:
    """Elevations inside the lookback window, newest first."""
    return [r for r in collect_ps_moves(date_str, settings=settings, session=session)
            if r["event_type"] == "ps_elevated"]


def collect_ps_moves(date_str: Optional[str] = None, settings: Optional[dict] = None,
                     session: Optional[requests.Session] = None) -> list[dict]:
    """Elevations and practice-squad promotions inside the lookback window,
    newest first, each tagged with its ``event_type``. One HTTP request.

    Returns ``[]`` — never raises — when the feed is unreachable or the
    ``roster.elevations`` block is disabled.
    """
    cfg = ((settings or get_settings()).get("roster", {}) or {}).get("elevations", {}) or {}
    if not cfg.get("enabled", True):
        logger.info("roster.elevations disabled — skipping ESPN transactions.")
        return []
    from processing.season import today_et

    # Only a run for today can tell a stale copy of the feed from a quiet day.
    et_today = today_et()
    rows = fetch_transactions(
        url=cfg.get("url", DEFAULT_URL),
        limit=int(cfg.get("limit", DEFAULT_LIMIT)),
        timeout=int(cfg.get("timeout", DEFAULT_TIMEOUT)),
        session=session,
        fresh_as_of=et_today if not date_str or date_str == et_today else None,
        attempts=int(cfg.get("attempts", DEFAULT_ATTEMPTS)),
    )
    elevations = parse_elevations(rows)
    promotions = parse_promotions(rows)
    days = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    today = date.fromisoformat(date_str) if date_str else datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=days)).isoformat()
    fresh = [e for e in elevations + promotions if e["date"] >= cutoff]
    n_elev = sum(1 for e in fresh if e["event_type"] == "ps_elevated")
    # The newest row's date is what exposes a stale feed: without it, a page
    # that stops yesterday reads exactly like a Saturday with no elevations.
    logger.info("ESPN elevations: %d in the last %d days (%d parsed from %d rows, newest row %s); "
                "practice-squad promotions: %d",
                n_elev, days, len(elevations), len(rows), newest_row_date(rows) or "none",
                len(fresh) - n_elev)
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

    rows = collect_ps_moves(args.date, settings=settings)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    for day in sorted(by_day, reverse=True):
        print(f"\n{day} — {len(by_day[day])} moves")
        for r in sorted(by_day[day], key=lambda x: (x["event_type"], x["team"], x["name"])):
            kind = "promoted" if r["event_type"] == "ps_promoted" else "elevated"
            print(f"  {kind:8} {r['team']:4} {r['pos']:5} {r['name']}")
    print(f"\n{len(rows)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
