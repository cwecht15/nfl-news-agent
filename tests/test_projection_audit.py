"""processing.projection_audit — one case per alert type, offline."""

import json

import pytest

from processing import projection_audit as pa
from processing.season import SeasonContext


def _ctx(**kw):
    base = dict(phase="in_season", season=2026, week=1, active_sheet="primary",
                sheet_weeks={"primary": 1, "secondary": 1}, today="2026-09-08",
                weekday="Tue", read_secondary=True)
    base.update(kw)
    return SeasonContext(**base)


def _schedule():
    return [
        {"week": 1, "away": "BUF", "home": "HST", "date": "2026-09-13"},
        {"week": 1, "away": "NE", "home": "SEA", "date": "2026-09-09"},
        {"week": 1, "away": "DAL", "home": "NYG", "date": "2026-09-13"},
        {"week": 2, "away": "HST", "home": "BUF", "date": "2026-09-20"},
        {"week": 2, "away": "SEA", "home": "NE", "date": "2026-09-20"},
        {"week": 2, "away": "KC", "home": "DAL", "date": "2026-09-20"},
        {"week": 2, "away": "NYG", "home": "MIA", "date": "2026-09-20"},
    ]


def _snapshot():
    players = {
        "00-0001": {"player_id": "00-0001", "name": "Josh Allen", "team": "BUF", "pos": "QB", "depth": 1, "status": "Active", "opp": "HST"},
        "00-0002": {"player_id": "00-0002", "name": "Ray Davis", "team": "BUF", "pos": "RB", "depth": 3, "status": "Active", "opp": "HST"},
        "00-0003": {"player_id": "00-0003", "name": "Keon Coleman", "team": "BUF", "pos": "WR", "depth": 4, "status": "PS", "opp": "HST"},
        "00-0004": {"player_id": "00-0004", "name": "Dawson Knox", "team": "BUF", "pos": "TE", "depth": 2, "status": "Active", "opp": "HST"},
        "00-0005": {"player_id": "00-0005", "name": "Jerry Jeudy", "team": "BUF", "pos": "WR", "depth": 2, "status": "Active", "opp": "HST"},
        "00-0006": {"player_id": "00-0006", "name": "Malik Nabers", "team": "NYG", "pos": "WR", "depth": 1, "status": "Active", "opp": "DAL"},
        "00-0007": {"player_id": "00-0007", "name": "Tyreek Hill", "team": "MIA", "pos": "WR", "depth": 1, "status": "Active", "opp": "BUF"},
    }
    # Pad BUF past MIN_TEAM_BLOCK_ROWS so it counts as a complete block
    for i in range(4):
        players[f"00-02{i:02d}"] = {"player_id": f"00-02{i:02d}", "name": f"Pad Wr{i}", "team": "BUF", "pos": "WR",
                                    "depth": 5 + i, "status": "Active", "opp": "HST"}
    output = {
        "00-0001": {"name": "Josh Allen", "pos": "QB", "team": "BUF", "ppr": 21.2, "pos_rank": "QB2"},
        "00-0002": {"name": "Ray Davis", "pos": "RB", "team": "BUF", "ppr": 6.0, "pos_rank": "RB40"},
        "00-0003": {"name": "Keon Coleman", "pos": "WR", "team": "BUF", "ppr": 0.0, "pos_rank": "WR120"},
        "00-0004": {"name": "Dawson Knox", "pos": "TE", "team": "BUF", "ppr": 5.5, "pos_rank": "TE20"},
        "00-0005": {"name": "Jerry Jeudy", "pos": "WR", "team": "BUF", "ppr": 9.0, "pos_rank": "WR40"},
        "00-0006": {"name": "Malik Nabers", "pos": "WR", "team": "NYG", "ppr": 18.0, "pos_rank": "WR3"},
        "00-0007": {"name": "Tyreek Hill", "pos": "WR", "team": "MIA", "ppr": 12.0, "pos_rank": "WR15"},
    }
    games = {
        "BUF": {"team": "BUF", "home_away": "Away", "opp": "HST", "metrics": {}},
        "NYG": {"team": "NYG", "home_away": "Home", "opp": "KC", "metrics": {}},   # wrong: schedule says DAL
    }
    kickers = {"00-0090": {"player_id": "00-0090", "name": "Tyler Bass", "team": "BUF", "pos": "K"}}
    return {"meta": {"season": 2026, "week": 1, "sheet": "primary", "date": "2026-09-08"},
            "players": players, "games": games, "output": output, "kickers": kickers}


