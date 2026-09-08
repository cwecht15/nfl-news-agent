"""Game-day inactives (in-season only).

Teams declare their inactives ~90 minutes before kickoff. NFL.com's
inactives page is rendered client-side, so the source here is ESPN's
per-game competitor roster:

    https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/
        events/<event>/competitions/<event>/competitors/<team>/roster

Each entry carries ``active`` (pregame: dressed / not dressed) and
``didNotPlay`` (set once the game is under way / final). ESPN 403s
browser-style User-Agents but answers a plain requests UA, so the session
here deliberately keeps the default one.

Player identity: ESPN ``playerId`` → nflverse ``espn_id`` (the nflverse
snapshot already on disk), which gives GSIS id, position and the
news-style team abbreviation without any per-player fetch; ESPN's own
``displayName`` is the fallback.

Output: ``data/inactives/<season>/wk<NN>.json`` — one file per week,
merged on every run:

    {"season": 2026, "week": 1, "updated_at": ..., "games": {
        "<event_id>": {"name": "NE @ SEA", "date": "2026-09-10T00:20Z",
                       "status": "STATUS_SCHEDULED", "home": "SEA", "away": "NE",
                       "teams": {"NE": {"published_at": ..., "phase": "pregame"|"postgame",
                                        "inactives": [{"name", "name_key", "pos", "team",
                                                       "jersey", "espn_id", "gsis_id"}]}}}}}

Guard against unpublished pregame rosters (every entry ``active: false``):
a pregame list is only accepted when the dressed count looks like a real
46–48 and the inactive count is small.

CLI:
    python collectors/inactives_collector.py [--date YYYY-MM-DD] [--week N] [--event ESPN_ID] [--all] [--season YYYY]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings
from processing import season as season_mod
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ROSTER_URL = ("https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
              "events/{event}/competitions/{event}/competitors/{team}/roster")

# Pregame publish guard: a real NFL game-day roster dresses 46–48 of 53.
MIN_DRESSED = 40
MAX_DRESSED = 53
MAX_INACTIVE = 15

# Only games kicking off within this window (or already started) are polled.
LOOKAHEAD = timedelta(hours=2, minutes=30)
LOOKBACK = timedelta(hours=30)

DEFAULT_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    return get_data_dir("inactives")


def week_file_path(season: int, week: int) -> Path:
    d = _base_dir() / str(season)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"wk{int(week):02d}.json"


def load_week_file(season: int, week: int) -> Optional[dict]:
    p = week_file_path(season, week)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_week_file(data: dict) -> Path:
    p = week_file_path(int(data["season"]), int(data["week"]))
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# ESPN fetches
# ---------------------------------------------------------------------------


def make_session() -> requests.Session:
    s = requests.Session()
    # ESPN's public JSON APIs reject browser UAs from scripts but accept the
    # library default — do NOT copy the browser UA the other collectors use.
    s.headers.update({"Accept": "application/json"})
    return s


def fetch_scoreboard(session: requests.Session, season: int, week: int, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Week's games: [{event_id, name, short_name, date (ISO UTC), status, home, away, competitors{ABBR: cid}}]."""
    r = session.get(SCOREBOARD_URL, params={"seasontype": 2, "week": week, "dates": season}, timeout=timeout)
    r.raise_for_status()
    games: list[dict] = []
    for ev in r.json().get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        competitors: dict[str, str] = {}
        home = away = ""
        for c in comp.get("competitors", []) or []:
            abbr = to_news(str((c.get("team") or {}).get("abbreviation") or ""), "espn")
            if not abbr:
                continue
            competitors[abbr] = str(c.get("id"))
            if c.get("homeAway") == "home":
                home = abbr
            elif c.get("homeAway") == "away":
                away = abbr
        games.append({
            "event_id": str(ev.get("id")),
            "name": ev.get("name", ""),
            "short_name": ev.get("shortName", ""),
            "date": ev.get("date", ""),
            "status": ((ev.get("status") or {}).get("type") or {}).get("name", ""),
            "home": home, "away": away,
            "competitors": competitors,
        })
    return games


