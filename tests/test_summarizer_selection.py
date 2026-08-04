"""Tests for the summarizer's pure selection helpers.

Covers league-wide candidate ordering, the other-sport tweet blocklist in
`_league_wide_eligible`, and the settings-driven team pool limit. No LLM
provider or network involved.
"""

import re
from datetime import datetime, timezone

import processing.summarizer as sm


def _dt(hour):
    return datetime(2026, 8, 2, hour, tzinfo=timezone.utc)


# ── _order_league_wide ──────────────────────────────────────────────────

def test_order_league_wide_outlets_before_tweets(make_item):
    older_espn = make_item("NFL announces rule change", source="ESPN NFL",
                           source_type="rss", published=_dt(6))
    newer_tweet = make_item("Some insider tweet about a trade",
                            source="Twitter/NFL Insiders",
                            source_type="twitter", published=_dt(12))
    ordered = sm._order_league_wide([newer_tweet, older_espn])
    assert ordered[0] is older_espn


def test_order_league_wide_primary_before_aggregator(make_item):
    reddit = make_item("League roundup", source="r/nfl",
                       source_type="reddit", published=_dt(12))
    espn = make_item("League roundup two", source="ESPN NFL",
                     source_type="rss", published=_dt(6))
    ordered = sm._order_league_wide([reddit, espn])
    assert ordered[0] is espn


def test_order_league_wide_recency_within_tier(make_item):
    a = make_item("Story A", source="ESPN NFL", source_type="rss", published=_dt(6))
    b = make_item("Story B", source="ESPN NFL", source_type="rss", published=_dt(9))
    ordered = sm._order_league_wide([a, b])
    assert ordered[0] is b


# ── _league_wide_eligible with exclude_re ───────────────────────────────

EXCLUDE_RE = re.compile(r"\b(MLB|Dodgers|Tigers)\b", re.IGNORECASE)


def test_other_sport_tweet_blocked(make_item):
    tweet = make_item("This is an all-time Taco trade. Worst trade in Tigers history.",
                      source_type="twitter")
    assert sm._league_wide_eligible(
        tweet, set(), sm._TWITTER_LEAGUE_SIGNAL, exclude_re=EXCLUDE_RE,
    ) is False


def test_other_sport_tweet_passes_without_exclude(make_item):
    tweet = make_item("This is an all-time Taco trade. Worst trade in Tigers history.",
                      source_type="twitter")
    # "trade" is a news signal, so without the blocklist the tweet slips in.
    assert sm._league_wide_eligible(
        tweet, set(), sm._TWITTER_LEAGUE_SIGNAL, exclude_re=None,
    ) is True


def test_nfl_tweet_not_blocked(make_item):
    tweet = make_item("Chiefs agree to trade for a WR", source_type="twitter")
    assert sm._league_wide_eligible(
        tweet, set(), sm._TWITTER_LEAGUE_SIGNAL, exclude_re=EXCLUDE_RE,
    ) is True


def test_non_twitter_always_eligible(make_item):
    item = make_item("MLB crossover story somehow untagged", source_type="rss")
    assert sm._league_wide_eligible(
        item, set(), sm._TWITTER_LEAGUE_SIGNAL, exclude_re=EXCLUDE_RE,
    ) is True


# ── _team_item_limit ────────────────────────────────────────────────────

def test_team_item_limit_from_settings(monkeypatch):
    monkeypatch.setattr(sm, "get_settings",
                        lambda: {"team_notes": {"item_limit": 12}})
    assert sm._team_item_limit() == 12


def test_team_item_limit_default(monkeypatch):
    monkeypatch.setattr(sm, "get_settings", lambda: {})
    assert sm._team_item_limit() == sm.TEAM_HIGHLIGHT_ITEM_LIMIT


def test_team_item_limit_bad_value_falls_back(monkeypatch):
    monkeypatch.setattr(sm, "get_settings",
                        lambda: {"team_notes": {"item_limit": "garbage"}})
    assert sm._team_item_limit() == sm.TEAM_HIGHLIGHT_ITEM_LIMIT
