"""processing.roster_events + collectors.nflverse_roster_collector — offline.

Covers the NFL.com description map, the news/tweet classifier (positives
and the speculation/negation negatives), player-name extraction, nflverse
transition normalization (incl. the DEV->ACT->DEV elevation relabel),
OurLads status records, ledger dedupe/confirmation and the state builder
(IR return weeks over a schedule with a bye, elevation counters, the
reported-events gate). Ledger/state I/O is redirected to ``tmp_path`` via
``roster_events._base_dir``.
"""

from __future__ import annotations

import json

import pytest

from collectors import nflverse_roster_collector as nv
from processing import roster_events as re_


SETTINGS = {
    "season": {"year": 2026, "phase": "in_season"},
    "roster": {
        "ir_min_games": 4,
        "max_elevations": 3,
        "confirm_window_days": 3,
        "apply_reported_events": True,
    },
}


def _sched():
    """Six BUF games with a Week 3 bye (mirrors tests/test_season.py)."""
    games = [
        ("BUF", "HST", 1, "2026-09-13"),
        ("MIA", "BUF", 2, "2026-09-20"),
        # week 3 bye for BUF
        ("BUF", "NYJ", 4, "2026-10-04"),
        ("NE", "BUF", 5, "2026-10-11"),
        ("BUF", "KC", 6, "2026-10-18"),
        ("DAL", "BUF", 7, "2026-10-25"),
        ("SEA", "NE", 1, "2026-09-09"),
        ("KC", "DEN", 3, "2026-09-27"),
    ]
    return [
        {"game_num": i, "week": wk, "away": a, "home": h, "date": d,
         "day": "", "time": "", "venue": "", "season": 2026}
        for i, (a, h, wk, d) in enumerate(games, 1)
    ]


def _player(gsis, name, team, pos, status, abbr):
    return {
        "gsis_id": gsis, "esb_id": "", "name": name, "name_key": nv.name_key(name),
        "team": team, "pos": pos, "depth_chart_position": pos,
        "status": status, "status_abbr": abbr, "label": nv.status_label(status, abbr), "week": 1,
    }


def _roster():
    return {
        "00-1": _player("00-1", "Ray Davis", "BUF", "RB", "ACT", "A01"),
        "00-2": _player("00-2", "Christian Barmore", "NE", "DT", "RES", "R01"),
        "00-3": _player("00-3", "Carson Steele", "KC", "RB", "DEV", "P01"),
        "00-4": _player("00-4", "Taylor Rapp", "DEN", "S", "CUT", "W03"),
    }


@pytest.fixture
def tmp_roster(tmp_path, monkeypatch):
    monkeypatch.setattr(re_, "_base_dir", lambda: tmp_path)
    monkeypatch.setattr(re_, "_SCHEDULE_CACHE", _sched())
    return tmp_path


# ---------------------------------------------------------------------------
# NFL.com descriptions
# ---------------------------------------------------------------------------


def test_nflcom_description_map_all_classify():
    for desc, expected in re_.NFLCOM_DESCRIPTION_MAP.items():
        assert expected in re_.EVENT_TYPES
        assert re_.classify_nflcom(desc) == expected
        assert re_.classify_nflcom(desc.lower()) == expected  # case-insensitive


def test_nflcom_unknown_and_variants():
    assert re_.classify_nflcom("Some Brand New Wording") == "status_change"
    assert re_.classify_nflcom("") == "status_change"
    assert re_.classify_nflcom("Reserve/Injured; Designated for Return") == "ir_designated_return"
    assert re_.classify_nflcom("Reserve/Suspended By Commissioner-Indefinite") == "suspended"
    assert re_.classify_nflcom("Terminated, Vested Veteran, all contracts; Not Against 90") == "released"
    assert re_.classify_nflcom("Waived, Injured") == "waived"
    assert re_.classify_nflcom("Practice Squad; International") == "ps_signed"


