"""collectors.odds_collector + the section / audit pieces that consume it.

Entirely offline: the two fixtures under tests/fixtures/ are real captures of
``SB_GameLines`` and the ``2026_W01`` market-history tab, so the parsing and
the partial-pull handling are exercised against the shapes the NFL Odds
project actually writes.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collectors import odds_collector as oc
from processing import odds_section as osec
from processing import projection_audit as pa

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_history():
    return json.loads((FIXTURES / "market_history_wk1.json").read_text(encoding="utf-8"))


def _fixture_games():
    return json.loads((FIXTURES / "sb_gamelines.json").read_text(encoding="utf-8"))


def _meta(**over):
    meta = {"pulled_at": "2026-09-10T09:52-04:00", "props_pull_id": "2026-09-10T13:52:05+00:00",
            "week_reported": 1, "stale_reason": "", "age_hours": 6.1,
            "thresholds": oc.DEFAULT_THRESHOLDS}
    meta.update(over)
    return meta


def _game(key=None, away="SF", home="LAR", spread=-3.0, total=48.0,
          home_ml=-180.0, away_ml=155.0, fp_flag=""):
    return {
        # the collector keys games AWAY@HOME, so derive it rather than letting
        # a caller's away/home silently disagree with a default key
        "key": key or f"{away}@{home}",
        "away": away, "home": home, "kickoff_et": "Thu 09/10 08:35 PM",
        "spread_home": spread, "total": total, "home_ml": home_ml, "away_ml": away_ml,
        "n_books": 39.0, "best": {}, "sharp": {"book": "pinnacle"},
        "sheet": {"spread_home": -3.5, "ou": 48.0, "fp_spread_delta": -0.2,
                  "fp_total_delta": 0.0, "fp_flag": fp_flag},
        "implied": oc._market_implied(spread, total),
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_status_time_infers_the_year_from_the_season():
    now = datetime(2026, 9, 10, 16, 0, tzinfo=oc.ET_ZONE)
    assert oc.parse_status_time("Thu Sep 10, 12:50 PM ET", 2026, now).startswith("2026-09-10T12:50")
    # January of the following calendar year still belongs to the 2026 season
    now_jan = datetime(2027, 1, 8, 10, 0, tzinfo=oc.ET_ZONE)
    got = oc.parse_status_time("Sat Jan 09, 1:00 PM ET", 2026, now_jan)
    assert got.startswith("2027-01-09T13:00")
    assert oc.parse_status_time("", 2026) is None
    assert oc.parse_status_time("nonsense", 2026) is None


def test_market_implied_totals_split_the_consensus_line():
    # LAR -3.75 on a 48 total => 25.875 / 22.125
    assert oc._market_implied(-3.75, 48.0) == {"home": 25.9, "away": 22.1}
    assert oc._market_implied(None, 48.0) == {"home": None, "away": None}


def test_float_parser_handles_sheet_cell_shapes():
    assert oc._f("+101") == 101.0
    assert oc._f("-3.75") == -3.75
    assert oc._f("64%") == 0.64
    assert oc._f("1,234") == 1234.0
    for blank in ("", "—", "-", None):
        assert oc._f(blank) is None


# ---------------------------------------------------------------------------
# Prop history collapse — the partial-pull regression
# ---------------------------------------------------------------------------


def test_previous_pull_is_resolved_per_player_stat_not_globally():
    """A --merge pull logs only the markets it refreshed.

    Pull 3 carries anytime_td alone. rec_yds must still diff pull 2 against
    pull 1 — never treat the anytime-only pull as "rec_yds vanished".
    """
    rows = [
        {"pulled_at": "2026-09-08T10:00:00+00:00", "gsis_id": "00-1", "player": "A", "team": "NE",
         "opp": "SEA", "pos": "WR", "stat": "rec_yds", "ours": 50.0, "mkt_mu": 50.0,
         "cons_line": 49.5, "n_books": 6, "delta": 0.0, "pct": 0.0, "flag": "", "game": "g",
         "event_id": "e", "kickoff_utc": ""},
        {"pulled_at": "2026-09-09T10:00:00+00:00", "gsis_id": "00-1", "player": "A", "team": "NE",
         "opp": "SEA", "pos": "WR", "stat": "rec_yds", "ours": 50.0, "mkt_mu": 62.0,
         "cons_line": 61.5, "n_books": 6, "delta": 12.0, "pct": 0.24, "flag": "", "game": "g",
         "event_id": "e", "kickoff_utc": ""},
        # anytime-TD-only merge pull, newest of all
        {"pulled_at": "2026-09-10T10:00:00+00:00", "gsis_id": "00-1", "player": "A", "team": "NE",
         "opp": "SEA", "pos": "WR", "stat": "anytime_td", "ours": 0.4, "mkt_mu": 0.45,
         "cons_line": None, "n_books": 6, "delta": 0.05, "pct": 0.1, "flag": "", "game": "g",
         "event_id": "e", "kickoff_utc": ""},
    ]
    props = oc.collapse_prop_history(rows)
    rec = props["00-1|rec_yds"]
    assert rec["current"]["mkt_mu"] == 62.0
    assert rec["previous"]["mkt_mu"] == 50.0          # not the anytime pull
    assert rec["opened"]["mkt_mu"] == 50.0
    assert rec["pulls"] == 2
    td = props["00-1|anytime_td"]
    assert td["previous"] is None and td["pulls"] == 1


def test_thin_quotes_are_stored_but_never_produce_a_change():
    rows = [
        {"pulled_at": "2026-09-08T10:00:00+00:00", "gsis_id": "00-2", "player": "B", "team": "NE",
         "opp": "SEA", "pos": "WR", "stat": "rec_yds", "ours": 10.0, "mkt_mu": 10.0,
         "cons_line": 9.5, "n_books": 1, "delta": 0.0, "pct": 0.0, "flag": "THIN", "game": "g",
         "event_id": "e", "kickoff_utc": ""},
        {"pulled_at": "2026-09-09T10:00:00+00:00", "gsis_id": "00-2", "player": "B", "team": "NE",
         "opp": "SEA", "pos": "WR", "stat": "rec_yds", "ours": 10.0, "mkt_mu": 90.0,
         "cons_line": 89.5, "n_books": 1, "delta": 80.0, "pct": 8.0, "flag": "THIN", "game": "g",
         "event_id": "e", "kickoff_utc": ""},
    ]
    props = oc.collapse_prop_history(rows)
    assert props["00-2|rec_yds"]["thin"] is True
    data, changes = oc.merge_into_week(None, 2026, 1, [], props, _meta(), "2026-09-09T12:00:00+00:00")
    assert [c for c in changes if c["type"] in ("prop_move", "prop_new")] == []
    assert "00-2|rec_yds" in data["props"]          # stored regardless


def test_real_history_fixture_parses_and_collapses():
    rows = _fixture_history()["rows"]
    props = oc.collapse_prop_history(rows)
    assert props, "fixture should yield player-stats"
    sample = next(iter(props.values()))
    assert {"opened", "current", "ours", "flag", "stat", "team"} <= set(sample)
    # captured teams are news-style after conversion (LA -> LAR, ARZ -> ARI)
    assert all(p["team"] not in ("LA", "ARZ", "BLT", "CLV", "HST") for p in props.values())


# ---------------------------------------------------------------------------
# Game-line diffing
# ---------------------------------------------------------------------------


def test_first_read_of_a_week_stores_the_opening_line_and_reports_no_movement():
    data, changes = oc.merge_into_week(None, 2026, 1, [_game()], {}, _meta(), "2026-09-08T12:00:00+00:00")
    g = data["games"]["SF@LAR"]
    assert g["opened"] == g["current"]
    assert len(g["history"]) == 1
    assert [c for c in changes if c["type"].endswith("_move")] == []


def test_spread_and_total_moves_fire_only_past_the_threshold():
    first, _ = oc.merge_into_week(None, 2026, 1, [_game(spread=-3.0, total=48.0)], {}, _meta(), "t1")
    # 0.25 spread / 0.5 total — both under threshold
    quiet, changes = oc.merge_into_week(first, 2026, 1, [_game(spread=-3.25, total=48.5)], {},
                                        _meta(), "t2")
    assert [c for c in changes if c["type"] in ("spread_move", "total_move")] == []
    # 1.0 spread / 2.0 total — both over
    _, changes = oc.merge_into_week(quiet, 2026, 1, [_game(spread=-4.25, total=46.5)], {},
                                    _meta(), "t3")
    types = {c["type"] for c in changes}
    assert "spread_move" in types and "total_move" in types
    spread = next(c for c in changes if c["type"] == "spread_move")
    assert "toward LAR" in spread["message"]
    assert spread["basis"] == "since last report"


def test_moneyline_move_fires_on_cents():
    first, _ = oc.merge_into_week(None, 2026, 1, [_game(home_ml=-180.0)], {}, _meta(), "t1")
    _, small = oc.merge_into_week(first, 2026, 1, [_game(home_ml=-190.0)], {}, _meta(), "t2")
    assert [c for c in small if c["type"] == "ml_move"] == []
    _, big = oc.merge_into_week(first, 2026, 1, [_game(home_ml=-220.0)], {}, _meta(), "t3")
    assert [c for c in big if c["type"] == "ml_move"]


def test_history_dedupes_on_content_not_on_the_clock():
    """A re-price reuses the odds' timestamp; an anytime-TD pull does not
    rewrite the game-lines tab at all. Two reads of an unchanged line must
    leave one history entry."""
    first, _ = oc.merge_into_week(None, 2026, 1, [_game()], {}, _meta(), "t1")
    again, _ = oc.merge_into_week(first, 2026, 1, [_game()], {}, _meta(), "t2")
    assert len(again["games"]["SF@LAR"]["history"]) == 1
    moved, _ = oc.merge_into_week(again, 2026, 1, [_game(spread=-4.5)], {}, _meta(), "t3")
    assert len(moved["games"]["SF@LAR"]["history"]) == 2


def test_sheet_drift_uses_the_odds_repos_own_fp_flag():
    _, changes = oc.merge_into_week(None, 2026, 1, [_game(fp_flag="FP-SPREAD")], {}, _meta(), "t1")
    drift = [c for c in changes if c["type"] == "sheet_drift"]
    assert len(drift) == 1
    assert "your sheet has" in drift[0]["message"]
    _, none = oc.merge_into_week(None, 2026, 1, [_game(fp_flag="")], {}, _meta(), "t1")
    assert [c for c in none if c["type"] == "sheet_drift"] == []


def test_game_line_fixture_converts_team_dialect_to_news_style():
    games = _fixture_games()
    abbrs = {g["away"] for g in games} | {g["home"] for g in games}
    assert not (abbrs & {"LA", "ARZ", "BLT", "CLV", "HST"}), abbrs


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


def test_collect_odds_marks_a_week_mismatch_stale_and_emits_no_movement(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(oc, "read_pull_status",
                        lambda gc, season, settings=None, now=None: {
                            "pulled_at": "2026-09-10T09:52-04:00", "week_reported": 1,
                            "detail": "2026 Wk1", "status": "ok"})
    monkeypatch.setattr(oc, "read_game_lines", lambda gc, settings=None: [_game()])
    monkeypatch.setattr(oc, "read_prop_history", lambda gc, s, w, settings=None: [])
    monkeypatch.setattr(oc.season_mod, "load_schedule", lambda **kw: [])
    res = oc.collect_odds("2026-09-16", week=2, season=2026, gc=object(), write=False)
    assert "Week 1" in res["pull"]["stale_reason"]
    assert res["changes"] == [] and res["errors"]


def test_collect_odds_never_raises_on_a_broken_read(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_base_dir", lambda: tmp_path)
    def _boom(*a, **kw):
        raise RuntimeError("sheets down")
    monkeypatch.setattr(oc, "read_pull_status", _boom)
    monkeypatch.setattr(oc.season_mod, "load_schedule", lambda **kw: [])
    res = oc.collect_odds("2026-09-10", week=1, season=2026, gc=object(), write=False)
    assert res["errors"] and "sheets down" in res["errors"][0]
    assert res["changes"] == []


# ---------------------------------------------------------------------------
# Report section
# ---------------------------------------------------------------------------


def _week_data(changes):
    return {"season": 2026, "week": 1,
            "pull": {"pulled_at": "2026-09-10T09:52-04:00", "stale_reason": ""},
            "games": {"NE@SEA": {"home": "SEA", "away": "NE"}},
            "changes": changes}


def test_section_pairs_a_prop_move_with_the_news_naming_that_player(make_item):
    change = {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Rhamondre Stevenson",
              "pos": "RB", "stat": "rush_yds", "magnitude": 2.5,
              "message": "**Rhamondre Stevenson** (RB, NE) Rush Yds 63.3, up from 48.2"}
    item = make_item("Rhamondre Stevenson in line for a bigger workload", teams=["NE"],
                     url="https://example.com/a")
    other = make_item("Seahawks sign a long snapper", teams=["SEA"], url="https://example.com/b")
    section = osec.build_odds_section(_week_data([change]), [item, other], use_llm=False)
    assert section["count"] == 1
    assert [s["url"] for s in section["numbered_sources"]] == ["https://example.com/a"]
    assert "[1]" in section["summary"]


def test_section_links_a_teammates_injury_to_a_backups_line_move(make_item):
    """The whole point: a backup's line moves because the starter is out."""
    change = {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Rhamondre Stevenson",
              "pos": "RB", "stat": "rush_yds", "magnitude": 2.5,
              "message": "**Rhamondre Stevenson** (RB, NE) Rush Yds up"}
    injuries = [{"team": "NE", "name": "TreVeyon Henderson", "type": "designation_set",
                 "new": "OUT", "message": "TreVeyon Henderson (ankle) listed OUT"}]
    section = osec.build_odds_section(_week_data([change]), [], injury_changes=injuries,
                                      use_llm=False)
    assert "teammate: TreVeyon Henderson (ankle) listed OUT" in section["summary"]


