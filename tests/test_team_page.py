"""Team page loaders (dashboard/team_data.py) and the shared line helpers it
uses from processing/line_insights.py."""

import math

from collectors import odds_collector as oc
from dashboard import team_data as td
from processing import line_insights as li

SETTINGS = {"odds": {"thresholds": oc.DEFAULT_THRESHOLDS}}


def _prop(player, team, stat, ours, opened, current, prev=None, thin=False, flag="", line=None):
    return {"gsis_id": f"id-{player}", "player": player, "team": team, "opp": "X", "pos": "WR",
            "stat": stat, "ours": ours, "flag": flag, "thin": thin, "pulls": 3,
            "opened": {"mkt_mu": opened}, "previous": {"mkt_mu": prev} if prev is not None else None,
            "current": {"mkt_mu": current, "cons_line": line}}


# ---------------------------------------------------------------------------
# Prop table: comparable units
# ---------------------------------------------------------------------------


def test_prop_table_puts_receptions_and_yards_on_one_scale():
    week = {"props": {
        "r": _prop("Catcher", "HOU", "receptions", 4.2, 4.5, 5.15, prev=4.5, line=4.5),   # +0.65 rec
        "y": _prop("Passer", "HOU", "pass_yds", 253.9, 245.4, 227.8, prev=245.4),        # -17.6 yds
    }}
    rows = {r["player"]: r for r in li.prop_table(week, SETTINGS)}
    assert round(rows["Catcher"]["move_pct"]) == 14 and round(rows["Passer"]["move_pct"]) == -7
    assert rows["Catcher"]["book_line"] == 4.5
    assert round(rows["Catcher"]["vs_you_pct"]) == 23
    assert round(rows["Catcher"]["last_pull"], 2) == 0.65


def test_anytime_td_is_shown_as_a_scoring_chance():
    assert round(li.display_value("anytime_td", 0.82), 1) == round(100 * (1 - math.exp(-0.82)), 1)
    assert li.display_value("rec_yds", 31.9) == 31.9
    assert li.stat_display_label("anytime_td") == "Anytime TD %"
    rows = li.prop_table({"props": {"t": _prop("Back", "MIN", "anytime_td", 0.61, 0.51, 0.82)}}, SETTINGS)
    assert round(rows[0]["open"]) == 40 and round(rows[0]["now"]) == 56


def test_prop_table_ranks_by_threshold_units_not_percent():
    """A TD chance going 3% -> 12% is +300%; ranked by percent it would bury
    a 17-yard passing move. In threshold units they rank as peers."""
    week = {"props": {
        "td": _prop("Longshot", "DEN", "anytime_td", 0.0, 0.03, 0.13),   # +0.10 rate = 1.0x thr
        "py": _prop("Passer", "HOU", "pass_yds", 253.9, 245.4, 227.8),   # -17.6 yds = 2.2x thr
    }}
    rows = li.prop_table(week, SETTINGS)
    assert [r["player"] for r in rows] == ["Passer", "Longshot"]
    assert rows[1]["move_pct"] > rows[0]["move_pct"] * -10          # the % really is bigger


def test_prop_table_filters_team_thin_played_and_unmoved():
    week = {"props": {
        "a": _prop("Mover", "NO", "rec_yds", 40.0, 40.0, 50.0),
        "b": _prop("Still", "NO", "rec_yds", 40.0, 40.0, 41.0),
        "c": _prop("Thin", "NO", "rec_yds", 40.0, 40.0, 60.0, thin=True),
        "d": _prop("Other", "KC", "rec_yds", 40.0, 40.0, 60.0),
        "e": _prop("Done", "BUF", "rec_yds", 40.0, 40.0, 60.0),
    }}
    assert [r["player"] for r in li.prop_table(week, SETTINGS, team="NO")] == ["Mover"]
    assert {r["player"] for r in li.prop_table(week, SETTINGS, team="NO", only_moved=False)} == {"Mover", "Still"}
    assert "Thin" in {r["player"] for r in li.prop_table(week, SETTINGS, team="NO", include_thin=True)}
    assert "Done" not in {r["player"] for r in li.prop_table(week, SETTINGS, played={"BUF"})}


# ---------------------------------------------------------------------------
# Game card: the team's own side of the line
# ---------------------------------------------------------------------------


def test_game_card_reads_the_line_from_each_teams_side():
    g = {"away": "NO", "home": "BAL", "kickoff_et": "Sun 09/20 01:00 PM",
         "opened": {"spread_home": -8.5, "total": 47.0},
         "current": {"spread_home": -8.0, "total": 46.5, "home_ml": -400.0, "away_ml": 320.0},
         "sheet": {"spread_home": -8.0, "ou": 46.5, "fp_flag": ""},
         "sharp": {"spread_home": -9.0, "total": 46.5},
         "history": [{"spread_home": -8.5, "total": 47.0, "home_ml": -420.0, "away_ml": 330.0, "at": "t1"},
                     {"spread_home": -8.0, "total": 46.5, "home_ml": -400.0, "away_ml": 320.0, "at": "t2"}]}
    week = {"games": {"NO@BAL": g}}
    no, bal = li.game_card(week, "NO"), li.game_card(week, "BAL")
    assert (no["spread_open"], no["spread_now"], no["sharp_spread"]) == (8.5, 8.0, 9.0)
    assert (bal["spread_open"], bal["spread_now"]) == (-8.5, -8.0)
    assert no["opp"] == "BAL" and not no["home"] and bal["home"]
    assert abs(no["implied_now"] + bal["implied_now"] - 46.5) < 0.11   # each side rounded to 0.1
    assert no["ml_now"] == 320.0 and [h["spread"] for h in no["history"]] == [8.5, 8.0]
    assert li.game_card(week, "KC") is None


