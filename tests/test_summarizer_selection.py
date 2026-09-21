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


# ── _strip_truncated_tail ───────────────────────────────────────────────

def test_strip_dangling_partial_bullet():
    text = "- **Cole Kmet (TE)** — Full bullet with citation. [3]\n\n- **K"
    assert sm._strip_truncated_tail(text) == \
        "- **Cole Kmet (TE)** — Full bullet with citation. [3]"


def test_strip_mid_sentence_tail():
    text = "- **A** — Complete thought. [1]\n- **B** — noted that Burden was not a full-time starter"
    assert sm._strip_truncated_tail(text) == "- **A** — Complete thought. [1]"


def test_complete_text_untouched():
    text = "- **A** — Complete thought. [1]\n\n- **B** — Another one. [2, 4]"
    assert sm._strip_truncated_tail(text) == text


def test_citation_before_period_untouched():
    text = "- **A** — shift suggests an adjustment on the 53 [10, 9]."
    assert sm._strip_truncated_tail(text) == text


def test_all_partial_returns_original():
    text = "- **K"
    assert sm._strip_truncated_tail(text) == text


# ── _diversify_by_source / _score ───────────────────────────────────────
#
# Ranking used to be (primary_title, deep_article, len(full_text), published),
# which made raw body length the tiebreaker for every ordinary item. On
# 2026-09-19 that buried a 745-char "Nacua misses practice" report under
# multi-thousand-char season columns, and LAR's notes came back with three
# defensive bullets and no Nacua.

def _with_body(item, body):
    item.full_text = body
    return item


def test_usage_signal_outranks_a_longer_generic_article(make_item):
    generic = _with_body(make_item("Rams season outlook", source="SI Rams"), "x" * 6000)
    signal = _with_body(make_item("Puka Nacua misses practice with a hip injury",
                                  source="Pro Football Talk"),
                        "The Rams added Nacua to the injury report Friday. " * 5)
    assert sm._diversify_by_source([generic, signal], 1) == [signal]


def test_signal_in_body_counts_when_title_is_bland(make_item):
    generic = _with_body(make_item("Notebook", source="SI Texans"), "y" * 6000)
    signal = _with_body(make_item("Texans notebook", source="SBN Texans (Battle Red Blog)"),
                        "Schultz is in line for more targets with Collins out.")
    assert sm._diversify_by_source([generic, signal], 1) == [signal]


def test_summary_counts_as_body_when_full_text_is_empty(make_item):
    """_build_news_context_line renders `full_text or summary`, so scoring
    full_text alone dropped rich-summary RSS items below empty ones."""
    rich = make_item("Panthers note", source="SBN Panthers", summary="z" * 3000)
    empty = make_item("Panthers headline", source="SI Panthers")
    assert sm._diversify_by_source([empty, rich], 1) == [rich]


def test_paywalled_title_length_breaks_the_tie(make_item):
    """The Athletic arrives with the dek crammed into the title and no body —
    the Coker/Person piece is exactly this shape."""
    dek = make_item("Coker has emerged as Carolina's No. 2 receiver, forming a 1-2 punch "
                    "with Tetairoa McMillan. " * 3, source="The Athletic NFL")
    bare = make_item("Panthers notes", source="SI Panthers")
    assert sm._diversify_by_source([bare, dek], 1) == [dek]


def test_league_roundup_ranks_below_team_reporting(make_item):
    """"NFL Week 2 uniforms" was tagged with all 32 teams and injected into all
    32 pools, burning a slot on content the prompt may not even write about."""
    roundup = _with_body(
        make_item("NFL Week 2 uniforms: Rams debut 'Classic Sol'", source="ESPN ARI",
                  teams=[f"T{i}" for i in range(32)]),
        "u" * 8000)
    local = _with_body(make_item("Cardinals injury notes", source="SI Cardinals", teams=["ARI"]), "x" * 500)
    assert sm._diversify_by_source([roundup, local], 1) == [local]


def test_two_team_matchup_preview_is_not_a_roundup(make_item):
    """A real game preview names both clubs — it must stay team reporting."""
    preview = _with_body(make_item("Seahawks vs. Cardinals Week 2 preview",
                                   source="SBN Seahawks", teams=["SEA", "ARI"]), "x" * 4000)
    short = _with_body(make_item("Cardinals note", source="SI Cardinals", teams=["ARI"]), "y" * 100)
    assert sm._diversify_by_source([short, preview], 1) == [preview]


def test_roundup_still_usable_when_the_pool_is_thin(make_item):
    """Demoted, not dropped — a quiet team should not get an empty pool."""
    roundup = _with_body(make_item("NFL Week 2 picks", source="ESPN ARI",
                                   teams=[f"T{i}" for i in range(25)]), "u" * 8000)
    assert sm._diversify_by_source([roundup], 4) == [roundup]


def test_transcripts_still_rank_below_news(make_item):
    class _T:
        channel_name = "Team Channel"
        published = _dt(9)

    news = _with_body(make_item("Camp note", source="ESPN NFL"), "body")
    assert sm._diversify_by_source([_T(), news], 1) == [news]
