"""Tests for processing.yt_cache (durable disk cache for YT report sections).

The cache directory is redirected to a tmp path so tests never touch the
real data/ tree.
"""

from pathlib import Path

import pytest

import processing.yt_cache as yt_cache


@pytest.fixture(autouse=True)
def _tmp_cache_dir(tmp_path, monkeypatch):
    """Point yt_cache at a throwaway directory for every test."""
    monkeypatch.setattr(yt_cache, "get_data_dir", lambda *a, **k: tmp_path)
    return tmp_path


def test_make_key_is_deterministic():
    k1 = yt_cache.make_key("2026-05-01", "2026-05-07", ["KC"], ["a", "b"])
    k2 = yt_cache.make_key("2026-05-01", "2026-05-07", ["KC"], ["a", "b"])
    assert k1 == k2


def test_make_key_order_independent():
    k1 = yt_cache.make_key("2026-05-01", "2026-05-07", ["KC", "PHI"], ["a", "b"])
    k2 = yt_cache.make_key("2026-05-01", "2026-05-07", ["PHI", "KC"], ["b", "a"])
    assert k1 == k2


def test_make_key_changes_with_range():
    k1 = yt_cache.make_key("2026-05-01", "2026-05-07", [], ["a"])
    k2 = yt_cache.make_key("2026-05-01", "2026-05-08", [], ["a"])
    assert k1 != k2


def test_make_key_changes_with_videos():
    k1 = yt_cache.make_key("2026-05-01", "2026-05-07", [], ["a"])
    k2 = yt_cache.make_key("2026-05-01", "2026-05-07", [], ["a", "c"])
    assert k1 != k2


def test_load_missing_returns_none():
    assert yt_cache.load("does-not-exist") is None


def test_save_then_load_round_trips():
    section = {"team_notes": {"KC": {"summary": "x"}}, "transcript_count": 3}
    yt_cache.save("key1", section, meta={"start": "2026-05-01"})
    record = yt_cache.load("key1")
    assert record is not None
    assert record["section"] == section
    assert record["meta"]["start"] == "2026-05-01"
    assert "generated_at" in record


def test_corrupt_cache_file_returns_none(_tmp_cache_dir):
    (_tmp_cache_dir / "bad.json").write_text("{not valid json", encoding="utf-8")
    assert yt_cache.load("bad") is None