# ---------------------------------------------------------------------------
# News classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,expected,name", [
    ("Bills placed RB Ray Davis on injured reserve, per source.", "ir_placed", "Ray Davis"),
    ("Ray Davis placed on IR", "ir_placed", "Ray Davis"),
    ("The Chargers are placing WR Mike Williams on reserve/injured.", "ir_placed", "Mike Williams"),
    ("Jaguars WR Brian Thomas Jr. suffers season-ending knee injury", "ir_placed", "Brian Thomas Jr."),
    ("Titans placed T.J. Watt on IR", "ir_placed", "T.J. Watt"),
    ("Lions placed Amon-Ra St. Brown on injured reserve", "ir_placed", "Amon-Ra St. Brown"),
    ("Christian Barmore designated to return from IR", "ir_designated_return", "Christian Barmore"),
    ("Patriots open 21-day practice window for DT Christian Barmore", "ir_designated_return", "Christian Barmore"),
    ("Jets activate WR Garrett Wilson from injured reserve", "ir_activated", "Garrett Wilson"),
    ("Cowboys activated DE Sam Williams off the PUP list", "pup_activated", "Sam Williams"),
    ("Lions activate G Giovanni Manu from NFI", "nfi_activated", "Giovanni Manu"),
    ("Dolphins placed OT Patrick Paul on the PUP list", "pup_placed", "Patrick Paul"),
    ("Chiefs elevate RB Carson Steele from the practice squad", "ps_elevated", "Carson Steele"),
    ("Standard elevation: Chiefs bring up RB Carson Steele", "ps_elevated", "Carson Steele"),
    ("Taylor Rapp is signing to the Broncos practice squad, per source (1st @mzenitz).", "ps_signed", "Taylor Rapp"),
    ("Report: Giants to sign DL Dean Lowry to practice squad", "ps_signed", "Dean Lowry"),
    ("The Buccaneers have brought rookie LB Caden Fordham back to the practice squad.", "ps_signed", "Caden Fordham"),
    ("Giants released WR Isaiah Hodgins from the practice squad", "ps_released", "Isaiah Hodgins"),
    ("Steelers sign RB Jaylen Warren to the active roster from the practice squad", "ps_promoted", "Jaylen Warren"),
    ("Broncos signed WR Lil'Jordan Humphrey to the 53-man roster", "ps_promoted", "Lil'Jordan Humphrey"),
    ("Eagles claimed CB Kevin Knowles off waivers from the Bucs", "claimed", "Kevin Knowles"),
    ("Dolphins waived RB Salvon Ahmed", "waived", "Salvon Ahmed"),
    ("Ravens release veteran LB Roquan Smith", "released", "Roquan Smith"),
    ("Dean Lowry was released by the Steelers as part of final cuts.", "released", "Dean Lowry"),
    ("#Bears released WR Ray-Ray McCloud with an injury settlement.", "injury_settlement", "Ray-Ray McCloud"),
    ("NFL suspends Rashee Rice for six games", "suspended", "Rashee Rice"),
    ("Rashee Rice reinstated by the NFL", "reinstated", "Rashee Rice"),
])
def test_classify_news_positives(title, expected, name):
    assert re_.classify_news(title) == expected
    got, key = re_.extract_player({"title": title, "category": "news"})
    assert got == name
    assert key == nv.name_key(name)


@pytest.mark.parametrize("title", [
    "Christian Barmore returns to practice",
    "Christian Barmore expected to return this week",
    "Barmore could return from IR next week",
    "Rookie WR is practice squad-eligible after clearing waivers",
    "Ray Davis is not expected to be placed on IR",
    "Bills not expected to be placed on IR: RB Ray Davis",
    "Could the Bills place Ray Davis on IR?",
    "Ray Davis avoided a season-ending injury",
    "Big Ten releases a statement",
    "Point spread released for Alabama football SEC opener at Kentucky",
    "Where did the players the Cardinals released end up?",
    "Bills cut down to 53",
    "Sam Darnold says the Seahawks have their work cut out for them",
    "TE Brayden Willis, whom the 49ers waived yesterday, is eligible to return to the practice squad.",
    "Congrats to @jollywipradio who was back on the air after a few months on IR.",
])
def test_classify_news_negatives(title):
    assert re_.classify_news(title) is None


