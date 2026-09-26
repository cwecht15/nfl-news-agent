"""processing.line_insights — "what moved" from a chosen baseline.

Open, since yesterday's report (a cutoff on when a pull was first *read*),
and the latest pull; team totals split into total and spread parts; player
lines grouped one row per player, with flags never adding a row.
"""

from collectors import odds_collector as oc
from processing import line_insights as li

SETTINGS = {"odds": {"thresholds": oc.DEFAULT_THRESHOLDS}}

# Three pulls: Tue (opening), Thu 4 PM ET, Fri 9:41 AM ET — first read by
# runs at Tue 10:04, Thu 14:59 and Fri 11:41 ET (UTC below).
TUE, THU, FRI = "2026-09-22T09:06-04:00", "2026-09-24T14:16-04:00", "2026-09-25T09:41-04:00"
PROPS_TUE, PROPS_THU = "2026-09-22T13:06:18+00:00", "2026-09-24T18:16:18+00:00"
PROPS_FRI_FULL, PROPS_FRI_TD = "2026-09-25T13:41:07+00:00", "2026-09-25T14:48:26+00:00"
PULL_LOG = [
    {"pulled_at": TUE, "props_pull_id": PROPS_TUE, "seen_at": "2026-09-22T14:04:20+00:00", "changes": []},
    # a re-read of the same pull carrying only flag changes — not a new pull
    {"pulled_at": TUE, "props_pull_id": PROPS_TUE, "seen_at": "2026-09-22T23:49:37+00:00", "changes": []},
    {"pulled_at": THU, "props_pull_id": PROPS_THU, "seen_at": "2026-09-24T18:59:43+00:00", "changes": []},
    # one read brought in a full prop pull AND an anytime-TD merge pull
    {"pulled_at": FRI, "props_pull_id": PROPS_FRI_TD, "seen_at": "2026-09-25T15:41:10+00:00", "changes": []},
]
YESTERDAY_REPORT = "2026-09-24T14:30:00+00:00"      # Thursday's morning report ran before Thu's pull


def _line(sp, tot, at):
    return {"spread_home": sp, "total": tot, "at": at}


def _game(away, home, *lines):
    return {"away": away, "home": home, "kickoff_et": "Sun 09/27 01:00 PM",
            "opened": lines[0], "current": lines[-1], "history": list(lines),
            "sheet": {"spread_home": lines[-1]["spread_home"], "ou": lines[-1]["total"], "fp_flag": ""}}


def _prop(player, team, stat, ours, history, flag="", thin=False):
    return {"gsis_id": f"id-{player}", "player": player, "team": team, "opp": "X", "pos": "WR",
            "stat": stat, "ours": ours, "flag": flag, "thin": thin, "pulls": len(history),
            "opened": {"mkt_mu": history[0][1], "at": history[0][0]},
            "previous": {"mkt_mu": history[-2][1], "at": history[-2][0]} if len(history) > 1 else None,
            "current": {"mkt_mu": history[-1][1], "cons_line": None, "at": history[-1][0]},
            "history": [list(h) for h in history]}


def _week(**extra):
    return {"pull": {"pulled_at": FRI, "props_pull_id": PROPS_FRI_TD},
            "pull_log": PULL_LOG, **extra}


def test_team_move_splits_exactly_into_total_and_spread_parts():
    # CHI -5.5 / 49.5 -> -4.5 / 47.0: total -2.5 costs each side 1.25;
    # the spread moving 1 away from CHI costs it 0.5 more and gives MIN 0.5.
    week = _week(games={"MIN@CHI": _game("MIN", "CHI", _line(-5.5, 49.5, TUE), _line(-4.5, 47.0, THU))})
    by = {r["team"]: r for r in li.team_movement(week, SETTINGS)}
    chi, mn = by["CHI"], by["MIN"]
    assert chi["base"] == 27.5 and chi["now"] == 25.75 and chi["move"] == -1.75
    assert chi["from_total"] == -1.25 and chi["from_spread"] == -0.5
    assert mn["move"] == -0.75 and mn["from_spread"] == 0.5
    for r in (chi, mn):
        assert abs(r["from_total"] + r["from_spread"] - r["move"]) < 1e-9
    assert li.why_text(chi) == "total -1.25, spread -0.5"
    assert li.why_text({"from_total": 0.0, "from_spread": 1.0}) == "spread +1"


