"""dashboard.projection_data — the Projections page's phase-aware source."""

import json

from dashboard import projection_data as pd_


def _write(d, name, payload):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(payload), encoding="utf-8")


def _weekly_tree(base):
    wk1p = base / "2026" / "wk01" / "primary" / "2026-09-08"
    wk1s = base / "2026" / "wk01" / "secondary" / "2026-09-08"
    wk2s = base / "2026" / "wk02" / "secondary" / "2026-09-15"
    wk2p = base / "2026" / "wk02" / "primary" / "2026-09-16"
    for d, tag in ((wk1p, "p1"), (wk1s, "s1"), (wk2s, "s2"), (wk2p, "p2")):
        _write(d, "players.json", {"00-1": {"player_id": "00-1", "name": "Josh Allen", "team": "BUF", "pos": "QB", "slot": 42, "metrics": {}}})
        _write(d, "kickers.json", {"00-9": {"player_id": "00-9", "name": "Tyler Bass", "team": "BUF", "pos": "K", "metrics": {}}})
        _write(d, "output.json", {"00-1": {"name": "Josh Allen", "pos": "QB", "team": "BUF", "ppr": 21.2, "pos_rank": "QB2", "tag": tag}})
        _write(d, "games.json", {"BUF": {"team": "BUF", "opp": "HST", "metrics": {"Line Src": "Vegas", "Plays": 62.1}}})
        _write(d, "meta.json", {"season": 2026, "week": int(d.parts[-3][2:]), "sheet": d.parts[-2], "date": d.name})
    (base / "changelog.csv").write_text(
        "date,season,week,sheet,kind,key,label,type,metric,old_value,new_value,details\n"
        "2026-09-08,2026,1,primary,players,00-1,Josh Allen,metric_change,TGT Adj,0,1,\n"
        "2026-09-08,2026,1,secondary,players,00-1,Josh Allen,metric_change,TGT Adj,0,2,\n"
        "2026-09-15,2026,2,secondary,output,00-1,Josh Allen,fantasy_change,PPR,20,21.2,\n"
        "2026-09-16,2026,2,primary,games,BUF,BUF,metric_change,Plays Adj,0,1,\n",
        encoding="utf-8",
    )


def test_weekly_source_index_and_mapping(tmp_path):
    _weekly_tree(tmp_path)
    src = pd_.WeeklySource(2026, base=tmp_path)
    assert src.mode == "in_season"
    assert src.dates() == ["2026-09-16", "2026-09-15", "2026-09-08"]
    # tie on 09-08 (both week 1) -> primary; 09-15 only secondary
    assert src.sheet_for_date("2026-09-08") == "primary"
    assert src.sheet_for_date("2026-09-15") == "secondary"
    assert src.week_for_date("2026-09-16") == 2
    assert src.dates_same_week("2026-09-16") == ["2026-09-15", "2026-09-16"]
    assert src.label == "Weekly sheet · Week 2 · primary"
    # kind mapping + kicker merge
    players = src.load("2026-09-08", "players")
    assert set(players) == {"00-1", "00-9"} and players["00-9"]["pos"] == "K"
    assert src.load("2026-09-08", "fantasy")["00-1"]["tag"] == "p1"
    assert src.load("2026-09-16", "teams")["BUF"]["metrics"]["Line Src"] == "Vegas"
    assert src.load("2026-09-01", "players") is None
    # changelog: kinds mapped, non-shown sheet rows dropped
    rows = src.changelog()
    assert [(r["date"], r["kind"], r["new_value"]) for r in rows] == [
        ("2026-09-08", "player", "1"), ("2026-09-15", "fantasy", "21.2"), ("2026-09-16", "team", "1"),
    ]


def test_preseason_source_and_selector(tmp_path, monkeypatch):
    pre = tmp_path / "projections"
    _write(pre / "2026-05-01", "players.json", {"00-1": {"name": "X"}})
    (pre / "changelog.csv").write_text("date,kind,key,label,type,metric,old_value,new_value,details\n2026-05-01,player,00-1,X,metric_change,TGT Adj,0,1,\n", encoding="utf-8")
    src = pd_.PreseasonSource(base=pre)
    assert src.mode == "offseason" and src.dates() == ["2026-05-01"] and src.week_for_date("2026-05-01") is None
    assert src.load("2026-05-01", "players") == {"00-1": {"name": "X"}}
    assert src.changelog()[0]["kind"] == "player"

    # selector: offseason phase -> preseason; in-season with no weekly data -> preseason fallback
    monkeypatch.setattr(pd_, "WEEKLY_DIR", tmp_path / "none")
    assert pd_.get_projection_source({"season": {"phase": "offseason", "year": 2026}}).mode == "offseason"
    assert pd_.get_projection_source({"season": {"phase": "in_season", "year": 2026}}).mode == "offseason"
    monkeypatch.setattr(pd_, "WEEKLY_DIR", tmp_path / "weekly")
    _weekly_tree(tmp_path / "weekly")
    assert pd_.get_projection_source({"season": {"phase": "in_season", "year": 2026}}).mode == "in_season"
