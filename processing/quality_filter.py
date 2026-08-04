"""Drop obvious-fluff news items before they reach dedup or the LLM.

Catches voting articles, trivia, jersey reveals, off-cycle mock drafts,
and similar low-signal patterns. Patterns are configured in
`config/settings.yaml` under `content_filter.drop_patterns` (applied to every
source) and `content_filter.drop_patterns_by_source_type` (applied only to a
named source_type, e.g. promo/holiday fluff scoped to `twitter`).
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
    # Patterns applied ONLY to items of a matching source_type (matched against
    # the title; for tweets the title is the full tweet text). Lets us strip
    # aggressive Twitter promo/holiday fluff — insider lists include team house
    # accounts that post marketing — without touching real RSS headlines.
    scoped_raw = cfg.get("drop_patterns_by_source_type") or {}
    scoped_patterns = {
        str(st): _compile_patterns(pats or [])
        for st, pats in scoped_raw.items()
    }
    keep_years = [str(y) for y in (cfg.get("mock_draft_keep_years") or [])]
    mock_pattern = re.compile(r"\bmock draft\b", re.IGNORECASE)

    kept: list[NewsItem] = []
    dropped: list[NewsItem] = []

    for item in items:
        title = item.title or ""

        if any(p.search(title) for p in drop_patterns):
            dropped.append(item)
            continue

        scoped = scoped_patterns.get(item.source_type)
        if scoped and any(p.search(title) for p in scoped):
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


# Title-only, precision-first injury classifier. An item must be PRIMARILY
# an injury-status update to be retagged; positive camp notes ("returns to
# practice after injury") must STAY in Team Notes — the exclude list wins
# on conflict. Override via settings.yaml `injury_classifier.patterns` /
# `exclude_patterns`.
DEFAULT_INJURY_PATTERNS = [
    r"\bcarted off\b",
    r"\b(tore|torn|tears?)\b.{0,30}\b(acl|mcl|pcl|achilles|labrum|meniscus|pec(toral)?|rotator cuff|quad|hamstring|calf|groin)\b",
    r"\bout for (the )?(season|year)\b",
    r"\bseason[- ]ending\b",
    r"\bplaced on (the )?(pup|nfi|injured reserve)\b",
    r"\bplaced on ir\b",
    r"\b(undergo(es|ing)?|underwent|will (have|undergo)|scheduled for)\b.{0,20}\bsurgery\b",
    r"\bmri\b",
    r"\bx[- ]rays?\b",
    r"\b(week[- ]to[- ]week|day[- ]to[- ]day)\b",
    r"\bruled out\b",
    r"\bconcussion protocol\b",
    r"\b(suffer(s|ed)?|sustain(s|ed)?)\b.{0,30}\b(injur|sprain|strain|fractur|tear|concussion)",
    r"\b(broken|fractured)\b.{0,20}\b(foot|hand|leg|arm|finger|thumb|ankle|collarbone|rib|wrist|fibula|tibia)\b",
    r"\bexpected to miss\b",
    r"\b(left|leaves?|exit(s|ed)?) (practice|the game|game)\b.{0,20}\bwith\b",
    r"\b(will|to) miss\b.{0,25}\b(weeks?|months?|season|time)\b",
]
DEFAULT_INJURY_EXCLUDE = [
    r"\breturn(s|ed|ing)? to (practice|the field|action|team drills)\b",
    r"\b(activated|cleared)\b",
    r"\bfull(y)? (participant|practice|cleared|healthy)\b",
    r"\bavoid(s|ed)? (a )?(serious|major)\b",
    r"\boff (the )?(pup|nfi|ir)\b",
    r"\bgood news\b",
]


def reclassify_injury_items(items: list[NewsItem]) -> list[NewsItem]:
    """Retag items whose TITLE is primarily an injury-status update.

    Runs after the quality filter and before dedup, so the summarizer and
    report_builder (which filter by category independently) agree. Mutates
    matching items in place (category -> "injury") and returns them for
    logging.
    """
    cfg = get_settings().get("injury_classifier", {}) or {}
    if not cfg.get("enabled", True):
        return []
    pos = _compile_patterns(cfg.get("patterns") or DEFAULT_INJURY_PATTERNS)
    neg = _compile_patterns(cfg.get("exclude_patterns") or DEFAULT_INJURY_EXCLUDE)
    changed: list[NewsItem] = []
    for item in items:
        if item.category in ("injury", "transaction"):
            continue
        title = item.title or ""
        if any(p.search(title) for p in pos) and not any(p.search(title) for p in neg):
            item.category = "injury"
            changed.append(item)
    if changed:
        logger.info("Injury classifier retagged %d items", len(changed))
        for item in changed[:20]:
            logger.debug("  retagged: %s", item.title)
    return changed