def test_team_movement_sorts_biggest_first_keeps_unchanged_and_drops_played():
    week = _week(games={
        "MIN@CHI": _game("MIN", "CHI", _line(-5.5, 49.5, TUE), _line(-4.5, 47.0, THU)),
        "NE@SEA": _game("NE", "SEA", _line(-3.0, 44.0, TUE)),
        "DET@BUF": _game("DET", "BUF", _line(-4.5, 53.5, TUE), _line(-7.5, 53.5, THU)),
    })
    rows = li.team_movement(week, SETTINGS, played={"BUF", "DET"})
    assert [r["team"] for r in rows][:2] == ["CHI", "MIN"]
    assert {r["team"] for r in rows if r["move"] == 0} == {"NE", "SEA"}
    assert not {"BUF", "DET"} & {r["team"] for r in rows}


def test_window_measures_from_what_had_been_read_by_the_report():
    week = _week(games={"MIN@CHI": _game(
        "MIN", "CHI", _line(-5.5, 49.5, TUE), _line(-4.5, 47.0, THU), _line(-4.5, 46.0, FRI))})
    # Thursday's pull was read after the report, so the window starts at Tuesday's line.
    chi = {r["team"]: r for r in li.team_movement(week, SETTINGS, baseline="window",
                                                   since=YESTERDAY_REPORT)}["CHI"]
    assert chi["base"] == 27.5 and chi["move"] == -2.25
    # A report after Thursday's read but before Friday's: only Friday's move is new.
    chi = {r["team"]: r for r in li.team_movement(week, SETTINGS, baseline="window",
                                                   since="2026-09-25T10:00:00+00:00")}["CHI"]
    assert chi["base_at"] == THU and chi["move"] == -0.5
    # No earlier report: the whole week counts, i.e. measured from open.
    assert li.team_movement(week, SETTINGS, baseline="window", since=None)[0]["move"] == -2.25


def test_window_compares_read_time_not_pull_time():
    """Friday's 9:41 AM ET pull was first read at 11:41 AM ET. A 10:00 AM ET
    report never saw it, even though the pull itself predates the report."""
    week = _week(games={"MIN@CHI": _game(
        "MIN", "CHI", _line(-5.5, 49.5, TUE), _line(-4.5, 47.0, THU), _line(-4.5, 46.0, FRI))})
    chi = {r["team"]: r for r in li.team_movement(week, SETTINGS, baseline="window",
                                                   since="2026-09-25T14:00:00+00:00")}["CHI"]
    assert chi["base_at"] == THU


def test_last_pull_covers_every_pull_the_latest_read_brought_in():
    week = _week(
        games={"MIN@CHI": _game("MIN", "CHI", _line(-5.5, 49.5, TUE), _line(-4.5, 47.0, THU))},
        props={
            # moved in the full Friday prop pull, which is not the latest props_pull_id
            "a": _prop("Full Pull", "HOU", "rec_yds", 50.0,
                       [(PROPS_THU, 50.0), (PROPS_FRI_FULL, 60.0)]),
            "b": _prop("TD Merge", "HOU", "anytime_td", 0.3,
                       [(PROPS_TUE, 0.30), (PROPS_FRI_TD, 0.50)]),
            "c": _prop("Thursday", "HOU", "rec_yds", 50.0,
                       [(PROPS_TUE, 40.0), (PROPS_THU, 55.0)]),
        })
    # CHI's line last changed Thursday, before the latest read: no move in the latest pull.
    assert li.team_movement(week, SETTINGS, baseline="last")[0]["move"] == 0
    players = {r["player"] for r in li.player_movement(week, SETTINGS, baseline="last")}
    assert players == {"Full Pull", "TD Merge"}