def test_section_does_not_attach_a_teams_news_to_every_one_of_its_props(make_item):
    change = {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Rhamondre Stevenson",
              "pos": "RB", "stat": "rush_yds", "magnitude": 2.5, "message": "x"}
    item = make_item("Patriots defense looks improved", teams=["NE"], url="https://example.com/c")
    section = osec.build_odds_section(_week_data([change]), [item], use_llm=False)
    assert section["numbered_sources"] == []


def test_section_reports_a_quiet_day_with_count_zero():
    section = osec.build_odds_section(_week_data([]), [], use_llm=False)
    assert section["count"] == 0
    assert "No market movement" in section["summary"]


def test_section_says_stale_rather_than_quiet_when_the_pull_is_old():
    wd = _week_data([])
    wd["pull"] = {"pulled_at": "2026-09-08T09:00-04:00", "stale_reason": "last odds pull was 52h ago"}
    section = osec.build_odds_section(wd, [], use_llm=False)
    assert "stale" in section["summary"] and "52h" in section["summary"]


def test_section_is_none_without_a_week_file():
    assert osec.build_odds_section(None, []) is None


# ---------------------------------------------------------------------------
# Report builder + audit consumers
# ---------------------------------------------------------------------------


def test_report_stores_a_trimmed_odds_payload():
    from reports.report_builder import _trim_odds_payload

    full = {"season": 2026, "week": 1, "updated_at": "t", "pull": {"pulled_at": "t"},
            "games": {"NE@SEA": {"home": "SEA", "away": "NE", "current": {"total": 44.0},
                                 "history": [{"total": 44.0}] * 40}},
            "props": {f"00-{i}|rec_yds": {} for i in range(1200)},
            "changes": [{"type": "total_move"}]}
    trimmed = _trim_odds_payload(full)
    assert trimmed["prop_count"] == 1200 and "props" not in trimmed
    assert "history" not in trimmed["games"]["NE@SEA"]
    assert trimmed["changes"] == [{"type": "total_move"}]
    assert _trim_odds_payload(None) == {}


