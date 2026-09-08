"""Offline tests for collectors/injury_report_collector.py.

Fixtures were captured from the live pages on 2026-09-08 (Week 1, the
NE @ SEA Wednesday opener) and trimmed to the relevant containers:

* tests/fixtures/teamsite_injury_patriots.html — patriots.com injury page
  (both clubs' tables; day headers Sun/Mon/Tue for the Wed 09-09 game)
* tests/fixtures/rotowire_practice.json — RotoWire practice-report XHR JSON
* tests/fixtures/nfl_injuries_wk1.html — NFL.com /injuries/ (h1 + report wrap)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from collectors import injury_report_collector as irc
from config_loader import get_teams
from processing.team_abbr import to_news

FIXTURES = Path(__file__).parent / "fixtures"

WEEK1_SCHEDULE = [
    {"game_num": 1, "week": 1, "away": "NE", "home": "SEA", "date": "2026-09-09", "day": "Wednesday"},
    {"game_num": 2, "week": 1, "away": "SF", "home": "LA", "date": "2026-09-10", "day": "Thursday"},
    {"game_num": 3, "week": 1, "away": "TB", "home": "CIN", "date": "2026-09-13", "day": "Sunday"},
    {"game_num": 4, "week": 1, "away": "ARZ", "home": "LAC", "date": "2026-09-13", "day": "Sunday"},
    {"game_num": 5, "week": 1, "away": "DEN", "home": "KC", "date": "2026-09-14", "day": "Monday"},
]


@pytest.fixture
def teamsite_html() -> str:
    return (FIXTURES / "teamsite_injury_patriots.html").read_text(encoding="utf-8")


@pytest.fixture
def rotowire_data() -> list[dict]:
    return json.loads((FIXTURES / "rotowire_practice.json").read_text(encoding="utf-8"))


@pytest.fixture
def nflcom_html() -> str:
    return (FIXTURES / "nfl_injuries_wk1.html").read_text(encoding="utf-8")


@pytest.fixture
def injuries_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(irc, "_base_dir", lambda: tmp_path)
    return tmp_path


def _by_name(rows: list[dict], name: str) -> dict:
    matches = [r for r in rows if r["name"] == name]
    assert len(matches) == 1, f"{name}: {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("DNP", "DNP"), ("LP", "LP"), ("FP", "FP"), ("", ""), ("-", ""), (None, ""),
    ("Did Not Participate In Practice", "DNP"),
    ("Did Not Participate", "DNP"),          # truncated in the DOM
    ("Limited Participation in Practice", "LP"),
    ("Limited Particip", "LP"),
    ("Full Participation in Practice", "FP"),
    ("full", "FP"),
    ("garbage", ""),
])
def test_normalize_practice(text, expected):
    assert irc.normalize_practice(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("OUT", "OUT"), ("Out", "OUT"), ("QUESTIONABLE", "Q"), ("Questionable", "Q"),
    ("DOUBTFUL", "D"), ("Doubtful", "D"), ("UNSPECIFIED", ""), ("", ""), (None, ""), ("-", ""),
    ("Q", "Q"), ("D", "D"),
])
def test_normalize_game_status(text, expected):
    assert irc.normalize_game_status(text) == expected
    assert expected in irc.GAME_STATUS


# ---------------------------------------------------------------------------
# Day headers -> dates
# ---------------------------------------------------------------------------


def test_map_day_headers_wednesday_opener():
    assert irc.map_day_headers_to_dates(["Sun", "Mon", "Tue"], date(2026, 9, 9)) == {
        "Sun": "2026-09-06", "Mon": "2026-09-07", "Tue": "2026-09-08",
    }


def test_map_day_headers_sunday_game():
    assert irc.map_day_headers_to_dates(["Wed", "Thu", "Fri"], "2026-09-13") == {
        "Wed": "2026-09-09", "Thu": "2026-09-10", "Fri": "2026-09-11",
    }


def test_map_day_headers_monday_game():
    assert irc.map_day_headers_to_dates(["Thu", "Fri", "Sat"], date(2026, 9, 14)) == {
        "Thu": "2026-09-10", "Fri": "2026-09-11", "Sat": "2026-09-12",
    }


def test_map_day_headers_ignores_non_weekday_headers():
    out = irc.map_day_headers_to_dates(["Player", "Wed", "Game Status"], "2026-09-13")
    assert out == {"Wed": "2026-09-09"}


def test_map_day_headers_fallback_from_today():
    # No game date: first header anchored on/before today, then consecutive.
    out = irc._map_day_headers_from_today(["Sun", "Mon", "Tue"], "2026-09-08")
    assert out == {"Sun": "2026-09-06", "Mon": "2026-09-07", "Tue": "2026-09-08"}
    out = irc._map_day_headers_from_today(["Wed", "Thu", "Fri"], "2026-09-10")
    assert out == {"Wed": "2026-09-09", "Thu": "2026-09-10", "Fri": "2026-09-11"}


def test_week_window_dates_is_tuesday_to_monday():
    win = irc.week_window_dates(WEEK1_SCHEDULE, 1)
    assert win["tuesday"] == "2026-09-08"
    assert win["wednesday"] == "2026-09-09"
    assert win["sunday"] == "2026-09-13"
    assert win["monday"] == "2026-09-14"
    assert irc.week_window_dates(WEEK1_SCHEDULE, 9) == {}


# ---------------------------------------------------------------------------
# Team-site parser
# ---------------------------------------------------------------------------


def test_team_site_parser_both_clubs(teamsite_html):
    rows = irc.parse_team_site_html(teamsite_html, game_date="2026-09-09", date_str="2026-09-08")
    assert {r["team"] for r in rows} == {"NE", "SEA"}
    assert len([r for r in rows if r["team"] == "NE"]) == 3
    assert len([r for r in rows if r["team"] == "SEA"]) == 8
    assert all(r["source"] == "team_site" for r in rows)

    barmore = _by_name(rows, "Christian Barmore")
    assert barmore["team"] == "NE"
    assert barmore["pos"] == "DT"
    assert barmore["injury"] == "Knee"
    assert barmore["name_key"] == "christian barmore"
    # Sun/Mon/Tue headers dated against the Wednesday game; blank Tue omitted
    assert barmore["practice"] == {"2026-09-06": "DNP", "2026-09-07": "FP"}
    assert barmore["game_status"] == ""            # UNSPECIFIED -> ""

    brown = _by_name(rows, "Ben Brown")
    assert brown["game_status"] == "OUT"
    assert brown["practice"] == {"2026-09-06": "DNP", "2026-09-07": "DNP"}

    henderson = _by_name(rows, "TreVeyon Henderson")
    assert henderson["pos"] == "RB"
    assert henderson["practice"] == {"2026-09-06": "DNP", "2026-09-07": "DNP"}

    bradford = _by_name(rows, "Anthony Bradford")
    assert bradford["team"] == "SEA"
    assert bradford["practice"] == {"2026-09-06": "LP", "2026-09-07": "FP"}
    assert all(code in irc.PRACTICE_CODES for r in rows for code in r["practice"].values())


def test_team_site_parser_no_table_is_not_posted():
    html = """<html><body>
      <div class="nfl-o-injury-report__container">
        <span class="nfl-o-injury-report__club-name">Los Angeles Rams</span>
        <p>The injury report will be posted later.</p>
      </div></body></html>"""
    rows, clubs = irc._parse_team_site(html, game_date="2026-09-10")
    assert rows == []
    assert clubs == {"LAR": False}
    assert irc.parse_team_site_html(html, game_date="2026-09-10") == []


def test_team_site_parser_drops_future_dated_cells():
    # A value in a column dated after the scrape date is garbage (stale grid).
    html = """<html><body><div class="nfl-o-injury-report__container">
      <span class="nfl-o-injury-report__club-name">Kansas City Chiefs</span>
      <table><thead><tr><th>Player</th><th>Position</th><th>Injury</th>
      <th>Wed</th><th>Thu</th><th>Fri</th><th>Game Status</th></tr></thead>
      <tbody><tr><td>Some Guy</td><td>WR</td><td>Hamstring</td><td>LP</td><td>LP</td><td>FP</td>
      <td class="nfl-o-injury-report__game-status">QUESTIONABLE</td></tr></tbody></table>
      </div></body></html>"""
    rows = irc.parse_team_site_html(html, game_date="2026-09-13", date_str="2026-09-10")
    assert rows[0]["team"] == "KC"
    assert rows[0]["practice"] == {"2026-09-09": "LP", "2026-09-10": "LP"}   # Fri 09-11 dropped
    assert rows[0]["game_status"] == "Q"


def test_fetch_team_site_report_uses_schedule_game_date(teamsite_html):
    class FakeResp:
        text = teamsite_html
        status_code = 200

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, timeout=None, **kw):
            assert url == "https://www.patriots.com/team/injury-report/"
            return FakeResp()

    team = {"abbr": "NE", "name": "New England Patriots", "site_domain": "patriots.com"}
    rows = irc.fetch_team_site_report(FakeSession(), team, settings={}, schedule=WEEK1_SCHEDULE,
                                      week=1, date_str="2026-09-08")
    assert {r["team"] for r in rows} == {"NE", "SEA"}
    assert _by_name(rows, "Christian Barmore")["practice"] == {"2026-09-06": "DNP", "2026-09-07": "FP"}


def test_fetch_team_site_report_skips_other_matchup(teamsite_html):
    """If the page shows a different opponent than this week's schedule, the
    weekday headers can't be dated → rows dropped, flagged as mismatch."""
    class FakeResp:
        text = teamsite_html

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, timeout=None, **kw):
            return FakeResp()

    sched = [{"week": 2, "away": "NE", "home": "MIA", "date": "2026-09-20"}]
    team = {"abbr": "NE", "site_domain": "patriots.com"}
    res = irc._fetch_team_site(FakeSession(), team, {}, sched, 2, "2026-09-15")
    assert res["mismatch"] is True
    assert res["rows"] == []
    assert res["clubs"] == {"NE": True, "SEA": True}


