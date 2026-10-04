"""Positional attrition — Week-1 baseline + this week's availability by slot."""

from processing import attrition as at

S = {}  # settings: use the module defaults, never config/settings.yaml


def _dc(name, team, pos, depth):
    return {name.lower(): {"name": name, "pos": pos, "generic_pos": pos, "depth": depth, "team": team}}


def _nv(gsis, name, team, status="ACT", dcp="CB"):
    return {gsis: {"gsis_id": gsis, "name": name, "name_key": at.name_key(name), "team": team,
                   "pos": "DB", "depth_chart_position": dcp, "status": status}}


def _baseline():
    depth = {}
    for args in [("Ace Corner", "NYG", "LCB", 1), ("Bo Backup", "NYG", "LCB", 2),
                 ("Cy Corner", "NYG", "RCB", 1), ("Nick Nickel", "NYG", "NB", 1),
                 ("Ps Guy", "NYG", "CB", 1),            # practice-squad block row
                 ("Ret Corner", "NYG", "KR", 1),        # starting SS overwritten by KR
                 ("Fred Free", "NYG", "FS", 1),
                 ("Lou Left", "NYG", "LT", 1),
                 ("Dee End", "NYG", "LDE", 1), ("Nate Nose", "NYG", "NT", 1),
                 ("Ollie Rush", "NYG", "LOLB", 1)]:
        depth.update(_dc(*args))
    depth.update(_dc("Ss Backup", "NYG", "SS", 2))
    nv = {}
    for g, n, dcp in [("1", "Ace Corner", "CB"), ("2", "Bo Backup", "CB"), ("3", "Cy Corner", "CB"),
                      ("4", "Nick Nickel", "CB"), ("6", "Ret Corner", "SS"), ("7", "Fred Free", "FS"),
                      ("8", "Lou Left", "T"), ("9", "Dee End", "DE"), ("10", "Nate Nose", "NT"),
                      ("11", "Ollie Rush", "OLB"), ("12", "Ss Backup", "SS")]:
        nv.update(_nv(g, n, "NYG", dcp=dcp))
    nv.update(_nv("5", "Ps Guy", "NYG", status="DEV"))
    proj = {
        "w1": {"player_id": "w1", "name": "Wes One", "team": "NYG", "pos": "WR", "slot": 1, "depth": 1, "status": "Active"},
        "w2": {"player_id": "w2", "name": "Wes Two", "team": "NYG", "pos": "WR", "slot": 2, "depth": 2, "status": "Active"},
        "w3": {"player_id": "w3", "name": "Wes Three", "team": "NYG", "pos": "WR", "slot": 3, "depth": 3, "status": "Active"},
        "w4": {"player_id": "w4", "name": "Wes Four", "team": "NYG", "pos": "WR", "slot": 4, "depth": 4, "status": "Active"},
        "q1": {"player_id": "q1", "name": "Quinn Bee", "team": "NYG", "pos": "QB", "slot": 5, "depth": 1, "status": "Active"},
    }
    return at.build_baseline(depth, proj, {"players": nv}, settings=S)


def _slots(base):
    return {e["label"]: e for e in base["teams"]["NYG"]}


def test_baseline_ranks_units_and_drops_practice_squad():
    s = _slots(_baseline())
    assert s["CB1"]["name"] == "Ace Corner" and s["CB2"]["name"] == "Cy Corner"
    assert s["CB3"]["name"] == "Nick Nickel" and s["CB3"]["starter"]
    assert s["CB4"]["name"] == "Bo Backup" and not s["CB4"]["starter"]
    assert all(e["name"] != "Ps Guy" for e in s.values())
    assert s["LT"]["unit"] == "OL"
    assert s["WR1"]["name"] == "Wes One" and s["WR3"]["starter"] and not s["WR4"]["starter"]


def test_odd_front_puts_des_inside():
    s = _slots(_baseline())
    assert s["DT1"]["name"] == "Dee End" and s["DT2"]["name"] == "Nate Nose"
    assert s["EDGE1"]["name"] == "Ollie Rush"


def test_returner_repair_fills_the_empty_starting_row():
    base = _baseline()
    s = _slots(base)
    assert s["S1"]["name"] == "Ret Corner" and s["S1"]["repaired"]
    assert any("Ret Corner" in r for r in base["repairs"])


def _state(**players):
    out = {"players": {}, "by_name": {}}
    for gsis, (status, team, extra) in players.items():
        out["players"][gsis] = {"gsis_id": gsis, "status": status, "team": team, **extra}
    return out


def _run(base, state=None, injuries=None, inactives=None, week=4):
    return at.build_attrition(base, state or {}, injuries, inactives or {}, week, settings=S)["NYG"]


