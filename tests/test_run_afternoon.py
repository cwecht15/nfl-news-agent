"""scripts.run_afternoon — in-place report update and offseason no-op."""

import json

import pytest

from models import DailyReport
from processing.season import SeasonContext
from reports import report_builder
from scripts import run_afternoon


def _ctx():
    return SeasonContext(phase="in_season", season=2026, week=1, active_sheet="primary",
                         sheet_weeks={"primary": 1}, today="2026-09-08", weekday="Tue", read_secondary=True)


def _morning_report(reports_dir):
    report = report_builder.build_report(
        date_str="2026-09-08",
        sections={"transactions": {"summary": "AM transactions", "count": 2},
                  "league_wide": {"summary": "AM league"}},
        team_highlights={},
        news_items=[],
        depth_chart_changes=[],
        projection_movers=[],
        roster_events=[{"event_id": "e1", "event_type": "ir_placed", "name": "A B", "team": "BUF", "pos": "RB",
                        "confidence": "official", "source": "nflcom_transactions", "detail": "Reserve/Injured"}],
        injury_changes=[],
        audit_alerts=[],
        season_meta=_ctx().to_dict(),
    )
    report_builder.save_report(report)
    return report


def test_update_report_in_place_merges_and_stamps(tmp_path, monkeypatch):
    monkeypatch.setattr(report_builder, "get_data_dir", lambda sub: tmp_path)
    _morning_report(tmp_path)

    new_events = [
        {"event_id": "e1", "event_type": "ir_placed", "name": "A B", "team": "BUF", "pos": "RB",
         "confidence": "official", "source": "nflcom_transactions", "detail": "Reserve/Injured"},   # duplicate
        {"event_id": "e2", "event_type": "ps_elevated", "name": "C D", "team": "KC", "pos": "WR",
         "confidence": "reported", "source": "news:Twitter", "detail": "elevated"},
    ]
    changes = [{"team": "NE", "name": "Ben Brown", "pos": "C", "injury": "Knee", "type": "designation_set",
                "old": "", "new": "OUT", "date": "2026-09-08", "source": "team_site", "message": "Ben Brown (C) ruled OUT"}]
    alerts = [{"type": "status_conflict", "key": "status_conflict|00-1|1", "severity": "error", "player": "A B",
               "team": "BUF", "message": "A B projected but on IR"}]

    import logging
    run_afternoon._update_report("2026-09-08", _ctx(), new_events, changes, alerts, logging.getLogger("t"))

    data = json.loads((tmp_path / "2026-09-08.json").read_text(encoding="utf-8"))
    assert data["pm_updated_at"]
    assert [e["event_id"] for e in data["roster_events"]] == ["e1", "e2"]
    assert data["injury_changes"][0]["name"] == "Ben Brown"
    assert data["audit_alerts"][0]["key"] == "status_conflict|00-1|1"
    # Morning sections preserved, in-season sections refreshed, order canonical
    keys = list(data["sections"])
    assert keys.index("transactions") < keys.index("roster_moves") < keys.index("injury_report_changes")
    assert keys.index("projection_audit") < keys.index("league_wide")
    assert data["sections"]["transactions"]["summary"] == "AM transactions"
    assert "C D" in data["sections"]["roster_moves"]["summary"]
    assert "reported, unconfirmed" in data["sections"]["roster_moves"]["summary"]
    assert "**Ben Brown (C) ruled OUT**" in data["sections"]["injury_report_changes"]["summary"]
    assert "Fix before publishing" in data["sections"]["projection_audit"]["summary"]
    # HTML re-rendered with the week header + evening stamp
    html = (tmp_path / "2026-09-08.html").read_text(encoding="utf-8")
    assert "Week 1" in html and "evening update" in html
    # Old reports still load
    assert DailyReport.from_json(str(tmp_path / "2026-09-08.json")).season_meta["week"] == 1


def test_update_report_writes_skeleton_when_morning_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(report_builder, "get_data_dir", lambda sub: tmp_path)
    import logging
    run_afternoon._update_report("2026-09-09", _ctx(), [], [], [], logging.getLogger("t"))
    data = json.loads((tmp_path / "2026-09-09.json").read_text(encoding="utf-8"))
    assert set(data["sections"]) >= {"roster_moves", "injury_report_changes", "projection_audit"}
    assert data["pm_updated_at"]


def test_run_pm_is_noop_in_offseason(monkeypatch):
    monkeypatch.setattr(run_afternoon, "get_season_context",
                        lambda today=None: SeasonContext("offseason", 2026, None, None, {}, "2026-09-08", "Tue", False))
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    assert run_afternoon.run_pm("2026-09-08") == 0


def test_update_report_skips_skeleton_for_inactives_only(tmp_path, monkeypatch):
    """A game-day poll is an update, not a producer.

    On 2026-09-10 the delayed Wednesday inactives cron authored a whole phantom
    report for a day that had not happened. It must decline instead.
    """
    monkeypatch.setattr(report_builder, "get_data_dir", lambda sub: tmp_path)
    import logging
    out = run_afternoon._update_report(
        "2026-09-09", _ctx(), None, None, [], logging.getLogger("t"),
        inactives={}, create_missing=False,
    )
    assert out is None
    assert not (tmp_path / "2026-09-09.json").exists()
    assert not (tmp_path / "2026-09-09.html").exists()


