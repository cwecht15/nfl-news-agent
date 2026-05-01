"""Drop obvious-fluff news items before they reach dedup or the LLM.

Catches voting articles, trivia, jersey reveals, off-cycle mock drafts,
and similar low-signal patterns. Patterns are configured in
`config/settings.yaml` under `content_filter.drop_patterns`.
"""

import logging
import re
from typing import Iterable

from config_loader import get_settings
from models import NewsItem

logger = logging.getLogger(__name__)


def _compile_patterns(patterns: Iterable[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns if p]


def filter_news_items(items: list[NewsItem]) -> tuple[list[NewsItem], list[NewsItem]]:
    """Return (kept, dropped) lists. Dropped items match a fluff pattern."""
    cfg = get_settings().get("content_filter", {}) or {}
    if not cfg.get("enabled", False):
        return list(items), []

    drop_patterns = _compile_patterns(cfg.get("drop_patterns") or [])
    keep_years = [str(y) for y in (cfg.get("mock_draft_keep_years") or [])]
    mock_pattern = re.compile(r"\bmock draft\b", re.IGNORECASE)

    kept: list[NewsItem] = []
    dropped: list[NewsItem] = []

    for item in items:
        title = item.title or ""

        if any(p.search(title) for p in drop_patterns):
            dropped.append(item)
            continue

        if mock_pattern.search(title):
            if keep_years and not any(y in title for y in keep_years):
                dropped.append(item)
                continue

        kept.append(item)

    if dropped:
        logger.info("Quality filter dropped %d items", len(dropped))
        for item in dropped[:20]:
            logger.debug("  dropped: %s", item.title)

    return kept, dropped