def test_multi_clause_tweet_yields_two_events_in_text_order():
    title = ("Bucs signed Caden Fordham to the practice squad. Additionally, the team has "
             "released practice squad cornerback Roman Parodie.")
    found = re_._match_news_all(title)
    assert [t for t, _ in found] == ["ps_signed", "ps_released"]
    events = re_.normalize_news([{"title": title, "teams": ["TB"], "source": "Twitter/NFL Insiders",
                                  "source_type": "twitter", "published": "2026-09-07T15:00:00+00:00"}],
                                "2026-09-07", schedule=_sched())
    assert [(e["event_type"], e["name"], e["team"]) for e in events] == [
        ("ps_signed", "Caden Fordham", "TB"), ("ps_released", "Roman Parodie", "TB"),
    ]
    assert all(e["confidence"] == "reported" and e["source_kind"] == "news" for e in events)


def test_news_requires_team_and_headline_roster_check():
    items = [
        {"title": "Big Ten releases a statement", "teams": [], "source": "X", "source_type": "twitter",
         "published": "2026-09-07T15:00:00+00:00"},
        # nickname fallback when the collector tagged no team
        {"title": "Bucs signed Caden Fordham to the practice squad.", "teams": [], "source": "X",
         "source_type": "twitter", "published": "2026-09-07T15:00:00+00:00"},
        # Title-Case headline: name must exist on a roster when known_names is given
        {"title": "Where Every Cardinals Roster Move Landed, Sign Three Players to Practice Squad",
         "teams": ["ARI"], "source": "SI", "source_type": "rss", "published": "2026-09-07T15:00:00+00:00"},
    ]
    events = re_.normalize_news(items, "2026-09-07", schedule=_sched(), known_names={"caden fordham"})
    assert [(e["event_type"], e["name"], e["team"]) for e in events] == [("ps_signed", "Caden Fordham", "TB")]


# ---------------------------------------------------------------------------
# Player extraction
# ---------------------------------------------------------------------------


def test_extract_player_sources():
    with_extra = {"title": "Whatever: Jaguars (Traded)", "category": "transaction",
                  "source": "NFL.com Transactions", "extra": {"player": "Keivie Rose"}}
    assert re_.extract_player(with_extra) == ("Keivie Rose", "keivie rose")
    old_style = {"title": "Keivie Rose: Jaguars (Terminated Via Waivers, all contracts)",
                 "category": "transaction", "source": "NFL.com Transactions"}
    assert re_.extract_player(old_style) == ("Keivie Rose", "keivie rose")
    tweet = {"title": "Bills placed RB Ray Davis on injured reserve, per source.", "category": "news"}
    assert re_.extract_player(tweet) == ("Ray Davis", "ray davis")
    assert re_.extract_player({"title": "Bills placed on IR", "category": "news"}) == (None, None)
    # curly apostrophes fold into the same key as nflverse
    assert re_.extract_player({"title": "Seahawks signed S D’Anthony Bell to their practice squad",
                               "category": "news"})[1] == nv.name_key("D'Anthony Bell")


def test_normalize_nflcom_old_style_and_extra():
    items = [
        {"title": "Keivie Rose: Jaguars (Terminated Via Waivers, all contracts)", "url": "u1",
         "source": "NFL.com Transactions", "source_type": "web", "category": "transaction",
         "teams": ["JAX"], "published": "2026-09-06T12:00:00+00:00"},
        {"title": "Ray Davis: Bills (Reserve/Injured)", "url": "u2",
         "source": "NFL.com Transactions", "source_type": "web", "category": "transaction",
         "teams": ["BUF"], "published": "2026-09-07T12:00:00+00:00",
         "extra": {"kind": "nfl_transaction", "tx_type": "Reserve/Injured", "nfl_category": "reserve-list",
                   "player": "Ray Davis", "from_team": "BUF", "to_team": "", "position": "RB",
                   "tx_date": "2026-09-07"}},
        {"title": "Some tweet about IR", "source": "Twitter", "source_type": "twitter", "category": "news",
         "teams": [], "published": "2026-09-07T12:00:00+00:00"},
    ]
    events = re_.normalize_nflcom(items, "2026-09-07", schedule=_sched())
    assert [(e["event_type"], e["name"], e["team"], e["date"]) for e in events] == [
        ("released", "Keivie Rose", "JAX", "2026-09-06"),
        ("ir_placed", "Ray Davis", "BUF", "2026-09-07"),
    ]
    assert events[0]["from_team"] == "JAX" and events[0]["confidence"] == "official"
    assert events[1]["pos"] == "RB" and events[1]["source"] == "nflcom_transactions"
    assert events[1]["week"] is None            # 9/7 is before the Week 1 Tuesday
    assert re_._week_for("2026-09-10", _sched()) == 1
    assert len(events[0]["event_id"]) == 16