def fetch_game_roster(session: requests.Session, event_id: str, competitor_id: str,
                      timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    r = session.get(ROSTER_URL.format(event=event_id, team=competitor_id), timeout=timeout)
    r.raise_for_status()
    return list(r.json().get("entries", []) or [])


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _name_key(name: str) -> str:
    from processing.sheet_reconciliation import _normalize_name
    return _normalize_name(name or "")


def _espn_index(nflverse: Optional[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for p in (nflverse or {}).values():
        eid = str(p.get("espn_id") or "").strip()
        if eid:
            idx[eid] = p
    return idx


def _athlete_cache_path() -> Path:
    return _base_dir() / "espn_athletes.json"


def _load_athlete_cache() -> dict[str, dict]:
    p = _athlete_cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_athlete_cache(cache: dict[str, dict]) -> None:
    _athlete_cache_path().write_text(json.dumps(cache, indent=0, ensure_ascii=False), encoding="utf-8")


def lookup_athlete(session: Optional[requests.Session], ref: str, cache: dict[str, dict]) -> dict:
    """{name, pos} for an ESPN athlete ``$ref`` (roster entries only carry a
    last name); results are cached on disk across runs."""
    if not ref:
        return {}
    key = ref.split("?")[0]
    if key in cache:
        return cache[key]
    if session is None:
        return {}
    try:
        a = session.get(ref, timeout=DEFAULT_TIMEOUT).json()
        rec = {"name": a.get("displayName") or a.get("fullName") or "",
               "pos": str((a.get("position") or {}).get("abbreviation") or "").upper()}
    except Exception:  # noqa: BLE001 — name fallback only
        rec = {}
    cache[key] = rec
    return rec


def parse_roster_entries(entries: list[dict], game_status: str, team: str,
                         nflverse: Optional[dict] = None, session: Optional[requests.Session] = None,
                         athlete_cache: Optional[dict] = None) -> tuple[str, list[dict]]:
    """Return ``(phase, inactives)``.

    * Scheduled game → pregame: inactive = ``active is False``; accepted only
      when the dressed/inactive counts look published (else ``("unpublished", [])``).
    * In-progress / final → postgame: inactive = ``didNotPlay is True``.

    Identity comes from nflverse via ``espn_id``; players missing there (new
    signings, last season's rosters) fall back to ESPN's athlete record.
    """
    if not entries:
        return "unpublished", []
    idx = _espn_index(nflverse)
    pregame = game_status in ("STATUS_SCHEDULED", "")
    cache = athlete_cache if athlete_cache is not None else {}

    def _row(e: dict) -> dict:
        pid = str(e.get("playerId") or "")
        nv = idx.get(pid, {})
        name = nv.get("name") or ""
        pos = nv.get("pos") or ""
        if not name:
            ath = lookup_athlete(session, str((e.get("athlete") or {}).get("$ref") or ""), cache)
            name = ath.get("name") or e.get("displayName") or ""
            pos = pos or ath.get("pos") or ""
        pos = pos or str((e.get("position") or {}).get("abbreviation") or "").upper()
        return {
            "name": name, "name_key": nv.get("name_key") or _name_key(name), "pos": pos,
            "team": team, "jersey": str(e.get("jersey") or ""), "espn_id": pid,
            "gsis_id": nv.get("gsis_id"),
        }

    if pregame:
        dressed = sum(1 for e in entries if e.get("active") is True)
        scratched = [e for e in entries if e.get("active") is False]
        if not (MIN_DRESSED <= dressed <= MAX_DRESSED and 1 <= len(scratched) <= MAX_INACTIVE):
            return "unpublished", []
        return "pregame", [_row(e) for e in scratched]

    dnp = [e for e in entries if e.get("didNotPlay") is True]
    return "postgame", [_row(e) for e in dnp]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _games_to_poll(games: list[dict], now: datetime, force_all: bool = False) -> list[dict]:
    """Games kicking off soon, in progress, or finished within LOOKBACK."""
    out = []
    for g in games:
        try:
            kick = datetime.fromisoformat(str(g["date"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if force_all or (now - LOOKBACK) <= kick <= (now + LOOKAHEAD):
            out.append(g)
    return out


def merge_into_week(data: Optional[dict], season: int, week: int, game: dict, team: str,
                    phase: str, inactives: list[dict], now_iso: str) -> tuple[dict, bool]:
    """Merge one team's list into the week file; returns ``(data, changed)``.

    A postgame (didNotPlay) list is authoritative and is never replaced by a
    later pregame read.
    """
    data = data or {"season": season, "week": week, "updated_at": now_iso, "games": {}}
    g = data["games"].setdefault(game["event_id"], {
        "name": game.get("name", ""), "short_name": game.get("short_name", ""), "date": game.get("date", ""),
        "status": game.get("status", ""), "home": game.get("home", ""), "away": game.get("away", ""), "teams": {},
    })
    g["status"] = game.get("status") or g.get("status", "")
    prev = g["teams"].get(team)
    if prev and prev.get("phase") == "postgame" and phase == "pregame":
        return data, False
    new_names = sorted(p["name_key"] for p in inactives)
    old_names = sorted(p["name_key"] for p in (prev or {}).get("inactives", []))
    changed = prev is None or new_names != old_names or prev.get("phase") != phase
    if changed:
        g["teams"][team] = {
            "published_at": (prev or {}).get("published_at") or now_iso,
            "updated_at": now_iso, "phase": phase, "inactives": inactives,
        }
    else:
        prev["updated_at"] = now_iso
    data["updated_at"] = now_iso
    return data, changed


def collect_inactives(date_str: Optional[str] = None, week: Optional[int] = None, settings: Optional[dict] = None,
                      session: Optional[requests.Session] = None, now: Optional[datetime] = None,
                      force_all: bool = False, only_event: Optional[str] = None,
                      season: Optional[int] = None) -> dict:
    """Poll ESPN for this week's games near kickoff and merge inactives into the week file.

    Never raises. Returns ``{season, week, file, games_polled, published: {team: n}, changes: [...],
    errors: [...]}`` where ``changes`` lists teams whose inactives were newly
    published or changed on this run (each with the player rows).
    """
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    date_str = date_str or now.date().isoformat()
    result: dict[str, Any] = {"date": date_str, "season": None, "week": week, "file": None,
                              "games_polled": 0, "published": {}, "changes": [], "errors": []}
    try:
        season = season or season_mod.get_season_year(settings)
        result["season"] = season
        schedule = season_mod.load_schedule(settings=settings, season=season)
        if week is None:
            week = season_mod.week_from_date(schedule, date_str) if schedule else None
        if not week:
            result["errors"].append("could not determine the NFL week")
            return result
        result["week"] = week

        session = session or make_session()
        games = fetch_scoreboard(session, season, week)
        polled = _games_to_poll(games, now, force_all=force_all)
        if only_event:
            polled = [g for g in games if g["event_id"] == str(only_event)]
        result["games_polled"] = len(polled)

        nflverse = None
        try:
            from collectors.nflverse_roster_collector import latest_nflverse_snapshot
            nflverse, _ = latest_nflverse_snapshot()
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"nflverse: {e}")

        data = load_week_file(season, week)
        now_iso = now.isoformat(timespec="seconds")
        athlete_cache = _load_athlete_cache()
        for g in polled:
            for team, cid in g["competitors"].items():
                try:
                    entries = fetch_game_roster(session, g["event_id"], cid)
                except Exception as e:  # noqa: BLE001
                    result["errors"].append(f"{g['short_name']} {team}: {e}")
                    continue
                phase, inactives = parse_roster_entries(entries, g["status"], team, nflverse,
                                                        session=session, athlete_cache=athlete_cache)
                if phase == "unpublished":
                    continue
                data, changed = merge_into_week(data, season, week, g, team, phase, inactives, now_iso)
                result["published"][team] = len(inactives)
                if changed:
                    result["changes"].append({
                        "team": team, "game": g["short_name"], "event_id": g["event_id"],
                        "phase": phase, "date": g["date"], "inactives": inactives,
                    })
        if data:
            result["file"] = str(save_week_file(data))
        if athlete_cache:
            _save_athlete_cache(athlete_cache)
        logger.info("Inactives week %s: %d games polled, %d teams published, %d changed",
                    week, len(polled), len(result["published"]), len(result["changes"]))
    except Exception as e:  # noqa: BLE001 — the pipeline must keep going
        logger.exception("Inactives collection failed")
        result["errors"].append(str(e))
    return result


def inactive_players_for_week(season: int, week: int) -> dict[tuple[str, str], dict]:
    """{(team, name_key): row} for every inactive published so far this week."""
    out: dict[tuple[str, str], dict] = {}
    data = load_week_file(season, week) or {}
    for g in (data.get("games") or {}).values():
        for team, t in (g.get("teams") or {}).items():
            for p in t.get("inactives") or []:
                out[(team, p.get("name_key", ""))] = {**p, "game": g.get("short_name", ""), "phase": t.get("phase")}
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Collect NFL game-day inactives from ESPN game rosters.")
    ap.add_argument("--date", default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--event", default=None, help="poll a single ESPN event id (debug)")
    ap.add_argument("--all", action="store_true", help="poll every game of the week regardless of kickoff time")
    ap.add_argument("--season", type=int, default=None, help="override the season (debug against last year)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = collect_inactives(args.date, week=args.week, force_all=args.all, only_event=args.event, season=args.season)
    print(f"Week {res['week']}: {res['games_polled']} games polled, teams published: {res['published']}")
    for c in res["changes"]:
        names = ", ".join(f"{p['name']} ({p['pos']})" for p in c["inactives"])
        print(f"  [{c['phase']}] {c['game']} {c['team']}: {names}")
    for e in res["errors"]:
        print("  error:", e)
    if res.get("file"):
        print("written:", res["file"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
