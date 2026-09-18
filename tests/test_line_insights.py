"""processing.line_insights — what on the Line Movement page is worth acting on."""

from collectors import odds_collector as oc
from processing import line_insights as li

SETTINGS = {"odds": {"thresholds": oc.DEFAULT_THRESHOLDS}}


def _game(away, home, open_sp, open_tot, sp, tot, sheet_sp=None, sheet_ou=None):
    return {
        "away": away, "home": home, "kickoff_et": "Sun 09/20 01:00 PM",
        "opened": {"spread_home": open_sp, "total": open_tot, "at": "2026-09-15T09:07-04:00"},
        "current": {"spread_home": sp, "total": tot, "home_ml": -150.0, "away_ml": 130.0,
                    "at": "2026-09-17T16:03-04:00"},
        "sheet": {"spread_home": sp if sheet_sp is None else sheet_sp,
                  "ou": tot if sheet_ou is None else sheet_ou, "fp_flag": ""},
        "history": [],
    }


def _prop(player, team, stat, ours, opened, current, flag="", thin=False, prev=None,
          cur_at="2026-09-17T20:03:42+00:00"):
    return {"gsis_id": f"id-{player}", "player": player, "team": team, "opp": "X", "pos": "WR",
            "stat": stat, "ours": ours, "flag": flag, "thin": thin, "pulls": 3,
            "opened": {"mkt_mu": opened},
            "previous": {"mkt_mu": prev} if prev is not None else None,
            "current": {"mkt_mu": current, "cons_line": None, "at": cur_at}}


def test_team_environment_flags_moves_and_sheet_gaps_and_skips_played_games():
    week = {"games": {
        "MIN@CHI": _game("MIN", "CHI", -5.5, 49.5, -4.5, 48.0),                 # CHI 27.5 -> 26.2
        "CAR@ATL": _game("CAR", "ATL", 1.0, 44.0, 1.0, 44.0, sheet_ou=47.0),  # sheet +1.5 a side
        "DET@BUF": _game("DET", "BUF", -4.5, 53.5, -5.5, 55.0),               # already played
    }}
    rows = li.team_environment(week, SETTINGS, played={"BUF", "DET"})
    by = {r["team"]: r for r in rows}
    assert "BUF" not in by and "DET" not in by
    assert by["CHI"]["implied_move"] == -1.3 and by["CHI"]["notable"]
    assert by["ATL"]["sheet_gap"] == 1.5 and by["ATL"]["notable"]
    assert not by["CAR"]["notable"] or by["CAR"]["sheet_gap"] == 1.5
    assert rows[0]["notable"]                          # notable rows sort first


def test_player_watchlist_keeps_away_moves_and_flags_drops_toward_noise_thin_played():
    week = {"props": {
        "a": _prop("Away Guy", "HOU", "pass_yds", 253.9, 245.4, 227.8),         # gap 8.5 -> 26.1
        "t": _prop("Toward Guy", "NE", "anytime_td", 0.60, 0.84, 0.61),        # gap .24 -> .01
        "r": _prop("Red Guy", "IND", "rec_yds", 35.2, 50.0, 51.0, flag="RED"),
        "n": _prop("Noise Guy", "KC", "receptions", 4.0, 4.1, 4.3),            # under threshold
        "h": _prop("Thin Guy", "KC", "rec_yds", 20.0, 20.0, 45.0, thin=True),
        "p": _prop("Played Guy", "BUF", "rec_yds", 20.0, 20.0, 45.0),
    }}
    rows = li.player_watchlist(week, SETTINGS, played={"BUF"})
    assert [r["player"] for r in rows] == ["Red Guy", "Away Guy"]   # flags first, then away movers
    assert rows[1]["trend"] == "away"
    with_toward = li.player_watchlist(week, SETTINGS, include_toward=True, played={"BUF"})
    assert "Toward Guy" in [r["player"] for r in with_toward]
    assert li.summary_counts(week, SETTINGS, played={"BUF"})["players_recheck"] == 2


def test_latest_pull_moves_are_derived_from_state_between_pulls():
    g = _game("SF", "LAR", -3.0, 48.0, -4.5, 48.0)
    g["history"] = [
        {"spread_home": -3.0, "total": 48.0, "home_ml": -150.0, "away_ml": 130.0, "at": "t1"},
        {"spread_home": -4.5, "total": 48.0, "home_ml": -150.0, "away_ml": 130.0,
         "at": "2026-09-17T16:03-04:00"},
    ]
    older = _game("NE", "SEA", -3.0, 44.0, -1.0, 44.0)
    older["history"] = [{"spread_home": -3.0, "total": 44.0, "at": "t0"},
                        {"spread_home": -1.0, "total": 44.0, "at": "t1"}]   # moved in an older pull
    week = {
        "pull": {"pulled_at": "2026-09-17T16:03-04:00", "props_pull_id": "2026-09-17T20:03:42+00:00"},
        "games": {"SF@LAR": g, "NE@SEA": older},
        "props": {
            "x": _prop("Mover", "HOU", "pass_yds", 250.0, 245.0, 228.0, prev=245.0),
            "y": _prop("Old", "HOU", "pass_yds", 250.0, 200.0, 240.0, prev=200.0, cur_at="older"),
        },
    }
    moves = li.latest_pull_moves(week, SETTINGS)
    assert sorted((m["type"], m.get("player") or m.get("game")) for m in moves) == [
        ("prop_move", "Mover"), ("spread_move", "SF@LAR")]
