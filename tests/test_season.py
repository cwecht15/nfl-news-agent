"""processing.season — phase switch, week resolution, schedule helpers."""

from datetime import date, datetime, timezone

import pytest

from processing import season


OFFSEASON = {"season": {"phase": "offseason", "year": 2026, "secondary_weekdays": ["Tue"]}}
IN_SEASON = {"season": {"phase": "in_season", "year": 2026, "secondary_weekdays": ["Tue"]}}


def _sched():
    """Six BUF games with a Week 3 bye, plus one other game per week."""
    games = [
        ("BUF", "HST", 1, "2026-09-13"),
        ("MIA", "BUF", 2, "2026-09-20"),
        # week 3 bye for BUF
        ("BUF", "NYJ", 4, "2026-10-04"),
        ("NE", "BUF", 5, "2026-10-11"),
        ("BUF", "KC", 6, "2026-10-18"),
        ("DAL", "BUF", 7, "2026-10-25"),
        ("SEA", "NE", 1, "2026-09-09"),   # Wednesday opener
        ("KC", "DEN", 3, "2026-09-27"),
    ]
    return [
        {"game_num": i, "week": wk, "away": a if i % 2 else h, "home": h if i % 2 else a,
         "date": d, "day": "", "time": "", "venue": "", "season": 2026}
        for i, (a, h, wk, d) in enumerate(games, 1)
    ]


def test_phase_defaults_to_offseason():
    assert season.get_phase({}) == "offseason"
    assert season.get_phase(OFFSEASON) == "offseason"
    assert season.get_phase(IN_SEASON) == "in_season"
    assert season.get_phase({"season": {"phase": "In-Season"}}) == "in_season"
    assert not season.is_in_season(OFFSEASON)


def test_offseason_context_never_touches_sheets():
    class Boom:
        def open_by_key(self, *_):
            raise AssertionError("gspread must not be called in the offseason")

    ctx = season.get_season_context(today="2026-09-08", gc=Boom(), settings=OFFSEASON, schedule=[])
    assert ctx.phase == "offseason"
    assert ctx.week is None and ctx.active_sheet is None
    assert not ctx.in_season


def test_secondary_only_on_tuesday():
    assert season.read_secondary_today(date(2026, 9, 8), IN_SEASON)      # Tue
    assert not season.read_secondary_today(date(2026, 9, 9), IN_SEASON)  # Wed
    assert not season.read_secondary_today(date(2026, 9, 13), IN_SEASON)  # Sun


def test_active_sheet_rule_higher_week_wins_tie_goes_to_primary():
    # Tuesday: secondary already on next week → secondary is the working copy
    wk, sheet = season.resolve_current_week({"primary": {"week": 3}, "secondary": {"week": 4}})
    assert (wk, sheet) == (4, "secondary")
    # Main sheet flipped → tie → primary
    wk, sheet = season.resolve_current_week({"primary": {"week": 4}, "secondary": {"week": 4}})
    assert (wk, sheet) == (4, "primary")
    # Secondary stale (not bumped yet) → primary
    wk, sheet = season.resolve_current_week({"primary": {"week": 4}, "secondary": {"week": 3}})
    assert (wk, sheet) == (4, "primary")
    # No metas → schedule fallback
    wk, sheet = season.resolve_current_week({}, _sched(), date(2026, 9, 15))
    assert (wk, sheet) == (2, "primary")


def test_in_season_context_uses_supplied_metas():
    ctx = season.get_season_context(
        today="2026-09-08", settings=IN_SEASON, schedule=_sched(),
        sheet_metas={"primary": {"season": 2026, "week": 1}, "secondary": {"season": 2026, "week": 1}},
    )
    assert ctx.in_season and ctx.week == 1 and ctx.active_sheet == "primary"
    assert ctx.weekday == "Tue" and ctx.read_secondary
    assert ctx.to_dict()["in_season"] is True