def _audit_odds(flag, ours, mkt, stat="rec_yds", pos="WR", gsis="00-9", fp_flag=""):
    return {
        "pull": {"pulled_at": "2026-09-10T09:52-04:00", "stale_reason": ""},
        "games": {"NE@SEA": {"home": "SEA", "away": "NE",
                             "current": {"spread_home": -3.0, "total": 44.0},
                             "sheet": {"spread_home": -1.0, "ou": 47.0, "fp_flag": fp_flag,
                                       "fp_spread_delta": -2.0, "fp_total_delta": -3.0}}},
        "props": {f"{gsis}|{stat}": {"gsis_id": gsis, "player": "Test Player", "team": "NE",
                                     "pos": pos, "stat": stat, "ours": ours, "flag": flag,
                                     "delta": (mkt - ours) if ours else None, "pct": 1.0,
                                     "thin": False, "current": {"mkt_mu": mkt, "n_books": 6}}},
    }


def test_audit_flags_a_sheet_line_that_drifted_from_the_market():
    odds = _audit_odds("", 50.0, 50.0, fp_flag="FP-BOTH")
    alerts = pa.check_market({}, {}, odds, 1, "primary", cfg={})
    drift = [a for a in alerts if a["type"] == "sheet_line_stale"]
    assert len(drift) == 1 and "FP-BOTH" in drift[0]["message"]