def _state():
    return {
        "season": 2026, "updated_at": "x",
        "players": {
            "00-0002": {"gsis_id": "00-0002", "name": "Ray Davis", "name_key": "ray davis", "team": "BUF", "pos": "RB",
                        "status": "IR", "status_source": "nflverse", "status_since": "2026-09-07",
                        "ir_date": "2026-09-07", "earliest_return_week": 6, "elevations_used": 0, "elevation_dates": [], "pending": []},
            "00-0003": {"gsis_id": "00-0003", "name": "Keon Coleman", "name_key": "keon coleman", "team": "BUF", "pos": "WR",
                        "status": "ACT", "status_source": "nflcom_transactions", "status_since": "2026-09-08",
                        "elevations_used": 0, "elevation_dates": [], "pending": []},
            "00-0005": {"gsis_id": "00-0005", "name": "Jerry Jeudy", "name_key": "jerry jeudy", "team": "CLE", "pos": "WR",
                        "status": "ACT", "status_source": "nflverse", "elevations_used": 0, "elevation_dates": [], "pending": []},
            "00-0010": {"gsis_id": "00-0010", "name": "Frank Gore Jr.", "name_key": "frank gore", "team": "BUF", "pos": "RB",
                        "status": "PS", "elevations_used": 1, "elevation_dates": ["2026-09-12"], "pending": []},
            "00-0011": {"gsis_id": "00-0011", "name": "Cash Jones", "name_key": "cash jones", "team": "ATL", "pos": "RB",
                        "status": "PS", "elevations_used": 3, "elevation_dates": ["2026-08-01", "2026-08-08", "2026-08-15"], "pending": []},
            "00-0012": {"gsis_id": "00-0012", "name": "Matt Milano", "name_key": "matt milano", "team": "BUF", "pos": "LB",
                        "status": "IR", "earliest_return_week": 1, "designated_return_date": None, "elevations_used": 0, "elevation_dates": [], "pending": []},
            "name:some guy": {"name": "Some Guy", "name_key": "some guy", "team": "BUF", "pos": "WR", "status": "ACT",
                              "elevations_used": 0, "elevation_dates": [],
                              "pending": [{"event_type": "ir_placed", "date": "2026-09-01", "source": "news:Twitter"}]},
        },
        "by_name": {"ray davis": "00-0002", "keon coleman": "00-0003", "jerry jeudy": "00-0005",
                    "frank gore": "00-0010", "cash jones": "00-0011", "matt milano": "00-0012"},
    }


def _nflverse():
    return {
        "00-0001": {"gsis_id": "00-0001", "name": "Josh Allen", "name_key": "josh allen", "team": "BUF", "pos": "QB", "status": "ACT", "status_abbr": "A01"},
        "00-0002": {"gsis_id": "00-0002", "name": "Ray Davis", "name_key": "ray davis", "team": "BUF", "pos": "RB", "status": "RES", "status_abbr": "R01"},
        "00-0020": {"gsis_id": "00-0020", "name": "James Cook", "name_key": "james cook", "team": "BUF", "pos": "RB", "status": "ACT", "status_abbr": "A01"},
        "00-0021": {"gsis_id": "00-0021", "name": "Deep Bench", "name_key": "deep bench", "team": "BUF", "pos": "WR", "status": "ACT", "status_abbr": "A01"},
        "00-0022": {"gsis_id": "00-0022", "name": "Some Lineman", "name_key": "some lineman", "team": "BUF", "pos": "OT", "status": "ACT", "status_abbr": "A01"},
        "00-0023": {"gsis_id": "00-0023", "name": "Bye Week Guy", "name_key": "bye week guy", "team": "MIA", "pos": "RB", "status": "ACT", "status_abbr": "A01"},
    }


def _ourlads():
    return {
        "james cook": {"name": "James Cook", "pos": "RB", "generic_pos": "RB", "depth": 1, "team": "BUF"},
        "deep bench": {"name": "Deep Bench", "pos": "WR", "generic_pos": "WR", "depth": 6, "team": "BUF"},
    }