def test_all_teams_have_site_domain():
    missing = [t["abbr"] for t in get_teams() if not t.get("site_domain")]
    assert missing == []
    assert len(get_teams()) == 32


# ---------------------------------------------------------------------------
# RotoWire parser
# ---------------------------------------------------------------------------


def test_rotowire_parser_maps_weekdays_via_game_date(rotowire_data):
    week_dates = irc.week_window_dates(WEEK1_SCHEDULE, 1)
    rows = irc.parse_rotowire_rows(rotowire_data, week_dates, schedule=WEEK1_SCHEDULE, week=1,
                                   date_str="2026-09-08")
    assert len(rows) == 11
    assert all(r["source"] == "rotowire" for r in rows)
    barmore = _by_name(rows, "Christian Barmore")
    assert barmore["team"] == "NE"
    # sunday/monday are the Sun/Mon BEFORE the Wednesday game, not the
    # Tue..Mon window's 09-13/09-14
    assert barmore["practice"] == {"2026-09-06": "DNP", "2026-09-07": "FP"}
    brown = _by_name(rows, "Ben Brown")
    assert brown["game_status"] == "OUT"
    assert brown["injury"] == "Knee"
    assert all(r["game_status"] in irc.GAME_STATUS for r in rows)


def test_rotowire_parser_falls_back_to_week_window_without_game():
    data = [{"player": "Bye Guy", "team": "GB", "pos": "WR", "status": "Questionable", "injtype": "Calf",
             "monday": "-", "tuesday": "-", "wednesday": "LP", "thursday": "FP", "friday": "-",
             "saturday": "-", "sunday": "-"}]
    week_dates = irc.week_window_dates(WEEK1_SCHEDULE, 1)
    rows = irc.parse_rotowire_rows(data, week_dates, schedule=WEEK1_SCHEDULE, week=1, date_str="2026-09-11")
    assert rows[0]["practice"] == {"2026-09-09": "LP", "2026-09-10": "FP"}
    assert rows[0]["game_status"] == "Q"


