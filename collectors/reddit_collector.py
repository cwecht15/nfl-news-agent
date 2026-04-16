"""Reddit r/nfl Collector.

Fetches recent posts from r/nfl/new via RSS. Filters out highlights,
memes, and discussion threads to keep only news-relevant posts.

Most breaking NFL news from Twitter insiders (Schefter, Rapoport, etc.)
gets posted to r/nfl within minutes, making this an effective free
proxy for Twitter coverage.
"""

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import feedparser
import requests

from config_loader import get_settings, get_data_dir, get_teams_by_abbr
from collectors.rss_collector import _detect_teams
from models import NewsItem

logger = logging.getLogger(__name__)

USER_AGENT = "NFL-News-Agent/1.0 (personal news aggregator)"

# Posts matching these patterns are skipped
SKIP_PATTERNS = [
    re.compile(r"^\[highlights?\]", re.IGNORECASE),
    re.compile(r"^(game|post.?game|pre.?game) thread", re.IGNORECASE),
    re.compile(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+talk\s+thread", re.IGNORECASE),
    re.compile(r"^(daily|weekly|free)\s+talk", re.IGNORECASE),
    re.compile(r"^water cooler", re.IGNORECASE),
    re.compile(r"^shitpost saturday", re.IGNORECASE),
    re.compile(r"^(whose|who'?s) line", re.IGNORECASE),
    re.compile(r"^judgement free", re.IGNORECASE),
    re.compile(r"^meme monday", re.IGNORECASE),
    re.compile(r"^talko tuesday", re.IGNORECASE),
]

# Reporter/insider bracket pattern: [Schefter], [Rapoport], etc.
REPORTER_PATTERN = re.compile(r"^\[([A-Z][a-zA-Z\s\-']+)\]")


def _should_skip(title: str) -> bool:
    """Return True if a post should be filtered out."""
    for pattern in SKIP_PATTERNS:
        if pattern.search(title):
            return True
    return False


def _extract_reporter(title: str) -> Optional[str]:
    """Extract reporter name from bracket-prefixed titles like [Schefter]."""
    m = REPORTER_PATTERN.match(title)
    if m:
        return m.group(1).strip()
    return None


def _clean_title(title: str) -> str:
    """Remove the [Reporter] prefix for a cleaner headline."""
    return REPORTER_PATTERN.sub("", title).strip()


def collect_reddit(lookback_hours: Optional[int] = None) -> list[NewsItem]:
    """Collect news posts from r/nfl/new via RSS.

    Returns:
        List of NewsItem objects from Reddit.
    """
    settings = get_settings()

    if not settings.get("reddit", {}).get("enabled", True):
        logger.info("Reddit collection is disabled in settings.")
        return []

    if lookback_hours is None:
        lookback_hours = settings["collection"]["lookback_hours"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    delay = settings["collection"].get("request_delay", 2.0)
    teams_by_abbr_map = get_teams_by_abbr()
    items: list[NewsItem] = []

    try:
        resp = requests.get(
            "https://www.reddit.com/r/nfl/new/.rss",
            headers={"User-Agent": USER_AGENT},
            timeout=settings["collection"].get("request_timeout", 30),
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error("Failed to fetch r/nfl RSS: %s", e)
        return []

    feed = feedparser.parse(resp.text)
    logger.info("Fetched %d posts from r/nfl/new.", len(feed.entries))

    skipped = 0
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        if not title:
            continue

        if _should_skip(title):
            skipped += 1
            continue

        # Parse published date
        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            from calendar import timegm
            ts = timegm(entry.published_parsed)
            pub_date = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
            from calendar import timegm
            ts = timegm(entry.updated_parsed)
            pub_date = datetime.fromtimestamp(ts, tz=timezone.utc)

        if pub_date is None:
            pub_date = datetime.now(timezone.utc)

        if pub_date < cutoff:
            continue

        # Extract reporter name if present
        reporter = _extract_reporter(title)
        clean = _clean_title(title) if reporter else title

        # Try to get the external link (not the Reddit comments link)
        link = entry.get("link", "")
        content_html = ""
        if entry.get("content"):
            content_html = entry["content"][0].get("value", "")
        elif entry.get("summary"):
            content_html = entry["summary"]

        # Reddit entries contain [link] pointing to external URL
        ext_match = re.search(r'href="(https?://(?!www\.reddit\.com)[^"]+)">\[link\]', content_html)
        external_url = ext_match.group(1) if ext_match else ""

        source_label = f"r/nfl via @{reporter}" if reporter else "r/nfl"
        detected_teams = _detect_teams(title, teams_by_abbr_map)

        items.append(NewsItem(
            title=clean,
            url=external_url or link,
            source=source_label,
            source_type="reddit",
            published=pub_date,
            summary=clean,
            teams=detected_teams,
            author=reporter or "",
            category="news",
        ))

    logger.info(
        "Collected %d news items from r/nfl (skipped %d highlights/threads).",
        len(items), skipped,
    )
    return items


def save_reddit_results(items: list[NewsItem], date_str: str):
    """Save collected Reddit items to a JSON file."""
    import json
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "reddit.json"
    data = [item.to_dict() for item in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d Reddit items to %s", len(items), path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = collect_reddit()
    for item in results:
        reporter = f" (@{item.author})" if item.author else ""
        print(f"[{item.source}]{reporter} {item.title}")
        if item.url:
            print(f"  {item.url}")
        print()
    print(f"Total: {len(results)} items")