# ---------------------------------------------------------------------------
# nflverse
# ---------------------------------------------------------------------------


def test_status_label_and_normalize_roster():
    assert nv.status_label("ACT", "A01") == "ACT"
    assert nv.status_label("DEV", "P07") == "PS"
    assert nv.status_label("CUT", "P01") == "FA"      # CUT rows keep their old abbr
    assert nv.status_label("RES", "R01") == "IR"
    assert nv.status_label("RES", "R48") == "IR"
    assert nv.status_label("RES", "R04") == "PUP"
    assert nv.status_label("RES", "R05") == "NFI"
    assert nv.status_label("RES", "R40") == "SUS"
    assert nv.status_label("RES", "ZZ9") == "IR"       # unknown reserve code -> IR
    assert nv.status_label("EXE", "E02") == "EXE"
    assert nv.status_label("RET", "R02") == "RET"
    rows = [
        {"gsis_id": "00-9", "esb_id": "X", "full_name": "Puka Nacua", "team": "LA", "position": "WR",
         "depth_chart_position": "WR", "status": "ACT", "status_description_abbr": "A01", "week": "1"},
        {"gsis_id": "", "full_name": "No Id", "team": "LA", "position": "WR", "status": "ACT",
         "status_description_abbr": "A01", "week": "1"},
    ]
    players = nv.normalize_roster(rows)
    assert list(players) == ["00-9"]
    assert players["00-9"]["team"] == "LAR" and players["00-9"]["label"] == "ACT"


def test_diff_and_normalize_nflverse_transitions():
    prev = _roster()
    cur = _roster()
    cur["00-1"] = _player("00-1", "Ray Davis", "BUF", "RB", "RES", "R01")            # ACT -> IR
    cur["00-2"] = _player("00-2", "Christian Barmore", "NE", "DT", "ACT", "A01")     # IR -> ACT
    cur["00-3"] = _player("00-3", "Carson Steele", "KC", "RB", "ACT", "A01")         # PS -> ACT
    cur["00-4"] = _player("00-4", "Taylor Rapp", "DEN", "S", "DEV", "P07")            # FA -> PS
    cur["00-5"] = _player("00-5", "New Guy", "MIA", "WR", "ACT", "A01")               # added

    transitions = nv.diff_nflverse(cur, prev)
    kinds = {t["gsis_id"]: t["kind"] for t in transitions}
    assert kinds == {"00-1": "status", "00-2": "status", "00-3": "status", "00-4": "status", "00-5": "added"}

    events = re_.normalize_nflverse(transitions, "2026-09-12", existing_events=[], schedule=_sched())
    by = {e["gsis_id"]: e for e in events}
    assert by["00-1"]["event_type"] == "ir_placed" and by["00-1"]["confidence"] == "confirmed"
    assert by["00-2"]["event_type"] == "ir_activated"
    assert by["00-3"]["event_type"] == "ps_promoted" and by["00-3"]["confidence"] == "reported"
    assert by["00-4"]["event_type"] == "ps_signed"
    assert by["00-5"]["event_type"] == "signed"
    assert all(e["source_kind"] == "nflverse" for e in events)