def test_audit_collapses_correlated_stats_into_one_alert_per_player():
    odds = _audit_odds("RED", 20.0, 40.0)
    odds["props"]["00-9|rush_yds"] = dict(odds["props"]["00-9|rec_yds"],
                                          stat="rush_yds", pct=0.5)
    rows = {"00-9": {"name": "Test Player", "team": "NE", "pos": "WR"}}
    alerts = pa.check_market(rows, {}, odds, 1, "primary", cfg={})
    gaps = [a for a in alerts if a["type"] == "market_proj_gap"]
    assert len(gaps) == 1
    assert "also" in gaps[0]["message"]


def test_audit_only_calls_a_player_market_only_when_absent_from_the_sheet():
    """MKT-ONLY also covers a player who IS on the sheet projected zero."""
    odds = _audit_odds("MKT-ONLY", 0.0, 0.5, stat="anytime_td", pos="RB")
    cfg = {"market_only_min": {"anytime_td": 0.2}}
    # on the sheet -> not an alert
    on_sheet = pa.check_market({"00-9": {}}, {}, odds, 1, "primary", cfg=cfg)
    assert [a for a in on_sheet if a["type"] == "market_only_player"] == []
    # absent -> alert
    absent = pa.check_market({}, {}, odds, 1, "primary", cfg=cfg)
    assert [a for a in absent if a["type"] == "market_only_player"]