def test_update_report_skeleton_omits_sections_for_none_sentinels(tmp_path, monkeypatch):
    """None means "this run didn't look", not "we looked and found nothing"."""
    monkeypatch.setattr(report_builder, "get_data_dir", lambda sub: tmp_path)
    import logging
    alerts = [{"type": "status_conflict", "key": "k1", "severity": "error",
               "player": "A B", "team": "BUF", "message": "A B projected but on IR"}]
    run_afternoon._update_report(
        "2026-09-09", _ctx(), None, None, alerts, logging.getLogger("t"), inactives={},
    )
    data = json.loads((tmp_path / "2026-09-09.json").read_text(encoding="utf-8"))
    assert "roster_moves" not in data["sections"]
    assert "injury_report_changes" not in data["sections"]
    assert "projection_audit" in data["sections"]
    assert "game_day_inactives" in data["sections"]
    # The whole point: never assert emptiness we did not verify.
    blob = json.dumps(data)
    assert "No roster moves recorded today" not in blob
    assert "No injury report changes today" not in blob


def test_run_pm_defaults_to_eastern_date(monkeypatch):
    """run_pm's default date comes from today_et, not the runner's clock."""
    seen = {}

    def fake_ctx(today=None):
        seen["today"] = today
        return SeasonContext("offseason", 2026, None, None, {}, today, "Wed", False)

    monkeypatch.setattr(run_afternoon, "today_et", lambda now=None: "2026-09-09")
    monkeypatch.setattr(run_afternoon, "get_season_context", fake_ctx)
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    assert run_afternoon.run_pm() == 0
    assert seen["today"] == "2026-09-09"


