"""Tests for processing.cross_day_filter.

`_is_exempt` is pure; `filter_recent_duplicates` is tested with a tmp raw
dir and a monkeypatched embedding backend so no model is loaded.
"""

import json
import re

import numpy as np
import pytest

import processing.cross_day_filter as cdf


# ── _is_exempt ──────────────────────────────────────────────────────────

CAMP_RE = [re.compile(r"training camp:?\s*latest\b", re.IGNORECASE)]


def test_exempt_by_category(make_item):
    item = make_item("Chiefs sign WR", category="transaction")
    assert cdf._is_exempt(item, {"transaction"}, []) is True


def test_exempt_by_title_pattern(make_item):
    item = make_item("2026 New York Jets training camp: Latest intel, updates")
    assert cdf._is_exempt(item, set(), CAMP_RE) is True


def test_not_exempt(make_item):
    item = make_item("Jets name starting QB")
    assert cdf._is_exempt(item, {"transaction"}, CAMP_RE) is False


# ── filter_recent_duplicates with skip_title_patterns ───────────────────

def _write_prior_day(raw_dir, date_str, titles):
    day = raw_dir / date_str
    day.mkdir(parents=True)
    (day / "rss.json").write_text(
        json.dumps([{"title": t} for t in titles]), encoding="utf-8"
    )


@pytest.fixture
def identical_embeddings(monkeypatch):
    """Every title embeds to the same unit vector → sim 1.0 for all pairs."""
    monkeypatch.setattr(
        cdf, "_compute_embeddings",
        lambda texts: np.tile(np.array([1.0, 0.0]), (len(texts), 1)),
    )


def test_pattern_exempt_item_kept(tmp_path, make_item, identical_embeddings):
    _write_prior_day(tmp_path, "2026-08-02",
                     ["2026 New York Jets training camp: Latest intel, updates"])
    item = make_item("2026 New York Jets training camp: Latest intel, updates")
    kept, dropped = cdf.filter_recent_duplicates(
        [item], raw_dir=tmp_path, current_date="2026-08-03",
        skip_title_patterns=[r"training camp:?\s*latest\b"],
    )
    assert kept == [item]
    assert dropped == []


def test_non_exempt_duplicate_dropped(tmp_path, make_item, identical_embeddings):
    _write_prior_day(tmp_path, "2026-08-02", ["Jets name starting QB"])
    item = make_item("Jets name starting QB")
    kept, dropped = cdf.filter_recent_duplicates(
        [item], raw_dir=tmp_path, current_date="2026-08-03",
        skip_title_patterns=[r"training camp:?\s*latest\b"],
    )
    assert kept == []
    assert len(dropped) == 1


def test_none_patterns_is_old_behavior(tmp_path, make_item, identical_embeddings):
    _write_prior_day(tmp_path, "2026-08-02",
                     ["2026 New York Jets training camp: Latest intel, updates"])
    item = make_item("2026 New York Jets training camp: Latest intel, updates")
    kept, dropped = cdf.filter_recent_duplicates(
        [item], raw_dir=tmp_path, current_date="2026-08-03",
        skip_title_patterns=None,
    )
    assert kept == []
    assert len(dropped) == 1
