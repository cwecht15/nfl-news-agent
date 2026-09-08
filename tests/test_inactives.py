"""collectors.inactives_collector + the audit / report pieces that consume it."""

import json
from datetime import datetime, timezone

import pytest

from collectors import inactives_collector as ic
from processing import projection_audit as pa
from processing.season import SeasonContext
from reports.report_builder import _build_inactives_section


def _entries(active_flags, dnp_flags=None, names=None):
    dnp_flags = dnp_flags or [False] * len(active_flags)
    names = names or [f"P{i}" for i in range(len(active_flags))]
    return [
        {"playerId": 1000 + i, "displayName": names[i], "jersey": str(i), "active": a, "didNotPlay": d,
         "athlete": {"$ref": f"http://x/athletes/{1000 + i}"}, "position": {"abbreviation": "WR"}}
        for i, (a, d) in enumerate(zip(active_flags, dnp_flags))
    ]


NFLVERSE = {
    "00-0001": {"gsis_id": "00-0001", "espn_id": "1000", "name": "Josh Allen", "name_key": "josh allen", "team": "BUF", "pos": "QB"},
    "00-0002": {"gsis_id": "00-0002", "espn_id": "1001", "name": "Ray Davis", "name_key": "ray davis", "team": "BUF", "pos": "RB"},
}


def test_pregame_published_roster_yields_inactives():
    flags = [True] * 46 + [False] * 7
    phase, rows = ic.parse_roster_entries(_entries(flags), "STATUS_SCHEDULED", "BUF", NFLVERSE)
    assert phase == "pregame" and len(rows) == 7
    # nflverse identity wins over ESPN's display name/position
    assert rows[0]["espn_id"] == "1046" and rows[0]["pos"] == "WR"


def test_pregame_unpublished_roster_is_ignored():
    # ESPN shows every entry inactive before the list is posted
    phase, rows = ic.parse_roster_entries(_entries([False] * 53), "STATUS_SCHEDULED", "BUF", NFLVERSE)
    assert phase == "unpublished" and rows == []
    phase, rows = ic.parse_roster_entries([], "STATUS_SCHEDULED", "BUF", NFLVERSE)
    assert phase == "unpublished"


def test_postgame_uses_did_not_play_and_nflverse_identity():
    flags = [False] * 53
    dnp = [False] * 53
    dnp[0] = dnp[1] = True
    phase, rows = ic.parse_roster_entries(_entries(flags, dnp), "STATUS_FINAL", "BUF", NFLVERSE)
    assert phase == "postgame"
    assert [(r["name"], r["pos"], r["gsis_id"]) for r in rows] == [("Josh Allen", "QB", "00-0001"), ("Ray Davis", "RB", "00-0002")]


def test_athlete_fallback_uses_cache_without_session():
    cache = {"http://x/athletes/1005": {"name": "Cache Hit", "pos": "TE"}}
    flags = [True] * 46 + [False] * 7
    ents = _entries(flags)
    ents[5]["active"] = False
    ents[46]["active"] = True
    phase, rows = ic.parse_roster_entries(ents, "STATUS_SCHEDULED", "BUF", {}, session=None, athlete_cache=cache)
    hit = [r for r in rows if r["espn_id"] == "1005"][0]
    assert hit["name"] == "Cache Hit" and hit["pos"] == "TE"


def test_games_to_poll_window():
    now = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)   # Sunday noon ET
    games = [
        {"event_id": "1", "date": "2026-09-13T17:00Z", "status": "STATUS_SCHEDULED"},   # 1 PM kick — in window
        {"event_id": "2", "date": "2026-09-13T20:25Z", "status": "STATUS_SCHEDULED"},   # 4:25 — too far ahead
        {"event_id": "3", "date": "2026-09-10T00:20Z", "status": "STATUS_FINAL"},       # Wed game — too old
        {"event_id": "4", "date": "2026-09-13T00:15Z", "status": "STATUS_FINAL"},       # Sat night — recent final
    ]
    assert [g["event_id"] for g in ic._games_to_poll(games, now)] == ["1", "4"]
    assert len(ic._games_to_poll(games, now, force_all=True)) == 4


