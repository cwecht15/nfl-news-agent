"""FantasyPoints articles collector.

Pulls recent NFL articles from the FantasyPoints v2 API and returns
NewsItem objects for the dedicated FantasyPoints section. Articles do
NOT enter the news dedup pool — they're rendered in their own section
via processing/fp_section.py.

Required env (set in .env or as GHA secrets):
    FANTASYPOINTS_AUTH    Value of the Authorization header (no "Bearer " prefix).
    FANTASYPOINTS_SOURCE  Value of the x-source header.
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from config_loader import (
    get_data_dir,
    get_fantasypoints_auth,
    get_fantasypoints_source,
    get_settings,
    get_teams_by_abbr,
)
from models import NewsItem

logger = logging.getLogger(__name__)

API_URL = "https://api.fantasypoints.com/v2/articles/nfl/recent"
SITE_BASE = "https://www.fantasypoints.com"


def _first(d: dict, *keys: str) -> str:
    """Return the first non-empty string value at any of the given keys."""
    for key in keys:
        val = d.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _parse_published(raw: str) -> Optional[datetime]:
    """Best-effort parse of varied datetime string formats."""
    if not raw:
        return None
    raw = raw.strip()

    # ISO 8601 with optional Z
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # RFC 2822 fallback
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _normalize_body(raw: str) -> str:
    """Return article body text suitable for the LLM prompt.

    The API returns Markdown for most articles. When tagged HTML is
    present (legacy/imported content), strip tags. Otherwise keep the
    Markdown intact — paragraph breaks help the model find structure.
    """
    if not raw:
        return ""
    if re.search(r"<[a-zA-Z]+[\s/>]", raw):
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text
    return raw.strip()


_FP_PATH_RE = re.compile(r"^/nfl/articles/([^/]+)/?$")


def _build_article_url(raw_url: str, published: Optional[datetime]) -> str:
    """Promote the API's site-relative path to a working article URL.

    The API returns paths like `/nfl/articles/<slug>`, but the public
    site is a hash-routed SPA whose canonical article URL is
    `https://www.fantasypoints.com/nfl/articles/<year>/<slug>#/` — the
    bare path without the year or trailing `#/` lands on the generic
    articles index. We inject the publish year and the hash route.

    Falls back to a plain absolute URL when the API path does not match
    the expected `/nfl/articles/<slug>` shape (defensive — older or
    nonstandard paths are still rendered as clickable links).
    """
    if not raw_url:
        return ""
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    path = raw_url if raw_url.startswith("/") else "/" + raw_url
    match = _FP_PATH_RE.match(path)
    if match and published is not None:
        slug = match.group(1)
        return f"{SITE_BASE}/nfl/articles/{published.year}/{slug}#/"

    return SITE_BASE + path


def _extract_author(article: dict) -> str:
    """Pull a human-readable author name from varied API shapes."""
    direct = _first(article, "author_name", "byline", "writer")
    if direct:
        return direct

    author = article.get("author")
    if isinstance(author, dict):
        for key in ("name", "full_name", "display_name"):
            val = str(author.get(key, "") or "").strip()
            if val:
                return val
        first = str(author.get("first_name", "") or "").strip()
        last = str(author.get("last_name", "") or "").strip()
        joined = f"{first} {last}".strip()
        if joined:
            return joined
    if isinstance(author, str) and author.strip():
        return author.strip()

    authors = article.get("authors")
    if isinstance(authors, list) and authors:
        names = []
        for a in authors:
            if isinstance(a, dict):
                names.append(_first(a, "name", "full_name", "display_name"))
            elif isinstance(a, str):
                names.append(a.strip())
        names = [n for n in names if n]
        if names:
            return ", ".join(names)

    return ""


def _contains_alias(text_lower: str, alias: str) -> bool:
    return bool(re.search(rf"\b{re.escape(alias)}\b", text_lower))


def _detect_teams(text: str, teams_by_abbr: dict) -> list[str]:
    """Detect NFL teams referenced in `text`. Mirrors rss_collector."""
    found: list[str] = []
    text_lower = text.lower()
    upper_tokens = set(re.findall(r"\b[A-Z]{2,3}\b", text))

    for abbr, team in teams_by_abbr.items():
        parts = team["name"].split()
        nickname = parts[-1].lower() if parts else ""
        aliases = [team["name"].lower(), nickname]

        if any(alias and _contains_alias(text_lower, alias) for alias in aliases):
            found.append(abbr)
            continue

        if abbr in upper_tokens:
            found.append(abbr)

    return found


def _articles_from_payload(payload: Any) -> list[dict]:
    """Pull the article list from whatever wrapping shape the API returns.

    The current FantasyPoints v2 API nests the list at
    `content.documents.values`. Older / alternate shapes that have
    `articles`, `data`, `items`, or `results` at the top level are
    also tolerated.
    """
    if isinstance(payload, list):
        return [a for a in payload if isinstance(a, dict)]

    if not isinstance(payload, dict):
        return []

    # FantasyPoints v2 envelope.
    content = payload.get("content")
    if isinstance(content, dict):
        documents = content.get("documents")
        if isinstance(documents, dict):
            values = documents.get("values")
            if isinstance(values, list):
                return [a for a in values if isinstance(a, dict)]
        # Fall through if `content` exists but in a different shape.

    # Generic JSON envelopes seen in similar APIs.
    for key in ("articles", "data", "items", "results"):
        val = payload.get(key)
        if isinstance(val, list):
            return [a for a in val if isinstance(a, dict)]
        if isinstance(val, dict):
            for inner in ("articles", "data", "items", "results", "values"):
                inner_val = val.get(inner)
                if isinstance(inner_val, list):
                    return [a for a in inner_val if isinstance(a, dict)]

    return [payload]  # single-article fallback


def collect_fantasypoints(
    lookback_hours: Optional[int] = None,
    limit: Optional[int] = None,
    teams_by_abbr: Optional[dict] = None,
) -> list[NewsItem]:
    """Fetch recent FantasyPoints NFL articles.

    Args:
        lookback_hours: Window in hours. Falls back to settings.fantasypoints.lookback_hours.
        limit: Max articles after filtering. Falls back to settings.fantasypoints.max_articles.
        teams_by_abbr: Optional pre-loaded team map.

    Returns:
        list[NewsItem] (newest first, capped at limit). Empty list on any failure.
    """
    settings = get_settings()
    fp_cfg = settings.get("fantasypoints", {}) or {}

    if not fp_cfg.get("enabled", True):
        logger.info("FantasyPoints collector disabled in settings.yaml.")
        return []

    if lookback_hours is None:
        lookback_hours = int(fp_cfg.get("lookback_hours", 24))
    if limit is None:
        limit = int(fp_cfg.get("max_articles", 15))

    try:
        auth = get_fantasypoints_auth()
        source_header = get_fantasypoints_source()
    except ValueError as e:
        logger.warning("FantasyPoints credentials missing: %s", e)
        return []

    headers = {
        "Authorization": auth,
        "x-source": source_header,
        "User-Agent": settings["collection"].get("user_agent", "NFL-News-Agent/1.0"),
        "Accept": "application/json",
    }
    timeout = int(settings["collection"].get("request_timeout", 30))

    logger.info(
        "Fetching FantasyPoints articles (lookback=%dh, cap=%d)",
        lookback_hours,
        limit,
    )
    try:
        response = requests.get(API_URL, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.warning("FantasyPoints API request failed: %s", e)
        return []

    try:
        payload = response.json()
    except ValueError as e:
        logger.warning("FantasyPoints API returned non-JSON: %s", e)
        return []

    articles = _articles_from_payload(payload)
    if not articles:
        logger.info("FantasyPoints API returned no articles.")
        return []

    if teams_by_abbr is None:
        teams_by_abbr = get_teams_by_abbr()

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items: list[NewsItem] = []

    for article in articles:
        title = _first(article, "title", "headline", "name")
        raw_url = _first(article, "url", "link", "permalink", "canonical_url")
        if not title or not raw_url:
            continue

        published_raw = _first(
            article,
            "publishedDate",
            "published_at",
            "publishedAt",
            "published",
            "pub_date",
            "date",
            "createdAt",
            "created_at",
        )
        published = _parse_published(published_raw) or datetime.now(timezone.utc)
        if published < cutoff:
            continue

        url = _build_article_url(raw_url, published)

        body_raw = _first(article, "body", "content", "html", "body_html", "text")
        full_text = _normalize_body(body_raw)
        excerpt = _first(article, "excerpt", "summary", "description", "subtitle")
        if not excerpt and full_text:
            excerpt = full_text[:400].strip()

        author = _extract_author(article)
        teams = _detect_teams(f"{title}. {excerpt} {full_text[:2000]}", teams_by_abbr)

        items.append(NewsItem(
            title=title,
            url=url,
            source="FantasyPoints",
            source_type="fantasypoints",
            published=published,
            summary=excerpt,
            full_text=full_text,
            teams=teams,
            author=author,
            category="news",
        ))

    items.sort(key=lambda it: it.published, reverse=True)
    if limit > 0:
        items = items[:limit]

    logger.info("FantasyPoints: %d articles within window.", len(items))
    return items


def save_fantasypoints_results(items: list[NewsItem], date_str: str) -> Path:
    """Save collected FantasyPoints items to data/raw/<date>/fantasypoints.json."""
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "fantasypoints.json"
    data = [item.to_dict() for item in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d FantasyPoints items to %s", len(items), path)
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = collect_fantasypoints()
    for item in results[:10]:
        print(f"[{item.author or '?'}] {item.title}")
        if item.teams:
            print(f"  Teams: {', '.join(item.teams)}")
        print(f"  {item.url}")
        print()
    print(f"Total: {len(results)} items")