def test_audit_ignores_a_bare_low_anytime_td_quote():
    odds = _audit_odds("MKT-ONLY", 0.0, 0.05, stat="anytime_td", pos="WR")
    alerts = pa.check_market({}, {}, odds, 1, "primary",
                             cfg={"market_only_min": {"anytime_td": 0.2}})
    assert [a for a in alerts if a["type"] == "market_only_player"] == []


def test_audit_market_checks_go_silent_on_a_stale_pull():
    odds = _audit_odds("RED", 20.0, 40.0, fp_flag="FP-BOTH")
    odds["pull"]["stale_reason"] = "last odds pull was 52h ago"
    rows = {"00-9": {"name": "Test Player", "team": "NE", "pos": "WR"}}
    assert pa.check_market(rows, {}, odds, 1, "primary", cfg={}) == []


def test_audit_market_checks_are_skippable_and_safe_without_odds():
    assert pa.check_market({}, {}, None, 1, "primary", cfg={}) == []
    odds = _audit_odds("RED", 20.0, 40.0)
    assert pa.check_market({"00-9": {}}, {}, odds, 1, "primary", cfg={"enabled": False}) == []


def test_state_flags_report_once_not_every_run():
    """A drifted sheet line stays drifted until someone edits the sheet.

    Re-reporting it every run would fill a section about what *changed* with
    things that did not; the standing version lives in the Projection Audit.
    """
    g = _game(fp_flag="FP-SPREAD")
    first, changes = oc.merge_into_week(None, 2026, 1, [g], {}, _meta(), "t1")
    assert [c for c in changes if c["type"] == "sheet_drift"]
    _, again = oc.merge_into_week(first, 2026, 1, [g], {}, _meta(), "t2")
    assert [c for c in again if c["type"] == "sheet_drift"] == []
    # ...but a changed verdict is news again
    _, worse = oc.merge_into_week(first, 2026, 1, [_game(fp_flag="FP-BOTH")], {}, _meta(), "t3")
    assert [c for c in worse if c["type"] == "sheet_drift"]