def _injuries():
    return {"season": 2026, "week": 1, "updated_at": "x", "teams": {
        "BUF": {"opp": "HST", "game_date": "2026-09-13", "players": {
            "dawson knox": {"name": "Dawson Knox", "pos": "TE", "injury": "Knee",
                            "practice": {"2026-09-09": "DNP", "2026-09-10": "DNP"}, "game_status": "OUT"},
            "josh allen": {"name": "Josh Allen", "pos": "QB", "injury": "Elbow",
                           "practice": {"2026-09-09": "LP", "2026-09-10": "DNP"}, "game_status": ""},
        }},
    }}


def _inputs():
    return {"snapshot": _snapshot(), "state": _state(), "nflverse": _nflverse(), "nflverse_date": "2026-09-08",
            "injuries": _injuries(), "ourlads": _ourlads(), "schedule": _schedule(), "errors": []}


@pytest.fixture
def isolated_dismissals(tmp_path, monkeypatch):
    p = tmp_path / "audit_dismissals.json"
    monkeypatch.setattr(pa, "DISMISSALS_PATH", p)
    return p


def _by_type(alerts):
    out = {}
    for a in alerts:
        out.setdefault(a["type"], []).append(a)
    return out


def test_every_alert_type_fires(isolated_dismissals):
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=_inputs(), write=False)
    by = _by_type(res["alerts"])

    # 1. projected active but on IR
    assert [a["player"] for a in by["status_conflict"]] == ["Ray Davis"]
    assert by["status_conflict"][0]["severity"] == "error"
    assert "eligible Wk 6" in by["status_conflict"][0]["message"]
    # 2. sheet says PS, roster says active
    assert [a["player"] for a in by["sheet_status_stale"]] == ["Keon Coleman"]
    # 3. wrong team
    assert [a["player"] for a in by["wrong_team"]] == ["Jerry Jeudy"]
    assert by["wrong_team"][0]["evidence"]["roster_team"] == "CLV"
    # 4. missing active: James Cook (OurLads #1) is a warning; depth-6 WR skipped; OT skipped; bye team skipped
    missing = {a["player"]: a["severity"] for a in by["missing_active"]}
    assert missing == {"James Cook": "warning"}
    # 5. out but projected
    assert [a["player"] for a in by["out_but_projected"]] == ["Dawson Knox"]
    assert [a["player"] for a in by["dnp_but_projected"]] == ["Josh Allen"]
    # 6. elevated this week but not on the sheet
    assert [a["player"] for a in by["elevated_not_projected"]] == ["Frank Gore Jr."]
    # 7. elevation limit
    assert [a["player"] for a in by["elevation_limit"]] == ["Cash Jones"]
    # 8. schedule: NYG opp mismatch + MIA on bye but projected
    assert by["opp_mismatch"][0]["team"] == "NYG" and by["opp_mismatch"][0]["evidence"]["schedule_opp"] == "DAL"
    assert by["bye_projected"][0]["team"] == "MIA"
    # 9. IR return window
    assert [a["player"] for a in by["ir_return_window"]] == ["Matt Milano"]
    # 10. unconfirmed report (7 days old > 3-day window)
    assert [a["player"] for a in by["unconfirmed_report"]] == ["Some Guy"]
    # 11. Tuesday and the secondary hasn't moved ahead
    assert len(by["stale_secondary"]) == 1

    # Keys are week-scoped and stable
    assert by["status_conflict"][0]["key"] == "status_conflict|00-0002|1"
    # Sorted errors first
    assert res["alerts"][0]["severity"] == "error"
    assert res["counts"]["status_conflict"] == 1


def test_dismissal_filters_and_expires_with_week(isolated_dismissals):
    key = "status_conflict|00-0002|1"
    pa.dismiss(key, "known — will fix Wed")
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=_inputs(), write=False)
    assert key not in {a["key"] for a in res["alerts"]}
    assert key in {a["key"] for a in res["dismissed"]}
    # next week → different key → alert returns
    inputs = _inputs()
    inputs["snapshot"]["meta"]["week"] = 2
    res2 = pa.run_audit(_ctx(week=2), "2026-09-15", run="test", inputs=inputs, write=False)
    assert any(a["type"] == "status_conflict" and a["week"] == 2 for a in res2["alerts"])
    pa.undismiss(key)
    assert pa.load_dismissals() == {}


