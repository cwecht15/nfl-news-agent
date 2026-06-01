"""Shared pytest fixtures and path setup.

Puts the project root on sys.path so tests can import the same way the
runtime does (`from processing... import`, `import scripts.snapshot_projections`).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import NewsItem


@pytest.fixture
def make_item():
    """Factory for NewsItem with sensible defaults.

    Usage: make_item("Title", source="ESPN", category="transaction", teams=["KC"])
    """
    def _make(
        title,
        source="ESPN NFL",
        summary="",
        category="news",
        teams=None,
        published=None,
        url="https://example.com",
        source_type="rss",
    ):
        return NewsItem(
            title=title,
            url=url,
            source=source,
            source_type=source_type,
            published=published or datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            summary=summary,
            teams=list(teams or []),
            category=category,
        )

    return _make


@pytest.fixture
def fake_teams_by_abbr():
    """A minimal teams_by_abbr map covering the edge cases we care about:

    - CAR (Carolina) — the abbreviation collides with the word "car"
    - WAS (Washington) — collides with the word "was"
    - KC / SF — multi-word names with distinctive nicknames
    """
    return {
        "CAR": {"name": "Carolina Panthers"},
        "WAS": {"name": "Washington Commanders"},
        "KC": {"name": "Kansas City Chiefs"},
        "SF": {"name": "San Francisco 49ers"},
        "PHI": {"name": "Philadelphia Eagles"},
        # Mirror the real MIN over-tagging failure (Vikings tagged onto
        # Bills/Patriots articles via body mentions).
        "MIN": {"name": "Minnesota Vikings"},
        "BUF": {"name": "Buffalo Bills"},
        "NE": {"name": "New England Patriots"},
    }
