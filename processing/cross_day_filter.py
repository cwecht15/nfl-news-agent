"""Cross-day duplicate filter.

Compares today's deduped news items against titles from the previous
N days of raw collections and drops items whose titles are near-
duplicates of something already reported. Reuses the same sentence-
transformers model as the intra-day deduplicator so the encode pass is
cheap after the model is loaded.
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import NewsItem
from processing.deduplicator import (
    _compute_embeddings,
    _cosine_similarity,
    _normalize_title,
)

logger = logging.getLogger(__name__)

RAW_FILES = ("rss.json", "web.json", "reddit.json")


def _is_exempt(
    item: NewsItem,
    skip_categories: set[str],
    skip_patterns: list[re.Pattern],
) -> bool:
    """Whether an item is exempt from cross-day suppression."""
    if item.category in skip_categories:
        return True
    title = item.title or ""
    return any(p.search(title) for p in skip_patterns)


def _load_raw_titles(raw_dir: Path, date_str: str) -> list[str]:
    """Load raw news titles for a single prior day. Missing files are skipped."""
    day_dir = raw_dir / date_str
    if not day_dir.exists():
        return []

    titles: list[str] = []
    for filename in RAW_FILES:
        path = day_dir / filename
        if not path.exists():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read %s: %s", path, e)
            continue

        if not isinstance(data, list):
            continue

        for record in data:
            title = record.get("title") if isinstance(record, dict) else None
            if title:
                titles.append(title)
    return titles


def load_lookback_titles(
    raw_dir: Path,
    current_date: str,
    lookback_days: int,
) -> list[str]:
    """Collect normalized titles from the previous `lookback_days` days."""
    try:
        today = datetime.strptime(current_date, "%Y-%m-%d")
    except ValueError:
        logger.warning("Invalid current_date '%s' — skipping lookback.", current_date)
        return []

    titles: list[str] = []
    for offset in range(1, lookback_days + 1):
        prior = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        titles.extend(_load_raw_titles(raw_dir, prior))

    return [_normalize_title(t) for t in titles if t]


def filter_recent_duplicates(
    items: list[NewsItem],
    raw_dir: Path,
    current_date: str,
    lookback_days: int = 2,
    threshold: float = 0.82,
    skip_categories: Optional[set[str]] = None,
    skip_title_patterns: Optional[list[str]] = None,
) -> tuple[list[NewsItem], list[tuple[NewsItem, float]]]:
    """Drop items whose title is a near-duplicate of any prior-day title.

    Args:
        items: Today's deduped news items.
        raw_dir: Path to data/raw (contains YYYY-MM-DD subdirs).
        current_date: Today's date as YYYY-MM-DD.
        lookback_days: How many prior days to compare against.
        threshold: Cosine similarity at which a match suppresses the item.
        skip_categories: Categories that are exempt (e.g. {"transaction"}).
            Transactions are usually day-unique events, so it can be worth
            letting them through even when wording is similar.
        skip_title_patterns: Regexes (case-insensitive) whose title matches
            are exempt — for rolling articles republished daily with fresh
            content under a near-identical title (e.g. ESPN's per-team
            "training camp: Latest intel, updates").

    Returns:
        (kept_items, dropped_pairs) where dropped_pairs is a list of
        (item, max_similarity) tuples for logging / debugging.
    """
    if not items or lookback_days <= 0:
        return list(items), []

    skip_categories = skip_categories or set()
    skip_patterns = [
        re.compile(p, re.IGNORECASE)
        for p in (skip_title_patterns or []) if p
    ]

    lookback_titles = load_lookback_titles(raw_dir, current_date, lookback_days)
    if not lookback_titles:
        logger.info("Cross-day filter: no lookback data, skipping.")
        return list(items), []

    # Encode today + lookback in one pass so they share the same model state.
    today_norm = [_normalize_title(item.title) for item in items]
    combined = today_norm + lookback_titles
    embeddings = _compute_embeddings(combined)
    if embeddings is None:
        logger.info(
            "Cross-day filter: embeddings unavailable (see deduplicator log), skipping."
        )
        return list(items), []

    today_vecs = embeddings[: len(today_norm)]
    lookback_vecs = embeddings[len(today_norm):]

    kept: list[NewsItem] = []
    dropped: list[tuple[NewsItem, float]] = []

    for item, vec in zip(items, today_vecs):
        if _is_exempt(item, skip_categories, skip_patterns):
            kept.append(item)
            continue

        max_sim = 0.0
        for other in lookback_vecs:
            sim = _cosine_similarity(vec, other)
            if sim > max_sim:
                max_sim = sim
                if max_sim >= threshold:
                    break

        if max_sim >= threshold:
            dropped.append((item, max_sim))
        else:
            kept.append(item)

    logger.info(
        "Cross-day filter: %d -> %d items (%d suppressed, lookback=%d days, threshold=%.2f).",
        len(items), len(kept), len(dropped), lookback_days, threshold,
    )
    for item, sim in dropped[:20]:
        logger.info("  suppressed (sim=%.2f): %s", sim, item.title)
    if len(dropped) > 20:
        logger.info("  ... and %d more", len(dropped) - 20)

    return kept, dropped