def test_rotowire_team_dialect():
    data = [{"player": "Rams Guy", "team": "LA", "pos": "RB", "status": "", "injtype": "",
             "monday": "FP", "tuesday": "-", "wednesday": "-", "thursday": "-", "friday": "-",
             "saturday": "-", "sunday": "-"}]
    rows = irc.parse_rotowire_rows(data, {}, schedule=WEEK1_SCHEDULE, week=1, date_str="2026-09-08")
    assert rows[0]["team"] == "LAR"
    assert rows[0]["practice"] == {"2026-09-07": "FP"}   # Monday before the Thu 09-10 game


# ---------------------------------------------------------------------------
# NFL.com parser
# ---------------------------------------------------------------------------


def test_nflcom_parser_week_and_teams(nflcom_html):
    week, rows = irc.parse_nflcom_html(nflcom_html, practice_date="2026-09-07")
    assert week == 1
    assert {r["team"] for r in rows} == {"NE", "SEA"}     # only the opener had tables posted
    assert all(r["source"] == "nflcom" for r in rows)
    barmore = _by_name(rows, "Christian Barmore")
    assert barmore["practice"] == {"2026-09-07": "FP"}
    brown = _by_name(rows, "Ben Brown")
    assert brown["practice"] == {"2026-09-07": "DNP"}
    assert brown["pos"] == "C"


