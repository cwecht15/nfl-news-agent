"""scripts.run_afternoon — in-place report update and offseason no-op."""

import json

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
