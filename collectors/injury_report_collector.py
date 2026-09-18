"""Structured NFL injury report tracker (in-season).

Why this exists
---------------
The offseason pipeline reads NFL.com's injury page as a blob of news items
(``collectors/web_scraper.scrape_injuries`` — untouched, still used for that
path). In-season the Wed/Thu/Fri practice reports are among the highest-value
fantasy signals, and a blob loses exactly what matters: *which day* a player
was DNP/LP/FP, whether that trended up or down through the week, and the
Friday designation. This module keeps one structured file per NFL week
(``data/injuries/<season>/wk<NN>.json``) that accumulates the whole practice
week across daily runs and emits typed change records for the report.

Three sources (verified live 2026-09-08), in precedence order:

1. **Team sites** — official, full week grid. Every club runs the same NFL
   platform at ``https://www.<site_domain>/team/injury-report/``. The page
   carries one ``div.nfl-o-injury-report__container`` per club in the matchup
   (the club itself AND its opponent), each with
   ``span.nfl-o-injury-report__club-name`` and a table whose headers are
   ``Player, Position, Injury, <Day>, <Day>, <Day>, Game Status``. The day
   headers are weekday abbreviations that shift with the game day (``Sun, Mon,
   Tue`` for a Wednesday opener; normally ``Wed, Thu, Fri``), so they are
   mapped to real dates by walking back from the team's game date. Cells are
   ``DNP|LP|FP|""``; game status is ``UNSPECIFIED|OUT|QUESTIONABLE|DOUBTFUL``.
   A page with no table means the report is not posted yet — not an error.
2. **RotoWire** practice report — the XHR JSON behind
   ``/football/practice-report.php`` (league-wide in one call). Unofficial
   endpoint: fail soft, and never the sole source of truth. Practice grid
   only — its ``status`` is RotoWire's own fantasy tag, not a designation.
3. **NFL.com /injuries/** — fallback. It only shows the LATEST day's practice
   status per player, so that status is attributed to the team's most recent
   practice-report day by Eastern time (a morning run → the previous day).
   The page keeps the finished week until the new week's first report, so it
   is ignored whenever its title's week isn't the week being collected.

Every practice code is held to the team's three report days for the week
(the club page's own day headers, else :func:`practice_report_days`), and a
game designation is only accepted from the team's final report day on —
anything earlier is left over from the previous week.

Every source produces the same row shape::

    {"team": "NE", "name": "Christian Barmore", "name_key": "christian barmore",
     "pos": "DT", "injury": "Knee", "practice": {"2026-09-06": "DNP", ...},
     "game_status": "", "source": "team_site"}

Rows are merged per (team, name_key) with team_site > rotowire > nflcom per
field; a lower-precedence source only fills gaps, and disagreements go to
``conflicts[]`` so the dashboard can show them. Team keys are NEWS-style
abbreviations (NE, LAR, ARI ...).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

from config_loader import get_data_dir, get_settings, get_teams
from processing.season import (
    games_for_week,
    get_season_year,
    load_schedule,
    opponent,
    week_from_date,
    weekday_name,
)
from processing.sheet_reconciliation import _normalize_name
from processing.team_abbr import nickname_to_news, to_news, to_proj

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRACTICE_CODES = {"DNP", "LP", "FP"}          # "" = not listed / no practice that day
GAME_STATUS = {"OUT", "D", "Q", ""}           # "" = no designation (UNSPECIFIED)

# DNP -> LP -> FP is the "getting healthier" direction.
PRACTICE_RANK = {"DNP": 0, "LP": 1, "FP": 2}

SOURCE_TEAM_SITE = "team_site"
SOURCE_ROTOWIRE = "rotowire"
SOURCE_NFLCOM = "nflcom"
SOURCE_PRECEDENCE = [SOURCE_TEAM_SITE, SOURCE_ROTOWIRE, SOURCE_NFLCOM]

# settings.yaml ``injury_report.sources`` names -> internal source labels
SETTINGS_SOURCE_NAMES = {
    "team_sites": SOURCE_TEAM_SITE,
    "team_site": SOURCE_TEAM_SITE,
    "rotowire": SOURCE_ROTOWIRE,
    "nflcom": SOURCE_NFLCOM,
    "nfl.com": SOURCE_NFLCOM,
}

CHANGE_TYPES = (
    "new_listing",
    "practice_upgrade",
    "practice_downgrade",
    "practice_status",      # first practice code for an already-listed player (no reference)
    "designation_set",
    "designation_changed",
    "cleared",
)

ROTOWIRE_URL = "https://www.rotowire.com/football/tables/practice-report.php?team="
ROTOWIRE_REFERER = "https://www.rotowire.com/football/practice-report.php"
NFLCOM_URL = "https://www.nfl.com/injuries/"

# The NFL club platform and RotoWire's XHR both expect a browser-like UA; the
# generic "NFL-News-Agent/1.0" from settings is not used for these fetches.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
ROTOWIRE_DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DEFAULT_TEAM_SITE_WORKERS = 8
DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    """Root of the injury week files. Module-level so tests can monkeypatch."""
    return get_data_dir("injuries")


def week_file_path(season: int, week: int) -> Path:
    return _base_dir() / str(season) / f"wk{int(week):02d}.json"


def load_week_file(season: int, week: int) -> Optional[dict]:
    path = week_file_path(season, week)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("Corrupt injury week file %s (%s) — starting fresh", path, e)
        return None


def _settings(settings: Optional[dict]) -> dict:
    return settings if settings is not None else get_settings()


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def make_session(settings: Optional[dict] = None, pool_size: int = DEFAULT_TEAM_SITE_WORKERS) -> requests.Session:
    """A requests session with a browser-like UA and a pool sized for the team-site fan-out."""
    cfg = _settings(settings)
    ua = cfg.get("injury_report", {}).get("user_agent") or BROWSER_UA
    session = requests.Session()
    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _timeout(settings: Optional[dict]) -> int:
    return int(_settings(settings).get("collection", {}).get("request_timeout", DEFAULT_TIMEOUT))


# Some club pages render a second, narrow-screen copy of the report whose
# player cell carries the position ("Shelby Harris, DT") and which has no
# Position column at all. Left alone that produces a second record per player
# under its own name_key ("shelby harris,"), so the week file shows everyone
# twice. Split the position back out before the key is computed — only when
# the tail is a real position token, so "Ricard, Jr." survives intact.
POSITION_TOKENS = {
    "QB", "RB", "FB", "HB", "WR", "TE",
    "OL", "OT", "OG", "G", "C", "T", "LT", "RT", "LG", "RG", "T/G", "G/C",
    "DL", "DT", "DE", "NT", "EDGE",
    "LB", "OLB", "ILB", "MLB",
    "DB", "CB", "S", "SS", "FS", "SAF", "NB",
    "K", "PK", "P", "LS", "ATH",
}


def split_trailing_pos(name: str) -> tuple[str, str]:
    """``"Shelby Harris, DT"`` -> ``("Shelby Harris", "DT")``.

    Returns the name unchanged (and an empty position) when the trailing
    token isn't a recognized position.
    """
    head, sep, tail = (name or "").rpartition(",")
    if not sep:
        return name, ""
    head, tail = head.strip(), tail.strip().upper()
    if head and tail in POSITION_TOKENS:
        return head, tail
    return name, ""


def _make_row(team: str, name: str, pos: str, injury: str, practice: dict[str, str],
              game_status: str, source: str) -> dict:
    name = " ".join((name or "").split())
    pos = (pos or "").strip()
    base, tail_pos = split_trailing_pos(name)
    if tail_pos:
        # Strip it either way so the key matches the wide table's row; an
        # explicit Position column still wins over the suffix.
        name, pos = base, pos or tail_pos
    return {
        "team": team,
        "name": name,
        "name_key": _normalize_name(name),
        "pos": pos,
        "injury": (injury or "").strip(),
        "practice": {d: c for d, c in practice.items() if c},
        "game_status": game_status or "",
        "source": source,
    }


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def normalize_practice(text: Any) -> str:
    """Practice-participation text/code → ``DNP`` | ``LP`` | ``FP`` | ``""``.

    Handles the team-site/RotoWire codes and NFL.com's long strings (which may
    be truncated in the DOM — matched by prefix).
    """
    t = " ".join(str(text or "").split()).upper()
    if not t or t in {"-", "--", "N/A", "NA"}:
        return ""
    if t in PRACTICE_CODES:
        return t
    if t.startswith("DID NOT") or t.startswith("DNP") or "NOT PARTICIPATE" in t or t.startswith("NO PRACTICE"):
        return "DNP"
    if t.startswith("LIMITED") or t.startswith("LP"):
        return "LP"
    if t.startswith("FULL") or t.startswith("FP"):
        return "FP"
    logger.debug("Unrecognized practice status %r", text)
    return ""


def normalize_game_status(text: Any) -> str:
    """Game-status text → ``OUT`` | ``D`` | ``Q`` | ``""``."""
    t = " ".join(str(text or "").split()).upper()
    if not t or t in {"UNSPECIFIED", "-", "--", "NONE", "N/A", "NA"}:
        return ""
    if t.startswith("OUT") or t == "O":
        return "OUT"
    if t.startswith("DOUBT") or t == "D":
        return "D"
    if t.startswith("QUEST") or t == "Q":
        return "Q"
    logger.debug("Unrecognized game status %r", text)
    return ""


# ---------------------------------------------------------------------------
# Day-header → date mapping
# ---------------------------------------------------------------------------


def _weekday_index(label: str) -> Optional[int]:
    key = str(label or "").strip().lower()[:3]
    return WEEKDAY_INDEX.get(key)


def _last_weekday_before(game_date: date, weekday: int) -> date:
    """Most recent ``weekday`` (0=Mon) strictly before ``game_date``."""
    delta = (game_date.weekday() - weekday) % 7
    if delta == 0:
        delta = 7
    return game_date - timedelta(days=delta)


def map_day_headers_to_dates(day_headers: list[str], game_date: Any) -> dict[str, str]:
    """``["Sun","Mon","Tue"]`` + game Wed 2026-09-09 → ``{"Sun": "2026-09-06", ...}``.

    Practices happen before the game, so each header is the most recent such
    weekday strictly before the game date. Non-weekday headers are ignored.
    """
    gd = _as_date(game_date)
    out: dict[str, str] = {}
    for h in day_headers:
        wd = _weekday_index(h)
        if wd is None:
            continue
        out[h] = _last_weekday_before(gd, wd).isoformat()
    return out


def _map_day_headers_from_today(day_headers: list[str], today: Any) -> dict[str, str]:
    """Fallback when no game date is known (bye / no schedule): anchor the
    first header to its most recent occurrence on or before ``today`` and
    treat later headers as the following occurrences (practice days run
    consecutively)."""
    d = _as_date(today)
    out: dict[str, str] = {}
    cursor: Optional[date] = None
    for h in day_headers:
        wd = _weekday_index(h)
        if wd is None:
            continue
        if cursor is None:
            cursor = d - timedelta(days=(d.weekday() - wd) % 7)
        else:
            step = (wd - cursor.weekday()) % 7 or 7
            cursor = cursor + timedelta(days=step)
        out[h] = cursor.isoformat()
    return out


def week_window_dates(schedule: list[dict], week: int) -> dict[str, str]:
    """RotoWire-style weekday keys → ISO dates for the NFL week's Tue..Mon
    window (week start = the Tuesday before the week's first game)."""
    games = games_for_week(schedule, week)
    if not games:
        return {}
    first = min(_as_date(g["date"]) for g in games)
    start = first
    while start.weekday() != 1:
        start -= timedelta(days=1)
    out: dict[str, str] = {}
    for i in range(7):
        d = start + timedelta(days=i)
        out[ROTOWIRE_DAY_KEYS[d.weekday()]] = d.isoformat()
    return out


def _team_game_info(schedule: Optional[list[dict]], team_news: str, week: Optional[int]) -> tuple[Optional[str], Optional[str]]:
    """(opp news abbr, game ISO date) for ``team_news`` in ``week``; (None, None) on bye/unknown."""
    if not schedule or not week or not team_news:
        return None, None
    info = opponent(schedule, to_proj(team_news), week)
    if not info:
        return None, None
    return to_news(info["opp"], "proj"), info["date"]


def _drop_future(practice: dict[str, str], max_date: Optional[str]) -> dict[str, str]:
    """A practice value attributed to a date after the scrape date is garbage
    (e.g. a source still showing last week's grid once the week rolled over)."""
    if not max_date:
        return practice
    return {d: c for d, c in practice.items() if d <= max_date}


# Games on a short week (Wednesday opener, Thursday night, Black Friday,
# Christmas) report the three days right before kickoff; everything else
# skips the day before (Sun -> Wed/Thu/Fri, Mon -> Thu/Fri/Sat, Sat ->
# Tue/Wed/Thu). Verified against the club pages' own day headers.
_SHORT_WEEK_GAME_DAYS = {2, 3, 4}   # Wed, Thu, Fri


def practice_report_days(game_date: Any) -> list[str]:
    """The three practice-report dates for a game on ``game_date`` (ISO, ascending).

    Only a fallback: the club page's own day headers (``Wed Thu Fri``) are
    authoritative and win whenever a team site was read.
    """
    gd = _as_date(game_date)
    offsets = (3, 2, 1) if gd.weekday() in _SHORT_WEEK_GAME_DAYS else (4, 3, 2)
    return [(gd - timedelta(days=n)).isoformat() for n in offsets]


def team_practice_days(schedule: Optional[list[dict]], week: Optional[int],
                       observed: Optional[dict[str, list[str]]] = None) -> Optional[dict[str, list[str]]]:
    """{news abbr: [practice-report dates]} for every team with a game in ``week``.

    ``observed`` (dates read off club-page headers) wins over the rule. A team
    missing from the result has no game this week (bye), so any row for it is
    left over from an earlier week. ``None`` when the schedule can't say.
    """
    if not schedule or not week:
        return None
    games = games_for_week(schedule, week)
    if not games:
        return None
    observed = observed or {}
    out: dict[str, list[str]] = {}
    for g in games:
        for side in ("home", "away"):
            abbr = to_news(g[side], "proj")
            out[abbr] = sorted(observed.get(abbr) or practice_report_days(g["date"]))
    return out


def restrict_to_practice_days(rows: list[dict], days: Optional[dict[str, list[str]]],
                              source: str) -> list[dict]:
    """Drop rows for teams without a game and practice codes on non-report days.

    NFL.com shows one "latest" practice status with no date; it is moved to
    the team's most recent report day on or before the date it was stamped
    with (a Saturday-evening scrape of a Sunday team is still Friday's
    report), and dropped when the team hasn't had a report day yet.
    """
    if days is None:
        return rows
    out: list[dict] = []
    dropped_teams: set[str] = set()
    for row in rows:
        team_days = days.get(row["team"])
        if team_days is None:
            dropped_teams.add(row["team"])
            continue
        practice = row.get("practice") or {}
        if source == SOURCE_NFLCOM:
            moved: dict[str, str] = {}
            for d, code in practice.items():
                earlier = [day for day in team_days if day <= d]
                if earlier:
                    moved[max(earlier)] = code
            practice = moved
        else:
            practice = {d: c for d, c in practice.items() if d in team_days}
        out.append({**row, "practice": practice})
    if dropped_teams:
        logger.info("%s: dropped rows for %s — no game this week", source, " ".join(sorted(dropped_teams)))
    return out


# ---------------------------------------------------------------------------
# Source 1: team sites
# ---------------------------------------------------------------------------


def _header_index(headers: list[str], *names: str) -> Optional[int]:
    lowered = [h.lower() for h in headers]
    for name in names:
        for i, h in enumerate(lowered):
            if h.startswith(name.lower()):
                return i
    return None


def parse_team_site_html(html: str, game_date: Any = None, date_str: Optional[str] = None) -> list[dict]:
    """Parse a club's ``/team/injury-report/`` page into rows for every club on it.

    ``game_date`` anchors the weekday headers; without it the headers are
    anchored to ``date_str`` (or today). Returns [] when no table is present.
    """
    rows, _clubs = _parse_team_site(html, game_date=game_date, date_str=date_str)
    return rows


def _parse_team_site(html: str, game_date: Any = None, date_str: Optional[str] = None) -> tuple[list[dict], dict[str, bool]]:
    """→ (rows, {club_abbr: has_table})."""
    rows, clubs, _days = _parse_team_site_days(html, game_date=game_date, date_str=date_str)
    return rows, clubs


def _parse_team_site_days(html: str, game_date: Any = None, date_str: Optional[str] = None
                          ) -> tuple[list[dict], dict[str, bool], dict[str, list[str]]]:
    """→ (rows, {club_abbr: has_table}, {club_abbr: dates its day headers map to})."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict] = []
    clubs: dict[str, bool] = {}
    club_days: dict[str, list[str]] = {}
    for container in soup.select("div.nfl-o-injury-report__container"):
        club_name = _text(container.select_one("span.nfl-o-injury-report__club-name")) or _text(
            container.select_one(".nfl-o-injury-report__title"))
        abbr = nickname_to_news(club_name)
        if not abbr:
            logger.warning("Team-site injury container with unknown club name %r — skipped", club_name)
            continue
        table = container.find("table")
        if table is None:
            clubs[abbr] = False
            continue
        clubs[abbr] = True
        headers = [_text(th) for th in table.select("thead th")]
        if not headers:
            first = table.find("tr")
            headers = [_text(c) for c in first.find_all(["th", "td"])] if first else []
        i_player = _header_index(headers, "player", "name")
        i_pos = _header_index(headers, "position", "pos")
        i_inj = _header_index(headers, "injury", "injuries")
        i_status = _header_index(headers, "game status", "status")
        day_cols = [(i, h) for i, h in enumerate(headers) if _weekday_index(h) is not None]
        day_headers = [h for _, h in day_cols]
        if game_date:
            day_dates = map_day_headers_to_dates(day_headers, game_date)
        else:
            day_dates = _map_day_headers_from_today(day_headers, date_str or date.today())
        if i_player is None:
            logger.warning("Team-site injury table for %s has unexpected headers %s", abbr, headers)
            continue
        if game_date:
            club_days[abbr] = sorted(set(day_dates.values()))

        body_rows = table.select("tbody tr") or table.find_all("tr")[1:]
        for tr in body_rows:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= i_player:
                continue
            name = _text(cells[i_player])
            if not name:
                continue
            pos = _text(cells[i_pos]) if i_pos is not None and i_pos < len(cells) else ""
            injury = _text(cells[i_inj]) if i_inj is not None and i_inj < len(cells) else ""
            status_cell = tr.find("td", class_="nfl-o-injury-report__game-status")
            if status_cell is None and i_status is not None and i_status < len(cells):
                status_cell = cells[i_status]
            practice: dict[str, str] = {}
            for i, h in day_cols:
                if i >= len(cells) or h not in day_dates:
                    continue
                code = normalize_practice(_text(cells[i]))
                if code:
                    practice[day_dates[h]] = code
            practice = _drop_future(practice, date_str)
            rows.append(_make_row(abbr, name, pos, injury, practice,
                                  normalize_game_status(_text(status_cell)), SOURCE_TEAM_SITE))
    return rows, clubs, club_days


def _fetch_team_site(session: requests.Session, team: dict, settings: Optional[dict] = None,
                     schedule: Optional[list[dict]] = None, week: Optional[int] = None,
                     date_str: Optional[str] = None) -> dict:
    """Fetch + parse one club's page. Raises on HTTP failure (the caller records it).

    → {"abbr", "url", "rows", "clubs": {abbr: has_table}, "has_table", "mismatch"}
    """
    abbr = team["abbr"]
    domain = (team.get("site_domain") or "").strip()
    if not domain:
        raise ValueError(f"{abbr} has no site_domain in teams.yaml")
    url = f"https://www.{domain}/team/injury-report/"
    resp = session.get(url, timeout=_timeout(settings))
    resp.raise_for_status()

    expected_opp, game_date = _team_game_info(schedule, abbr, week)
    rows, clubs, club_days = _parse_team_site_days(resp.text, game_date=game_date, date_str=date_str)
    if not clubs and len(resp.text) < 20_000:
        # A real club page is ~250 KB even with no report posted; a tiny body
        # usually means an interstitial / bot wall rather than "not posted".
        logger.warning("%s injury page unexpectedly small (%d chars, no report container) — possible block",
                       abbr, len(resp.text))

    mismatch = False
    if expected_opp:
        allowed = {abbr, expected_opp}
        others = {c for c in clubs if c not in allowed}
        if others:
            # The page is showing a different matchup (most likely last or
            # next week's report). Its weekday headers can't be dated against
            # this week's game, so the rows are dropped rather than mis-dated.
            mismatch = True
            logger.info("%s injury page shows %s, expected %s vs %s this week — skipped",
                        abbr, sorted(clubs), abbr, expected_opp)
    elif clubs and schedule and week and games_for_week(schedule, week):
        # On a bye the page still carries last week's report; with no game
        # date its headers would be dated against today and land in this week.
        mismatch = True
        logger.info("%s injury page shows %s, but %s has no game in week %s — skipped",
                    abbr, sorted(clubs), abbr, week)
    if mismatch:
        rows, club_days = [], {}
    return {
        "abbr": abbr,
        "url": url,
        "rows": rows,
        "clubs": clubs,
        "practice_days": club_days,
        "has_table": any(clubs.values()),
        "mismatch": mismatch,
    }


def fetch_team_site_report(session: requests.Session, team: dict, settings: Optional[dict] = None,
                           schedule: Optional[list[dict]] = None, week: Optional[int] = None,
                           date_str: Optional[str] = None) -> list[dict]:
    """Rows for BOTH clubs on ``team``'s injury-report page ([] when no table).

    ``team`` is a ``config/teams.yaml`` entry (needs ``abbr`` + ``site_domain``).
    ``schedule``/``week`` give the game date used to date the weekday headers;
    both are loaded on demand when omitted.
    """
    if schedule is None:
        schedule = load_schedule(settings=settings)
    if week is None and schedule:
        week = week_from_date(schedule, date_str)
    return _fetch_team_site(session, team, settings, schedule, week, date_str)["rows"]


def fetch_all_team_sites(session: requests.Session, settings: Optional[dict] = None,
                         schedule: Optional[list[dict]] = None, week: Optional[int] = None,
                         date_str: Optional[str] = None, workers: Optional[int] = None,
                         teams: Optional[list[dict]] = None) -> tuple[list[dict], dict]:
    """ThreadPool over the 32 club pages → (rows, status).

    ``status`` = {"with_table": [abbr...], "without_table": [abbr...],
    "mismatch": [abbr...], "failed": {abbr: error}, "practice_days": {abbr:
    [dates from its table's day headers]}}. Rows from a club that appears on
    two pages (its own + its opponent's) are deduped in :func:`merge_sources`.
    """
    cfg = _settings(settings)
    workers = workers or int(cfg.get("injury_report", {}).get("team_site_workers", DEFAULT_TEAM_SITE_WORKERS))
    teams = list(teams if teams is not None else get_teams())
    status: dict = {"with_table": [], "without_table": [], "mismatch": [], "failed": {}, "practice_days": {}}
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_fetch_team_site, session, t, cfg, schedule, week, date_str): t["abbr"]
            for t in teams
        }
        for fut in as_completed(futures):
            abbr = futures[fut]
            try:
                result = fut.result()
            except Exception as e:  # noqa: BLE001 — one club failing must not sink the rest
                logger.warning("Team-site injury report failed for %s: %s", abbr, e)
                status["failed"][abbr] = str(e)
                continue
            if result["mismatch"]:
                status["mismatch"].append(abbr)
            elif result["has_table"]:
                status["with_table"].append(abbr)
            else:
                status["without_table"].append(abbr)
            rows.extend(result["rows"])
            for club, days in (result.get("practice_days") or {}).items():
                status["practice_days"][club] = sorted(set(status["practice_days"].get(club, [])) | set(days))
    for k in ("with_table", "without_table", "mismatch"):
        status[k].sort()
    logger.info("Team-site injury reports: %d with a table, %d not posted, %d mismatched, %d failed; %d rows",
                len(status["with_table"]), len(status["without_table"]), len(status["mismatch"]),
                len(status["failed"]), len(rows))
    return rows, status


# ---------------------------------------------------------------------------
# Source 2: RotoWire practice report
# ---------------------------------------------------------------------------


def parse_rotowire_rows(data: list[dict], week_dates: dict[str, str],
                        schedule: Optional[list[dict]] = None, week: Optional[int] = None,
                        date_str: Optional[str] = None) -> list[dict]:
    """RotoWire JSON entries → rows.

    Weekday keys are dated by walking back from the team's game date when the
    schedule knows it (the same rule as the team-site headers — RotoWire's
    ``sunday``/``monday`` for the Week 1 Wednesday opener are the Sun/Mon
    *before* the game, outside the Tue..Mon week window); ``week_dates`` is
    the fallback for teams without a game.

    RotoWire's ``status`` is its own fantasy tag, not the club's game
    designation: on the Friday morning of Week 2, before any Sunday club had
    designated anyone, 131 of 231 rows read "Questionable", and a tag sticks
    from the week before. It is never used as ``game_status``.
    """
    rows: list[dict] = []
    game_dates: dict[str, Optional[str]] = {}
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        team = to_news(str(entry.get("team", "")), "rotowire")
        name = entry.get("player") or f"{entry.get('firstname', '')} {entry.get('lastname', '')}"
        if not team or not str(name).strip():
            continue
        if team not in game_dates:
            game_dates[team] = _team_game_info(schedule, team, week)[1]
        gd = game_dates[team]
        practice: dict[str, str] = {}
        for key in ROTOWIRE_DAY_KEYS:
            code = normalize_practice(entry.get(key, ""))
            if not code:
                continue
            if gd:
                d = _last_weekday_before(_as_date(gd), ROTOWIRE_DAY_KEYS.index(key)).isoformat()
            else:
                d = week_dates.get(key)
            if d:
                practice[d] = code
        practice = _drop_future(practice, date_str)
        rows.append(_make_row(team, str(name), entry.get("pos", ""), entry.get("injtype", ""),
                              practice, "", SOURCE_ROTOWIRE))
    return rows


def fetch_rotowire_report(session: requests.Session, week_dates: dict[str, str],
                          settings: Optional[dict] = None, schedule: Optional[list[dict]] = None,
                          week: Optional[int] = None, date_str: Optional[str] = None) -> list[dict]:
    """League-wide practice report rows (source ``rotowire``). Raises on HTTP/JSON failure."""
    resp = session.get(
        ROTOWIRE_URL,
        headers={
            "Referer": ROTOWIRE_REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=_timeout(settings),
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"RotoWire practice report returned {type(data).__name__}, expected a list")
    rows = parse_rotowire_rows(data, week_dates, schedule=schedule, week=week, date_str=date_str)
    logger.info("RotoWire practice report: %d rows", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Source 3: NFL.com /injuries/
# ---------------------------------------------------------------------------


def nflcom_practice_date(scrape_dt: Optional[datetime] = None) -> str:
    """Date the single NFL.com practice status belongs to: scrape time in
    America/New_York minus 12h (a 10:00 UTC run reports yesterday's practice;
    a 22:00 UTC run reports today's)."""
    dt = scrape_dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        et = dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — no tz database: assume EDT
        et = dt.astimezone(timezone(timedelta(hours=-4)))
    return (et - timedelta(hours=12)).date().isoformat()


_WEEK_RE = re.compile(r"Week\s+(\d+)\s+of\s+the\s+(\d{4})\s+Season", re.IGNORECASE)
_ROOFLINE_WEEK_RE = re.compile(r"WEEK\s+(\d+)", re.IGNORECASE)


def parse_nflcom_html(html: str, practice_date: str) -> tuple[Optional[int], list[dict]]:
    """NFL.com injuries page → (week, rows). Team label per table is the
    ``div.nfl-t-stats__title`` nickname preceding it; the matchup-strip
    abbreviations (``AZ``, ``LAR``) are the fallback, mapped via ``to_news``."""
    soup = BeautifulSoup(html, "html.parser")
    week: Optional[int] = None
    m = _WEEK_RE.search(_text(soup.select_one("h1.nfl-o-page-title")))
    if m:
        week = int(m.group(1))
    else:
        m = _ROOFLINE_WEEK_RE.search(_text(soup.select_one("h2.nfl-c-content-header__roofline")))
        if m:
            week = int(m.group(1))

    rows: list[dict] = []
    for unit in soup.select("section.nfl-o-injury-report__unit"):
        abbrs = [to_news(_text(a), "nflcom") for a in unit.select(".nfl-c-matchup-strip__team-abbreviation")]
        current_team = ""
        idx = 0
        for el in unit.find_all("div"):
            cls = el.get("class") or []
            if "nfl-t-stats__title" in cls:
                current_team = nickname_to_news(_text(el)) or (abbrs[idx] if idx < len(abbrs) else "")
                idx += 1
            elif "d3-o-table--horizontal-scroll" in cls:
                table = el.find("table")
                if table is None or not current_team:
                    continue
                rows.extend(_parse_nflcom_table(table, current_team, practice_date))
    return week, rows


def _parse_nflcom_table(table, team: str, practice_date: str) -> list[dict]:
    headers = [_text(th) for th in table.select("thead th")]
    i_player = _header_index(headers, "player") if headers else 0
    i_pos = _header_index(headers, "position", "pos") if headers else 1
    i_inj = _header_index(headers, "injur") if headers else 2
    i_prac = _header_index(headers, "practice") if headers else 3
    i_status = _header_index(headers, "game status", "status") if headers else 4
    if i_player is None:
        return []
    rows = []
    for tr in table.select("tbody tr") or table.find_all("tr")[1:]:
        cells = tr.find_all(["td", "th"])
        if len(cells) <= i_player:
            continue
        name = _text(cells[i_player])
        if not name:
            continue

        def cell(i: Optional[int]) -> str:
            return _text(cells[i]) if i is not None and i < len(cells) else ""

        code = normalize_practice(cell(i_prac))
        practice = {practice_date: code} if code and practice_date else {}
        rows.append(_make_row(team, name, cell(i_pos), cell(i_inj), practice,
                              normalize_game_status(cell(i_status)), SOURCE_NFLCOM))
    return rows


def fetch_nflcom_report(session: requests.Session, settings: Optional[dict] = None,
                        scrape_dt: Optional[datetime] = None,
                        date_str: Optional[str] = None) -> tuple[Optional[int], list[dict]]:
    """(week from the page title, rows with source ``nflcom``). Raises on HTTP failure."""
    resp = session.get(NFLCOM_URL, timeout=_timeout(settings))
    resp.raise_for_status()
    practice_date = nflcom_practice_date(scrape_dt)
    if date_str and practice_date > date_str:
        practice_date = date_str
    week, rows = parse_nflcom_html(resp.text, practice_date)
    logger.info("NFL.com injuries: week %s, %d rows (practice status dated %s)", week, len(rows), practice_date)
    return week, rows


# ---------------------------------------------------------------------------
# Merge across sources
# ---------------------------------------------------------------------------


def merge_sources(rows_by_source: dict[str, list[dict]]) -> tuple[list[dict], list[dict]]:
    """Dedupe rows by (team, name_key) with team_site > rotowire > nflcom.

    A lower-precedence source only fills gaps (empty pos/injury/game_status,
    practice dates the higher source didn't have). Disagreements on a practice
    code for the same date, or on game_status, are recorded in ``conflicts``
    as ``{team, name, field, date, values: {source: value}}``. Two rows from
    the *same* source (a club seen on its own page and its opponent's) merge
    silently.
    """
    merged: dict[tuple[str, str], dict] = {}
    conflicts: list[dict] = []
    for source in SOURCE_PRECEDENCE:
        for row in rows_by_source.get(source, []) or []:
            key = (row["team"], row["name_key"])
            if key not in merged:
                cur = copy.deepcopy(row)
                cur["source"] = source
                cur["sources"] = [source]
                cur["_practice_src"] = {d: source for d in cur["practice"]}
                cur["_status_src"] = source if cur.get("game_status") else ""
                merged[key] = cur
                continue
            cur = merged[key]
            if source not in cur["sources"]:
                cur["sources"].append(source)
            for f in ("name", "pos", "injury"):
                if not cur.get(f) and row.get(f):
                    cur[f] = row[f]
            gs = row.get("game_status") or ""
            if gs:
                if not cur.get("game_status"):
                    cur["game_status"] = gs
                    cur["_status_src"] = source
                elif cur["game_status"] != gs:
                    if cur["_status_src"] != source:
                        conflicts.append({
                            "team": row["team"], "name": cur["name"], "field": "game_status",
                            "date": None, "values": {cur["_status_src"]: cur["game_status"], source: gs},
                        })
                    else:
                        logger.warning("%s %s: same-source game_status disagreement %s vs %s (%s)",
                                       row["team"], cur["name"], cur["game_status"], gs, source)
            for d, code in (row.get("practice") or {}).items():
                if not code:
                    continue
                have = cur["practice"].get(d)
                if not have:
                    cur["practice"][d] = code
                    cur["_practice_src"][d] = source
                elif have != code:
                    src = cur["_practice_src"].get(d, cur["source"])
                    if src != source:
                        conflicts.append({
                            "team": row["team"], "name": cur["name"], "field": "practice",
                            "date": d, "values": {src: have, source: code},
                        })
                    else:
                        logger.warning("%s %s: same-source practice disagreement on %s: %s vs %s (%s)",
                                       row["team"], cur["name"], d, have, code, source)
    out = []
    for cur in merged.values():
        cur.pop("_practice_src", None)
        # Which source supplied the designation — a RotoWire "Questionable"
        # filling an official UNSPECIFIED gap is not the same as a club's
        # Friday designation, and the dashboard should be able to say so.
        cur["game_status_source"] = cur.pop("_status_src", "") or ""
        out.append(cur)
    return out, conflicts


# ---------------------------------------------------------------------------
# Week file
# ---------------------------------------------------------------------------


def _team_days(team: dict) -> Optional[list[str]]:
    """A week-file team's practice-report dates: stored ones, else the rule
    from its game date, else None (unknown — nothing is filtered)."""
    if team.get("practice_days"):
        return list(team["practice_days"])
    if team.get("game_date"):
        return practice_report_days(team["game_date"])
    return None


def designation_open(team: dict, date_str: str) -> bool:
    """Game designations go out with a team's final practice report (Fri for
    Sunday, Sat for Monday, Wed for Thursday), so before that day any status
    is left over from the previous week."""
    days = _team_days(team)
    return not days or date_str >= max(days)


def _sanitize_team(team: dict) -> int:
    """Enforce the week's shape on a team already in the file: practice only
    on report days, no designation reported before designation day (that is
    last week's), no RotoWire tag posing as a designation. A designation with
    no ``game_status_date`` predates this rule and is dropped unless a source
    reported it again this run. Players left with nothing but a ``cleared``
    marker are removed. Idempotent; returns how many values it dropped."""
    days = _team_days(team)
    designation_day = max(days) if days else None
    dropped = 0
    players = team.get("players") or {}
    for key in list(players):
        p = players[key]
        if days is not None:
            kept = {d: c for d, c in (p.get("practice") or {}).items() if d in days}
            dropped += len(p.get("practice") or {}) - len(kept)
            p["practice"] = kept
        stale = designation_day is not None and (p.get("game_status_date") or "") < designation_day
        if p.get("game_status") and (stale or p.get("game_status_source") == SOURCE_ROTOWIRE):
            p["game_status"] = ""
            p.pop("game_status_source", None)
            p.pop("game_status_date", None)
            dropped += 1
        if p.get("cleared") and not p.get("practice") and not p.get("game_status"):
            del players[key]
    return dropped


def merge_into_week(rows: list[dict], season: int, week: int, date_str: str,
                    schedule: Optional[list[dict]] = None,
                    sources_used: Optional[dict[str, int]] = None,
                    conflicts: Optional[list[dict]] = None,
                    practice_days: Optional[dict[str, list[str]]] = None) -> tuple[dict, Optional[dict]]:
    """Fold today's merged rows into ``data/injuries/<season>/wk<NN>.json``.

    Per player: practice dict union (today's value wins for the same date),
    game_status / injury / pos latest-non-empty wins, ``first_seen`` kept,
    ``last_seen`` bumped. Players that vanished from a team's report today
    (the team *was* reported by some source) get ``cleared: <date>`` instead
    of being deleted, so the file still shows the whole week and
    :func:`diff_week` can emit ``cleared``. Returns (new, previous).

    ``practice_days`` ({abbr: dates}, from :func:`team_practice_days`) is
    stored per team; every team is then held to it — see
    :func:`_sanitize_team` — which also repairs a file written before the
    rule existed.
    """
    prev = load_week_file(season, week)
    cur: dict = copy.deepcopy(prev) if prev else {"season": int(season), "week": int(week), "teams": {}, "conflicts": []}
    cur["season"] = int(season)
    cur["week"] = int(week)
    cur["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur["last_run_date"] = date_str
    if sources_used is None:
        sources_used = {}
        for r in rows:
            for s in r.get("sources") or [r.get("source", "")]:
                if s:
                    sources_used[s] = sources_used.get(s, 0) + 1
    cur["sources_used"] = dict(sources_used)
    cur["conflicts"] = list(conflicts or [])
    cur.setdefault("teams", {})
    for abbr, days in (practice_days or {}).items():
        if abbr in cur["teams"] and days:
            cur["teams"][abbr]["practice_days"] = sorted(days)

    teams_today: dict[str, set[str]] = {}
    for row in rows:
        abbr = row["team"]
        teams_today.setdefault(abbr, set()).add(row["name_key"])
        team = cur["teams"].setdefault(abbr, {"opp": None, "game_date": None, "players": {}})
        if schedule and (team.get("opp") is None or team.get("game_date") is None):
            opp, gd = _team_game_info(schedule, abbr, week)
            team["opp"], team["game_date"] = opp, gd
        if (practice_days or {}).get(abbr):
            team["practice_days"] = sorted(practice_days[abbr])
        players = team.setdefault("players", {})
        p = players.get(row["name_key"])
        if p is None:
            p = {
                "name": row["name"], "pos": row.get("pos", ""), "injury": row.get("injury", ""),
                "practice": {}, "game_status": "", "first_seen": date_str, "last_seen": date_str,
                "source": row.get("source", ""),
            }
            players[row["name_key"]] = p
        p.pop("cleared", None)
        for d, code in (row.get("practice") or {}).items():
            if code:
                p["practice"][d] = code
        if row.get("game_status") and designation_open(team, date_str):
            p["game_status"] = row["game_status"]
            p["game_status_source"] = row.get("game_status_source") or row.get("source", "")
            p["game_status_date"] = date_str     # last run a source reported it
        for f in ("injury", "pos", "name"):
            if row.get(f):
                p[f] = row[f]
        p["last_seen"] = date_str
        if row.get("source"):
            p["source"] = row["source"]

    for abbr, team in cur["teams"].items():
        seen = teams_today.get(abbr)
        if seen is None:
            continue  # team not reported today by any source — leave its players alone
        for key, p in team.get("players", {}).items():
            if key not in seen and not p.get("cleared"):
                p["cleared"] = date_str

    dropped = sum(_sanitize_team(team) for team in cur["teams"].values())
    if dropped:
        logger.info("Injury week file: dropped %d values outside the week's report days / designation day",
                    dropped)

    path = week_file_path(season, week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Injury week file updated: %s (%d teams)", path, len(cur["teams"]))
    return cur, prev


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _day_label(iso: str) -> str:
    try:
        return weekday_name(iso)
    except ValueError:
        return iso


def _practice_summary(practice: dict[str, str]) -> str:
    return ", ".join(f"{_day_label(d)} {practice[d]}" for d in sorted(practice))


def _status_label(gs: str, source: str = "") -> str:
    label = {"OUT": "Out", "D": "Doubtful", "Q": "Questionable"}.get(gs, gs)
    if label and source and source != SOURCE_TEAM_SITE:
        label = f"{label} (per {source})"   # unofficial fill, not the club's designation
    return label


def _latest_code_before(practice: dict[str, str], day: str) -> tuple[str, str]:
    """(date, code) of the most recent practice entry strictly before ``day``."""
    earlier = [d for d in practice if d < day and practice[d]]
    if not earlier:
        return "", ""
    d = max(earlier)
    return d, practice[d]


def diff_week(prev: Optional[dict], cur: dict) -> list[dict]:
    """Typed change records between two week-file states.

    Types: ``new_listing``, ``practice_upgrade`` (toward FP), ``practice_downgrade``,
    ``practice_status`` (first code for an already-listed player with nothing to
    compare against), ``designation_set``, ``designation_changed``, ``cleared``.
    Each: ``{team, name, pos, injury, type, old, new, date, source, message}``.
    """
    changes: list[dict] = []
    prev_teams = (prev or {}).get("teams", {}) or {}
    cur_teams = cur.get("teams", {}) or {}
    run_date = cur.get("last_run_date", "")

    def rec(abbr: str, p: dict, ctype: str, old: str, new: str, day: str, message: str) -> dict:
        return {
            "team": abbr, "name": p.get("name", ""), "pos": p.get("pos", ""),
            "injury": p.get("injury", ""), "type": ctype, "old": old, "new": new,
            "date": day, "source": p.get("source", ""), "message": message,
        }

    def who(abbr: str, p: dict) -> str:
        pos = f" {p['pos']}" if p.get("pos") else ""
        inj = f" ({p['injury']})" if p.get("injury") else ""
        return f"{abbr}{pos} {p.get('name', '')}{inj}"

    for abbr in sorted(cur_teams):
        team = cur_teams[abbr]
        prev_players = (prev_teams.get(abbr) or {}).get("players", {}) or {}
        for key, p in (team.get("players") or {}).items():
            pp = prev_players.get(key)
            practice = p.get("practice") or {}

            if p.get("cleared"):
                if pp and not pp.get("cleared"):
                    old = _practice_summary(pp.get("practice") or {})
                    if pp.get("game_status"):
                        old = f"{old}; {_status_label(pp['game_status'], pp.get('game_status_source', ''))}".strip("; ")
                    changes.append(rec(abbr, pp, "cleared", old, "", p["cleared"],
                                       f"{who(abbr, pp)} no longer on the injury report"))
                continue

            if pp is None or pp.get("cleared"):
                new = _practice_summary(practice)
                if p.get("game_status"):
                    new = f"{new}; {_status_label(p['game_status'], p.get('game_status_source', ''))}".strip("; ")
                day = max(practice) if practice else (p.get("last_seen") or run_date)
                detail = f": {new}" if new else ""
                changes.append(rec(abbr, p, "new_listing", "", new, day,
                                   f"{who(abbr, p)} added to the injury report{detail}"))
                continue

            prev_practice = pp.get("practice") or {}
            for d in sorted(practice):
                code = practice[d]
                old = prev_practice.get(d, "")
                if old == code:
                    continue
                ref_code = old
                if not ref_code:
                    _ref_day, ref_code = _latest_code_before(practice, d)
                if not ref_code:
                    changes.append(rec(abbr, p, "practice_status", "", code, d,
                                       f"{who(abbr, p)}: {code} {_day_label(d)}"))
                    continue
                if ref_code == code:
                    continue  # same status as the previous practice day — not a change
                ctype = "practice_upgrade" if PRACTICE_RANK.get(code, -1) > PRACTICE_RANK.get(ref_code, -1) else "practice_downgrade"
                changes.append(rec(abbr, p, ctype, ref_code, code, d,
                                   f"{who(abbr, p)}: {ref_code} -> {code} {_day_label(d)}"))

            og, ng = pp.get("game_status") or "", p.get("game_status") or ""
            if ng and not og:
                changes.append(rec(abbr, p, "designation_set", "", ng, p.get("last_seen") or run_date,
                                   f"{who(abbr, p)} designated {_status_label(ng, p.get('game_status_source', ''))}"))
            elif ng and og and ng != og:
                changes.append(rec(abbr, p, "designation_changed", og, ng, p.get("last_seen") or run_date,
                                   f"{who(abbr, p)}: {_status_label(og, pp.get('game_status_source', ''))} -> "
                                   f"{_status_label(ng, p.get('game_status_source', ''))}"))

    # Players present before but missing entirely now (defensive: merge_into_week
    # keeps them with a ``cleared`` marker, but a hand-edited file might not).
    for abbr in sorted(prev_teams):
        cur_players = (cur_teams.get(abbr) or {}).get("players", {}) or {}
        for key, pp in ((prev_teams[abbr] or {}).get("players") or {}).items():
            if pp.get("cleared") or key in cur_players:
                continue
            old = _practice_summary(pp.get("practice") or {})
            changes.append(rec(abbr, pp, "cleared", old, "", run_date,
                               f"{who(abbr, pp)} no longer on the injury report"))
    return changes


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_sources(settings: dict, sources: Optional[list[str]]) -> list[str]:
    raw = sources if sources is not None else settings.get("injury_report", {}).get("sources", ["team_sites", "rotowire", "nflcom"])
    out: list[str] = []
    for s in raw:
        label = SETTINGS_SOURCE_NAMES.get(str(s).strip().lower())
        if label and label not in out:
            out.append(label)
        elif not label:
            logger.warning("Unknown injury_report source %r ignored", s)
    return out


def collect_injury_report(date_str: Optional[str] = None, settings: Optional[dict] = None,
                          sources: Optional[list[str]] = None, session: Optional[requests.Session] = None,
                          schedule: Optional[list[dict]] = None, scrape_dt: Optional[datetime] = None) -> dict:
    """Run the configured sources, merge, fold into the week file, diff.

    Never raises: every source is fail-soft and problems land in ``errors``.
    Returns ``{date, week, season, file, rows, sources_used, team_sites_with_table,
    team_sites_without_table, team_sites_mismatch, team_sites_failed, changes,
    conflicts, errors}``.
    """
    date_str = date_str or date.today().isoformat()
    result: dict = {
        "date": date_str, "week": None, "season": None, "file": None, "rows": 0,
        "sources_used": {}, "team_sites_with_table": [], "team_sites_without_table": [],
        "team_sites_mismatch": [], "team_sites_failed": {}, "nflcom_week_mismatch": None,
        "changes": [], "conflicts": [], "errors": [],
    }
    try:
        cfg = _settings(settings)
        season = get_season_year(cfg)
        result["season"] = season
        if schedule is None:
            try:
                schedule = load_schedule(settings=cfg, season=season)
            except Exception as e:  # noqa: BLE001
                logger.warning("Schedule unavailable: %s", e)
                schedule = []
        week = week_from_date(schedule, date_str) if schedule else None
        result["week"] = week
        wanted = _resolve_sources(cfg, sources)
        if session is None:
            session = make_session(cfg)

        rows_by_source: dict[str, list[dict]] = {}
        nfl_week: Optional[int] = None

        observed_days: dict[str, list[str]] = {}
        if SOURCE_TEAM_SITE in wanted:
            try:
                rows, status = fetch_all_team_sites(session, cfg, schedule, week, date_str)
                rows_by_source[SOURCE_TEAM_SITE] = rows
                result["team_sites_with_table"] = status["with_table"]
                result["team_sites_without_table"] = status["without_table"]
                result["team_sites_mismatch"] = status["mismatch"]
                result["team_sites_failed"] = status["failed"]
                observed_days = status.get("practice_days") or {}
            except Exception as e:  # noqa: BLE001
                logger.exception("Team-site injury reports failed")
                result["errors"].append(f"team_sites: {e}")

        if SOURCE_ROTOWIRE in wanted:
            try:
                week_dates = week_window_dates(schedule, week) if (schedule and week) else {}
                rows_by_source[SOURCE_ROTOWIRE] = fetch_rotowire_report(
                    session, week_dates, cfg, schedule=schedule, week=week, date_str=date_str)
            except Exception as e:  # noqa: BLE001
                logger.warning("RotoWire practice report failed (soft): %s", e)
                result["errors"].append(f"rotowire: {e}")

        if SOURCE_NFLCOM in wanted:
            try:
                nfl_week, rows_by_source[SOURCE_NFLCOM] = fetch_nflcom_report(
                    session, cfg, scrape_dt=scrape_dt, date_str=date_str)
            except Exception as e:  # noqa: BLE001
                logger.warning("NFL.com injuries failed (soft): %s", e)
                result["errors"].append(f"nflcom: {e}")

        if week is None:
            week = nfl_week
            result["week"] = week
        if week is None:
            result["errors"].append("could not determine the NFL week (no schedule, no NFL.com title)")
            return result
        if nfl_week is not None and nfl_week != week and rows_by_source.get(SOURCE_NFLCOM):
            # NFL.com keeps the finished week up until the new week's first
            # report (the Tuesday of Week 2 still said "Week 1"); taking it
            # would carry last week's designations and final practice status
            # into this week's file.
            logger.info("NFL.com injuries page is week %d, collecting week %d — %d rows ignored",
                        nfl_week, week, len(rows_by_source[SOURCE_NFLCOM]))
            result["nflcom_week_mismatch"] = nfl_week
            rows_by_source[SOURCE_NFLCOM] = []

        practice_days = team_practice_days(schedule, week, observed_days)
        for source in list(rows_by_source):
            rows_by_source[source] = restrict_to_practice_days(rows_by_source[source], practice_days, source)

        # Unique players per source (a club is on its own page AND its opponent's).
        result["sources_used"] = {s: len({(r["team"], r["name_key"]) for r in rows})
                                  for s, rows in rows_by_source.items()}
        merged, conflicts = merge_sources(rows_by_source)
        result["rows"] = len(merged)
        result["conflicts"] = conflicts
        if conflicts:
            logger.info("Injury report: %d source conflicts", len(conflicts))

        cur, prev = merge_into_week(merged, season, week, date_str, schedule=schedule,
                                    sources_used=result["sources_used"], conflicts=conflicts,
                                    practice_days=practice_days)
        result["file"] = str(week_file_path(season, week))
        result["changes"] = diff_week(prev, cur)
        logger.info("Injury report week %d: %d players, %d changes", week, len(merged), len(result["changes"]))
    except Exception as e:  # noqa: BLE001 — the daily pipeline must keep going
        logger.exception("Injury report collection failed")
        result["errors"].append(str(e))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(result: dict) -> None:
    print(f"Injury report {result['date']}  season {result['season']}  week {result['week']}")
    print(f"  file: {result['file']}")
    print(f"  merged rows: {result['rows']}")
    for s in SOURCE_PRECEDENCE:
        if s in result["sources_used"]:
            print(f"  {s:>10}: {result['sources_used'][s]} rows")
    with_t = result["team_sites_with_table"]
    without = result["team_sites_without_table"]
    print(f"  team sites with a table ({len(with_t)}): {' '.join(with_t) or '-'}")
    print(f"  team sites not posted   ({len(without)}): {' '.join(without) or '-'}")
    if result["team_sites_mismatch"]:
        print(f"  team sites other matchup ({len(result['team_sites_mismatch'])}): {' '.join(result['team_sites_mismatch'])}")
    if result.get("nflcom_week_mismatch"):
        print(f"  NFL.com still on week {result['nflcom_week_mismatch']} — its rows were ignored")
    if result["team_sites_failed"]:
        print(f"  team sites failed ({len(result['team_sites_failed'])}):")
        for abbr, err in sorted(result["team_sites_failed"].items()):
            print(f"    {abbr}: {err}")
    changes = result["changes"]
    print(f"  changes ({len(changes)}):")
    for c in changes[:20]:
        print(f"    [{c['type']}] {c['message']}")
    if len(changes) > 20:
        print(f"    ... {len(changes) - 20} more")
    conflicts = result["conflicts"]
    print(f"  conflicts ({len(conflicts)}):")
    for c in conflicts[:20]:
        vals = ", ".join(f"{k}={v}" for k, v in c["values"].items())
        print(f"    {c['team']} {c['name']} {c['field']}{' ' + c['date'] if c.get('date') else ''}: {vals}")
    if result["errors"]:
        print("  errors:")
        for e in result["errors"]:
            print(f"    {e}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the structured NFL injury report for a date.")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--sources", default=None,
                        help="Comma-separated subset of team_sites,rotowire,nflcom (default: settings)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    sources = [s.strip() for s in args.sources.split(",")] if args.sources else None
    result = collect_injury_report(args.date, sources=sources)
    _print_summary(result)
    return 1 if (result["errors"] and not result["file"]) else 0


if __name__ == "__main__":
    sys.exit(main())