def test_market_only_bullet_needs_a_meaningful_quote():
    def _prop(mkt, stat="anytime_td"):
        return {"x|" + stat: {"gsis_id": "x", "player": "Backup", "team": "NE", "opp": "SEA",
                              "pos": "WR", "stat": stat, "ours": 0.0, "flag": "MKT-ONLY",
                              "thin": False, "pulls": 1, "delta": None, "pct": None, "game": "g",
                              "opened": {"mkt_mu": mkt}, "previous": None,
                              "current": {"mkt_mu": mkt, "n_books": 6}}}
    _, low = oc.merge_into_week(None, 2026, 1, [], _prop(0.05), _meta(), "t1")
    assert [c for c in low if c["type"] == "market_only"] == []
    _, high = oc.merge_into_week(None, 2026, 1, [], _prop(0.45), _meta(), "t1")
    assert [c for c in high if c["type"] == "market_only"]
    # a yardage line has no floor — having one at all means expected volume
    _, yards = oc.merge_into_week(None, 2026, 1, [], _prop(12.0, "rec_yds"), _meta(), "t1")
    assert [c for c in yards if c["type"] == "market_only"]


def test_spread_messages_are_always_signed():
    """Unsigned "IND 3.5" reads as favoured by 3.5; the home dog is "+3.5"."""
    first, _ = oc.merge_into_week(None, 2026, 1, [_game(away="BAL", home="IND", spread=5.0)],
                                  {}, _meta(), "t1")
    _, changes = oc.merge_into_week(first, 2026, 1,
                                    [_game(away="BAL", home="IND", spread=3.5)], {}, _meta(), "t2")
    msg = next(c for c in changes if c["type"] == "spread_move")["message"]
    assert "IND +3.5" in msg and "was +5" in msg and "toward IND" in msg


def test_opened_is_not_echoed_when_it_equals_the_current_line():
    first, _ = oc.merge_into_week(None, 2026, 1, [_game(total=48.0)], {}, _meta(), "t1")
    moved, _ = oc.merge_into_week(first, 2026, 1, [_game(total=44.0)], {}, _meta(), "t2")
    _, back = oc.merge_into_week(moved, 2026, 1, [_game(total=48.0)], {}, _meta(), "t3")
    msg = next(c for c in back if c["type"] == "total_move")["message"]
    assert "opened" not in msg


def test_grouped_new_lines_do_not_render_a_missing_before_value():
    """"Rec ? → 1.2" is worse than "Rec 1.2" — a new line has no before."""
    changes = [
        {"type": "prop_new", "player": "Jordan Whittington", "gsis_id": "00-7", "pos": "WR",
         "team": "LAR", "stat": "receptions", "stat_label": "Rec", "old": None, "new": 1.2,
         "ours": 0.4, "magnitude": 0.5, "basis": "new this week", "message": "x"},
        {"type": "prop_new", "player": "Jordan Whittington", "gsis_id": "00-7", "pos": "WR",
         "team": "LAR", "stat": "rec_yds", "stat_label": "Rec Yds", "old": None, "new": 14.7,
         "ours": 8.7, "magnitude": 0.5, "basis": "new this week", "message": "x"},
    ]
    grouped = osec._group_player_moves(changes)
    assert len(grouped) == 1
    msg = grouped[0]["message"]
    assert "?" not in msg and "→" not in msg
    assert "Rec 1.2" in msg and "Rec Yds 14.7" in msg and "new lines this week" in msg


def test_anytime_td_new_line_shows_the_implied_rate_not_a_blank():
    """anytime_td is priced, not an O/U line, so cons_line is blank for it."""
    props = {"x|anytime_td": {"gsis_id": "x", "player": "Brevin Jordan", "team": "HOU",
                              "opp": "LAR", "pos": "TE", "stat": "anytime_td", "ours": 0.3,
                              "flag": "", "thin": False, "pulls": 1, "delta": None, "pct": None,
                              "game": "g", "opened": {"mkt_mu": 0.28},
                              "previous": None, "current": {"mkt_mu": 0.28, "cons_line": None}}}
    _, changes = oc.merge_into_week(None, 2026, 1, [], props, _meta(), "t1")
    msg = next(c for c in changes if c["type"] == "prop_new")["message"]
    assert "implying 0.28" in msg


