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

    # Lazy import to avoid circular dependency at module load.
    from collectors.web_scraper import fetch_article_body, _matches_generic_domain
    import requests as _requests
    enrich_session = _requests.Session()
    enrich_session.headers.update({
        "User-Agent": settings["collection"].get("user_agent", "NFL-News-Agent/1.0")
    })
    enrich_timeout = int(settings["collection"].get("request_timeout", 30))

    for feed_config in feeds:
        name = feed_config["name"]
        url = feed_config["url"]
        category = feed_config.get("category", "national")
        # Optional: pre-tag every item from this feed with these team
        # abbreviations. Useful for single-team blogs (e.g. SB Nation
        # team sites) where alias detection would otherwise miss items
        # whose titles only mention player names.
        feed_teams = list(feed_config.get("teams") or [])

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

                # Some feeds (notably SB Nation team blogs) include the
                # full article body in `content:encoded`. Use it when
                # present so team-note prompts get player-level detail.
                full_text = ""
                content_list = entry.get("content") or []
                if content_list:
                    raw = str(content_list[0].get("value", "") or "")
                    if raw:
                        cleaned = re.sub(r"<[^>]+>", " ", raw)
                        cleaned = re.sub(r"\s+", " ", cleaned).strip()
                        if len(cleaned) > 8000:
                            cleaned = cleaned[:8000].rsplit(" ", 1)[0] + "…"
                        full_text = cleaned

                # Tag teams mentioned in title + summary + body
                search_text = f"{title} {summary} {full_text}"
                detected = _detect_teams(search_text, teams_by_abbr)
                # Union of feed-level pre-tags and detected teams.
                teams = list(dict.fromkeys(feed_teams + detected))

                # If feed didn't carry body content but the link points to
                # a known-extractable site (ESPN, CBS, NBC, Yahoo),
                # fetch the article body to give team-notes prompts the
                # same depth they get from SI/SBN/Athletic.
                if not full_text and _matches_generic_domain(link):
                    full_text = fetch_article_body(
                        enrich_session, link, enrich_timeout
                    )

                items.append(NewsItem(
                    title=title,
                    url=link,
                    source=name,
                    source_type="rss",
                    published=pub_date,
                    summary=summary,
                    full_text=full_text,
                    teams=teams,
                    author=author,
                    category=category,
                ))

        except Exception as e:
            logger.error("Failed to fetch feed %s: %s", name, e)

        time.sleep(delay)

    logger.info("Collected %d items from RSS feeds.", len(items))
    return items


# ESPN API team IDs (abbreviation -> ESPN numeric ID)
ESPN_TEAM_IDS = {
    "ARI": 22, "ATL": 1, "BAL": 33, "BUF": 2, "CAR": 29, "CHI": 3,
    "CIN": 4, "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9,
    "HOU": 34, "IND": 11, "JAX": 30, "KC": 12, "LAC": 24, "LAR": 14,
    "LV": 13, "MIA": 15, "MIN": 16, "NE": 17, "NO": 18, "NYG": 19,
    "NYJ": 20, "PHI": 21, "PIT": 23, "SEA": 26, "SF": 25, "TB": 27,
    "TEN": 10, "WAS": 28,
}


def collect_espn_team_news(
    lookback_hours: Optional[int] = None,
    teams_by_abbr: Optional[dict] = None,
) -> list[NewsItem]:
    """Collect team-specific news from ESPN's API for all 32 teams.

    Returns NewsItem objects with team pre-tagged from the API.
    """
    import requests

    settings = get_settings()
    if not settings.get("espn_team_feeds", {}).get("enabled", True):
        logger.info("ESPN team feeds disabled in settings.")
        return []

    if lookback_hours is None:
        lookback_hours = settings["collection"]["lookback_hours"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    delay = settings["collection"].get("request_delay", 2.0)
    user_agent = settings["collection"].get("user_agent", "NFL-News-Agent/1.0")

    if teams_by_abbr is None:
        from config_loader import get_teams_by_abbr
        teams_by_abbr = get_teams_by_abbr()

    items: list[NewsItem] = []
    seen_ids: set[int] = set()  # dedupe across teams (articles appear for multiple teams)

    # Enrichment session for fetching article bodies.
    from collectors.web_scraper import fetch_article_body
    import requests as _requests
    enrich_session = _requests.Session()
    enrich_session.headers.update({"User-Agent": user_agent})

    logger.info("Collecting ESPN team-specific news for %d teams...", len(ESPN_TEAM_IDS))

    for abbr, espn_id in ESPN_TEAM_IDS.items():
        try:
            resp = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/news?team={espn_id}",
                headers={"User-Agent": user_agent},
                timeout=settings["collection"].get("request_timeout", 30),
            )
            resp.raise_for_status()
            data = resp.json()

            for article in data.get("articles", []):
                article_id = article.get("id")
                if article_id in seen_ids:
                    continue
                seen_ids.add(article_id)

                headline = article.get("headline", "").strip()
                if not headline:
                    continue

                # Parse published date
                pub_str = article.get("published", "")
                try:
                    pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pub_date = datetime.now(timezone.utc)

                if pub_date < cutoff:
                    continue

                description = article.get("description", "").strip()

                # Extract teams from API categories
                api_teams = []
                for cat in article.get("categories", []):
                    if cat.get("type") == "team":
                        # Map ESPN abbreviation to our abbreviation
                        team_desc = cat.get("description", "")
                        for our_abbr, team_info in teams_by_abbr.items():
                            if team_info["name"].lower() in team_desc.lower():
                                api_teams.append(our_abbr)
                                break

                # Fallback: use the team we queried for
                if not api_teams:
                    api_teams = [abbr]

                # Also detect from text for any missed teams
                text_teams = _detect_teams(f"{headline} {description}", teams_by_abbr)
                all_teams = list(dict.fromkeys(api_teams + text_teams))

                article_url = article.get("links", {}).get("web", {}).get("href", "")
                full_body = ""
                if article_url:
                    full_body = fetch_article_body(
                        enrich_session, article_url,
                        timeout=settings["collection"].get("request_timeout", 30),
                    )

                items.append(NewsItem(
                    title=headline,
                    url=article_url,
                    source=f"ESPN {abbr}",
                    source_type="espn_api",
                    published=pub_date,
                    summary=description,
                    full_text=full_body,
                    teams=all_teams,
                    category="news",
                ))

        except Exception as e:
            logger.warning("ESPN API failed for %s: %s", abbr, e)

        time.sleep(delay / 2)  # lighter delay for API calls

    logger.info("Collected %d unique articles from ESPN team feeds.", len(items))
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
