"""Web scraper.

Scrapes configured web sources including:
- NFL.com transactions
- NFL.com injuries
- The Athletic NFL hub (with cookies-based auth)
"""

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from bs4 import BeautifulSoup

from collectors.rss_collector import _detect_teams
from config_loader import (
    get_athletic_cookies_path,
    get_data_dir,
    get_settings,
    get_teams_by_abbr,
    get_web_sources,
)
from models import NewsItem

logger = logging.getLogger(__name__)
LAST_WEB_SOURCE_STATUS: dict[str, dict] = {}


def _get_session(settings: dict) -> requests.Session:
    """Create a requests session with configured headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": settings["collection"].get(
            "user_agent", "NFL-News-Agent/1.0"
        ),
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def _reset_source_status():
    """Clear source status from the previous web collection run."""
    LAST_WEB_SOURCE_STATUS.clear()


def _set_source_status(
    source_name: str,
    status: str,
    message: str,
    severity: str = "info",
    **details,
):
    """Store structured status for a web source."""
    LAST_WEB_SOURCE_STATUS[source_name] = {
        "source": source_name,
        "status": status,
        "severity": severity,
        "message": message,
        **details,
    }


def get_last_web_source_status() -> dict[str, dict]:
    """Return structured source status from the latest web collection run."""
    return {
        source: dict(payload)
        for source, payload in LAST_WEB_SOURCE_STATUS.items()
    }


def _load_cookies_file(session: requests.Session, path: Path):
    """Load Netscape-format cookies into a requests session."""
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    session.cookies.update(jar)


def _inspect_cookie_freshness(path: Path) -> dict:
    """Inspect Athletic/NYT cookie expiry timestamps from a cookies file."""
    jar = MozillaCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)

    relevant = [
        cookie for cookie in jar
        if any(
            domain in (cookie.domain or "")
            for domain in ("nytimes.com", "theathletic.com")
        )
    ]
    expiries = [
        int(cookie.expires)
        for cookie in relevant
        if getattr(cookie, "expires", None)
    ]

    latest_expiry = max(expiries) if expiries else None
    latest_expiry_dt = (
        datetime.fromtimestamp(latest_expiry, tz=timezone.utc)
        if latest_expiry is not None
        else None
    )
    expired = (
        latest_expiry_dt is not None
        and latest_expiry_dt <= datetime.now(timezone.utc)
    )

    return {
        "cookie_count": len(relevant),
        "latest_expiry": (
            latest_expiry_dt.isoformat() if latest_expiry_dt else None
        ),
        "expired": expired,
    }


def _parse_iso_datetime(raw: str) -> Optional[datetime]:
    """Parse an ISO-like datetime string into UTC."""
    value = str(raw or "").strip()
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_athletic_url_date(url: str) -> Optional[datetime]:
    """Parse a YYYY/MM/DD date embedded in an Athletic article URL."""
    match = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if not match:
        return None

    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def _is_athletic_article_url(url: str) -> bool:
    """Return whether a URL looks like an Athletic article."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if "nytimes.com" not in parsed.netloc and "theathletic.com" not in parsed.netloc:
        return False
    return bool(re.search(r"/athletic/\d+", parsed.path))