def test_week_from_date_uses_tuesday_boundaries():
    s = _sched()
    assert season.week_from_date(s, date(2026, 9, 8)) == 1    # Tue before the Wed opener
    assert season.week_from_date(s, date(2026, 9, 14)) == 1   # Monday after
    assert season.week_from_date(s, date(2026, 9, 15)) == 2   # Tuesday → new week
    assert season.week_from_date(s, date(2026, 8, 1)) is None


def test_bye_and_opponent():
    s = _sched()
    assert "BUF" in season.teams_on_bye(s, 3)
    assert "BUF" not in season.teams_on_bye(s, 1)
    opp = season.opponent(s, "BUF", 2)
    assert opp["opp"] == "MIA" and opp["home_away"] == "Away"
    assert season.opponent(s, "BUF", 3) is None
    # news-style input is normalized
    assert season.opponent(s, "HOU", 1, source="news")["opp"] == "BUF"


def test_earliest_return_week_skips_bye():
    s = _sched()
    # Placed the Tuesday of week 1 → misses weeks 1, 2, 4, 5 (bye in 3 doesn't count) → back week 6
    assert season.earliest_return_week(s, "BUF", "2026-09-08", min_games=4) == 6
    # Placed after the week-1 game → misses 2, 4, 5, 6 → back week 7
    assert season.earliest_return_week(s, "BUF", "2026-09-14", min_games=4) == 7
    # Not enough games left
    assert season.earliest_return_week(s, "BUF", "2026-10-20", min_games=4) is None


def test_parse_schedule_rows_layout():
    rows = [
        ["Game Num", "Wk", "Team 1", "Team 2", "", "Date", "Day", "Time", "Stadium"],
        ["1", "1", "NE", "SEA", "", "2026-09-09", "Wednesday", "8:20 PM", "Lumen Field", "junk", "ARZ"],
        ["", "", "", "", "", "", "", "", ""],
        ["2", "x", "SF", "LA", "", "2026-09-10", "Thursday", "8:35 PM", "MCG"],
    ]
    games = season._parse_schedule_rows(rows, 2026)
    assert len(games) == 1
    g = games[0]
    assert g["away"] == "NE" and g["home"] == "SEA" and g["venue"] == "Lumen Field"


# ---------------------------------------------------------------------------
# today_et — the run date follows the NFL's Eastern day, not the runner clock
# ---------------------------------------------------------------------------


def test_today_et_uses_eastern_not_utc():
    """The 2026-09-09 incident: a 19:50 ET cron that GitHub delayed to 01:31 UTC."""
    assert season.today_et(datetime(2026, 9, 10, 1, 31, tzinfo=timezone.utc)) == "2026-09-09"
    # Same UTC day, well inside it -> unchanged
    assert season.today_et(datetime(2026, 9, 10, 16, 0, tzinfo=timezone.utc)) == "2026-09-10"
    # EST half of the season (UTC-5), so the boundary moves an hour later
    assert season.today_et(datetime(2026, 12, 15, 2, 0, tzinfo=timezone.utc)) == "2026-12-14"
    assert season.today_et(datetime(2026, 12, 15, 6, 0, tzinfo=timezone.utc)) == "2026-12-15"


def test_today_et_treats_naive_input_as_utc():
    assert season.today_et(datetime(2026, 9, 10, 1, 31)) == "2026-09-09"


def test_get_season_context_defaults_to_eastern_today(monkeypatch):
    """Proves get_season_context's fallback is actually wired to today_et."""
    monkeypatch.setattr(season, "today_et", lambda now=None: "2026-09-09")
    ctx = season.get_season_context(settings=OFFSEASON, schedule=[])
    assert ctx.today == "2026-09-09"
    assert ctx.weekday == "Wed"


def test_read_secondary_today_defaults_to_eastern(monkeypatch):
    """Monday night in ET must not read the Tuesday secondary sheet."""
    monkeypatch.setattr(season, "today_et", lambda now=None: "2026-09-08")  # Tue
    assert season.read_secondary_today(settings=IN_SEASON) is True
    monkeypatch.setattr(season, "today_et", lambda now=None: "2026-09-07")  # Mon
    assert season.read_secondary_today(settings=IN_SEASON) is False