def test_nflcom_abbreviation_dialect():
    assert to_news("AZ", "nflcom") == "ARI"
    assert to_news("LAR", "nflcom") == "LAR"
    assert to_news("LA", "nflcom") == "LAR"


def test_nflcom_parser_falls_back_to_strip_abbreviation():
    html = """<html><body><h1 class="nfl-o-page-title">Week 3 of the 2026 Season</h1>
    <section class="nfl-o-injury-report__unit">
      <span class="nfl-c-matchup-strip__team-abbreviation">AZ</span>
      <span class="nfl-c-matchup-strip__team-abbreviation">LAR</span>
      <div class="nfl-t-stats__title">Unknown Nickname</div>
      <div class="d3-o-table--horizontal-scroll"><table>
        <thead><tr><th>Player</th><th>Position</th><th>Injuries</th><th>Practice Status</th><th>Game Status</th></tr></thead>
        <tbody><tr><td>Cardinal Guy</td><td>WR</td><td>Knee</td><td>Limited Participation in Practice</td><td>Doubtful</td></tr></tbody>
      </table></div>
    </section></body></html>"""
    week, rows = irc.parse_nflcom_html(html, "2026-09-23")
    assert week == 3
    assert rows[0]["team"] == "ARI"
    assert rows[0]["practice"] == {"2026-09-23": "LP"}
    assert rows[0]["game_status"] == "D"
    assert rows[0]["injury"] == "Knee"


def test_nflcom_practice_date_is_eastern_minus_12h():
    # 10:00 UTC morning run = 06:00 ET -> previous day
    assert irc.nflcom_practice_date(datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)) == "2026-09-09"
    # 22:00 UTC afternoon run = 18:00 ET -> same day
    assert irc.nflcom_practice_date(datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)) == "2026-09-10"


# ---------------------------------------------------------------------------
# Merge precedence
# ---------------------------------------------------------------------------


def _row(team, name, source, practice, game_status="", pos="WR", injury=""):
    return irc._make_row(team, name, pos, injury, practice, game_status, source)


def test_merge_sources_precedence_and_conflicts():
    team_site = [_row("KC", "Rashee Rice", "team_site", {"2026-09-09": "FP"}, pos="WR", injury="Knee")]
    rotowire = [_row("KC", "Rashee Rice", "rotowire", {"2026-09-09": "LP", "2026-09-10": "DNP"}, game_status="Q")]
    nflcom = [_row("KC", "Rashee Rice", "nflcom", {"2026-09-10": "LP"}, game_status="D", pos="")]
    merged, conflicts = irc.merge_sources({"team_site": team_site, "rotowire": rotowire, "nflcom": nflcom})
    assert len(merged) == 1
    m = merged[0]
    assert m["source"] == "team_site"
    assert m["sources"] == ["team_site", "rotowire", "nflcom"]
    assert m["practice"]["2026-09-09"] == "FP"          # team_site beats rotowire
    assert m["practice"]["2026-09-10"] == "DNP"         # rotowire fills the missing day
    assert m["game_status"] == "Q"                      # rotowire fills the gap
    assert m["game_status_source"] == "rotowire"
    assert m["injury"] == "Knee" and m["pos"] == "WR"
    fields = {(c["field"], c["date"]) for c in conflicts}
    assert ("practice", "2026-09-09") in fields          # team_site FP vs rotowire LP
    assert ("practice", "2026-09-10") in fields          # rotowire DNP vs nflcom LP
    assert ("game_status", None) in fields               # rotowire Q vs nflcom D
    practice_conflict = next(c for c in conflicts if c["date"] == "2026-09-09")
    assert practice_conflict["values"] == {"team_site": "FP", "rotowire": "LP"}
    assert practice_conflict["team"] == "KC" and practice_conflict["name"] == "Rashee Rice"


