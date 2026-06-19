"""Collect tweets from X/Twitter lists via the TwitterAPI.io REST API.

API-only and CI-safe (just an API key in an env var), so unlike the YouTube
tool this can run on GitHub Actions. Mirrors the Podcast tool's shape:

  data/raw/<date>/twitter.json   — tweets as NewsItem records (merged by URL)
  data/twitter_seen.json         — dedup set of tweet IDs, updated in place

Provider: TwitterAPI.io (https://docs.twitterapi.io). It's a third-party
scraper API (not X's sanctioned API) — far cheaper ($0.15 / 1,000 tweets) and
needs no OAuth, but is ToS-gray and may need upkeep if X changes things.

Endpoint: ``GET https://api.twitterapi.io/twitter/list/tweets``
  - header ``X-API-Key``
  - params ``listId``, ``cursor`` (empty string for the first page),
    ``includeReplies``, ``sinceTime`` (Unix seconds — server-side lookback)
  - returns ``{"tweets": [...], "has_next_page": bool, "next_cursor": str}``

Each tweet maps to a :class:`models.NewsItem` (``source_type="twitter"``), so
the existing summarizer / dashboard plumbing can consume it unchanged.

Lists are configured in ``config/sources.yaml`` under ``twitter_lists:`` and the
behaviour knobs live in ``config/settings.yaml`` under ``twitter:``. The
``nitter:`` block and the old Nitter code path are retired — Nitter is dead.
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from collectors.rss_collector import detect_teams_for_item
from config_loader import (
    get_data_dir,
    get_settings,
    get_teams_by_abbr,
    get_twitter_api_key,
    get_twitter_lists,
)
from models import NewsItem

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.twitterapi.io/twitter/list/tweets"
_USER_AGENT = "NFL-News-Agent/1.0 (+twitter list collector)"
# TwitterAPI.io's createdAt is the classic Twitter format, e.g.
# "Tue Dec 10 07:00:30 +0000 2024".
_CREATED_FMT = "%a %b %d %H:%M:%S %z %Y"


# ──────────────────────────────────────────────────────────────────────
# Seen-tweet dedup (parity with data/podcast_seen.json)
# ──────────────────────────────────────────────────────────────────────

def _seen_path() -> Path:
    return get_data_dir("") / "twitter_seen.json"


def _load_seen() -> set[str]:
    path = _seen_path()
    if not path.exists():
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f).get("seen_ids", []))
    except Exception:
        return set()


def _save_seen(seen: set[str]) -> None:
    with open(_seen_path(), "w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen)}, f, indent=2, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────

def _parse_created(value: str) -> datetime:
    try:
        return datetime.strptime(str(value).strip(), _CREATED_FMT)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


def _fetch_page(
    list_id: str,
    cursor: str,
    api_key: str,
    since_unix: int,
    include_replies: bool,
    timeout: int,
) -> Optional[dict]:
    """Fetch one page of a list timeline. Returns the parsed JSON or None."""
    params = {
        "listId": list_id,
        "cursor": cursor,
        "includeReplies": "true" if include_replies else "false",
        "sinceTime": str(since_unix),
    }
    try:
        resp = requests.get(
            _BASE_URL,
            params=params,
            headers={"X-API-Key": api_key, "User-Agent": _USER_AGENT},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("TwitterAPI.io request failed for list %s: %s", list_id, e)
        return None


def _tweet_to_item(
    tweet: dict,
    list_cfg: dict,
    teams_by_abbr: dict,
) -> Optional[NewsItem]:
    text = str(tweet.get("text", "") or "").strip()
    url = str(tweet.get("url", "") or "").strip()
    if not text or not url:
        return None

    author = tweet.get("author") or {}
    user_name = str(author.get("userName", "") or "").strip()
    display = str(author.get("name", "") or "").strip() or user_name
    author_label = f"{display} (@{user_name})" if user_name else display

    pub = _parse_created(tweet.get("createdAt", ""))

    # A team-tagged list (e.g. a single-team beat list) pre-tags every tweet;
    # an untagged league-wide insider list detects teams from the tweet text.
    feed_teams = list_cfg.get("team")
    feed_teams = [feed_teams] if isinstance(feed_teams, str) and feed_teams else (
        feed_teams if isinstance(feed_teams, list) else []
    )
    teams = detect_teams_for_item(text, "", "", feed_teams, teams_by_abbr)

    list_name = str(list_cfg.get("name", "") or "Twitter list").strip()
    return NewsItem(
        title=" ".join(text.split()),
        url=url,
        source=f"Twitter/{list_name}",
        source_type="twitter",
        published=pub,
        summary=text,
        full_text=text,
        teams=teams,
        author=author_label,
        category="news",
    )


# ──────────────────────────────────────────────────────────────────────
# Per-list processing
# ──────────────────────────────────────────────────────────────────────

def _process_list(
    list_cfg: dict,
    api_key: str,
    cutoff: datetime,
    max_tweets: int,
    include_replies: bool,
    timeout: int,
    delay: float,
    seen_snapshot: frozenset,
    teams_by_abbr: dict,
) -> tuple[list[NewsItem], set[str]]:
    list_id = str(list_cfg.get("list_id", "") or "").strip()
    name = str(list_cfg.get("name", "") or "").strip()
    if not list_id:
        return [], set()

    since_unix = int(cutoff.timestamp())
    cursor = ""
    items: list[NewsItem] = []
    new_seen: set[str] = set()

    while len(items) < max_tweets:
        page = _fetch_page(
            list_id, cursor, api_key, since_unix, include_replies, timeout
        )
        if not page:
            break

        tweets = page.get("tweets") or []
        if not tweets:
            break

        stop = False
        for tweet in tweets:
            tid = str(tweet.get("id", "") or "").strip()
            if not tid or tid in seen_snapshot or tid in new_seen:
                continue

            pub = _parse_created(tweet.get("createdAt", ""))
            if pub < cutoff:
                # Results are newest-first, so once we cross the cutoff the
                # rest of the page (and later pages) are older too.
                stop = True
                break

            new_seen.add(tid)
            item = _tweet_to_item(tweet, list_cfg, teams_by_abbr)
            if item:
                items.append(item)
            if len(items) >= max_tweets:
                stop = True
                break

        if stop or not page.get("has_next_page"):
            break
        cursor = str(page.get("next_cursor", "") or "")
        if not cursor:
            break
        if delay:
            time.sleep(delay)

    logger.info("%s [%s]: %d new tweet(s)", name, list_id, len(items))
    return items, new_seen


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────

def collect_twitter_list(
    date_str: str,
    lookback_hours: Optional[int] = None,
) -> list[NewsItem]:
    """Collect recent tweets across all configured Twitter lists.

    Dedup is by tweet ID via ``data/twitter_seen.json``; a per-run cap and the
    server-side ``sinceTime`` lookback keep the (per-tweet billed) cost bounded.
    """
    settings = get_settings()
    twitter_cfg = settings.get("twitter", {}) or {}
    if not twitter_cfg.get("enabled", False):
        logger.info("Twitter collection disabled in settings (twitter.enabled).")
        return []

    lists = [l for l in get_twitter_lists() if str(l.get("list_id", "")).strip()]
    if not lists:
        logger.warning("No Twitter lists configured under sources.yaml twitter_lists.")
        return []

    try:
        api_key = get_twitter_api_key()
    except ValueError as e:
        logger.error("%s", e)
        return []

    collection = settings.get("collection", {})
    effective_lookback = (
        lookback_hours if lookback_hours is not None
        else int(twitter_cfg.get("lookback_hours", 28))
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_lookback)
    max_tweets = int(twitter_cfg.get("max_per_run", 500))
    include_replies = bool(twitter_cfg.get("include_replies", False))
    timeout = int(collection.get("request_timeout", 30))
    delay = float(collection.get("request_delay", 1.0))
    teams_by_abbr = get_teams_by_abbr()

    seen = _load_seen()
    snapshot = frozenset(seen)

    logger.info(
        "Scanning %d Twitter list(s) (lookback=%dh, max=%d, replies=%s)...",
        len(lists), effective_lookback, max_tweets, include_replies,
    )

    all_items: list[NewsItem] = []
    for cfg in lists:
        try:
            items, new_seen = _process_list(
                cfg, api_key, cutoff, max_tweets, include_replies,
                timeout, delay, snapshot, teams_by_abbr,
            )
        except Exception as e:
            logger.error("Twitter list %s failed: %s", cfg.get("name"), e)
            continue
        all_items.extend(items)
        seen.update(new_seen)

    _save_seen(seen)
    logger.info(
        "Collected %d tweet(s) (%d total seen).", len(all_items), len(seen),
    )
    return all_items


def save_twitter_results(items: list[NewsItem], date_str: str) -> Path:
    """Merge new tweets into data/raw/<date>/twitter.json (union by URL).

    MERGE, not overwrite: ``collect_twitter_list`` only returns tweets not yet
    in ``twitter_seen.json``, so a second run on the same date would otherwise
    rewrite the file with just the handful of new tweets and drop everything
    collected earlier that day. Unioning by tweet URL keeps same-day re-runs
    (e.g. a manual run + the CI cron) additive and loss-free.
    """
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "twitter.json"
    by_url: dict[str, dict] = {}
    if path.exists():
        try:
            for rec in json.loads(path.read_text(encoding="utf-8")):
                url = rec.get("url")
                if url:
                    by_url[url] = rec
        except Exception:
            pass
    for item in items:
        rec = item.to_dict()
        if rec.get("url"):
            by_url[rec["url"]] = rec
    merged = list(by_url.values())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    logger.info(
        "Saved %d tweet records (%d new this run) to %s",
        len(merged), len(items), path,
    )
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = collect_twitter_list(today)
    for item in results[:10]:
        print(f"[{item.author}] {item.title[:120]}")
        print(f"  {item.url}  teams={item.teams}")
        print()
    print(f"Total: {len(results)} tweets")