def _walk_json_nodes(value):
    """Yield all dict nodes within an arbitrary JSON structure."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_json_nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_json_nodes(nested)


def _candidate_title(node: dict) -> str:
    """Extract a best-effort headline from a parsed JSON node."""
    for key in ("headline", "title", "name"):
        value = str(node.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _extract_athletic_candidates_from_json(
    soup: BeautifulSoup,
    base_url: str,
) -> list[dict]:
    """Parse embedded JSON for article metadata."""
    candidates = []
    scripts = soup.select("script[type='application/ld+json'], script#__NEXT_DATA__")

    for script in scripts:
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        for node in _walk_json_nodes(payload):
            title = _candidate_title(node)
            url = str(node.get("url", "") or "").strip()
            if not title or not url:
                continue

            absolute_url = urljoin(base_url, url)
            if not _is_athletic_article_url(absolute_url):
                continue

            summary = str(node.get("description", "") or "").strip()
            published = (
                _parse_iso_datetime(str(node.get("datePublished", "") or ""))
                or _parse_iso_datetime(str(node.get("dateCreated", "") or ""))
                or _parse_athletic_url_date(absolute_url)
            )

            candidates.append({
                "title": title,
                "url": absolute_url,
                "summary": summary,
                "published": published,
            })

    return candidates


def _extract_athletic_candidates_from_html(
    soup: BeautifulSoup,
    base_url: str,
) -> list[dict]:
    """Fallback extraction using visible article cards."""
    candidates = []
    containers = soup.select("article")

    if not containers:
        containers = [
            anchor.parent
            for anchor in soup.select("a[href*='/athletic/']")
            if anchor.parent is not None
        ]

    for container in containers:
        anchors = container.select("a[href]")
        best_anchor = None
        best_text = ""

        for anchor in anchors:
            href = str(anchor.get("href", "") or "").strip()
            url = urljoin(base_url, href)
            if not _is_athletic_article_url(url):
                continue

            text = (
                anchor.get("aria-label")
                or anchor.get("title")
                or anchor.get_text(" ", strip=True)
            )
            text = str(text or "").strip()
            if len(text) > len(best_text):
                best_anchor = anchor
                best_text = text

        if best_anchor is None or not best_text:
            continue

        href = str(best_anchor.get("href", "") or "").strip()
        url = urljoin(base_url, href)
        summary_tag = container.find("p")
        summary = summary_tag.get_text(" ", strip=True) if summary_tag else ""
        time_tag = container.find("time")
        published = None
        if time_tag:
            published = _parse_iso_datetime(time_tag.get("datetime", ""))
        if published is None:
            published = _parse_athletic_url_date(url)

        candidates.append({
            "title": best_text,
            "url": url,
            "summary": summary,
            "published": published,
        })

    return candidates


def _athletic_team_slug(url: str) -> str:
    """Extract the team slug from an Athletic team page URL."""
    match = re.search(r"/athletic/nfl/team/([^/]+)/?", url)
    if not match:
        return ""
    return match.group(1).strip().lower()


def _resolve_athletic_team_abbr(team_url: str, teams_by_abbr: dict) -> Optional[str]:
    """Map an Athletic team page URL to an NFL team abbreviation."""
    slug = _athletic_team_slug(team_url)
    if not slug:
        return None

    special_cases = {
        "niners": "SF",
    }
    if slug in special_cases:
        return special_cases[slug]

    for abbr, team in teams_by_abbr.items():
        name = team["name"].lower()
        parts = name.split()
        nickname = parts[-1] if parts else ""
        city = " ".join(parts[:-1]).replace(".", "")

        if nickname and nickname in slug:
            return abbr
        if city and city.replace(" ", "-") in slug:
            return abbr
        if slug.startswith(f"{abbr.lower()}-"):
            return abbr

    return None


def _discover_athletic_team_pages(
    session: requests.Session,
    url: str,
    timeout: int,
    teams_by_abbr: dict,
) -> dict[str, str]:
    """Discover dedicated Athletic NFL team pages from the league hub."""
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    if _athletic_auth_redirected(resp):
        return {}
    soup = BeautifulSoup(resp.text, "html.parser")

    team_pages: dict[str, str] = {}
    for anchor in soup.select("a[href*='/athletic/nfl/team/']"):
        href = str(anchor.get("href", "") or "").strip()
        team_url = urljoin(url, href)
        abbr = _resolve_athletic_team_abbr(team_url, teams_by_abbr)
        if abbr and abbr not in team_pages:
            team_pages[abbr] = team_url

    return team_pages


def _athletic_auth_redirected(response: requests.Response) -> bool:
    """Return whether the response landed on an Athletic login page."""
    final_url = str(getattr(response, "url", "") or "")
    return "/athletic/login" in final_url or "/athletic/login2" in final_url


def _normalize_athletic_summary(title: str, parent_text: str) -> str:
    """Strip the title out of the card text to leave a shorter summary."""
    cleaned = re.sub(r"\s+", " ", str(parent_text or "").strip())
    if not cleaned:
        return ""

    if cleaned.startswith(title):
        cleaned = cleaned[len(title):].strip(" -—:|")

    cleaned = re.sub(r"\s+\d+$", "", cleaned).strip()
    return cleaned[:400]


ATHLETIC_BODY_MAX_CHARS = 8000

# Domains we'll opportunistically fetch article bodies for via the
# generic extractor. Each entry maps a URL substring to a list of CSS
# selectors that locate the article body (in order of preference).
GENERIC_ARTICLE_SELECTORS = {
    "espn.com": ["div.article-body", "article"],
    "cbssports.com": ["article", "div.ArticleBody"],
    "nbcsports.com": ["article", "div.article-body"],
    "yahoo.com": ["div.caas-body", "article"],
}

GENERIC_BODY_MAX_CHARS = 8000


def _matches_generic_domain(url: str) -> list[str] | None:
    """Return the list of selectors to try for `url`, or None if unknown."""
    for domain, selectors in GENERIC_ARTICLE_SELECTORS.items():
        if domain in url:
            return selectors
    return None


def fetch_article_body(
    session: "requests.Session",
    url: str,
    timeout: int,
    max_chars: int = GENERIC_BODY_MAX_CHARS,
) -> str:
    """Generic article body extractor for non-paywalled news sites.

    Tries domain-specific CSS selectors first, then falls back to <article>
    or the whole document. Returns "" on any error. Retries once on the
    first request exception — ESPN.com in particular occasionally rate-
    limits the GHA datacenter IP, and a single retry after a short pause
    is enough to recover most of the time. Without the retry, the silent
    empty body cascades into "ESPN never wins the dedup picker" for that
    whole run.
    """
    selectors = _matches_generic_domain(url) or ["article"]

    resp = None
    for attempt in (1, 2):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            break
        except Exception as e:
            if attempt == 1:
                logger.debug("Article body fetch attempt %d failed for %s: %s; retrying",
                             attempt, url, e)
                time.sleep(1.5)
                continue
            logger.debug("Article body fetch failed for %s: %s", url, e)
            return ""
    if resp is None:
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    container = None
    for sel in selectors:
        found = soup.select_one(sel)
        if found and found.find_all("p"):
            container = found
            break
    if container is None:
        container = soup

    paragraphs = [p.get_text(" ", strip=True) for p in container.find_all("p")]
    body = "\n".join(p for p in paragraphs if p and len(p) > 25)
    if len(body) > max_chars:
        body = body[:max_chars].rsplit(" ", 1)[0] + "…"
    return body


def _fetch_athletic_article_body(
    session: requests.Session,
    url: str,
    timeout: int,
) -> str:
    """Fetch an Athletic article and return up to ATHLETIC_BODY_MAX_CHARS of body text.

    Returns "" on auth redirect or any error — callers must tolerate
    a thin/empty body. The session must already have cookies loaded.
    """
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("Athletic body fetch failed for %s: %s", url, e)
        return ""

    if _athletic_auth_redirected(resp):
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    # The Athletic article body lives inside <article>; if absent, fall
    # back to the full document. Extract <p> text and concatenate.
    container = soup.find("article") or soup
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in container.find_all("p")
    ]
    body = "\n".join(p for p in paragraphs if p and len(p) > 25)
    if len(body) > ATHLETIC_BODY_MAX_CHARS:
        body = body[:ATHLETIC_BODY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return body


def _extract_team_page_stories(
    soup: BeautifulSoup,
    page_url: str,
    team_abbr: str,
    teams_by_abbr: dict,
    cutoff: datetime,
    max_items_per_team: int,
) -> list[NewsItem]:
    """Extract recent stories from a dedicated Athletic team page."""
    items: list[NewsItem] = []
    seen_urls: set[str] = set()

    for anchor in soup.select("a[href*='/athletic/']"):
        href = str(anchor.get("href", "") or "").strip()
        article_url = urljoin(page_url, href)

        if not _is_athletic_article_url(article_url):
            continue
        if article_url in seen_urls:
            continue

        title = (
            anchor.get("aria-label")
            or anchor.get("title")
            or anchor.get_text(" ", strip=True)
        )
        title = str(title or "").strip()
        if len(title) < 12:
            continue

        parent = anchor.parent
        parent_text = parent.get_text(" ", strip=True) if parent else title
        summary = _normalize_athletic_summary(title, parent_text)
        detected_teams = _detect_teams(f"{title} {summary}", teams_by_abbr)
        if team_abbr not in detected_teams:
            continue

        published = _parse_athletic_url_date(article_url)
        if published is None:
            time_tag = parent.find("time") if parent else None
            if time_tag:
                published = _parse_iso_datetime(time_tag.get("datetime", ""))
        if published is None or published < cutoff:
            continue

        seen_urls.add(article_url)
        items.append(NewsItem(
            title=title,
            url=article_url,
            source="The Athletic NFL",
            source_type="web",
            published=published,
            summary=summary,
            full_text=summary,  # placeholder; enriched by body fetch below
            teams=detected_teams,
            category="news",
        ))

        if len(items) >= max_items_per_team:
            break

    return items


def scrape_athletic_nfl(
    session: requests.Session,
    settings: dict,
    teams_by_abbr: dict,
    source_config: dict,
) -> list[NewsItem]:
    """Scrape recent NFL stories from The Athletic team pages using a cookies file."""
    url = source_config.get("url", "https://www.nytimes.com/athletic/nfl/")
    timeout = settings["collection"].get("request_timeout", 30)
    lookback_hours = settings["collection"].get("lookback_hours", 28)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    max_items_per_team = int(source_config.get("max_items_per_team", 5) or 5)

    try:
        cookies_path = get_athletic_cookies_path()
    except ValueError as exc:
        _set_source_status(
            source_config.get("name", "The Athletic NFL"),
            status="missing_cookies",
            severity="warning",
            message=str(exc),
        )
        logger.warning("Skipping The Athletic: %s", exc)
        return []

    try:
        logger.info("Scraping The Athletic NFL: %s", url)
        _load_cookies_file(session, cookies_path)
        source_name = source_config.get("name", "The Athletic NFL")
        cookie_info = _inspect_cookie_freshness(cookies_path)
        team_pages = _discover_athletic_team_pages(
            session,
            url,
            timeout,
            teams_by_abbr,
        )
        if not team_pages:
            message = (
                "The Athletic team pages could not be discovered. "
                "Your Athletic cookie may be expired or the site layout changed."
            )
            if cookie_info.get("expired"):
                expiry = cookie_info.get("latest_expiry")
                message = (
                    "The Athletic cookie file appears expired"
                    f"{f' (latest expiry: {expiry})' if expiry else ''}. "
                    "Export a fresh cookies.txt file."
                )
            _set_source_status(
                source_name,
                status="athletic_unavailable",
                severity="warning",
                message=message,
                latest_expiry=cookie_info.get("latest_expiry"),
            )
            logger.warning("No Athletic team pages were discovered from %s", url)
            return []

        items: list[NewsItem] = []
        for abbr, team_url in sorted(team_pages.items()):
            resp = session.get(team_url, timeout=timeout)
            resp.raise_for_status()
            if _athletic_auth_redirected(resp):
                message = (
                    "The Athletic redirected to a login page while fetching team stories. "
                    "Your cookie file is likely expired."
                )
                _set_source_status(
                    source_name,
                    status="athletic_login_required",
                    severity="warning",
                    message=message,
                    latest_expiry=cookie_info.get("latest_expiry"),
                )
                logger.warning(message)
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            team_items = _extract_team_page_stories(
                soup,
                team_url,
                abbr,
                teams_by_abbr,
                cutoff,
                max_items_per_team,
            )
            # Enrich each item with the article body so the team-notes
            # prompt can extract player-level detail. The Colts article
            # `colts-draft-depth-nfl-draft-2026` is exactly the kind of
            # piece that's worthless from the title alone.
            for item in team_items:
                body = _fetch_athletic_article_body(session, item.url, timeout)
                if body:
                    item.full_text = body
                    if not item.summary:
                        item.summary = body[:500]
            items.extend(team_items)

        if not items:
            message = (
                "The Athletic returned no recent team-page stories. "
                "If this repeats, refresh your Athletic cookies file."
            )
            if cookie_info.get("expired"):
                expiry = cookie_info.get("latest_expiry")
                message = (
                    "The Athletic cookie file appears expired"
                    f"{f' (latest expiry: {expiry})' if expiry else ''}. "
                    "Refresh the cookies file."
                )
            _set_source_status(
                source_name,
                status="athletic_no_recent_items",
                severity="warning",
                message=message,
                latest_expiry=cookie_info.get("latest_expiry"),
                team_pages=len(team_pages),
            )
            logger.warning(message)
        else:
            message = (
                f"Collected {len(items)} Athletic stories across {len(team_pages)} team pages."
            )
            if cookie_info.get("expired"):
                expiry = cookie_info.get("latest_expiry")
                message += (
                    " The cookie file appears past its latest expiry timestamp"
                    f"{f' ({expiry})' if expiry else ''}; refresh it if this source stops updating."
                )
            severity = "warning" if cookie_info.get("expired") else "info"
            status = "ok_with_expired_cookie" if cookie_info.get("expired") else "ok"
            _set_source_status(
                source_name,
                status=status,
                severity=severity,
                message=message,
                latest_expiry=cookie_info.get("latest_expiry"),
                item_count=len(items),
                team_pages=len(team_pages),
            )
            logger.info(
                "Found %d recent Athletic NFL stories across %d team pages.",
                len(items),
                len(team_pages),
            )

        return items

    except requests.RequestException as e:
        _set_source_status(
            source_config.get("name", "The Athletic NFL"),
            status="request_failed",
            severity="warning",
            message=f"Failed to scrape The Athletic NFL: {e}",
        )
        logger.error("Failed to scrape The Athletic NFL: %s", e)
    except Exception as e:
        _set_source_status(
            source_config.get("name", "The Athletic NFL"),
            status="parse_failed",
            severity="warning",
            message=f"Error parsing The Athletic NFL page: {e}",
        )
        logger.error("Error parsing The Athletic NFL page: %s", e)

    return []


def scrape_transactions(
    session: requests.Session,
    settings: dict,
    teams_by_abbr: dict,
    lookback_hours: Optional[int] = None,
) -> list[NewsItem]:
    """Scrape NFL.com transaction category pages."""
    return scrape_transactions_category_pages(
        session,
        settings,
        teams_by_abbr,
        lookback_hours=lookback_hours,
    )


def _normalize_transaction_team_text(text: str) -> str:
    """Clean duplicated team-label text from NFL.com club cells."""
    normalized = re.sub(r"\s+", " ", text).strip()
    parts = normalized.split()
    midpoint = len(parts) // 2
    if len(parts) % 2 == 0 and midpoint and parts[:midpoint] == parts[midpoint:]:
        return " ".join(parts[:midpoint])
    return normalized


def _extract_transaction_team(cell, teams_by_abbr: dict) -> tuple[str, list[str]]:
    """Extract a normalized team label and abbreviation list from a transaction cell."""
    club_name = cell.select_one(".d3-o-club-fullname")
    raw_text = (
        club_name.get_text(" ", strip=True)
        if club_name is not None
        else cell.get_text(" ", strip=True)
    )
    text = _normalize_transaction_team_text(raw_text)
    if not text or text == "--":
        return "--", []
    return text, _match_team(text, teams_by_abbr)


def scrape_transactions_category_pages(
    session: requests.Session,
    settings: dict,
    teams_by_abbr: dict,
    lookback_hours: Optional[int] = None,
) -> list[NewsItem]:
    """Scrape NFL.com transaction rows from the direct category pages."""
    timeout = settings["collection"].get("request_timeout", 30)
    if lookback_hours is None:
        lookback_hours = settings["collection"].get("lookback_hours", 28)

    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).date()
    items: list[NewsItem] = []
    seen_rows: set[tuple[str, str, str, str, str, str]] = set()

    current_month = datetime.now(timezone.utc).replace(day=1)
    previous_month = (current_month - timedelta(days=1)).replace(day=1)
    seen_months: set[tuple[int, int]] = set()
    month_specs: list[tuple[int, int]] = []
    for month_start in (current_month, previous_month):
        month_key = (month_start.year, month_start.month)
        if month_key in seen_months:
            continue
        seen_months.add(month_key)
        month_specs.append(month_key)

    categories = (
        "trades",
        "signings",
        "reserve-list",
        "waivers",
        "terminations",
        "other",
    )

    try:
        for year, month in month_specs:
            for category in categories:
                url = (
                    f"https://www.nfl.com/transactions/league/{category}/{year}/{month}"
                )
                logger.info("Scraping NFL.com transactions [%s]: %s", category, url)
                resp = session.get(url, timeout=timeout)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                table = soup.select_one(
                    ".d3-o-table.d3-o-table--detailed.d3-o-team-stats--detailed"
                )
                if not table:
                    continue

                headers = [
                    re.sub(r"\s+", " ", th.get_text(" ", strip=True)).strip().lower()
                    for th in table.select("thead th")
                ]
                if not headers:
                    continue

                header_map = {header: idx for idx, header in enumerate(headers)}
                if not all(
                    header in header_map
                    for header in ("date", "name", "transaction")
                ):
                    continue

                rows = table.select("tbody tr")
                for row in rows:
                    cells = row.find_all("td")
                    if len(cells) < len(headers):
                        continue

                    from_idx = header_map.get("from")
                    to_idx = header_map.get("to")
                    date_idx = header_map["date"]
                    name_idx = header_map["name"]
                    position_idx = header_map.get("position")
                    transaction_idx = header_map["transaction"]

                    date_text = re.sub(
                        r"\s+",
                        " ",
                        cells[date_idx].get_text(" ", strip=True),
                    ).strip()
                    name_text = re.sub(
                        r"\s+",
                        " ",
                        cells[name_idx].get_text(" ", strip=True),
                    ).strip()
                    transaction_text = re.sub(
                        r"\s+",
                        " ",
                        cells[transaction_idx].get_text(" ", strip=True),
                    ).strip()
                    position_text = ""
                    if position_idx is not None:
                        position_text = re.sub(
                            r"\s+",
                            " ",
                            cells[position_idx].get_text(" ", strip=True),
                        ).strip()

                    if not date_text or not name_text or not transaction_text:
                        continue

                    from_text = "--"
                    from_teams: list[str] = []
                    if from_idx is not None:
                        from_text, from_teams = _extract_transaction_team(
                            cells[from_idx],
                            teams_by_abbr,
                        )

                    to_text = "--"
                    to_teams: list[str] = []
                    if to_idx is not None:
                        to_text, to_teams = _extract_transaction_team(
                            cells[to_idx],
                            teams_by_abbr,
                        )

                    pub_date = _parse_transaction_date(date_text, default_year=year)
                    if pub_date.date() < cutoff_date:
                        continue

                    row_key = (
                        category,
                        date_text,
                        name_text,
                        transaction_text,
                        from_text,
                        to_text,
                    )
                    if row_key in seen_rows:
                        continue
                    seen_rows.add(row_key)

                    teams = []
                    for abbr in from_teams + to_teams:
                        if abbr and abbr not in teams:
                            teams.append(abbr)

                    if not teams:
                        teams = _detect_teams(
                            " ".join(
                                part
                                for part in (
                                    from_text,
                                    to_text,
                                    name_text,
                                    transaction_text,
                                )
                                if part and part != "--"
                            ),
                            teams_by_abbr,
                        )

                    movement_bits = []
                    if from_text != "--" and to_text != "--" and from_text == to_text:
                        movement_bits.append(f"team {from_text}")
                    else:
                        if from_text != "--":
                            movement_bits.append(f"from {from_text}")
                        if to_text != "--":
                            movement_bits.append(f"to {to_text}")
                    if position_text:
                        movement_bits.append(f"position {position_text}")
                    movement_bits.append(f"date {date_text}")

                    summary = (
                        f"{name_text}: {transaction_text}; "
                        + "; ".join(movement_bits)
                    )

                    title = f"{name_text}: {transaction_text}"
                    if from_text != "--" or to_text != "--":
                        if from_text != "--" and to_text != "--" and from_text == to_text:
                            route = from_text
                        else:
                            route = " -> ".join(
                                part for part in (from_text, to_text) if part != "--"
                            )
                        if route:
                            title = f"{name_text}: {route} ({transaction_text})"

                    items.append(NewsItem(
                        title=title,
                        url=url,
                        source="NFL.com Transactions",
                        source_type="web",
                        published=pub_date,
                        summary=summary,
                        full_text=summary,
                        teams=teams,
                        category="transaction",
                    ))

        logger.info("Found %d transactions.", len(items))
    except requests.RequestException as e:
        logger.error("Failed to scrape transactions: %s", e)
    except Exception as e:
        logger.error("Error parsing transactions page: %s", e)

    return items


def scrape_injuries(
    session: requests.Session,
    settings: dict,
    teams_by_abbr: dict,
) -> list[NewsItem]:
    """Scrape NFL.com injury reports."""
    url = "https://www.nfl.com/injuries/"
    timeout = settings["collection"].get("request_timeout", 30)
    items = []

    try:
        logger.info("Scraping NFL.com injuries: %s", url)
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Injury reports are organized by team
        team_sections = soup.select("[class*='injury']")
        if not team_sections:
            team_sections = soup.select("section, .d3-l-col__col-12")

        for section in team_sections:
            # Try to find team name in section header
            header = section.find(["h2", "h3", "h4"])
            if not header:
                continue

            team_name = header.get_text(strip=True)
            teams = _match_team(team_name, teams_by_abbr)
            if not teams:
                continue

            # Collect all player injury entries in this section
            rows = section.select("table tbody tr, li")
            injury_lines = []
            for row in rows:
                text = row.get_text(" ", strip=True)
                if text and len(text) > 5:
                    injury_lines.append(text)

            if injury_lines:
                full_text = "\n".join(injury_lines)
                items.append(NewsItem(
                    title=f"{team_name} Injury Report",
                    url=url,
                    source="NFL.com Injuries",
                    source_type="web",
                    published=datetime.now(timezone.utc),
                    summary=f"{len(injury_lines)} players listed",
                    full_text=full_text,
                    teams=teams,
                    category="injury",
                ))

        logger.info("Found injury reports for %d teams.", len(items))

    except requests.RequestException as e:
        logger.error("Failed to scrape injuries: %s", e)
    except Exception as e:
        logger.error("Error parsing injuries page: %s", e)

    return items


def _parse_transaction_date(
    text: str,
    default_year: Optional[int] = None,
) -> datetime:
    """Try to parse a date string from the transactions table."""
    text = text.strip()
    if default_year is None:
        default_year = datetime.now(timezone.utc).year

    for fmt in ("%m/%d",):
        try:
            dt = datetime.strptime(text, fmt)
            dt = dt.replace(year=default_year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _match_team(text: str, teams_by_abbr: dict) -> list[str]:
    """Match team name or abbreviation in text."""
    text_lower = text.lower()
    for abbr, team in teams_by_abbr.items():
        name = team["name"].lower()
        # Check full name, city, or nickname
        parts = name.split()
        nickname = parts[-1] if parts else ""
        if name in text_lower or nickname in text_lower:
            return [abbr]
        if abbr.lower() == text_lower.strip():
            return [abbr]
    return []


SI_TEAM_ITEM_LIMIT = 10
SI_BODY_MAX_CHARS = 8000


def _fetch_si_article_body(
    session: requests.Session,
    url: str,
    timeout: int,
) -> str:
    """Fetch an SI.com article and return body text up to SI_BODY_MAX_CHARS."""
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.debug("SI body fetch failed for %s: %s", url, e)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.find("article") or soup
    paragraphs = [
        p.get_text(" ", strip=True)
        for p in container.find_all("p")
    ]
    body = "\n".join(p for p in paragraphs if p and len(p) > 25)
    if len(body) > SI_BODY_MAX_CHARS:
        body = body[:SI_BODY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return body


def scrape_si_team(
    session: requests.Session,
    settings: dict,
    teams_by_abbr: dict,
    source: dict,
) -> list[NewsItem]:
    """Scrape an SI.com team page (e.g. https://www.si.com/nfl/bills).

    SI doesn't expose RSS for team pages, so we pull article links from
    the team index. Articles follow the pattern
    `/nfl/<team-slug>/onsi/<article-slug>`. We don't have a published
    date in the index HTML, so each item is stamped at scrape time —
    the lookback filter and cross-day dedup keep them tidy.
    """
    name = source.get("name", "SI Team")
    url = source.get("url", "")
    team = source.get("team", "")

    if not url or not team:
        logger.warning("SI team source missing url or team: %s", name)
        return []

    timeout = settings["collection"].get("request_timeout", 30)
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("SI team fetch failed for %s: %s", name, e)
        _set_source_status(
            name, status="error", severity="error",
            message=f"Fetch failed: {e}", item_count=0,
        )
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    team_slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    pattern = f"/nfl/{team_slug}/onsi/"

    article_urls: list[tuple[str, str]] = []  # (title, url)
    seen_urls: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if pattern not in href:
            continue
        if href.startswith("/"):
            href = "https://www.si.com" + href
        if href in seen_urls:
            continue

        title = a.get_text(strip=True)
        if not title or len(title) < 15:
            continue

        seen_urls.add(href)
        article_urls.append((title, href))
        if len(article_urls) >= SI_TEAM_ITEM_LIMIT:
            break

    items: list[NewsItem] = []
    for title, href in article_urls:
        body = _fetch_si_article_body(session, href, timeout)
        items.append(NewsItem(
            title=title,
            url=href,
            source=name,
            source_type="web",
            published=datetime.now(timezone.utc),
            summary="",
            full_text=body,
            teams=[team],
            category="news",
        ))

    logger.info(
        "SI team %s: %d items (avg body %d chars) from %s",
        team, len(items),
        int(sum(len(i.full_text) for i in items) / max(len(items), 1)),
        url,
    )
    _set_source_status(
        name, status="ok", severity="info",
        message=f"Collected {len(items)} items.",
        item_count=len(items),
    )
    return items


def collect_web(lookback_hours: Optional[int] = None) -> list[NewsItem]:
    """Collect news from all configured web sources.

    Returns:
        List of NewsItem objects.
    """
    settings = get_settings()
    teams_by_abbr = get_teams_by_abbr()
    if lookback_hours is None:
        lookback_hours = settings["collection"].get("lookback_hours", 28)
    session = _get_session(settings)
    delay = settings["collection"].get("request_delay", 2.0)
    all_items: list[NewsItem] = []
    _reset_source_status()

    web_sources = get_web_sources()
    logger.info("Scraping %d web sources...", len(web_sources))

    for source in web_sources:
        source_type = source.get("type", "")

        if source_type == "transactions":
            items = scrape_transactions_category_pages(
                session,
                settings,
                teams_by_abbr,
                lookback_hours=lookback_hours,
            )
        elif source_type == "injuries":
            items = scrape_injuries(session, settings, teams_by_abbr)
        elif source_type == "athletic_nfl":
            items = scrape_athletic_nfl(
                session,
                settings,
                teams_by_abbr,
                source,
            )
        elif source_type == "si_team":
            items = scrape_si_team(session, settings, teams_by_abbr, source)
        else:
            logger.warning("Unknown web source type: %s", source_type)
            continue

        if source.get("name") not in LAST_WEB_SOURCE_STATUS:
            _set_source_status(
                source.get("name", source_type),
                status="ok",
                severity="info",
                message=f"Collected {len(items)} items.",
                item_count=len(items),
            )

        all_items.extend(items)
        time.sleep(delay)

    logger.info("Collected %d items from web scraping.", len(all_items))
    return all_items


def save_web_results(items: list[NewsItem], date_str: str):
    """Save collected web items to a JSON file."""
    import json
    out_dir = get_data_dir("raw", date_str)
    path = out_dir / "web.json"
    data = [item.to_dict() for item in items]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d web items to %s", len(items), path)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = collect_web()
    for item in results[:10]:
        print(f"[{item.source}] {item.title}")
        if item.teams:
            print(f"  Teams: {', '.join(item.teams)}")
        print(f"  {item.summary}")
        print()
    print(f"Total: {len(results)} items")