def test_merge_sources_same_source_duplicates_merge_silently():
    # A club appears on its own page and on its opponent's page.
    a = _row("SEA", "AJ Barner", "team_site", {"2026-09-06": "FP"})
    b = _row("SEA", "AJ Barner", "team_site", {"2026-09-06": "FP", "2026-09-07": "FP"})
    merged, conflicts = irc.merge_sources({"team_site": [a, b]})
    assert len(merged) == 1
    assert merged[0]["practice"] == {"2026-09-06": "FP", "2026-09-07": "FP"}
    assert conflicts == []


def test_merge_sources_keys_on_team_and_name_key():
    a = _row("NE", "Marvin Harrison Jr.", "team_site", {"2026-09-09": "LP"})
    b = _row("NE", "Marvin Harrison", "rotowire", {"2026-09-09": "LP"})
    c = _row("ARI", "Marvin Harrison", "rotowire", {"2026-09-09": "LP"})
    merged, _ = irc.merge_sources({"team_site": [a], "rotowire": [b, c]})
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# Week file merge + diff
# ---------------------------------------------------------------------------


def test_merge_into_week_accumulates_across_days(injuries_dir):
    day1 = [
        _row("NE", "Christian Barmore", "team_site", {"2026-09-09": "DNP"}, pos="DT", injury="Knee"),
        _row("NE", "Ben Brown", "team_site", {"2026-09-09": "DNP"}, pos="C", injury="Knee"),
    ]
    cur1, prev1 = irc.merge_into_week(day1, 2026, 2, "2026-09-16", schedule=[
        {"week": 2, "away": "NE", "home": "MIA", "date": "2026-09-20"}])
    assert prev1 is None
    path = irc.week_file_path(2026, 2)
    assert path == injuries_dir / "2026" / "wk02.json"
    assert path.exists()
    ne = cur1["teams"]["NE"]
    assert ne["opp"] == "MIA" and ne["game_date"] == "2026-09-20"
    p = ne["players"]["christian barmore"]
    assert p["first_seen"] == "2026-09-16" and p["last_seen"] == "2026-09-16"

    day2 = [
        _row("NE", "Christian Barmore", "team_site", {"2026-09-09": "DNP", "2026-09-10": "LP"},
             pos="DT", injury="Knee"),
    ]
    cur2, prev2 = irc.merge_into_week(day2, 2026, 2, "2026-09-17",
                                      sources_used={"team_site": 1}, conflicts=[])
    assert prev2["teams"]["NE"]["players"]["christian barmore"]["practice"] == {"2026-09-09": "DNP"}
    p = cur2["teams"]["NE"]["players"]["christian barmore"]
    assert p["practice"] == {"2026-09-09": "DNP", "2026-09-10": "LP"}
    assert p["first_seen"] == "2026-09-16" and p["last_seen"] == "2026-09-17"
    assert "cleared" not in p
    # Ben Brown vanished from a team that WAS reported today -> cleared marker, still in file
    brown = cur2["teams"]["NE"]["players"]["ben brown"]
    assert brown["cleared"] == "2026-09-17"
    assert cur2["sources_used"] == {"team_site": 1}
    assert cur2["season"] == 2026 and cur2["week"] == 2
    assert cur2["last_run_date"] == "2026-09-17"
    # opp/game_date survive a merge with no schedule
    assert cur2["teams"]["NE"]["opp"] == "MIA"

    on_disk = irc.load_week_file(2026, 2)
    assert on_disk["teams"]["NE"]["players"]["christian barmore"]["practice"] == p["practice"]

    # Day 3: a team not reported at all leaves its players untouched (no cleared)
    day3 = [_row("SEA", "AJ Barner", "rotowire", {"2026-09-11": "FP"}, pos="TE")]
    cur3, _ = irc.merge_into_week(day3, 2026, 2, "2026-09-18")
    assert "cleared" not in cur3["teams"]["NE"]["players"]["christian barmore"]
    assert cur3["teams"]["SEA"]["players"]["aj barner"]["source"] == "rotowire"
    # ... and a cleared player who reappears loses the marker
    day4 = [_row("NE", "Ben Brown", "team_site", {"2026-09-11": "LP"}, pos="C")]
    cur4, _ = irc.merge_into_week(day4, 2026, 2, "2026-09-19")
    assert "cleared" not in cur4["teams"]["NE"]["players"]["ben brown"]
    assert cur4["teams"]["NE"]["players"]["ben brown"]["practice"] == {"2026-09-09": "DNP", "2026-09-11": "LP"}


