"""Offline tests for collectors/espn_transactions_collector.py.

The fixture is a live capture of ESPN's league transaction feed from
2026-09-12 — Week 1's Saturday, the season's first elevation window. It holds
every shape the parser has to survive: both verbs, all four tails, doubled
position tokens, names with suffixes and internal periods, and descriptions
that bundle elevations with unrelated moves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collectors import espn_transactions_collector as etc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rows() -> list[dict]:
    return json.loads((FIXTURES / "espn_transactions.json").read_text(encoding="utf-8"))["transactions"]


@pytest.fixture(scope="module")
def parsed(rows) -> list[dict]:
    return etc.parse_elevations(rows)


def _names(parsed: list[dict]) -> set[str]:
    return {p["name"] for p in parsed}


def test_parses_every_elevation_in_the_feed(parsed):
    # 27 elevation rows: 16 naming two players, 11 naming one.
    assert len(parsed) == 43
    assert {p["date"] for p in parsed} == {"2026-09-09", "2026-09-10", "2026-09-12"}


@pytest.mark.parametrize("name,team,pos", [
    ("Bralen Trice", "ATL", "LB"),          # ... from the practice squad
    ("Ben Stille", "DET", "DL"),            # ... to their active roster
    ("Britain Covey", "PHI", "WR"),         # ... to the active roster
    ("Lan Larison", "NE", "RB"),            # ... from the practice squad to the active roster
    ("Chris Myarick", "LV", "TE"),          # "Elevating", not "Elevated"
    ("Mohamoud Diabate", "TEN", "LB"),      # doubled position token: "LB LB"
])
def test_elevation_shapes(parsed, name, team, pos):
    hit = next((p for p in parsed if p["name"] == name), None)
    assert hit is not None, f"{name} not parsed"
    assert (hit["team"], hit["pos"]) == (team, pos)


@pytest.mark.parametrize("name", [
    "Frank Gore Jr.",        # suffix with a period, mid-clause
    "Velus Jones Jr.",       # same, followed by " from the practice squad"
    "Rodney Thomas II",      # numeral suffix
    "C.J. Donaldson",        # initials with internal periods
    "D'Angelo Ross",         # apostrophe
    "Julian Good-Jones",     # hyphen
])
def test_names_survive_intact(parsed, name):
    assert name in _names(parsed)


@pytest.mark.parametrize("name", [
    "AJ Finley",         # "Signed ... from the practice squad to the active roster"
    "Dalen Cambre",      # "Signed ... from the practice squad"
    "Levi Onwuzurike",   # "Signed ... to the practice squad"
    "Cam Williams",      # "Placed ... on injured reserve"
    "Tucker Kraft",      # "Agreed to term ... contract extension"
    "Keidron Smith",     # "Released ... from the practice squad"
])
def test_other_moves_in_the_same_description_are_ignored(parsed, name):
    assert name not in _names(parsed)


def test_multi_player_rows_keep_everyone(parsed):
    """The failure mode of the news-headline path: only the first name survives."""
    sf = sorted(p["name"] for p in parsed if p["team"] == "SF")
    assert sf == ["KhaDarel Hodge", "Ogbo Okoronkwo"]


def test_strip_positions_handles_repeats_and_bare_names():
    assert etc._strip_positions("LB LB Mohamoud Diabate") == ("Mohamoud Diabate", "LB")
    assert etc._strip_positions("EDGE Clelin Ferrell") == ("Clelin Ferrell", "EDGE")
    assert etc._strip_positions("Kalen King") == ("Kalen King", "")


def test_parse_is_quiet_on_junk():
    assert etc.parse_elevations([]) == []
    assert etc.parse_elevations([{"description": "", "team": {"abbreviation": "ATL"}}]) == []
    assert etc.parse_elevations([{"description": "Elevated the mood.", "team": {}}]) == []
    # A one-word "name" is a fragment, not a player.
    assert etc.parse_elevations(
        [{"description": "Elevated QB from the practice squad.",
          "team": {"abbreviation": "ATL"}, "date": "2026-09-12T07:00Z"}]) == []


def test_lookback_window_filters_old_rows(rows, monkeypatch):
    monkeypatch.setattr(etc, "fetch_transactions", lambda **kw: rows)
    settings = {"roster": {"elevations": {"enabled": True, "lookback_days": 1}}}
    out = etc.collect_elevations("2026-09-12", settings=settings)
    assert {e["date"] for e in out} == {"2026-09-12"}


def test_disabled_collector_returns_nothing(monkeypatch):
    def boom(**kw):  # pragma: no cover — must never be called
        raise AssertionError("fetch attempted while disabled")

    monkeypatch.setattr(etc, "fetch_transactions", boom)
    assert etc.collect_elevations(settings={"roster": {"elevations": {"enabled": False}}}) == []


def test_fetch_failure_is_non_fatal(monkeypatch):
    class Boom:
        def get(self, *a, **kw):
            raise RuntimeError("network down")

    assert etc.fetch_transactions(session=Boom()) == []