def test_injuries_only_runs_just_the_injury_report_and_audit(monkeypatch):
    """The Friday designation refresh: injury sources + audit + report, nothing
    else (no Sheets re-read, no OurLads, no nflverse), and never a skeleton."""
    calls = {}

    def fake_steps(**kw):
        calls["steps"] = kw
        return None, [{"type": "designation_set"}], [{"type": "out_but_projected"}], None

    def fake_update(date_str, ctx, roster_events, injury_changes, audit_alerts, logger, **kw):
        calls["update"] = (roster_events, injury_changes, audit_alerts, kw)

    monkeypatch.setattr(run_afternoon, "get_season_context", lambda today=None: _ctx())
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    monkeypatch.setattr(run_afternoon, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(run_afternoon, "clear_status", lambda: None)
    monkeypatch.setattr(run_afternoon, "run_in_season_steps", fake_steps)
    monkeypatch.setattr(run_afternoon, "_update_report", fake_update)
    for heavy in ("_refresh_active_sheet", "_rescrape_depth_charts", "_collect_pm_transactions", "run_odds_step"):
        monkeypatch.setattr(run_afternoon, heavy, lambda *a, **k: (_ for _ in ()).throw(AssertionError(heavy)))

    assert run_afternoon.run_pm("2026-09-18", injuries_only=True) == 0
    assert calls["steps"]["skip"] == {"elevations", "roster", "inactives"}
    assert calls["steps"]["run"] == "injuries"
    roster_events, injury_changes, audit_alerts, kw = calls["update"]
    assert roster_events is None                      # "did not look" - roster section untouched
    assert injury_changes and audit_alerts
    assert kw.get("create_missing") is False


# ---------------------------------------------------------------------------
# --only: on-demand refresh targets (the dashboard's Refresh buttons)
# ---------------------------------------------------------------------------


def test_parse_targets_accepts_csv_whitespace_list_and_all():
    assert run_afternoon.parse_targets("roster") == {"roster"}
    assert run_afternoon.parse_targets(" roster , elevations ") == {"roster", "elevations"}
    assert run_afternoon.parse_targets(["injuries"]) == {"injuries"}
    assert run_afternoon.parse_targets("all") == set(run_afternoon.REFRESH_TARGETS)
    assert run_afternoon.parse_targets(None) == set()
    assert run_afternoon.parse_targets("") == set()


def test_parse_targets_rejects_an_unknown_token_loudly():
    with pytest.raises(ValueError) as e:
        run_afternoon.parse_targets("roster,rosters")
    msg = str(e.value)
    assert "rosters" in msg
    for t in run_afternoon.REFRESH_TARGETS:
        assert t in msg


@pytest.mark.parametrize("targets,skip,tx,odds", [
    ({"roster"},       {"elevations", "injuries", "inactives"}, False, False),
    ({"elevations"},   {"roster", "injuries", "inactives"},     False, False),
    ({"injuries"},     {"elevations", "roster", "inactives"},   False, False),
    # inactives implies elevations (the Saturday 4 PM ET deadline lands in this window)
    ({"inactives"},    {"roster", "injuries"},                  False, True),
    # transactions implies roster (the scrape is only useful as roster events)
    ({"transactions"}, {"elevations", "injuries", "inactives"}, True,  False),
    ({"roster", "injuries"}, {"elevations", "inactives"},       False, False),
])
def test_plan_for_targets_skip_sets(targets, skip, tx, odds):
    plan = run_afternoon.plan_for_targets(targets)
    assert plan["skip"] == skip
    assert plan["collect_transactions"] is tx
    assert plan["odds"] is odds
    assert plan["run"] == "refresh"
    assert "audit" not in plan["skip"]          # every refresh re-runs the audit


def test_plan_for_all_targets_skips_nothing():
    plan = run_afternoon.plan_for_targets(set(run_afternoon.REFRESH_TARGETS))
    assert plan["skip"] == set()
    assert plan["collect_transactions"] and plan["odds"]


def test_only_modes_match_the_existing_only_flags():
    """The regression net for "don't break --injuries-only / --inactives-only".

    These literals are the ones hard-coded in run_pm's two older branches and
    pinned by six crons and three .bat dispatchers.
    """
    assert run_afternoon.plan_for_targets({"injuries"})["skip"] == {"elevations", "roster", "inactives"}
    assert run_afternoon.plan_for_targets({"inactives"})["skip"] == {"roster", "injuries"}


def _only_harness(monkeypatch, calls, heavy=("_refresh_active_sheet", "_rescrape_depth_charts")):
    def fake_steps(**kw):
        calls["steps"] = kw
        return [{"event_id": "e1"}], None, [{"type": "missing_active"}], None

    def fake_update(date_str, ctx, roster_events, injury_changes, audit_alerts, logger, **kw):
        calls["update"] = (roster_events, injury_changes, audit_alerts, kw)

    monkeypatch.setattr(run_afternoon, "get_season_context", lambda today=None: _ctx())
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    monkeypatch.setattr(run_afternoon, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(run_afternoon, "clear_status", lambda: None)
    monkeypatch.setattr(run_afternoon, "run_in_season_steps", fake_steps)
    monkeypatch.setattr(run_afternoon, "_update_report", fake_update)
    for name in heavy:
        monkeypatch.setattr(run_afternoon, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))


def test_only_roster_runs_just_the_roster_step(monkeypatch):
    """No Sheets re-read, no OurLads, no transactions scrape, no odds."""
    calls = {}
    _only_harness(monkeypatch, calls,
                  heavy=("_refresh_active_sheet", "_rescrape_depth_charts",
                         "_collect_pm_transactions", "run_odds_step"))
    assert run_afternoon.run_pm("2026-09-22", only="roster") == 0
    assert calls["steps"]["skip"] == {"elevations", "injuries", "inactives"}
    assert calls["steps"]["run"] == "refresh"
    assert calls["steps"]["news_items"] == []
    _roster, _inj, _alerts, kw = calls["update"]
    assert kw.get("create_missing") is False
    assert kw.get("odds") is None


def test_only_transactions_feeds_the_scrape_into_the_roster_step(monkeypatch):
    calls = {}
    sentinel = [object()]
    _only_harness(monkeypatch, calls,
                  heavy=("_refresh_active_sheet", "_rescrape_depth_charts", "run_odds_step"))
    monkeypatch.setattr(run_afternoon, "_collect_pm_transactions", lambda *a, **k: sentinel)
    assert run_afternoon.run_pm("2026-09-22", only="transactions") == 0
    assert calls["steps"]["news_items"] is sentinel
    assert "roster" not in calls["steps"]["skip"]


def test_only_inactives_reads_the_odds_sheet(monkeypatch):
    calls = {}
    _only_harness(monkeypatch, calls,
                  heavy=("_refresh_active_sheet", "_rescrape_depth_charts", "_collect_pm_transactions"))
    monkeypatch.setattr(run_afternoon, "run_odds_step", lambda *a, **k: {"season": 2026})
    monkeypatch.setattr(run_afternoon, "_odds_section", lambda *a, **k: {"summary": "x"})
    assert run_afternoon.run_pm("2026-09-22", only="inactives") == 0
    assert calls["update"][3].get("odds") == {"season": 2026}


def test_only_is_a_noop_in_the_offseason(monkeypatch):
    monkeypatch.setattr(run_afternoon, "get_season_context",
                        lambda today=None: SeasonContext("offseason", 2026, None, None, {}, "2026-06-01", "Mon", False))
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    assert run_afternoon.run_pm("2026-06-01", only="all") == 0


def test_only_rejects_a_bad_target_before_collecting_anything(monkeypatch):
    monkeypatch.setattr(run_afternoon, "get_season_context", lambda today=None: _ctx())
    monkeypatch.setattr(run_afternoon, "setup_logging", lambda d: None)
    monkeypatch.setattr(run_afternoon, "write_status", lambda *a, **k: None)
    monkeypatch.setattr(run_afternoon, "clear_status", lambda: None)
    for name in ("_collect_pm_transactions", "run_in_season_steps", "run_odds_step"):
        monkeypatch.setattr(run_afternoon, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    with pytest.raises(ValueError):
        run_afternoon.run_pm("2026-09-22", only="nonsense")