# ---------------------------------------------------------------------------
# Team data loaders
# ---------------------------------------------------------------------------


def test_injury_rows_use_the_teams_own_days_and_skip_cleared():
    week = {"teams": {"NO": {"practice_days": ["2026-09-16", "2026-09-17", "2026-09-18"], "players": {
        "k": {"name": "Alvin Kamara", "pos": "RB", "injury": "Knee",
              "practice": {"2026-09-16": "FP", "2026-09-17": "FP"}, "game_status": ""},
        "o": {"name": "Chris Olave", "pos": "WR", "injury": "Hamstring",
              "practice": {"2026-09-17": "LP"}, "game_status": "Q"},
        "c": {"name": "Gone Guy", "pos": "TE", "practice": {"2026-09-16": "DNP"}, "cleared": "2026-09-17"},
    }}}}
    days, rows = td.injury_rows(week, "NO")
    assert days == ["Wed 09-16", "Thu 09-17", "Fri 09-18"]
    assert [r["Player"] for r in rows] == ["Chris Olave", "Alvin Kamara"]   # designated first
    assert rows[0]["Game status"] == "Questionable" and rows[0]["Thu 09-17"] == "LP"
    assert td.injury_rows(week, "KC") == ([], [])


def test_audit_alerts_match_the_projection_dialect():
    audit = {"alerts": [
        {"team": "HST", "severity": "info", "type": "x"},       # HOU on the sheet
        {"team": "HST", "severity": "error", "type": "y"},
        {"team": "BUF", "severity": "warning", "type": "z"},
    ]}
    assert [a["type"] for a in td.audit_alerts(audit, "HOU")] == ["y", "x"]


def test_projection_rows_filter_team_and_carry_the_ppr_change():
    output = {
        "a": {"name": "Nico Collins", "pos": "WR", "team": "HST", "ppr": 16.0, "pos_rank": "WR8",
              "stats": {"TGTs": 8.4, "RECs": 5.6, "REC Yds": 78.0}},
        "b": {"name": "Depth Body", "pos": "WR", "team": "HST", "ppr": 0.0, "stats": {}},
        "c": {"name": "Other", "pos": "WR", "team": "BUF", "ppr": 10.0, "stats": {}},
    }
    rows = td.projection_rows(output, {"a": {"depth": 1, "status": "Active"}}, "HOU",
                              previous_output={"a": {"ppr": 15.2}})
    assert [r["Player"] for r in rows] == ["Nico Collins"]
    assert rows[0]["Δ PPR"] == 0.8 and rows[0]["Rec Yds"] == 78.0 and rows[0]["Depth"] == 1


def test_depth_chart_maps_ourlads_slug_and_reads_caps_names():
    depth = {
        "1": {"name": "Jacoby Brissett", "pos": "QB", "generic_pos": "QB", "depth": 1, "team": "ARZ"},
        "2": {"name": "JAMES CONNER", "pos": "RB", "generic_pos": "RB", "depth": 2, "team": "ARZ"},
        "3": {"name": "Trey Benson", "pos": "RB", "generic_pos": "RB", "depth": 1, "team": "ARZ"},
        "4": {"name": "Some Tackle", "pos": "LT", "generic_pos": "OL", "depth": 1, "team": "ARZ"},
    }
    rows = td.depth_chart(depth, "ARI")
    assert rows == [{"Slot": "QB", "#1": "Jacoby Brissett"},
                    {"Slot": "RB", "#1": "Trey Benson", "#2": "James Conner"}]
    assert td.readable_name("DJ MOORE") == "DJ Moore" and td.readable_name("Rome Odunze") == "Rome Odunze"


def test_event_rows_skip_ourlads_name_only_rows_and_old_events():
    events = [
        {"date": "2026-09-18", "team": "BUF", "name": "Greg Dortch", "event_type": "ps_elevated",
         "source_kind": "nflverse", "source": "nflverse"},
        {"date": "2026-09-16", "team": "BUF", "name": "X", "event_type": "status_change",
         "source_kind": "ourlads", "source": "ourlads"},
        {"date": "2026-09-10", "team": "BUF", "name": "Old", "event_type": "signed", "source_kind": "official"},
        {"date": "2026-09-17", "to_team": "BUF", "team": "", "name": "Claimed", "event_type": "claimed",
         "source_kind": "official", "source": "NFL.com"},
    ]
    rows = td.event_rows(events, "BUF", since="2026-09-15")
    assert [r["Player"] for r in rows] == ["Greg Dortch", "Claimed"]


def test_roster_rows_off_the_53_only():
    state = {"players": {
        "a": {"name": "IR Guy", "team": "NO", "pos": "RB", "status": "IR", "earliest_return_week": 5},
        "b": {"name": "PS Guy", "team": "NO", "pos": "WR", "status": "PS", "elevations_used": 1},
        "c": {"name": "Active", "team": "NO", "pos": "WR", "status": "ACT"},
        "d": {"name": "PS Lineman", "team": "NO", "pos": "OL", "status": "PS"},
    }}
    assert [r["Player"] for r in td.roster_rows(state, "NO")] == ["IR Guy", "PS Guy", "PS Lineman"]
    skill = td.roster_rows(state, "NO", skill_only=True)
    assert [r["Player"] for r in skill] == ["IR Guy", "PS Guy"] and skill[0]["Eligible Wk"] == 5