def test_ir_from_an_earlier_week_stays_down_and_activation_clears_it():
    base = _baseline()
    sched = [{"week": w, "home": "NYG", "away": "DAL", "date": d}
             for w, d in [(1, "2026-09-13"), (2, "2026-09-20"), (3, "2026-09-27"), (4, "2026-10-04")]]
    st = _state(**{"1": ("IR", "NYG", {"ir_date": "2026-09-16", "earliest_return_week": 6}),
                   "3": ("IR", "NYG", {"ir_date": "2026-09-30"})})
    res = at.build_attrition(base, st, None, {}, 4, schedule=sched, settings=S)["NYG"]["CB"]
    down = {s["label"]: s for s in res["down"]}
    assert down["CB1"]["since_week"] == 2 and down["CB1"]["return_week"] == 6
    assert res["level"] == "severe" and res["starters_down"] == 2.0
    assert "IR since Wk2" == at.slot_status_text(down["CB1"])

    back = _run(base, _state(**{"1": ("ACT", "NYG", {})}))
    assert back["CB"]["level"] == "none" and back["CB"]["down"] == []


def test_questionable_is_partial_and_dnp_is_an_early_warning():
    base = _baseline()
    inj = {"teams": {"NYG": {"players": {
        "wes one": {"name": "Wes One", "game_status": "Q", "injury": "Hamstring", "practice": {"2026-10-02": "LP"}},
        "wes two": {"name": "Wes Two", "game_status": "", "injury": "Knee", "practice": {"2026-09-30": "DNP"}},
        "wes three": {"name": "Wes Three", "game_status": "", "injury": "NIR - Rest", "practice": {"2026-09-30": "DNP"}},
    }}}}
    wr = _run(base, injuries=inj)["WR"]
    by = {s["label"]: s for s in wr["slots"]}
    assert by["WR1"]["code"] == "Q" and by["WR1"]["weight"] == 0.35
    assert by["WR2"]["code"] == "DNP" and by["WR2"]["weight"] == 0.5
    assert by["WR3"]["weight"] == 0          # rest day is not an availability signal
    assert wr["level"] == "mild"
    assert at.unit_cell(wr) == "WR1(Q)·WR2(DNP)"


def test_most_severe_source_wins():
    base = _baseline()
    inj = {"teams": {"NYG": {"players": {"wes one": {"name": "Wes One", "game_status": "Q", "injury": "Ankle"}}}}}
    ina = {4: {"games": {"g": {"teams": {"NYG": {"inactives": [{"name": "Wes One", "gsis_id": "w1", "team": "NYG"}]}}}}}}
    wr1 = next(s for s in _run(base, injuries=inj, inactives=ina)["WR"]["slots"] if s["label"] == "WR1")
    assert wr1["code"] == "INACTIVE" and wr1["weight"] == 1.0


def test_missed_weeks_come_from_earlier_inactives():
    base = _baseline()
    ina = {2: {"games": {"g": {"teams": {"NYG": {"inactives": [{"name": "Wes One", "gsis_id": "w1", "team": "NYG"}]}}}}}}
    wr1 = next(s for s in _run(base, inactives=ina)["WR"]["slots"] if s["label"] == "WR1")
    assert wr1["missed_weeks"] == [2] and wr1["weight"] == 0


def test_departed_players_are_greyed_not_scored():
    base = _baseline()
    res = _run(base, _state(**{"1": ("ACT", "DAL", {}), "3": ("FA", "NYG", {})}))["CB"]
    down = {s["label"]: s for s in res["down"]}
    assert down["CB1"]["departed"] and down["CB1"]["detail"] == "now DAL"
    assert down["CB2"]["departed"] and down["CB1"]["impact"] == 0
    assert res["score"] == 0 and res["level"] == "none"


def test_suspension_counts():
    res = _run(_baseline(), _state(**{"q1": ("SUS", "NYG", {})}))["QB"]
    assert res["level"] == "severe"


def test_prompt_lines():
    base = _baseline()
    st = _state(**{"w1": ("IR", "NYG", {}), "1": ("IR", "NYG", {}), "3": ("IR", "NYG", {})})
    units = _run(base, st)
    cfg = at.config(S)
    assert at.own_team_line(units, cfg) == "WR1 One (IR)"
    assert at.opponent_line(units, cfg).startswith("CB1 Corner (IR), CB2 Corner (IR)")
    assert at.opponent_line(_run(base), cfg) == ""


def test_also_rows_recover_a_returners_real_slot():
    depth = {"kay ret": {"name": "Kay Ret", "pos": "KR", "generic_pos": "RET", "depth": 1, "team": "NYG",
                         "also": [{"pos": "RCB", "depth": 1}, {"pos": "PR", "depth": 2}]},
             "al left": {"name": "Al Left", "pos": "LCB", "generic_pos": "CB", "depth": 1, "team": "NYG"}}
    nv = {**_nv("1", "Kay Ret", "NYG"), **_nv("2", "Al Left", "NYG")}
    base = at.build_baseline(depth, {}, {"players": nv}, settings=S)
    s = {e["label"]: e for e in base["teams"]["NYG"]}
    assert s["CB2"]["name"] == "Kay Ret" and s["CB2"]["row"] == "RCB" and not s["CB2"]["repaired"]
    assert base["repairs"] == []
