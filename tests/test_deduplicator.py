"""Tests for processing.deduplicator.

Split into two groups:

1. Pure helpers (title normalization, transaction-name overlap, keyword
   overlap, primary-source ranking) — fully deterministic, no model.
2. `deduplicate()` / `flatten_groups()` behavior that holds regardless of
   whether sentence-transformers is loaded. In particular, the
   transaction-name gate runs *before* any similarity scoring, so distinct
   players never merge even if their titles are near-identical.
"""

from datetime import datetime, timezone

import processing.deduplicator as dd


# ── Pure helpers ────────────────────────────────────────────────────────

def test_normalize_title_strips_prefix_and_punctuation():
    assert dd._normalize_title("BREAKING: Chiefs sign Brown!") == "chiefs sign brown"
    assert dd._normalize_title("Report - Eagles trade up") == "eagles trade up"


def test_keyword_overlap_ignores_stopwords():
    # "the" / "to" are stopwords; only "chiefs"/"sign"/"brown" count
    a = dd._normalize_title("The Chiefs sign Brown")
    b = dd._normalize_title("Chiefs to sign Brown")
    assert dd._keyword_overlap(a, b) == 1.0


def test_keyword_overlap_empty_is_zero():
    assert dd._keyword_overlap("the", "a") == 0.0


def test_transaction_name_overlap_same_player_merges():
    a = dd._normalize_title("Chiefs sign WR Marquise Brown")
    b = dd._normalize_title("Marquise Brown signing with Kansas City")
    assert dd._transaction_name_overlap(a, b) is True


def test_transaction_name_overlap_different_players_dont_merge():
    a = dd._normalize_title("Chiefs sign Marquise Brown")
    b = dd._normalize_title("Chiefs sign Justin Watson")
    assert dd._transaction_name_overlap(a, b) is False


def test_transaction_name_overlap_requires_two_words_for_full_names():
    # Only the shared first name "Marquise" overlaps -> not enough
    a = dd._normalize_title("Marquise Brown signed")
    b = dd._normalize_title("Marquise Goodwin signed")
    assert dd._transaction_name_overlap(a, b) is False


# ── Primary-source ranking ──────────────────────────────────────────────

def test_is_primary_source():
    assert dd.is_primary_source("ESPN NFL") is True
    assert dd.is_primary_source("Pro Football Talk") is True
    assert dd.is_primary_source("SBN Cardinals (Revenge of the Birds)") is False
    assert dd.is_primary_source("SI Bills") is False
    assert dd.is_primary_source("r/nfl via @Schefter") is False
    assert dd.is_primary_source("") is False


def test_pick_primary_prefers_original_outlet_over_blog(make_item):
    espn = make_item("Chiefs sign Brown", source="ESPN NFL", summary="short")
    sbn = make_item(
        "Chiefs sign Brown",
        source="SBN Chiefs (Arrowhead Pride)",
        summary="a much much much longer body of analysis",
    )
    # Even though SBN has the longer summary, ESPN wins on tier.
    assert dd.pick_primary([sbn, espn]) is espn


def test_pick_primary_longest_summary_within_tier(make_item):
    a = make_item("Story", source="ESPN NFL", summary="short")
    b = make_item("Story", source="CBS Sports NFL", summary="a longer summary body")
    assert dd.pick_primary([a, b]) is b


# ── deduplicate() / flatten_groups() (backend-independent) ───────────────

def test_distinct_transactions_never_merge(make_item):
    """Two different signings with structurally identical titles must stay
    separate — the transaction-name gate blocks the merge before similarity
    is even computed, so this holds with or without embeddings."""
    items = [
        make_item("Marquise Brown: Kansas City Chiefs (Exclusive Rights Signing)",
                  category="transaction", teams=["KC"]),
        make_item("Justin Watson: Kansas City Chiefs (Exclusive Rights Signing)",
                  category="transaction", teams=["KC"]),
    ]
    groups = dd.deduplicate(items)
    assert len(groups) == 2


def test_same_transaction_two_sources_merges(make_item):
    items = [
        make_item("Chiefs sign WR Marquise Brown", source="ESPN NFL",
                  category="transaction", teams=["KC"],
                  published=datetime(2026, 5, 1, 9, tzinfo=timezone.utc)),
        make_item("Marquise Brown signing with Kansas City Chiefs", source="Pro Football Talk",
                  category="transaction", teams=["KC"],
                  published=datetime(2026, 5, 1, 10, tzinfo=timezone.utc)),
    ]
    groups = dd.deduplicate(items)
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_empty_input():
    assert dd.deduplicate([]) == []


def test_flatten_appends_also_reported_by(make_item):
    a = make_item("Chiefs sign WR Marquise Brown", source="ESPN NFL",
                  category="transaction", teams=["KC"], summary="ESPN body")
    b = make_item("Marquise Brown signing with Kansas City Chiefs", source="SBN Chiefs",
                  category="transaction", teams=["KC"], summary="blog body")
    flat = dd.flatten_groups(dd.deduplicate([a, b]))
    assert len(flat) == 1
    # ESPN is the primary; SBN is credited in the appended note.
    assert flat[0].source == "ESPN NFL"
    assert "Also reported by" in flat[0].summary
    assert "SBN Chiefs" in flat[0].summary
