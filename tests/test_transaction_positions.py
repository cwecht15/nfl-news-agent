"""Position tags on the Transactions briefing.

NFL.com's transaction feed leaves ``position`` blank and most signings are
street free agents on no club's depth chart, so the depth-chart lookup alone
left bullets like "Texans signed Kenneth Murray, Jr." with no position.
"""

from __future__ import annotations

from datetime import datetime, timezone

import processing.summarizer as sm
from models import NewsItem


def _tx(player, team, extra_position=""):
    return NewsItem(
        title=f"{player}: Team (Free Agent Signing)",
        url="https://www.nfl.com/transactions/",
        source="NFL.com Transactions",
        source_type="web",
        published=datetime(2026, 9, 17, tzinfo=timezone.utc),
        category="transaction",
        teams=[team],
        extra={"kind": "nfl_transaction", "player": player, "to_team": team, "from_team": "",
               "position": extra_position},
    )


ROSTER = {
    "kenneth murray": [("HOU", "LB")],
    "jaylin simpson": [("MIA", "DB")],
    "chris moore": [("BAL", "WR"), ("TEN", "DB")],   # two players, one name
    "josh johnson": [("SF", "QB"), ("CLE", "DB")],
    "rome odunze": [("CHI", "WR")],
}


def test_depth_chart_wins_over_roster():
    lookup = {"rome odunze": "WR", "jalen carter": "DT"}
    assert sm._transaction_position(_tx("Jalen Carter", "PHI"), lookup, ROSTER) == "DT"


def test_depth_chart_matched_on_normalized_name():
    # OurLads keys the suffix and the periods; the feed may not
    lookup = {"marvin harrison jr.": "WR", "c.j. stroud": "QB"}
    assert sm._transaction_position(_tx("Marvin Harrison", "ARI"), lookup, {}) == "WR"
    assert sm._transaction_position(_tx("CJ Stroud", "HOU"), lookup, {}) == "QB"


def test_roster_fallback_for_free_agents():
    assert sm._transaction_position(_tx("Jaylin Simpson", "MIA"), {}, ROSTER) == "DB"
    assert sm._transaction_position(_tx("Kenneth Murray, Jr.", "HOU"), {}, ROSTER) == "LB"


def test_duplicate_name_resolved_on_the_transaction_team():
    assert sm._transaction_position(_tx("Chris Moore", "BAL"), {}, ROSTER) == "WR"
    # neither player is on the signing team and they disagree -> no guess
    assert sm._transaction_position(_tx("Josh Johnson", "NYJ"), {}, ROSTER) == ""


def test_feed_position_used_when_present():
    assert sm._transaction_position(_tx("Nobody Known", "KC", extra_position="LT"), {}, {}) == "OL"


def test_unknown_player_has_no_position():
    assert sm._transaction_position(_tx("Nobody Known", "KC"), {"rome odunze": "WR"}, ROSTER) == ""


def test_prompt_lines_carry_the_position(monkeypatch):
    captured = {}

    def fake_call(client, prompt, runtime, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(sm, "_resolve_client_and_runtime", lambda c: (None, {}))
    monkeypatch.setattr(sm, "_call_model", fake_call)
    sm.summarize_transactions([_tx("Jaylin Simpson", "MIA")], position_lookup={}, roster_positions=ROSTER)
    assert "[MIA / DB] Jaylin Simpson" in captured["prompt"]