def test_build_report_attaches_the_section_and_trims_the_payload(tmp_path, monkeypatch):
    """The AM pipeline's path: build_report(line_movement=..., odds=...)."""
    from reports import report_builder as rb

    monkeypatch.setattr(rb, "get_data_dir", lambda *a, **k: tmp_path)
    section = {"summary": "### Spreads\n- **NE@SEA** SEA -3.5", "count": 1,
               "sources": [], "numbered_sources": []}
    odds = {"season": 2026, "week": 1, "pull": {"pulled_at": "t"},
            "games": {"NE@SEA": {"home": "SEA", "away": "NE", "current": {"total": 44.0},
                                 "history": [{"total": 44.0}]}},
            "props": {"a|rec_yds": {}}, "changes": [{"type": "spread_move"}]}
    report = rb.build_report(
        date_str="2026-09-10", sections={}, team_highlights={}, news_items=[],
        line_movement=section, odds=odds, season_meta={"week": 1},
    )
    assert report.sections["line_movement"]["count"] == 1
    assert report.odds["prop_count"] == 1 and "props" not in report.odds
    # ordered next to the other "numbers that moved" sections
    assert list(report.sections) == ["line_movement"]
    assert rb.SECTION_ORDER.index("line_movement") == rb.SECTION_ORDER.index("projection_movers") + 1


def test_build_report_without_odds_adds_no_line_movement_section(tmp_path, monkeypatch):
    """The offseason/no-odds path must leave the section set untouched."""
    from reports import report_builder as rb

    monkeypatch.setattr(rb, "get_data_dir", lambda *a, **k: tmp_path)
    report = rb.build_report(date_str="2026-09-10", sections={}, team_highlights={}, news_items=[])
    assert "line_movement" not in report.sections
    assert report.odds == {}


# ---------------------------------------------------------------------------
# Selection budgets
# ---------------------------------------------------------------------------


def _many(ctype, n, **over):
    rows = []
    for i in range(n):
        c = {"type": ctype, "team": "NE", "game": f"G{i}@X", "player": "", "pos": "",
             "magnitude": float(n - i), "message": f"{ctype} {i}"}
        if ctype in ("prop_move", "prop_new"):
            c.update(player=f"Player {i}", gsis_id=f"00-{i}", stat="rec_yds",
                     stat_label="Rec Yds", old=1.0, new=2.0)
        c.update(over)
        rows.append(c)
    return rows


def test_a_busy_slate_cannot_starve_moneylines_or_sheet_drift():
    """spread(8) + total(8) exactly consumed a 16-game budget, so moneylines
    and the sheet-vs-market rows never rendered on the days they matter most."""
    changes = _many("spread_move", 10) + _many("total_move", 10) + _many("ml_move", 5) \
        + _many("sheet_drift", 5)
    picked = osec._select(changes, settings={"odds": {"report": {"max_games": 16, "max_props": 15}}})
    kinds = {c["type"] for c in picked}
    assert "ml_move" in kinds and "sheet_drift" in kinds


def test_a_busy_prop_slate_cannot_starve_new_lines_or_market_only():
    changes = _many("prop_move", 30) + _many("prop_new", 6) + _many("market_only", 6)
    picked = osec._select(changes, settings={"odds": {"report": {"max_games": 16, "max_props": 15}}})
    kinds = {c["type"] for c in picked}
    assert "prop_new" in kinds and "market_only" in kinds


def test_selection_returns_copies_so_the_persisted_changes_stay_clean():
    """`_cites`/`_events` are annotations for rendering, not report data."""
    wd = _week_data([{"type": "spread_move", "team": "SEA", "game": "NE@SEA", "player": "",
                      "magnitude": 1.0, "message": "x"}])
    original = wd["changes"][0]
    osec.build_odds_section(wd, [], use_llm=False)
    assert "_cites" not in original and "_events" not in original


# ---------------------------------------------------------------------------
# Staleness and flag state
# ---------------------------------------------------------------------------