def test_merge_into_week_game_status_latest_non_empty_wins(injuries_dir):
    irc.merge_into_week([_row("KC", "Guy", "team_site", {}, game_status="Q")], 2026, 3, "2026-09-25")
    cur, _ = irc.merge_into_week([_row("KC", "Guy", "team_site", {"2026-09-25": "FP"})], 2026, 3, "2026-09-26")
    assert cur["teams"]["KC"]["players"]["guy"]["game_status"] == "Q"   # empty doesn't erase
    cur, _ = irc.merge_into_week([_row("KC", "Guy", "team_site", {}, game_status="OUT")], 2026, 3, "2026-09-26")
    assert cur["teams"]["KC"]["players"]["guy"]["game_status"] == "OUT"
    assert cur["teams"]["KC"]["players"]["guy"]["game_status_source"] == "team_site"


def _player(name, pos, practice, game_status="", injury="Knee", cleared=None, source="team_site"):
    p = {"name": name, "pos": pos, "injury": injury, "practice": practice, "game_status": game_status,
         "first_seen": "2026-09-16", "last_seen": "2026-09-17", "source": source}
    if cleared:
        p["cleared"] = cleared
    return p


def _week(players_by_team: dict, run_date="2026-09-17") -> dict:
    return {"season": 2026, "week": 2, "last_run_date": run_date,
            "teams": {t: {"opp": None, "game_date": None, "players": ps} for t, ps in players_by_team.items()}}


