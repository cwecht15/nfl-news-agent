"""collectors.odds_api — direct game-lines pulls, the props dispatch, the ledger budget.

Fully offline: the Odds API and GitHub are both a FakeRequests that routes by
URL, the data dir is tmp_path, and the clock/sleep seams are patched, so no
test here can spend a credit or start a workflow run.
"""

from datetime import datetime, timedelta, timezone

import pytest

from collectors import odds_api
from collectors import odds_collector as oc

ET = odds_api.ET_ZONE


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._body


def _quota(used, remaining, last=0):
    return {"x-requests-used": str(used), "x-requests-remaining": str(remaining),
            "x-requests-last": str(last)}


class FakeRequests:
    """Routes GETs by URL suffix; records every call."""

    def __init__(self, routes=None, post=None):
        self.routes = routes or {}
        self.post_response = post or FakeResponse(204)
        self.gets: list[dict] = []
        self.posts: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": dict(params or {}), "headers": headers})
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp() if callable(resp) else resp
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        return self.post_response

    def called(self, suffix):
        return [g for g in self.gets if g["url"].endswith(suffix)]


SETTINGS = {
    "season": {"year": 2026},
    "odds": {
        "max_pull_age_hours": 30,
        "api": {
            "enabled": True, "key_env": "ODDS_API_KEY", "min_remaining_credits": 1000,
            "game_lines": {"regions": ["us", "us2", "eu"], "markets": ["h2h", "spreads", "totals"],
                           "sharp_book": "pinnacle", "fp_flag": {"spread": 1.0, "total": 1.5}},
            "props": {"repo": "o/nfl-odds", "workflow": "pull-sportsbook.yml", "ref": "main",
                      "mode": "refresh", "token_env": "NFL_ODDS_GH_TOKEN", "wait_minutes": 15,
                      "min_gap_hours": 6, "max_per_day": 2, "max_per_week": 4,
                      "min_remaining_credits": 7000},
        },
    },
}

SCHEDULE = [
    {"week": 5, "away": "TB", "home": "DAL", "date": "2026-10-08", "time": "8:15 PM"},
    {"week": 5, "away": "BUF", "home": "LA", "date": "2026-10-12", "time": "8:15 PM"},
    {"week": 6, "away": "DAL", "home": "NYG", "date": "2026-10-15", "time": "8:15 PM"},
]


def _book(key, sp_home, sp_away, over, under, ml_home, ml_away):
    return {"key": key, "title": key, "markets": [
        {"key": "h2h", "outcomes": [{"name": "Dallas Cowboys", "price": ml_home},
                                    {"name": "Tampa Bay Buccaneers", "price": ml_away}]},
        {"key": "spreads", "outcomes": [
            {"name": "Dallas Cowboys", "price": sp_home[1], "point": sp_home[0]},
            {"name": "Tampa Bay Buccaneers", "price": sp_away[1], "point": sp_away[0]}]},
        {"key": "totals", "outcomes": [
            {"name": "Over", "price": over[1], "point": over[0]},
            {"name": "Under", "price": under[1], "point": under[0]}]},
    ]}


EVENT = {
    "id": "ev1", "commence_time": "2026-10-09T00:15:00Z",
    "home_team": "Dallas Cowboys", "away_team": "Tampa Bay Buccaneers",
    "bookmakers": [
        _book("draftkings", (-8.5, -110), (8.5, -110), (48, -110), (48, -110), -450, 350),
        _book("fanduel", (-8.0, -115), (8.0, -105), (47.5, -110), (47.5, -110), -500, 380),
        _book("pinnacle", (-8.5, -105), (8.5, -105), (48, -108), (48, -112), -449, 370),
    ],
}

