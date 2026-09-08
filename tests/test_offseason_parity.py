"""In-season work must leave the offseason path byte-identical.

These tests pin the behaviours that the in-season build touched only
additively: NewsItem serialization, the depth-chart diff, the report
section set, and the team-abbreviation dialect helpers.
"""

from datetime import datetime, timezone

from collectors.depth_chart_collector import diff_depth_charts, split_reserve_changes
from models import DailyReport, NewsItem
from processing import team_abbr
from reports.report_builder import SECTION_ORDER, build_report


def test_newsitem_extra_is_omitted_when_empty_and_tolerated_when_missing():
    item = NewsItem("t", "u", "s", "web", datetime(2026, 9, 8, tzinfo=timezone.utc))
    d = item.to_dict()
    assert "extra" not in d
    assert NewsItem.from_dict(d).extra == {}
    # old raw JSON (no extra key) still round-trips
    legacy = {k: v for k, v in d.items()}
    assert NewsItem.from_dict(legacy).title == "t"
    # populated extra survives
    item2 = NewsItem("t", "u", "s", "web", datetime(2026, 9, 8, tzinfo=timezone.utc), extra={"tx_type": "Traded"})
    assert NewsItem.from_dict(item2.to_dict()).extra == {"tx_type": "Traded"}


def _dc(name, team, pos, depth, generic=None):
    return {"name": name, "pos": pos, "generic_pos": generic or pos, "depth": depth, "team": team}


def test_diff_depth_charts_unchanged_and_split_is_opt_in():
    prev = {
        "a b": _dc("A B", "BUF", "LWR", 2, "WR"),
        "c d": _dc("C D", "BUF", "IR", 2),
        "e f": _dc("E F", "BUF", "IR", 1),
        "g h": _dc("G H", "BUF", "RB", 1),
    }
    cur = {
        "a b": _dc("A B", "BUF", "LWR", 1, "WR"),   # real promotion
        "c d": _dc("C D", "BUF", "IR", 1),          # within-IR promotion (noise)
        "e f": _dc("E F", "BUF", "IR", 2),          # within-IR demotion (noise)
        "g h": _dc("G H", "BUF", "IR", 3),          # Active -> IR
        "i j": _dc("I J", "BUF", "PUP", 1),         # added straight onto PUP
    }
    raw = diff_depth_charts(cur, prev)
    types = sorted(c["type"] for c in raw)
    # Offseason semantics: reserve buckets are ordinary positions
    assert types == ["added", "demoted", "position_change", "promoted", "promoted"]

    depth, status = split_reserve_changes(raw)
    assert [c["name"] for c in depth if c["type"] == "promoted"] == ["A B"]
    assert not [c for c in depth if c["type"] in ("demoted",)]
    assert {(c["name"], c["old_status"], c["new_status"]) for c in status} == {
        ("G H", "Active", "IR"),
        ("I J", None, "PUP"),
    }


def test_build_report_without_in_season_kwargs_keeps_section_set():
    sections = {
        "transactions": {"summary": "x", "count": 1},
        "injuries": {"summary": "y", "count": 1},
        "league_wide": {"summary": "z"},
    }
    report = build_report(
        date_str="2026-09-07",
        sections=sections,
        team_highlights={},
        news_items=[],
        depth_chart_changes=[],
        projection_movers=[],
    )
    assert list(report.sections) == [
        "transactions", "injuries", "depth_chart_movement", "projection_movers", "league_wide",
    ]
    assert all(k in SECTION_ORDER for k in report.sections)
    payload = report.__dict__
    # No in-season leakage into an offseason report
    assert payload.get("roster_events", []) == []
    assert payload.get("injury_changes", []) == []
    assert payload.get("audit_alerts", []) == []
    assert payload.get("season_meta", {}) == {}


def test_daily_report_from_json_backfills_new_fields(tmp_path):
    legacy = {
        "date": "2026-05-01", "generated_at": "x", "sections": {}, "team_highlights": {},
        "collection_stats": {}, "llm_usage": {}, "alerts": [], "depth_chart_changes": [],
        "projection_movers": [], "yt_section": {},
    }
    p = tmp_path / "r.json"
    p.write_text(__import__("json").dumps(legacy), encoding="utf-8")
    r = DailyReport.from_json(str(p))
    assert r.date == "2026-05-01"
    for fld in ("roster_events", "injury_changes", "audit_alerts", "season_meta"):
        if hasattr(r, fld):
            assert getattr(r, fld) in ([], {})


def test_team_abbr_dialects():
    assert team_abbr.to_news("ARZ", "ourlads") == "ARI"
    assert team_abbr.to_news("LA", "nflverse") == "LAR"
    assert team_abbr.to_news("AZ", "nflcom") == "ARI"
    assert team_abbr.to_news("HST", "proj") == "HOU"
    assert team_abbr.to_proj("LAR") == "LA"
    assert team_abbr.to_proj("ARZ", "ourlads") == "ARZ"
    assert team_abbr.to_proj("KC") == "KC"
    assert team_abbr.from_news("LAR", "nflverse") == "LA"
    assert team_abbr.nickname_to_news("Patriots") == "NE"
    assert team_abbr.nickname_to_news("Los Angeles Rams") == "LAR"
    assert team_abbr.nickname_to_news("49ers") == "SF"
    assert team_abbr.nickname_to_news("") == ""
    assert team_abbr.same_team("LA", "LAR", "nflverse", "news")
