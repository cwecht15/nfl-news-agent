"""Season phase + NFL week awareness.

The pipeline historically had no notion of season or week. This module is
the single place that answers:

* Are we in ``offseason`` or ``in_season``?  (``config/settings.yaml → season.phase``)
* What NFL week is it, and which weekly projections sheet is the working
  copy today?  (each sheet's own ``Working_Game_Proj!C2`` cell; the
  secondary sheet is only consulted on the weekdays listed in
  ``season.secondary_weekdays`` — Tuesday, when next week's projections
  start there while Monday Night Football finishes)
* Schedule questions: games this week, teams on bye, a team's opponent,
  and how many games a team plays after a given date (IR return math).

Every function accepts ``settings=`` so tests can bypass the ``lru_cache``
on ``config_loader.get_settings``. Nothing here raises for the offseason
path — ``get_season_context`` returns ``week=None`` without touching
Google Sheets when ``phase != in_season``.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings, get_teams
from processing.team_abbr import to_news, to_proj

logger = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

PHASE_OFFSEASON = "offseason"
PHASE_IN_SEASON = "in_season"

SCHEDULE_MAX_AGE_DAYS = 7


# ---------------------------------------------------------------------------
# Settings accessors
# ---------------------------------------------------------------------------


def _settings(settings: Optional[dict]) -> dict:
    return settings if settings is not None else get_settings()


def get_phase(settings: Optional[dict] = None) -> str:
    phase = str(_settings(settings).get("season", {}).get("phase", PHASE_OFFSEASON)).strip().lower()
    return PHASE_IN_SEASON if phase in {"in_season", "inseason", "in-season", "season"} else PHASE_OFFSEASON


def is_in_season(settings: Optional[dict] = None) -> bool:
    return get_phase(settings) == PHASE_IN_SEASON


def get_season_year(settings: Optional[dict] = None) -> int:
    year = _settings(settings).get("season", {}).get("year")
    try:
        return int(year)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).year


def get_in_season_projection_settings(settings: Optional[dict] = None) -> dict:
    return dict(_settings(settings).get("projections", {}).get("in_season", {}))


def secondary_weekdays(settings: Optional[dict] = None) -> set[str]:
    days = _settings(settings).get("season", {}).get("secondary_weekdays", ["Tue"])
    return {str(d).strip()[:3].title() for d in days}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def weekday_name(d: Any) -> str:
    return WEEKDAYS[_as_date(d).weekday()]


def read_secondary_today(d: Any = None, settings: Optional[dict] = None) -> bool:
    """True when the secondary projections sheet should be read today."""
    d = _as_date(d) if d is not None else date.today()
    return weekday_name(d) in secondary_weekdays(settings)


def day_role(d: Any) -> str:
    """Human label for what an in-season weekday usually means."""
    wd = weekday_name(d)
    return {
        "Tue": "new week - waivers & roster churn",
        "Wed": "first practice report",
        "Thu": "second practice report / TNF",
        "Fri": "final practice report & designations",
        "Sat": "elevations & Saturday moves",
        "Sun": "game day",
        "Mon": "game day (MNF) / recap",
    }.get(wd, wd)


# ---------------------------------------------------------------------------
# Schedule (cached from the primary sheet's Schedule tab)
# ---------------------------------------------------------------------------


def _schedule_path(season: int) -> Path:
    return get_data_dir("schedule") / f"{season}.json"


def _parse_schedule_rows(rows: list[list[str]], season: int) -> list[dict]:
    """Schedule tab → list of games.

    Verified layout: A Game Num, B Wk, C Team 1 (**away**), D Team 2 (**home**),
    E blank, F Date (YYYY-MM-DD), G Day, H Time, I venue. Columns J+ are an
    unrelated lookup table and are ignored. Abbreviations are proj-style.
    """
    games: list[dict] = []
    for row in rows[1:]:
        if len(row) < 6:
            continue
        num, wk, away, home, _blank, dt = (row + [""] * 6)[:6]
        if not wk or not away or not home or not dt:
            continue
        try:
            week = int(str(wk).strip())
        except ValueError:
            continue
        try:
            game_date = date.fromisoformat(str(dt).strip()[:10]).isoformat()
        except ValueError:
            continue
        games.append({
            "game_num": int(num) if str(num).strip().isdigit() else None,
            "week": week,
            "away": to_proj(away.strip(), "proj"),
            "home": to_proj(home.strip(), "proj"),
            "date": game_date,
            "day": (row[6].strip() if len(row) > 6 else ""),
            "time": (row[7].strip() if len(row) > 7 else ""),
            "venue": (row[8].strip() if len(row) > 8 else ""),
            "season": season,
        })
    return games


def fetch_schedule(gc, settings: Optional[dict] = None) -> list[dict]:
    """Read the Schedule tab from the primary in-season sheet."""
    cfg = get_in_season_projection_settings(settings)
    sheet_id = cfg.get("sheets", {}).get("primary")
    tab = cfg.get("schedule_sheet", "Schedule")
    if not sheet_id:
        raise ValueError("projections.in_season.sheets.primary is not configured")
    ws = gc.open_by_key(sheet_id).worksheet(tab)
    rows = ws.get_values("A1:I400")
    return _parse_schedule_rows(rows, get_season_year(settings))


def save_schedule(games: list[dict], season: int) -> Path:
    path = _schedule_path(season)
    payload = {
        "season": season,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_schedule(
    gc=None,
    refresh: bool = False,
    settings: Optional[dict] = None,
    season: Optional[int] = None,
) -> list[dict]:
    """Return the season schedule, refreshing the disk cache from Sheets when
    stale (> SCHEDULE_MAX_AGE_DAYS) or missing and a gspread client is given.

    Never raises: with no cache and no client it returns [] and logs.
    """
    season = season or get_season_year(settings)
    path = _schedule_path(season)
    cached: Optional[dict] = None
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = None

    stale = True
    if cached and cached.get("fetched_at") and not refresh:
        try:
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc) - fetched).days >= SCHEDULE_MAX_AGE_DAYS
        except ValueError:
            stale = True

    if (stale or refresh) and gc is not None:
        try:
            games = fetch_schedule(gc, settings)
            if games:
                save_schedule(games, season)
                logger.info("Schedule refreshed: %d games for %d", len(games), season)
                return games
        except Exception as e:  # noqa: BLE001 — cache fallback below
            logger.warning("Schedule refresh failed (using cache if any): %s", e)

    if cached:
        return list(cached.get("games", []))
    logger.warning("No schedule available for %d (no cache, no client)", season)
    return []


def games_for_week(schedule: list[dict], week: int) -> list[dict]:
    return [g for g in schedule if g.get("week") == week]


def all_teams_proj() -> list[str]:
    return [to_proj(t["abbr"]) for t in get_teams()]


def teams_on_bye(schedule: list[dict], week: int) -> set[str]:
    playing = set()
    for g in games_for_week(schedule, week):
        playing.add(g["home"])
        playing.add(g["away"])
    if not playing:
        return set()
    return {t for t in all_teams_proj() if t not in playing}


def opponent(schedule: list[dict], team: str, week: int, source: str = "proj") -> Optional[dict]:
    """{'opp': 'HST', 'home_away': 'Home', 'date': ...} for ``team`` in ``week``, or None on bye."""
    t = to_proj(team, source)
    for g in games_for_week(schedule, week):
        if g["home"] == t:
            return {"opp": g["away"], "home_away": "Home", "date": g["date"], "game": g}
        if g["away"] == t:
            return {"opp": g["home"], "home_away": "Away", "date": g["date"], "game": g}
    return None


def team_games(schedule: list[dict], team: str, source: str = "proj") -> list[dict]:
    t = to_proj(team, source)
    return sorted(
        (g for g in schedule if g["home"] == t or g["away"] == t),
        key=lambda g: (g["date"], g["week"]),
    )


def team_games_after(schedule: list[dict], team: str, d: Any, n: Optional[int] = None,
                     source: str = "proj", inclusive: bool = True) -> list[dict]:
    """Games ``team`` plays on/after date ``d`` (byes are simply absent)."""
    day = _as_date(d).isoformat()
    games = [g for g in team_games(schedule, team, source)
             if (g["date"] >= day if inclusive else g["date"] > day)]
    return games[:n] if n else games


def earliest_return_week(schedule: list[dict], team: str, placed_date: Any,
                         min_games: int = 4, source: str = "proj") -> Optional[int]:
    """Week of the first game a player placed on IR on ``placed_date`` may
    play: they must miss ``min_games`` team games (byes don't count).
    Returns None when the season doesn't have enough games left."""
    games = team_games_after(schedule, team, placed_date, source=source)
    if len(games) <= min_games:
        return None
    return games[min_games]["week"]


def week_from_date(schedule: list[dict], d: Any = None) -> Optional[int]:
    """NFL week containing ``d``: a week runs from the Tuesday before its
    first game through the following Monday."""
    d = _as_date(d) if d is not None else date.today()
    if not schedule:
        return None
    first_game: dict[int, date] = {}
    for g in schedule:
        gd = date.fromisoformat(g["date"])
        w = g["week"]
        if w not in first_game or gd < first_game[w]:
            first_game[w] = gd
    current = None
    for w in sorted(first_game):
        start = first_game[w]
        while start.weekday() != 1:  # back up to Tuesday
            start -= timedelta(days=1)
        if start <= d:
            current = w
        else:
            break
    return current


# ---------------------------------------------------------------------------
# Sheet week cells + active-sheet rule
# ---------------------------------------------------------------------------


def read_sheet_week(gc, sheet_id: str, settings: Optional[dict] = None) -> dict:
    """{'season': 2026, 'week': 3} from ``Working_Game_Proj!C1:C2`` (one call)."""
    cfg = get_in_season_projection_settings(settings)
    tab = cfg.get("game_sheet", "Working_Game_Proj")
    ws = gc.open_by_key(sheet_id).worksheet(tab)
    vals = ws.get_values("C1:C2")
    season = int(str(vals[0][0]).strip()) if vals and vals[0] and vals[0][0] else None
    week = int(str(vals[1][0]).strip()) if len(vals) > 1 and vals[1] and vals[1][0] else None
    return {"season": season, "week": week}


def resolve_current_week(
    sheet_metas: dict[str, dict],
    schedule: Optional[list[dict]] = None,
    today: Any = None,
) -> tuple[Optional[int], Optional[str]]:
    """(week, active_sheet). Higher Current Week wins; tie → primary.
    Falls back to the schedule-derived week when no sheet meta is usable."""
    best_week, best_sheet = None, None
    for label in ("primary", "secondary"):  # primary first so ties keep primary
        meta = sheet_metas.get(label) or {}
        wk = meta.get("week")
        if wk is None:
            continue
        if best_week is None or wk > best_week:
            best_week, best_sheet = wk, label
    if best_week is not None:
        return best_week, best_sheet
    if schedule:
        return week_from_date(schedule, today), "primary"
    return None, None


# ---------------------------------------------------------------------------
# Context object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeasonContext:
    phase: str
    season: int
    week: Optional[int]
    active_sheet: Optional[str]          # "primary" | "secondary" | None
    sheet_weeks: dict = field(default_factory=dict)  # {"primary": 3, "secondary": 4}
    today: str = ""                      # ISO date
    weekday: str = ""                    # "Tue"
    read_secondary: bool = False

    @property
    def in_season(self) -> bool:
        return self.phase == PHASE_IN_SEASON

    def to_dict(self) -> dict:
        d = asdict(self)
        d["in_season"] = self.in_season
        d["day_role"] = day_role(self.today) if self.today else ""
        return d


def get_season_context(
    today: Any = None,
    gc=None,
    settings: Optional[dict] = None,
    sheet_metas: Optional[dict[str, dict]] = None,
    schedule: Optional[list[dict]] = None,
) -> SeasonContext:
    """Build the context for a run. Never raises.

    * offseason → week None, no Sheets access.
    * in-season → reads each sheet's C1:C2 (primary always; secondary only on
      ``secondary_weekdays``) unless ``sheet_metas`` is supplied; on any
      Sheets failure falls back to the schedule cache.
    """
    settings = _settings(settings)
    phase = get_phase(settings)
    season = get_season_year(settings)
    d = _as_date(today) if today is not None else date.today()
    wd = weekday_name(d)
    read_secondary = read_secondary_today(d, settings)

    if phase != PHASE_IN_SEASON:
        return SeasonContext(phase, season, None, None, {}, d.isoformat(), wd, read_secondary)

    metas: dict[str, dict] = dict(sheet_metas or {})
    if not metas and gc is not None:
        cfg = get_in_season_projection_settings(settings)
        sheets = cfg.get("sheets", {})
        for label in ("primary", "secondary"):
            if label == "secondary" and not read_secondary:
                continue
            sid = sheets.get(label)
            if not sid:
                continue
            try:
                metas[label] = read_sheet_week(gc, sid, settings)
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not read Current Week from %s sheet: %s", label, e)

    if schedule is None:
        schedule = load_schedule(gc, settings=settings, season=season)

    week, active = resolve_current_week(metas, schedule, d)
    sheet_weeks = {k: v.get("week") for k, v in metas.items() if v.get("week") is not None}
    return SeasonContext(phase, season, week, active, sheet_weeks, d.isoformat(), wd, read_secondary)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ctx = get_season_context()
    print(json.dumps(ctx.to_dict(), indent=2))
    if ctx.in_season and ctx.week:
        sched = load_schedule()
        print("games this week:", len(games_for_week(sched, ctx.week)))
        print("byes:", sorted(teams_on_bye(sched, ctx.week)))