def test_diff_week_produces_each_change_type():
    prev = _week({
        "NE": {
            "up": _player("Up Guy", "WR", {"2026-09-16": "DNP"}),
            "down": _player("Down Guy", "RB", {"2026-09-16": "FP"}),
            "set": _player("Set Guy", "TE", {"2026-09-16": "LP"}),
            "changed": _player("Changed Guy", "QB", {"2026-09-16": "LP"}, game_status="Q"),
            "gone": _player("Gone Guy", "CB", {"2026-09-16": "DNP"}, game_status="OUT"),
            "same": _player("Same Guy", "S", {"2026-09-16": "LP"}),
            "first": _player("First Guy", "K", {}, source="nflcom"),
            "corrected": _player("Corrected Guy", "OL", {"2026-09-16": "LP"}),
        },
    }, run_date="2026-09-16")
    cur = _week({
        "NE": {
            "up": _player("Up Guy", "WR", {"2026-09-16": "DNP", "2026-09-17": "LP"}),
            "down": _player("Down Guy", "RB", {"2026-09-16": "FP", "2026-09-17": "DNP"}),
            "set": _player("Set Guy", "TE", {"2026-09-16": "LP", "2026-09-17": "LP"}, game_status="Q"),
            "changed": _player("Changed Guy", "QB", {"2026-09-16": "LP", "2026-09-17": "DNP"}, game_status="OUT"),
            "gone": _player("Gone Guy", "CB", {"2026-09-16": "DNP"}, game_status="OUT", cleared="2026-09-17"),
            "same": _player("Same Guy", "S", {"2026-09-16": "LP", "2026-09-17": "LP"}),
            "first": _player("First Guy", "K", {"2026-09-17": "LP"}),
            "corrected": _player("Corrected Guy", "OL", {"2026-09-16": "FP"}),
            "new": _player("New Guy", "WR", {"2026-09-17": "DNP"}, injury="Ankle"),
        },
        "SEA": {
            "newq": _player("New Q Guy", "TE", {"2026-09-17": "LP"}, game_status="Q"),
        },
    })
    changes = irc.diff_week(prev, cur)
    by_key = {(c["name"], c["type"]): c for c in changes}

    up = by_key[("Up Guy", "practice_upgrade")]
    assert up["old"] == "DNP" and up["new"] == "LP" and up["date"] == "2026-09-17"
    assert up["team"] == "NE" and up["pos"] == "WR" and up["injury"] == "Knee"
    assert "DNP -> LP" in up["message"] and "Thu" in up["message"]

    down = by_key[("Down Guy", "practice_downgrade")]
    assert down["old"] == "FP" and down["new"] == "DNP"

    assert by_key[("Set Guy", "designation_set")]["new"] == "Q"
    assert ("Set Guy", "practice_upgrade") not in by_key      # LP -> LP is not a change
    assert ("Set Guy", "practice_downgrade") not in by_key

    changed = by_key[("Changed Guy", "designation_changed")]
    assert changed["old"] == "Q" and changed["new"] == "OUT"
    assert ("Changed Guy", "practice_downgrade") in by_key    # LP -> DNP too

    gone = by_key[("Gone Guy", "cleared")]
    assert gone["date"] == "2026-09-17"
    assert "Out" in gone["old"]

    assert not any(c["name"] == "Same Guy" for c in changes)

    first = by_key[("First Guy", "practice_status")]
    assert first["old"] == "" and first["new"] == "LP"

    corrected = by_key[("Corrected Guy", "practice_upgrade")]
    assert corrected["date"] == "2026-09-16" and corrected["old"] == "LP" and corrected["new"] == "FP"

    new = by_key[("New Guy", "new_listing")]
    assert new["new"] == "Thu DNP" and new["date"] == "2026-09-17" and new["injury"] == "Ankle"
    newq = by_key[("New Q Guy", "new_listing")]
    assert newq["team"] == "SEA" and newq["new"] == "Thu LP; Questionable"

    assert {c["type"] for c in changes} == {
        "practice_upgrade", "practice_downgrade", "practice_status",
        "designation_set", "designation_changed", "cleared", "new_listing",
    }
    for c in changes:
        assert set(c) == {"team", "name", "pos", "injury", "type", "old", "new", "date", "source", "message"}
        assert c["type"] in irc.CHANGE_TYPES


def test_diff_week_first_run_is_all_new_listings():
    cur = _week({"NE": {"a": _player("A", "WR", {"2026-09-16": "DNP"}), "b": _player("B", "RB", {})}})
    changes = irc.diff_week(None, cur)
    assert [c["type"] for c in changes] == ["new_listing", "new_listing"]
    assert changes[1]["date"] == "2026-09-17"      # no practice -> last_seen


def test_diff_week_player_missing_entirely_is_cleared():
    prev = _week({"NE": {"a": _player("A", "WR", {"2026-09-16": "DNP"})}}, run_date="2026-09-16")
    cur = _week({"NE": {}}, run_date="2026-09-17")
    changes = irc.diff_week(prev, cur)
    assert len(changes) == 1 and changes[0]["type"] == "cleared" and changes[0]["date"] == "2026-09-17"
    # already-cleared players don't re-fire
    prev2 = _week({"NE": {"a": _player("A", "WR", {}, cleared="2026-09-16")}})
    cur2 = _week({"NE": {"a": _player("A", "WR", {}, cleared="2026-09-16")}})
    assert irc.diff_week(prev2, cur2) == []


# ---------------------------------------------------------------------------
# Orchestration (offline, fetchers stubbed)
# ---------------------------------------------------------------------------