def test_missing_inputs_are_soft(isolated_dismissals):
    inputs = _inputs()
    inputs["state"] = None
    inputs["injuries"] = None
    inputs["nflverse"] = None
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=inputs, write=False)
    types = {a["type"] for a in res["alerts"]}
    assert "status_conflict" not in types and "missing_active" not in types and "out_but_projected" not in types
    assert "opp_mismatch" in types  # schedule check still runs

    inputs["snapshot"] = None
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=inputs, write=False)
    assert res["alerts"] == [] and any("snapshot" in e for e in res["errors"])


def test_stale_secondary_only_on_secondary_days(isolated_dismissals):
    inputs = _inputs()
    ctx = _ctx(weekday="Wed", read_secondary=False)
    res = pa.run_audit(ctx, "2026-09-09", run="test", inputs=inputs, write=False)
    assert not [a for a in res["alerts"] if a["type"] == "stale_secondary"]


def test_writes_audit_file(isolated_dismissals, tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "get_data_dir", lambda sub: tmp_path)
    res = pa.run_audit(_ctx(), "2026-09-08", run="am", inputs=_inputs(), write=True)
    p = tmp_path / "2026-09-08-am.json"
    assert p.exists()
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["week"] == 1 and data["alerts"]
    assert res["file"] == str(p)


def test_incomplete_team_block_collapses_to_one_warning(isolated_dismissals):
    inputs = _inputs()
    # NYG has a single row on the sheet while nflverse has its starting QB active
    inputs["nflverse"]["00-0030"] = {"gsis_id": "00-0030", "name": "Jaxson Dart", "name_key": "jaxson dart",
                                     "team": "NYG", "pos": "QB", "status": "ACT", "status_abbr": "A01"}
    inputs["ourlads"]["jaxson dart"] = {"name": "Jaxson Dart", "pos": "QB", "generic_pos": "QB", "depth": 1, "team": "NYG"}
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=inputs, write=False)
    by = _by_type(res["alerts"])
    assert [a["team"] for a in by["team_block_incomplete"]] == ["NYG"]
    assert not [a for a in by.get("missing_active", []) if a["team"] == "NYG"]


def test_missing_active_rules(isolated_dismissals):
    inputs = _inputs()
    # Pad BUF to a full block so per-player checks run there
    for i in range(10):
        inputs["snapshot"]["players"][f"00-01{i:02d}"] = {"player_id": f"00-01{i:02d}", "name": f"Pad {i}", "team": "BUF",
                                                          "pos": "WR", "depth": 5 + i, "status": "Active", "opp": "HST"}
    inputs["nflverse"].update({
        "00-0031": {"gsis_id": "00-0031", "name": "Starter Qb", "name_key": "starter qb", "team": "BUF", "pos": "QB", "status": "ACT", "status_abbr": "A01"},
        "00-0032": {"gsis_id": "00-0032", "name": "Backup Qb", "name_key": "backup qb", "team": "BUF", "pos": "QB", "status": "ACT", "status_abbr": "A01"},
        "00-0033": {"gsis_id": "00-0033", "name": "Reggie Gilliam", "name_key": "reggie gilliam", "team": "BUF", "pos": "RB", "status": "ACT", "status_abbr": "A01"},
    })
    inputs["ourlads"].update({
        "starter qb": {"name": "Starter Qb", "pos": "QB", "generic_pos": "QB", "depth": 1, "team": "BUF"},
        "backup qb": {"name": "Backup Qb", "pos": "QB", "generic_pos": "QB", "depth": 2, "team": "BUF"},
        "reggie gilliam": {"name": "Reggie Gilliam", "pos": "FB", "generic_pos": "FB", "depth": 1, "team": "BUF"},
    })
    res = pa.run_audit(_ctx(), "2026-09-08", run="test", inputs=inputs, write=False)
    missing = {a["player"]: a["severity"] for a in _by_type(res["alerts"]).get("missing_active", [])}
    assert missing.get("Starter Qb") == "error"      # missing QB1 is an error
    assert "Backup Qb" not in missing                 # QB2 never projected
    assert "Reggie Gilliam" not in missing            # fullback never projected
    assert missing.get("James Cook") == "warning"