def test_merge_into_week_tracks_changes_and_protects_postgame():
    game = {"event_id": "9", "name": "Buffalo Bills at Houston Texans", "short_name": "BUF @ HST",
            "date": "2026-09-13T17:00Z", "status": "STATUS_SCHEDULED", "home": "HOU", "away": "BUF"}
    rows = [{"name": "Ray Davis", "name_key": "ray davis", "pos": "RB", "team": "BUF", "jersey": "22", "espn_id": "1001", "gsis_id": "00-0002"}]
    data, changed = ic.merge_into_week(None, 2026, 1, game, "BUF", "pregame", rows, "t1")
    assert changed and data["games"]["9"]["teams"]["BUF"]["phase"] == "pregame"
    data, changed = ic.merge_into_week(data, 2026, 1, game, "BUF", "pregame", rows, "t2")
    assert not changed and data["games"]["9"]["teams"]["BUF"]["published_at"] == "t1"
    game["status"] = "STATUS_FINAL"
    data, changed = ic.merge_into_week(data, 2026, 1, game, "BUF", "postgame", rows, "t3")
    assert changed and data["games"]["9"]["teams"]["BUF"]["phase"] == "postgame"
    data, changed = ic.merge_into_week(data, 2026, 1, game, "BUF", "pregame", [], "t4")
    assert not changed and data["games"]["9"]["teams"]["BUF"]["phase"] == "postgame"
    assert ("BUF", "ray davis") in {(t, p["name_key"]) for g in data["games"].values() for t, tt in g["teams"].items() for p in tt["inactives"]}


def test_collect_inactives_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(ic, "fetch_scoreboard", lambda s, season, week: [
        {"event_id": "9", "name": "Buffalo Bills at Houston Texans", "short_name": "BUF @ HOU",
         "date": "2026-09-13T17:00Z", "status": "STATUS_SCHEDULED", "home": "HOU", "away": "BUF",
         "competitors": {"BUF": "2", "HOU": "34"}},
    ])
    rosters = {"2": _entries([True] * 46 + [False] * 7), "34": _entries([False] * 53)}   # HOU not posted yet
    monkeypatch.setattr(ic, "fetch_game_roster", lambda s, e, cid: rosters[cid])
    monkeypatch.setattr(ic.season_mod, "load_schedule", lambda **kw: [{"week": 1, "away": "BUF", "home": "HST", "date": "2026-09-13"}])
    monkeypatch.setattr("collectors.nflverse_roster_collector.latest_nflverse_snapshot", lambda **kw: (NFLVERSE, "2026-09-13"))
    now = datetime(2026, 9, 13, 16, 0, tzinfo=timezone.utc)
    res = ic.collect_inactives("2026-09-13", week=1, settings={"season": {"year": 2026}}, session=object(), now=now)
    assert res["week"] == 1 and res["published"] == {"BUF": 7} and len(res["changes"]) == 1
    data = json.loads((tmp_path / "2026" / "wk01.json").read_text(encoding="utf-8"))
    assert "HOU" not in data["games"]["9"]["teams"]
    idx = ic.inactive_players_for_week(2026, 1)
    assert all(k[0] == "BUF" for k in idx) and len(idx) == 7


def test_audit_flags_projected_inactives(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "DISMISSALS_PATH", tmp_path / "d.json")
    ctx = SeasonContext("in_season", 2026, 1, "primary", {"primary": 1}, "2026-09-13", "Sun", False)
    snapshot = {"meta": {"season": 2026, "week": 1, "sheet": "primary", "date": "2026-09-13"},
                "players": {"00-0002": {"player_id": "00-0002", "name": "Ray Davis", "team": "BUF", "pos": "RB", "depth": 3, "status": "Active"}},
                "output": {"00-0002": {"ppr": 6.0, "pos_rank": "RB40", "name": "Ray Davis", "pos": "RB", "team": "BUF"}},
                "games": {}, "kickers": {}}
    inputs = {"snapshot": snapshot, "state": None, "nflverse": None, "injuries": None, "ourlads": {}, "schedule": [],
              "inactives": {("BUF", "ray davis"): {"name": "Ray Davis", "pos": "RB", "game": "BUF @ HOU", "phase": "pregame"}},
              "errors": []}
    res = pa.run_audit(ctx, "2026-09-13", run="test", inputs=inputs, write=False)
    hits = [a for a in res["alerts"] if a["type"] == "inactive_but_projected"]
    assert len(hits) == 1 and hits[0]["severity"] == "error" and "INACTIVE" in hits[0]["message"]


def test_inactives_report_section():
    week = {"games": {"9": {"short_name": "BUF @ HOU", "date": "2026-09-13T17:00Z", "home": "HOU", "away": "BUF",
                            "teams": {"BUF": {"phase": "pregame", "inactives": [
                                {"name": "Ray Davis", "pos": "RB"}, {"name": "Some Guard", "pos": "G"}]}}}}}
    sec = _build_inactives_section(week)
    assert sec["count"] == 2
    assert "**Ray Davis (RB)**" in sec["summary"] and "Some Guard (G)" in sec["summary"]
    assert _build_inactives_section({})["count"] == 0