def test_collect_injury_report_offline(injuries_dir, monkeypatch, teamsite_html, rotowire_data, nflcom_html):
    settings = {"season": {"phase": "in_season", "year": 2026},
                "injury_report": {"enabled": True, "sources": ["team_sites", "rotowire", "nflcom"],
                                  "team_site_workers": 2},
                "collection": {"request_timeout": 5}}

    def fake_team_sites(session, cfg, schedule, week, date_str, workers=None, teams=None):
        rows = irc.parse_team_site_html(teamsite_html, game_date="2026-09-09", date_str=date_str)
        return rows, {"with_table": ["NE", "SEA"], "without_table": ["LAR"], "mismatch": [], "failed": {"KC": "boom"}}

    def fake_rotowire(session, week_dates, cfg=None, schedule=None, week=None, date_str=None):
        assert week_dates["tuesday"] == "2026-09-08"
        return irc.parse_rotowire_rows(rotowire_data, week_dates, schedule=schedule, week=week, date_str=date_str)

    def fake_nflcom(session, cfg=None, scrape_dt=None, date_str=None):
        return irc.parse_nflcom_html(nflcom_html, "2026-09-07")

    monkeypatch.setattr(irc, "fetch_all_team_sites", fake_team_sites)
    monkeypatch.setattr(irc, "fetch_rotowire_report", fake_rotowire)
    monkeypatch.setattr(irc, "fetch_nflcom_report", fake_nflcom)

    result = irc.collect_injury_report("2026-09-08", settings=settings, schedule=WEEK1_SCHEDULE, session=object())
    assert result["errors"] == []
    assert result["week"] == 1 and result["season"] == 2026
    assert result["rows"] == 11
    assert result["sources_used"] == {"team_site": 11, "rotowire": 11, "nflcom": 11}
    assert result["team_sites_with_table"] == ["NE", "SEA"]
    assert result["team_sites_failed"] == {"KC": "boom"}
    assert result["conflicts"] == []                 # all three sources agree on the live data
    assert Path(result["file"]) == injuries_dir / "2026" / "wk01.json"
    assert len(result["changes"]) == 11 and all(c["type"] == "new_listing" for c in result["changes"])

    data = irc.load_week_file(2026, 1)
    assert set(data["teams"]) == {"NE", "SEA"}
    assert data["teams"]["NE"]["opp"] == "SEA" and data["teams"]["NE"]["game_date"] == "2026-09-09"
    assert data["teams"]["SEA"]["opp"] == "NE"
    barmore = data["teams"]["NE"]["players"]["christian barmore"]
    assert barmore["practice"] == {"2026-09-06": "DNP", "2026-09-07": "FP"}
    assert barmore["injury"] == "Knee"
    brown = data["teams"]["NE"]["players"]["ben brown"]
    assert brown["game_status"] == "OUT" and brown["game_status_source"] == "team_site"

    # Second run the same day: nothing new
    result2 = irc.collect_injury_report("2026-09-08", settings=settings, schedule=WEEK1_SCHEDULE, session=object())
    assert result2["changes"] == []


def test_collect_injury_report_never_raises(injuries_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(irc, "fetch_all_team_sites", boom)
    monkeypatch.setattr(irc, "fetch_rotowire_report", boom)
    monkeypatch.setattr(irc, "fetch_nflcom_report", boom)
    settings = {"season": {"year": 2026}, "injury_report": {"sources": ["team_sites", "rotowire", "nflcom"]},
                "collection": {}}
    result = irc.collect_injury_report("2026-09-08", settings=settings, schedule=WEEK1_SCHEDULE, session=object())
    assert len(result["errors"]) == 3
    assert result["week"] == 1
    assert result["rows"] == 0 and result["changes"] == []
    # Week file still written (empty) so the next run has a baseline
    assert Path(result["file"]).exists()


def test_collect_injury_report_week_from_nflcom_when_no_schedule(injuries_dir, monkeypatch, nflcom_html):
    monkeypatch.setattr(irc, "fetch_nflcom_report", lambda s, c=None, scrape_dt=None, date_str=None:
                        irc.parse_nflcom_html(nflcom_html, "2026-09-07"))
    settings = {"season": {"year": 2026}, "injury_report": {"sources": ["nflcom"]}, "collection": {}}
    result = irc.collect_injury_report("2026-09-08", settings=settings, schedule=[], session=object())
    assert result["errors"] == []
    assert result["week"] == 1
    assert result["sources_used"] == {"nflcom": 11}
    data = irc.load_week_file(2026, 1)
    assert data["teams"]["NE"]["opp"] is None        # no schedule -> no opp