def test_a_wrong_week_read_never_merges_next_weeks_slate(tmp_path, monkeypatch):
    """SB_GameLines holds only the newest pull, so a wrong-week read is next
    week's games — merging it would write matchups this week never had."""
    monkeypatch.setattr(oc, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(oc.season_mod, "load_schedule", lambda **kw: [])
    stored, _ = oc.merge_into_week(None, 2026, 1, [_game(away="NE", home="SEA")], {},
                                   _meta(), "t1")
    oc.save_week_file(stored)

    monkeypatch.setattr(oc, "read_pull_status",
                        lambda gc, season, settings=None, now=None: {
                            "pulled_at": "2026-09-17T09:52-04:00", "week_reported": 2,
                            "detail": "2026 Wk2", "status": "ok"})
    monkeypatch.setattr(oc, "read_game_lines",
                        lambda gc, settings=None: [_game(away="KC", home="DEN")])
    monkeypatch.setattr(oc, "read_prop_history", lambda gc, s, w, settings=None: [])
    res = oc.collect_odds("2026-09-16", week=1, season=2026, gc=object(), write=True)

    after = oc.load_week_file(2026, 1)
    assert list(after["games"]) == ["NE@SEA"], "next week's game leaked into wk01"
    assert after["changes"] == []
    assert res["pull"]["stale_reason"]


def test_an_empty_source_read_does_not_replay_every_flag_next_run():
    """A missing or mid-write tab returns [] rather than raising; wiping the
    flag state with it would re-emit every drift/market-only as brand new."""
    g = _game(fp_flag="FP-SPREAD")
    props = {"x|rec_yds": {"gsis_id": "x", "player": "P", "team": "NE", "opp": "SEA", "pos": "WR",
                           "stat": "rec_yds", "ours": 0.0, "flag": "MKT-ONLY", "thin": False,
                           "pulls": 1, "delta": None, "pct": None, "game": "g",
                           "opened": {"mkt_mu": 20.0}, "previous": None,
                           "current": {"mkt_mu": 20.0, "cons_line": 19.5}}}
    first, changes = oc.merge_into_week(None, 2026, 1, [g], props, _meta(), "t1")
    assert {c["type"] for c in changes} >= {"sheet_drift", "market_only"}
    assert len(first["flags_seen"]) == 2

    # a run where the history tab was missing and the game tab mid-write
    blank, none_changes = oc.merge_into_week(first, 2026, 1, [], {}, _meta(), "t2")
    assert [c for c in none_changes if c["type"] in ("sheet_drift", "market_only")] == []
    assert len(blank["flags_seen"]) == 2, "flag state was wiped by an empty read"

    # ...so the next good read stays quiet instead of replaying them
    _, again = oc.merge_into_week(blank, 2026, 1, [g], props, _meta(), "t3")
    assert [c for c in again if c["type"] in ("sheet_drift", "market_only")] == []


# ---------------------------------------------------------------------------
# News pairing details
# ---------------------------------------------------------------------------


def test_last_name_pairing_survives_punctuation(make_item):
    """"…Brown, who took first-team reps…" — a comma after the surname is the
    common case in prose and used to defeat the fallback entirely."""
    change = {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Antonio Brown",
              "pos": "WR", "stat": "rec_yds", "magnitude": 2.0, "message": "x"}
    item = make_item("Practice report", teams=["NE"], url="https://example.com/p",
                     summary="Brown, who took first-team reps, is expected to start.")
    section = osec.build_odds_section(_week_data([change]), [item], use_llm=False)
    assert len(section["numbered_sources"]) == 1


def test_items_without_a_url_get_separate_citation_numbers(make_item):
    changes = [
        {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Alpha One", "pos": "WR",
         "stat": "rec_yds", "magnitude": 2.0, "message": "a"},
        {"type": "prop_move", "team": "NE", "game": "NE@SEA", "player": "Beta Two", "pos": "WR",
         "stat": "rec_yds", "magnitude": 1.0, "message": "b"},
    ]
    a = make_item("Alpha One breaks out", teams=["NE"], url="")
    b = make_item("Beta Two returns", teams=["NE"], url="")
    section = osec.build_odds_section(_week_data(changes), [a, b], use_llm=False)
    titles = {s["title"] for s in section["numbered_sources"]}
    assert titles == {"Alpha One breaks out", "Beta Two returns"}


def test_a_stale_pull_is_not_reported_as_a_quiet_day():
    """"No movement" and "we could not read the market" are different claims."""
    wd = _week_data([])
    wd["pull"] = {"pulled_at": "2026-09-08T09:00-04:00",
                  "stale_reason": "the last odds pull priced Week 2"}
    section = osec.build_odds_section(wd, [], use_llm=False)
    assert "No market movement" not in section["summary"]
    assert "Week 2" in section["summary"]