def test_nflverse_elevation_round_trip_relabels():
    prev = _roster()
    up = _roster()
    up["00-3"] = _player("00-3", "Carson Steele", "KC", "RB", "ACT", "A01")
    ledger = re_.normalize_nflverse(nv.diff_nflverse(up, prev), "2026-09-13", existing_events=[], schedule=_sched())
    assert ledger[0]["event_type"] == "ps_promoted"

    back = nv.diff_nflverse(prev, up)   # ACT -> DEV two days later
    new = re_.normalize_nflverse(back, "2026-09-15", existing_events=ledger, schedule=_sched())
    assert new == []                                   # the reversion is not a fresh ps_signed
    assert ledger[0]["event_type"] == "ps_elevated"
    assert ledger[0]["confidence"] == "confirmed"
    assert ledger[0]["relabeled_from"] == "ps_promoted"

    # outside the 7-day window it is a genuine PS re-signing
    ledger2 = re_.normalize_nflverse(nv.diff_nflverse(up, prev), "2026-09-01", existing_events=[], schedule=_sched())
    new2 = re_.normalize_nflverse(back, "2026-09-15", existing_events=ledger2, schedule=_sched())
    assert ledger2[0]["event_type"] == "ps_promoted"
    assert [e["event_type"] for e in new2] == ["ps_signed"]


def test_nflverse_team_change_and_abbr_only_ignored():
    prev = _roster()
    cur = _roster()
    cur["00-1"] = _player("00-1", "Ray Davis", "MIA", "RB", "ACT", "A01")      # same status, new team
    cur["00-3"] = _player("00-3", "Carson Steele", "KC", "RB", "DEV", "P07")   # P01 -> P07, still PS
    events = re_.normalize_nflverse(nv.diff_nflverse(cur, prev), "2026-09-12", schedule=_sched())
    assert [(e["event_type"], e["from_team"], e["to_team"]) for e in events] == [("team_change", "BUF", "MIA")]


# ---------------------------------------------------------------------------
# OurLads
# ---------------------------------------------------------------------------


def test_normalize_ourlads():
    changes = [
        {"type": "status_change", "name": "Ray Davis", "team": "BUF", "pos": "RB", "generic_pos": "RB",
         "old_status": "Active", "new_status": "IR", "depth": 1, "message": "Ray Davis (BUF) Active -> IR"},
        {"type": "status_change", "name": "Christian Barmore", "team": "NE", "pos": "DT", "generic_pos": "DT",
         "old_status": "IR", "new_status": "Active", "depth": 1, "message": "..."},
        {"type": "status_change", "name": "Kyler Murray", "team": "ARZ", "pos": "QB", "generic_pos": "QB",
         "old_status": None, "new_status": "PUP", "depth": 1, "message": "..."},
        {"type": "status_change", "name": "Gone Guy", "team": "DAL", "pos": "WR", "generic_pos": "WR",
         "old_status": "IR", "new_status": None, "depth": 1, "message": "no longer listed on IR"},
    ]
    events = re_.normalize_ourlads(changes, "2026-09-07", schedule=_sched())
    assert [(e["event_type"], e["team"]) for e in events] == [
        ("ir_placed", "BUF"), ("ir_activated", "NE"), ("status_change", "ARI"), ("status_change", "DAL"),
    ]
    assert events[2]["detail"] == "..."   # first seen on PUP: undated, so not a placement
    assert all(e["confidence"] == "confirmed" and e["source_kind"] == "ourlads" for e in events)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _ev(name, etype, date, source="news:PFT", kind="news", conf="reported", team="BUF", **kw):
    return re_.make_event(date_str=date, name=name, event_type=etype, source=source,
                          source_kind=kind, confidence=conf, team=team, schedule=_sched(), **kw)


def test_append_events_dedupes(tmp_roster):
    a = _ev("Ray Davis", "ir_placed", "2026-09-07")
    a_dup = dict(a)
    near = _ev("Ray Davis", "ir_placed", "2026-09-08", source="news:Twitter")   # same kind, +1 day
    other_kind = _ev("Ray Davis", "ir_placed", "2026-09-08", source="nflcom_transactions",
                     kind="official", conf="official")
    assert re_.append_events([a, a_dup, near, other_kind]) == 2
    assert re_.append_events([a, near]) == 0
    ledger = re_.load_events()
    assert [e["source_kind"] for e in ledger] == ["news", "official"]
    assert (tmp_roster / "events.jsonl").exists()


