"""RSS Feed Collector.

Polls configurable RSS feeds and returns NewsItem objects.
Feeds are defined in config/sources.yaml — add/remove without touching code.
"""

import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

import feedparser

from config_loader import get_rss_feeds, get_settings, get_data_dir
from models import NewsItem

logger = logging.getLogger(__name__)


def _parse_published(entry) -> Optional[datetime]:
    """Extract a timezone-aware datetime from a feed entry."""
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            try:
                from calendar import timegm
                ts = timegm(parsed)
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except Exception:
                continue
    # Fallback: try raw string
    for attr in ("published", "updated"):
        raw = getattr(entry, attr, None)
        if raw:
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(raw)
            except Exception:
                continue
    return None


def _contains_alias(text_lower: str, alias: str) -> bool:
    """Check whether an alias appears as a whole-word phrase."""
    return bool(re.search(rf"\b{re.escape(alias)}\b", text_lower))


def _detect_teams(text: str, teams_by_abbr: dict) -> list[str]:
    """Detect teams using full names, nicknames, unique cities, and uppercase abbreviations."""
    found = []
    text_lower = text.lower()
    upper_tokens = set(re.findall(r"\b[A-Z]{2,3}\b", text))

    for abbr, team in teams_by_abbr.items():
        parts = team["name"].split()
        nickname = parts[-1].lower() if parts else ""
        aliases = [team["name"].lower(), nickname]

        if any(alias and _contains_alias(text_lower, alias) for alias in aliases):
            found.append(abbr)
            continue

        # Only match abbreviations when they appear explicitly as uppercase tokens,
        # which avoids false positives like CAR in "car crash" or WAS in "was".
        if abbr in upper_tokens:
            found.append(abbr)

    return found


def collect_rss(
    lookback_hours: Optional[int] = None,
    teams_by_abbr: Optional[dict] = None,
) -> list[NewsItem]:
    """Collect news from all configured RSS feeds.

    Args:
        lookback_hours: Only return items published within this window.
                        Defaults to settings.yaml value.
        teams_by_abbr: Dict of team abbreviation -> team info for tagging.

    Returns:
        List of NewsItem objects.
    """
    settings = get_settings()
    if lookback_hours is None:
        lookback_hours = settings["collection"]["lookback_hours"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    feeds = get_rss_feeds()
    delay = settings["collection"].get("request_delay", 2.0)
    items: list[NewsItem] = []

    if teams_by_abbr is None:
        from config_loader import get_teams_by_abbr
        teams_by_abbr = get_teams_by_abbr()

    logger.info("Polling %d RSS feeds (lookback=%dh)...", len(feeds), lookback_hours)

    for feed_config in feeds:
        name = feed_config["name"]
        url = feed_config["url"]
        category = feed_config.get("category", "national")

        try:
            logger.debug("Fetching feed: %s", name)
            parsed = feedparser.parse(url)

            if parsed.bozo and not parsed.entries:
                logger.warning("Feed error for %s: %s", name, parsed.bozo_exception)
                continue

            for entry in parsed.entries:
                pub_date = _parse_published(entry)
                if pub_date is None:
                    pub_date = datetime.now(timezone.utc)

                # Skip old entries
                if pub_date < cutoff:
                    continue

                title = str(entry.get("title", "") or "").strip()
                link = str(entry.get("link", "") or "").strip()
                summary = str(entry.get("summary", "") or "").strip()
                author = str(entry.get("author", "") or "").strip()

                if not title or not link:
                    continue

                # Tag teams mentioned in title + summary
                search_text = f"{title} {summary}"
                teams = _detect_teams(search_text, teams_by_abbr)

                items.append(NewsItem(
                    title=title,
                    url=link,
                    source=name,
                    source_type="rss",
                    published=pub_date,
                    summary=summary,
                    teams=teams,
                    author=author,
                    category=category,
                ))

        except Exception as e:
            logger.error("Failed to fetch feed %s: %s", name, e)

        time.sleep(delay)

    logger.info("Collected %d items from RSS feeds.", len(items))
    return items


def save_rss_results(items: list[NewsItem], date_str: str):
    """Save collected RSS items to a JSON file."""
    import json
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "rss.json"
    data = [item.to_dict() for item in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d RSS items to %s", len(items), path)


if __name__ == "__main__":
    # Quick test: run this file directly to see what feeds return
    logging.basicConfig(level=logging.INFO)
    results = collect_rss()
    for item in results[:10]:
        print(f"[{item.source}] {item.title}")
        if item.teams:
            print(f"  Teams: {', '.join(item.teams)}")
        print(f"  {item.url}")
        print()
    print(f"Total: {len(results)} items")