def test_player_movement_groups_stats_and_reads_direction_and_trend():
    week = _week(props={
        "r": _prop("Rec Guy", "NE", "receptions", 4.0, [(PROPS_TUE, 4.1), (PROPS_THU, 4.7)]),
        "y": _prop("Rec Guy", "NE", "rec_yds", 45.0, [(PROPS_TUE, 48.5), (PROPS_THU, 56.0)]),
        "m": _prop("Mixed Guy", "KC", "rush_yds", 60.0, [(PROPS_TUE, 60.0), (PROPS_THU, 70.0)]),
        "n": _prop("Mixed Guy", "KC", "receptions", 2.0, [(PROPS_TUE, 3.0), (PROPS_THU, 2.0)]),
    })
    rows = li.player_movement(week, SETTINGS)
    by = {r["player"]: r for r in rows}
    rec = by["Rec Guy"]
    assert rec["direction"] == "up" and rec["vs_you"] == "away"
    assert rec["summary"] == "Rec Yds 48.5→56.0 (+7.5) · Rec 4.1→4.7 (+0.6)"
    assert rec["size"] == 7.5 / oc.DEFAULT_THRESHOLDS["props"]["rec_yds"]
    mixed = by["Mixed Guy"]
    assert mixed["direction"] == "mixed" and mixed["vs_you"] == "mixed"
    assert rows[0]["size"] >= rows[-1]["size"]


def test_flags_do_not_add_rows_and_small_thin_played_moves_are_left_out():
    week = _week(props={
        "flag": _prop("Red Only", "NE", "rec_yds", 20.0, [(PROPS_TUE, 50.0), (PROPS_THU, 50.0)], flag="RED"),
        "small": _prop("Small", "NE", "receptions", 4.0, [(PROPS_TUE, 4.0), (PROPS_THU, 4.2)]),
        "thin": _prop("Thin", "NE", "rec_yds", 20.0, [(PROPS_TUE, 20.0), (PROPS_THU, 45.0)], thin=True),
        "played": _prop("Played", "BUF", "rec_yds", 20.0, [(PROPS_TUE, 20.0), (PROPS_THU, 45.0)]),
        "td": _prop("Tiny TD", "NE", "anytime_td", 0.03, [(PROPS_TUE, 0.030), (PROPS_THU, 0.045)]),
    })
    assert li.player_movement(week, SETTINGS, played={"BUF"}) == []
    assert [r["player"] for r in li.player_movement(week, SETTINGS, played={"BUF"}, min_size=0.3)] == ["Small"]


def test_props_without_history_fall_back_to_open_and_previous():
    """Week files written before props carried a history."""
    p = _prop("Old File", "NE", "rec_yds", 40.0, [(PROPS_TUE, 40.0), (PROPS_THU, 48.0), (PROPS_FRI_FULL, 55.0)])
    del p["history"]
    week = _week(props={"p": p})
    assert li.player_movement(week, SETTINGS)[0]["stats"][0]["base"] == 40.0          # open
    # latest read: previous (Thursday) was read before it
    assert li.player_movement(week, SETTINGS, baseline="last")[0]["stats"][0]["base"] == 48.0


def test_prop_table_default_baseline_is_unchanged():
    week = _week(props={"r": _prop("Rec Guy", "NE", "rec_yds", 45.0,
                                   [(PROPS_TUE, 48.5), (PROPS_THU, 56.0), (PROPS_FRI_FULL, 60.0)])})
    assert li.prop_table(week, SETTINGS)[0]["open"] == 48.5
    assert li.prop_table(week, SETTINGS, baseline="last", only_moved=False)[0]["open"] == 56.0