def test_confirm_reported_links_official_within_window():
    reported = _ev("Ray Davis", "ir_placed", "2026-09-06")
    official = _ev("Ray Davis", "ir_placed", "2026-09-08", source="nflcom_transactions",
                   kind="official", conf="official")
    far = _ev("Ray Davis", "ir_activated", "2026-09-01")
    unrelated = _ev("Other Guy", "ir_placed", "2026-09-07", source="nflcom_transactions",
                    kind="official", conf="official")
    # news "waived" confirmed by official "released" (same "cut" family)
    waived = _ev("Cut Guy", "waived", "2026-09-07")
    released = _ev("Cut Guy", "released", "2026-09-07", source="nflcom_transactions",
                   kind="official", conf="official")
    events = re_.confirm_reported([reported, official, far, unrelated, waived, released], window_days=3)
    assert reported["confirmed_by"] == official["event_id"]
    assert reported["confidence"] == "confirmed"
    assert far["confirmed_by"] is None
    assert waived["confirmed_by"] == released["event_id"]
    assert official["confirmed_by"] is None
    assert events is not None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_build_state_ir_return_week_skips_bye(tmp_roster):
    roster = _roster()
    ev = _ev("Ray Davis", "ir_placed", "2026-09-15", source="nflcom_transactions",
             kind="official", conf="official", gsis_id="00-1")
    state = re_.build_state([ev], roster, _sched(), settings=SETTINGS,
                            baseline_date="2026-09-14", as_of="2026-09-15")
    p = state["players"]["00-1"]
    assert p["status"] == "IR" and p["status_source"] == "official"
    assert p["ir_date"] == "2026-09-15"
    # BUF games after 9/15: wk2, (bye wk3), wk4, wk5, wk6 -> misses 4 -> back wk 7
    assert p["earliest_return_week"] == 7
    assert state["by_name"]["ray davis"] == "00-1"
    assert state["baseline"] == {"source": "nflverse", "date": "2026-09-14", "players": 4}
    assert state["as_of_week"] == 2
    # baseline IR player without an event keeps nflverse status, no return week
    assert state["players"]["00-2"]["status"] == "IR"
    assert state["players"]["00-2"]["earliest_return_week"] is None

    activated = _ev("Ray Davis", "ir_activated", "2026-10-27", source="nflverse", kind="nflverse",
                    conf="confirmed", gsis_id="00-1")
    state2 = re_.build_state([ev, activated], roster, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-10-27")
    assert state2["players"]["00-1"]["status"] == "ACT"
    assert state2["players"]["00-1"]["ir_date"] is None


def test_build_state_history_only_fills_ir_date():
    roster = _roster()   # Barmore is IR in the baseline
    ev = _ev("Christian Barmore", "ir_placed", "2026-09-02", source="nflcom_transactions",
             kind="official", conf="official", team="NE")
    state = re_.build_state([ev], roster, _sched(), settings=SETTINGS,
                            baseline_date="2026-09-14", as_of="2026-09-14")
    p = state["players"]["00-2"]
    assert p["ir_date"] == "2026-09-02"          # resolved by name to the GSIS record
    assert p["status_source"] == "nflverse"      # history-only: baseline status untouched
    # a pre-baseline "released" outside the override window must NOT
    # override the baseline (13 days old: nflverse has long caught up)
    rel = _ev("Ray Davis", "released", "2026-09-01", source="nflcom_transactions",
              kind="official", conf="official", gsis_id="00-1")
    state2 = re_.build_state([rel], roster, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-09-14")
    assert state2["players"]["00-1"]["status"] == "ACT"


def test_build_state_recent_official_move_overrides_stale_baseline():
    """nflverse lags NFL.com by days: Darius Slayton was terminated 2026-09-07
    and the 09-09 nflverse file still had him ACT on NYG, so the audit called
    him 'active but not on the sheet'. A recent official move applies when
    the baseline still shows the pre-move state."""
    roster = _roster()   # Ray Davis: ACT / BUF in the baseline
    rel = _ev("Ray Davis", "released", "2026-09-12", source="nflcom_transactions",
              kind="official", conf="official", gsis_id="00-1", team="BUF")
    state = re_.build_state([rel], roster, _sched(), settings=SETTINGS,
                            baseline_date="2026-09-14", as_of="2026-09-14")
    p = state["players"]["00-1"]
    assert p["status"] == "FA" and p["status_source"] == "official"
    assert p["status_since"] == "2026-09-12"

    # ...but not when the baseline already moved past it: released by BUF,
    # then nflverse shows him ACT on another team (a signing the ledger
    # never saw) -> the release is history.
    moved = {k: dict(v) for k, v in roster.items()}
    moved["00-1"]["team"] = "MIA"
    state2 = re_.build_state([rel], moved, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-09-14")
    assert state2["players"]["00-1"]["status"] == "ACT"
    assert state2["players"]["00-1"]["team"] == "MIA"

    # a reported (news-only) cut gets no such override
    tweet = _ev("Ray Davis", "released", "2026-09-12", gsis_id="00-1", team="BUF")
    state3 = re_.build_state([tweet], roster, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-09-14")
    assert state3["players"]["00-1"]["status"] == "ACT"

    # window is configurable
    tight = {"season": SETTINGS["season"], "roster": dict(SETTINGS["roster"], official_override_days=1)}
    state4 = re_.build_state([rel], roster, _sched(), settings=tight,
                             baseline_date="2026-09-14", as_of="2026-09-14")
    assert state4["players"]["00-1"]["status"] == "ACT"


def test_build_state_elevation_counters():
    roster = _roster()
    e1 = _ev("Carson Steele", "ps_elevated", "2026-09-13", source="news:PFT", team="KC")                  # reported
    e2 = _ev("Carson Steele", "ps_elevated", "2026-09-14", source="nflverse", kind="nflverse",
             conf="confirmed", team="KC", gsis_id="00-3")                                                  # confirms e1
    e3 = _ev("Carson Steele", "ps_elevated", "2026-09-21", source="nflverse", kind="nflverse",
             conf="confirmed", team="KC", gsis_id="00-3")
    e4 = _ev("Carson Steele", "ps_elevated", "2026-09-28", source="news:Twitter", team="KC")               # unconfirmed
    events = re_.confirm_reported([e1, e2, e3, e4], window_days=3)
    state = re_.build_state(events, roster, _sched(), settings=SETTINGS,
                            baseline_date="2026-09-12", as_of="2026-09-28")
    p = state["players"]["00-3"]
    assert p["status"] == "PS"                       # elevations never change status
    assert p["elevations_used"] == 2                 # 9/13-14 counted once, 9/21
    assert p["elevations_reported"] == 1             # 9/28 unconfirmed
    assert p["elevation_dates"] == ["2026-09-13", "2026-09-21", "2026-09-28"]
    assert [x["event_type"] for x in p["pending"]] == ["ps_elevated"]


def test_build_state_reported_gate_and_precedence():
    roster = _roster()
    tweet = _ev("Ray Davis", "ir_placed", "2026-09-15", gsis_id="00-1")
    state = re_.build_state([tweet], roster, _sched(), settings=SETTINGS,
                            baseline_date="2026-09-14", as_of="2026-09-15")
    assert state["players"]["00-1"]["status"] == "IR"
    assert state["players"]["00-1"]["pending"][0]["event_id"] == tweet["event_id"]

    off = {"season": SETTINGS["season"], "roster": dict(SETTINGS["roster"], apply_reported_events=False)}
    state_off = re_.build_state([tweet], roster, _sched(), settings=off,
                                baseline_date="2026-09-14", as_of="2026-09-15")
    p = state_off["players"]["00-1"]
    assert p["status"] == "ACT"
    assert p["pending"][0]["applied"] is False

    # same date: official beats reported regardless of list order
    official = _ev("Ray Davis", "signed", "2026-09-15", source="nflcom_transactions", kind="official",
                   conf="official", gsis_id="00-1", to_team="MIA")
    state2 = re_.build_state([official, tweet], roster, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-09-15")
    assert state2["players"]["00-1"]["status"] == "ACT"
    assert state2["players"]["00-1"]["team"] == "MIA"

    # name-only player (no GSIS anywhere) lives under name:<key>; with no
    # baseline record to protect, an event older than the baseline still applies
    ghost = _ev("Street Guy", "ps_signed", "2026-09-12", team="DAL")
    state3 = re_.build_state([ghost], roster, _sched(), settings=SETTINGS,
                             baseline_date="2026-09-14", as_of="2026-09-15")
    assert state3["players"]["name:street guy"]["status"] == "PS"
    assert state3["players"]["name:street guy"]["team"] == "DAL"


def test_resolve_gsis_disambiguates_by_team():
    roster = _roster()
    roster["00-7"] = _player("00-7", "Ray Davis", "SEA", "WR", "ACT", "A01")
    assert re_.resolve_gsis("ray davis", "BUF", roster) == "00-1"
    assert re_.resolve_gsis("ray davis", "SEA", roster) == "00-7"
    assert re_.resolve_gsis("ray davis", "", roster) is None
    assert re_.resolve_gsis("christian barmore", "", roster) == "00-2"
    assert re_.resolve_gsis("nobody here", "BUF", roster) is None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def test_run_roster_step_end_to_end(tmp_roster, monkeypatch):
    prev = _roster()
    cur = _roster()
    cur["00-3"] = _player("00-3", "Carson Steele", "KC", "RB", "ACT", "A01")   # PS -> ACT (reported)
    news = [
        {"title": "Ray Davis: Bills (Reserve/Injured)", "url": "u", "source": "NFL.com Transactions",
         "source_type": "web", "category": "transaction", "teams": ["BUF"],
         "published": "2026-09-15T12:00:00+00:00"},
        {"title": "Bills placed RB Ray Davis on injured reserve, per source.", "url": "t",
         "source": "Twitter/NFL Insiders", "source_type": "twitter", "category": "news", "teams": ["BUF"],
         "published": "2026-09-14T22:00:00+00:00"},
        {"title": "Chiefs signed RB Carson Steele to the active roster", "url": "t2",
         "source": "Pro Football Talk", "source_type": "rss", "category": "national", "teams": ["KC"],
         "published": "2026-09-15T12:00:00+00:00"},
    ]
    dc = [{"type": "status_change", "name": "Ray Davis", "team": "BUF", "pos": "RB", "generic_pos": "RB",
           "old_status": "Active", "new_status": "IR", "depth": 1, "message": "m"}]
    out = re_.run_roster_step("2026-09-15", news_items=news, dc_status_changes=dc,
                              nflverse_players=cur, prev_nflverse=prev, settings=SETTINGS, schedule=_sched())
    types = sorted((e["event_type"], e["source_kind"]) for e in out["new_events"])
    assert types == [("ir_placed", "news"), ("ir_placed", "official"), ("ir_placed", "ourlads"),
                     ("ps_promoted", "news"), ("ps_promoted", "nflverse")]
    assert out["counts"]["appended"] == 5
    tweet = next(e for e in out["new_events"] if e["source_kind"] == "news" and e["event_type"] == "ir_placed")
    assert tweet["confirmed_by"] and tweet["confidence"] == "confirmed"
    official = next(e for e in out["new_events"] if e["source_kind"] == "official")
    assert official["earliest_return_week"] == 7
    st = out["state"]["players"]
    assert st["00-1"]["status"] == "IR" and st["00-1"]["earliest_return_week"] == 7
    assert st["00-3"]["status"] == "ACT"
    assert (tmp_roster / "state.json").exists()
    assert json.loads((tmp_roster / "state.json").read_text(encoding="utf-8"))["as_of"] == "2026-09-15"
    assert re_.load_state()["baseline"]["date"] == "2026-09-15"

    # second run with the same inputs appends nothing new
    again = re_.run_roster_step("2026-09-15", news_items=news, dc_status_changes=dc,
                                nflverse_players=cur, prev_nflverse=prev, settings=SETTINGS, schedule=_sched())
    assert again["counts"]["appended"] == 0
    assert again["counts"]["ledger"] == 5
