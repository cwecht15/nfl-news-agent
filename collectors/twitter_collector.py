"""Twitter/X Collector (DEFERRED).

Uses Nitter RSS bridges to collect tweets from NFL insiders.
This collector is built last — the pipeline works without it.
Set nitter.enabled: true in settings.yaml to activate.

Beat writers and insiders configured in config/sources.yaml.
"""

import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import feedparser

from config_loader import get_beat_writers, get_settings, get_data_dir
from models import NewsItem

logger = logging.getLogger(__name__)


def _try_nitter_instances(handle: str, instances: list[str]) -> Optional[list]:
    """Try multiple Nitter instances to get a user's RSS feed."""
    for base_url in instances:
        url = f"{base_url}/{handle}/rss"
        try:
            feed = feedparser.parse(url)
            if feed.entries:
                return feed.entries
        except Exception as e:
            logger.debug("Nitter instance %s failed for %s: %s", base_url, handle, e)
            continue
    return None


def collect_twitter(lookback_hours: Optional[int] = None) -> list[NewsItem]:
    """Collect tweets from configured insiders via Nitter RSS.

    Returns:
        List of NewsItem objects from Twitter/Nitter.
    """
    settings = get_settings()

    # Check if Twitter collection is enabled
    if not settings.get("nitter", {}).get("enabled", False):
        logger.info("Twitter/Nitter collection is disabled in settings.")
        return []

    instances = settings.get("nitter", {}).get("instances", [])
    if not instances:
        logger.warning("No Nitter instances configured.")
        return []

    if lookback_hours is None:
        lookback_hours = settings["collection"]["lookback_hours"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    writers = get_beat_writers()
    delay = settings["collection"].get("request_delay", 2.0)
    items: list[NewsItem] = []

    logger.info("Polling %d Twitter accounts via Nitter...", len(writers))

    for writer in writers:
        handle = writer["handle"]
        name = writer["name"]
        outlet = writer.get("outlet", "")
        writer_teams = writer.get("teams", [])

        entries = _try_nitter_instances(handle, instances)
        if entries is None:
            logger.warning("Could not fetch tweets for @%s", handle)
            time.sleep(delay)
            continue

        for entry in entries:
            # Parse date
            pub_date = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                from calendar import timegm
                ts = timegm(entry.published_parsed)
                pub_date = datetime.fromtimestamp(ts, tz=timezone.utc)

            if pub_date is None:
                pub_date = datetime.now(timezone.utc)

            if pub_date < cutoff:
                continue

            title = entry.get("title", "").strip()
            link = entry.get("link", "").strip()

            if not title:
                continue

            items.append(NewsItem(
                title=title,
                url=link,
                source=f"Twitter/@{handle}",
                source_type="twitter",
                published=pub_date,
                summary=title,
                teams=writer_teams,
                author=f"{name} ({outlet})" if outlet else name,
                category="news",
            ))

        time.sleep(delay)

    logger.info("Collected %d items from Twitter/Nitter.", len(items))
    return items


def save_twitter_results(items: list[NewsItem], date_str: str):
    """Save collected Twitter items to a JSON file."""
    import json
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "twitter.json"
    data = [item.to_dict() for item in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d Twitter items to %s", len(items), path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = collect_twitter()
    for item in results[:10]:
        print(f"[@{item.author}] {item.title}")
        print(f"  {item.url}")
        print()
    print(f"Total: {len(results)} items")
