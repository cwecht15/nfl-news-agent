"""Tests for processing.quality_filter.filter_news_items.

The filter reads config via get_settings(); we monkeypatch that so the
tests pin behavior to a known config instead of whatever is in
config/settings.yaml today.
"""

import processing.quality_filter as qf


CONFIG = {
    "content_filter": {
        "enabled": True,
        "drop_patterns": [
            r"\b(vote|voting)\b",
            r"\btrivia\b",
            r"\b(jersey|uniform) reveal\b",
        ],
        "mock_draft_keep_years": ["2026", "2027"],
    }
}


def _patch(monkeypatch, cfg=CONFIG):
    monkeypatch.setattr(qf, "get_settings", lambda: cfg)


def test_disabled_filter_keeps_everything(monkeypatch, make_item):
    monkeypatch.setattr(qf, "get_settings", lambda: {"content_filter": {"enabled": False}})
    items = [make_item("Vote for the best uniform"), make_item("Trivia time")]
    kept, dropped = qf.filter_news_items(items)
    assert len(kept) == 2
    assert dropped == []


def test_drops_voting_and_trivia(monkeypatch, make_item):
    _patch(monkeypatch)
    items = [
        make_item("Fans vote on the top QB"),
        make_item("NFL trivia: name this player"),
        make_item("Chiefs sign WR Marquise Brown"),  # legit, kept
    ]
    kept, dropped = qf.filter_news_items(items)
    kept_titles = {i.title for i in kept}
    assert "Chiefs sign WR Marquise Brown" in kept_titles
    assert len(dropped) == 2


def test_jersey_reveal_dropped(monkeypatch, make_item):
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("Jets unveil new jersey reveal")])
    assert kept == []
    assert len(dropped) == 1


def test_mock_draft_current_year_kept(monkeypatch, make_item):
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("2026 NFL Mock Draft: first round")])
    assert len(kept) == 1
    assert dropped == []


def test_mock_draft_offcycle_year_dropped(monkeypatch, make_item):
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("2030 NFL Mock Draft projection")])
    assert kept == []
    assert len(dropped) == 1


def test_mock_draft_no_year_dropped(monkeypatch, make_item):
    """A bare 'mock draft' title with no keep-year mentioned is fluff."""
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("Way-too-early mock draft")])
    assert kept == []
    assert len(dropped) == 1


def test_case_insensitive(monkeypatch, make_item):
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("FANS VOTE on the worst rule")])
    assert kept == []


def test_empty_title_is_kept(monkeypatch, make_item):
    _patch(monkeypatch)
    kept, dropped = qf.filter_news_items([make_item("")])
    assert len(kept) == 1