EVENTS = [
    {"id": "ev1", "home_team": "Dallas Cowboys", "away_team": "Tampa Bay Buccaneers",
     "commence_time": "2026-10-09T00:15:00Z"},
    {"id": "ev2", "home_team": "Los Angeles Rams", "away_team": "Buffalo Bills",
     "commence_time": "2026-10-13T00:15:00Z"},
    # Next week's TNF sneaks into a wide window; it must not be written.
    {"id": "ev9", "home_team": "New York Giants", "away_team": "Dallas Cowboys",
     "commence_time": "2026-10-16T00:15:00Z"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(odds_api.season_mod, "load_schedule", lambda **kw: list(SCHEDULE))
    monkeypatch.setattr(odds_api, "_sheet_games", lambda season, week: {})
    monkeypatch.setattr(odds_api, "_sleep", lambda s: None)
    monkeypatch.setenv("ODDS_API_KEY", "k-test")
    monkeypatch.delenv("NFL_ODDS_GH_TOKEN", raising=False)
    return tmp_path


def _install(monkeypatch, fake):
    monkeypatch.setattr(odds_api, "_requests", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Game-row construction
# ---------------------------------------------------------------------------


def test_build_game_row_consensus_best_sharp_implied_and_flag():
    sheet_game = {"metrics": {"Spread": -10.0, "O/U": 46.0}}
    g = odds_api.build_game_row(EVENT, "TB", "DAL", sheet_game=sheet_game, settings=SETTINGS)
    assert g["key"] == "TB@DAL"
    assert g["spread_home"] == -8.5 and g["total"] == 48.0
    assert g["home_ml"] == -450.0 and g["away_ml"] == 370.0
    assert g["n_books"] == 3
    assert g["best"] == {"home_spread": "-8 (-115)", "away_spread": "+8.5 (-105)",
                         "over": "47.5 (-110)", "under": "48 (-110)",
                         "home_ml": "-449", "away_ml": "380"}
    sharp = g["sharp"]
    assert sharp["book"] == "pinnacle" and sharp["spread_home"] == -8.5
    assert sharp["total"] == 48 and sharp["home_ml"] == -449
    ph, pa = odds_api.american_to_prob(-449), odds_api.american_to_prob(370)
    assert sharp["home_p"] == round(ph / (ph + pa), 3)
    assert g["implied"] == oc._market_implied(-8.5, 48.0)
    assert g["sheet"] == {"spread_home": -10.0, "ou": 46.0, "fp_spread_delta": 1.5,
                          "fp_total_delta": 2.0, "fp_flag": "FP-BOTH"}
    assert g["kickoff_et"] == "Thu 10/08 08:15 PM"
    # Same keys read_game_lines produces, so merge_into_week needs no new branch.
    assert set(g) == {"key", "away", "home", "kickoff_et", "spread_home", "total", "home_ml",
                      "away_ml", "n_books", "best", "sharp", "sheet", "implied"}


def test_fp_flag_single_halves_and_none():
    flag = lambda s, o: odds_api._sheet_block(-8.5, 48.0, s, o, {"spread": 1.0, "total": 1.5})["fp_flag"]
    assert flag(-9.5, 48.0) == "FP-SPREAD"
    assert flag(-8.5, 46.5) == "FP-TOTAL"
    assert flag(-8.0, 47.5) == ""
    assert odds_api._sheet_block(-8.5, 48.0, None, None, {})["fp_flag"] == ""


def test_sheet_line_falls_back_to_the_stored_game_and_is_re_measured():
    prev = {"sheet": {"spread_home": -7.0, "ou": 48.0, "fp_flag": ""}}
    g = odds_api.build_game_row(EVENT, "TB", "DAL", sheet_game=None, prev_game=prev,
                                settings=SETTINGS)
    assert g["sheet"]["spread_home"] == -7.0
    assert g["sheet"]["fp_spread_delta"] == -1.5
    assert g["sheet"]["fp_flag"] == "FP-SPREAD"


def test_resolve_team_exact_fallback_and_unknown():
    lookup = odds_api.team_lookup()
    assert odds_api.resolve_team("Washington Commanders", lookup) == "WAS"
    assert odds_api.resolve_team("Commanders", lookup) == "WAS"          # nickname fallback
    assert odds_api.resolve_team("LA Rams", lookup) == "LAR"
    assert odds_api.resolve_team("Toronto Argonauts", lookup) == ""


def test_events_filtered_to_the_schedule_week():
    rows = odds_api.week_schedule(2026, 5, schedule=SCHEDULE)
    assert {(r["away"], r["home"]) for r in rows} == {("TB", "DAL"), ("BUF", "LAR")}
    kept = odds_api.filter_events(EVENTS, rows)
    assert [(e["away"], e["home"]) for e in kept] == [("TB", "DAL"), ("BUF", "LAR")]
    start, end = odds_api.events_window(rows)
    assert start == "2026-10-07T12:15:00Z"          # TNF 00:15Z Fri minus 36h
    assert end == "2026-10-13T06:15:00Z"            # MNF plus 6h


def test_quota_headers_parsed():
    q = odds_api.parse_quota(_quota(4612, 15388, 9))
    assert q == {"last": 9, "used": 4612, "remaining": 15388}
    assert odds_api.parse_quota({}) == {"last": None, "used": None, "remaining": None}


# ---------------------------------------------------------------------------
# pull_game_lines
# ---------------------------------------------------------------------------


def _lines_fake(remaining=15397):
    return FakeRequests(routes={
        "/sports": FakeResponse(200, [], _quota(4603, remaining)),
        "/events": FakeResponse(200, EVENTS, _quota(4603, remaining)),
        "/odds": FakeResponse(200, [EVENT], _quota(4612, remaining - 9, 9)),
    })


def test_missing_key_is_non_fatal_and_makes_no_call(env, monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY")
    fake = _install(monkeypatch, _lines_fake())
    res = odds_api.pull_game_lines(season=2026, week=5, settings=SETTINGS)
    assert res["ok"] is False and "no ODDS_API_KEY" in res["reason"]
    assert fake.gets == []
    assert odds_api.fetch_quota(SETTINGS) == {"error": "no ODDS_API_KEY"}


def test_refuses_below_the_credit_floor(env, monkeypatch):
    fake = _install(monkeypatch, _lines_fake(remaining=900))
    res = odds_api.pull_game_lines(season=2026, week=5, settings=SETTINGS)
    assert res["ok"] is False and "900" in res["reason"]
    assert not fake.called("/odds") and not fake.called("/events")


def test_dry_run_spends_nothing(env, monkeypatch):
    fake = _install(monkeypatch, _lines_fake())
    res = odds_api.pull_game_lines(season=2026, week=5, settings=SETTINGS, dry_run=True,
                                   now=datetime(2026, 10, 8, 14, tzinfo=timezone.utc))
    assert res["ok"] is True
    assert "would spend 9 credits" in res["reason"]
    assert len(res["events"]) == 2
    assert not fake.called("/odds")
    assert not (env / "api_usage.json").exists()
    ev = fake.called("/events")[0]["params"]
    assert ev["commenceTimeFrom"] == "2026-10-07T12:15:00Z"
    assert "apiKey" in ev


def test_pull_writes_api_lines_keeps_props_pull_and_logs(env, monkeypatch):
    # A stored week file from the sheet read, with a stale 38h props pull.
    prev = {"season": 2026, "week": 5, "games": {}, "props": {"00-1|rec_yds": {"stat": "rec_yds"}},
            "pull": {"pulled_at": "2026-10-06T09:04-04:00", "props_pull_id": "p1",
                     "week_reported": 5, "stale_reason": "last odds pull was 38h ago",
                     "age_hours": 38.0},
            "pull_log": [], "changes": []}
    oc.save_week_file(prev)
    fake = _install(monkeypatch, _lines_fake())
    now = datetime(2026, 10, 8, 14, 12, tzinfo=timezone.utc)
    res = odds_api.pull_game_lines(season=2026, week=5, settings=SETTINGS, run="refresh",
                                   requested_by="dashboard", now=now)
    assert res["ok"] is True, res["reason"]
    assert res["credits_used"] == 9 and res["credits_remaining"] == 15388
    odds_call = fake.called("/odds")[0]["params"]
    assert odds_call["regions"] == "us,us2,eu"
    assert odds_call["markets"] == "h2h,spreads,totals"
    assert odds_call["eventIds"] == "ev1,ev2"

    data = oc.load_week_file(2026, 5)
    pull = data["pull"]
    assert pull["games_source"] == "api" and pull["games_stale_reason"] == ""
    assert pull["games_at"].startswith("2026-10-08T10:12")
    # The project's pull is untouched: props are still 38h old.
    assert pull["pulled_at"] == "2026-10-06T09:04-04:00"
    assert pull["stale_reason"] == "last odds pull was 38h ago"
    assert "00-1|rec_yds" in data["props"]
    assert data["games"]["TB@DAL"]["current"]["at"] == pull["games_at"]
    assert data["pull_log"][-1]["source"] == "api"
    assert data["pull_log"][-1]["credits_used"] == 9

    ledger = odds_api.load_ledger()
    e = ledger["pulls"][-1]
    assert (e["kind"], e["run"], e["requested_by"], e["credits_used"], e["ok"]) == \
        ("game_lines", "refresh", "dashboard", 9, True)
    assert ledger["quota"]["remaining"] == 15388


def test_ledger_append_caps_entries(env, monkeypatch):
    monkeypatch.setattr(odds_api, "LEDGER_KEEP", 3)
    for i in range(5):
        odds_api.append_ledger({"at": f"2026-10-0{i + 1}T12:00:00+00:00", "kind": "game_lines",
                                "ok": True, "credits_used": 9},
                               quota={"used": 100 + i, "remaining": 900 - i})
    led = odds_api.load_ledger()
    assert len(led["pulls"]) == 3
    assert led["pulls"][0]["at"].startswith("2026-10-03")
    assert led["quota"]["remaining"] == 896


# ---------------------------------------------------------------------------
# props_budget
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)      # Thu 2:00 PM ET


def _props(at, ok=True, week=5, credits=550):
    return {"at": at.isoformat(), "kind": "props", "ok": ok, "season": 2026, "week": week,
            "credits_used": credits}


def _budget(pulls, project=None, remaining=15000):
    return odds_api.props_budget({"pulls": pulls, "quota": {"remaining": remaining}}, project,
                                 SETTINGS, now=NOW, season_week=(2026, 5))


def test_budget_allows_a_clean_slate():
    b = _budget([])
    assert b["allowed"] and b["today"] == 0 and b["this_week"] == 0
    assert "0 of 2 today" in b["reason"]


def test_budget_gap_against_our_last_pull():
    b = _budget([_props(NOW - timedelta(hours=2))])
    assert not b["allowed"]
    assert b["next_at"] == (NOW + timedelta(hours=4)).isoformat()
    assert "next allowed" in b["reason"]


def test_budget_gap_against_the_projects_own_pull():
    b = _budget([], project=(NOW - timedelta(hours=1)).astimezone(ET).isoformat(timespec="minutes"))
    assert not b["allowed"] and "NFL Odds project" in b["reason"]
    assert b["next_at"] == (NOW + timedelta(hours=5)).isoformat()


def test_budget_per_day():
    b = _budget([_props(NOW - timedelta(hours=7)), _props(NOW - timedelta(hours=13, minutes=30))])
    assert not b["allowed"] and b["today"] == 2
    assert b["reason"].startswith("2 of 2 today used")
    assert b["next_at"].startswith("2026-10-09T04:00")      # midnight ET


def test_budget_per_week_and_failed_free_runs_dont_count():
    old = NOW - timedelta(days=1, hours=8)
    pulls = [_props(old - timedelta(hours=h)) for h in (0, 7, 14, 21)]
    pulls.append(_props(NOW - timedelta(hours=1), ok=False, credits=0))   # failed, nothing spent
    b = _budget(pulls)
    assert not b["allowed"] and b["this_week"] == 4
    assert "resets next NFL week" in b["reason"]
    # A different week's pulls do not count toward this one.
    assert _budget([_props(old, week=4)])["allowed"]


def test_budget_keeps_the_credit_reserve():
    b = _budget([], remaining=7400)
    assert not b["allowed"] and "reserve" in b["reason"]


def test_lines_budget():
    assert odds_api.lines_budget({"remaining": 5000}, SETTINGS)[0]
    ok, why = odds_api.lines_budget({"remaining": 999}, SETTINGS)
    assert not ok and "999" in why
    assert odds_api.lines_budget({}, SETTINGS)[0]


# ---------------------------------------------------------------------------
# pull_props
# ---------------------------------------------------------------------------


def _props_fake(runs_body, used_after=5160):
    quotas = iter([_quota(4610, 15390), _quota(used_after, 20000 - used_after)])
    return FakeRequests(
        routes={
            "/sports": lambda: FakeResponse(200, [], next(quotas)),
            "/runs": lambda: FakeResponse(200, runs_body()),
        },
        post=FakeResponse(204),
    )


def test_props_dispatch_waits_and_records_credits(env, monkeypatch):
    monkeypatch.setenv("NFL_ODDS_GH_TOKEN", "gh-test")
    t0 = datetime.now(timezone.utc)
    runs = {"workflow_runs": [
        {"created_at": (t0 - timedelta(hours=3)).isoformat(), "status": "completed",
         "conclusion": "success", "html_url": "old"},
        {"created_at": (t0 + timedelta(seconds=5)).isoformat(), "status": "completed",
         "conclusion": "success", "html_url": "https://github.com/o/nfl-odds/actions/runs/1"},
    ]}
    fake = _install(monkeypatch, _props_fake(lambda: runs))
    res = odds_api.pull_props(2026, 5, SETTINGS, run="refresh", requested_by="refresh")
    assert res["ok"] is True, res["reason"]
    post = fake.posts[0]
    assert post["url"].endswith("/repos/o/nfl-odds/actions/workflows/pull-sportsbook.yml/dispatches")
    assert post["json"] == {"ref": "main", "inputs": {"mode": "refresh"}}
    assert post["headers"]["Authorization"] == "Bearer gh-test"
    assert res["run_url"].endswith("/runs/1")
    assert res["credits_used"] == 550
    e = odds_api.load_ledger()["pulls"][-1]
    assert (e["kind"], e["mode"], e["ok"], e["credits_used"]) == ("props", "refresh", True, 550)


def test_props_timeout_is_recorded_not_raised(env, monkeypatch):
    monkeypatch.setenv("NFL_ODDS_GH_TOKEN", "gh-test")
    t0 = datetime.now(timezone.utc)
    runs = {"workflow_runs": [{"created_at": (t0 + timedelta(seconds=2)).isoformat(),
                               "status": "in_progress", "conclusion": None, "html_url": "u"}]}
    ticks = iter(range(0, 10_000, 600))
    monkeypatch.setattr(odds_api, "_clock", lambda: float(next(ticks)))
    _install(monkeypatch, _props_fake(lambda: runs, used_after=4610))
    res = odds_api.pull_props(2026, 5, SETTINGS)
    assert res["ok"] is False and "timed out" in res["reason"]
    e = odds_api.load_ledger()["pulls"][-1]
    assert e["ok"] is False and e["note"] == "timed out waiting for the run"


def test_props_dry_run_and_budget_refusal_dispatch_nothing(env, monkeypatch):
    monkeypatch.setenv("NFL_ODDS_GH_TOKEN", "gh-test")
    fake = _install(monkeypatch, _props_fake(lambda: {"workflow_runs": []}))
    res = odds_api.pull_props(2026, 5, SETTINGS, dry_run=True)
    assert res["ok"] is True and "dry run" in res["reason"]
    assert fake.posts == []

    odds_api.append_ledger(_props(datetime.now(timezone.utc) - timedelta(hours=1)))
    fake = _install(monkeypatch, _props_fake(lambda: {"workflow_runs": []}))
    res = odds_api.pull_props(2026, 5, SETTINGS)
    assert res["ok"] is False and res["reason"].startswith("budget:")
    assert fake.posts == []


def test_props_missing_token_is_non_fatal(env, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")          # no gh fallback on CI
    fake = _install(monkeypatch, _props_fake(lambda: {"workflow_runs": []}))
    res = odds_api.pull_props(2026, 5, SETTINGS)
    assert res["ok"] is False and "NFL_ODDS_GH_TOKEN" in res["reason"]
    assert fake.posts == []


def test_find_props_run_ignores_runs_before_the_dispatch():
    t0 = datetime(2026, 10, 8, 18, tzinfo=timezone.utc)
    runs = [{"created_at": "2026-10-08T17:59:40Z", "id": 1},     # within 30s skew
            {"created_at": "2026-10-08T17:50:00Z", "id": 0}]
    assert odds_api._find_props_run(runs, t0)["id"] == 1
    assert odds_api._find_props_run(runs[1:], t0) is None
