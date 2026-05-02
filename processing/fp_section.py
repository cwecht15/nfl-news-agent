"""Build the FantasyPoints articles report section.

Renders a per-player rollup: each NFL player gets one bullet drawn from
the day's FantasyPoints articles, with inline `[Author, N]` citations.
The numbered list of source articles is attached as `sources` so the
HTML renderer's standard footer renders clickable links.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import NewsItem
from processing.summarizer import (
    _call_model,
    _init_usage_tracker,
    _resolve_client_and_runtime,
)

logger = logging.getLogger(__name__)

ARTICLE_BODY_CHARS = 5000
# gpt-5.4-mini with high reasoning_effort spends most of a small budget on
# reasoning, leaving little for visible text. Cap high enough that a 15-
# article rollup completes in one call.
MAX_OUTPUT_TOKENS = 6000

PROMPT_INSTRUCTIONS = """You are an NFL fantasy football analyst summarizing today's
articles from FantasyPoints into a per-player rollup for a daily report.

Read every article below. For each NFL player meaningfully discussed, produce
ONE concise bullet capturing the article's specific take on that player's
fantasy outlook, role, usage, or situation. Skip generic restatements
("Player X is a starter"). Skip players only named in passing.

Format every bullet exactly like this, one per line, no numbering:
- **Player Name** — concise fantasy/usage takeaway. Author Name [N]

Where N is the source article number from the list below — use ONLY the
digit in brackets (e.g. `[1]`, not `[Author, 1]`), and place the
author's name immediately before the bracket so the reader sees both.

If the same player appears across multiple articles with different takes,
emit multiple bullets — one per source. Use the author's name exactly as
provided. If author is missing, omit it (still emit `[N]`).

Order players by relevance: skill players (QB/RB/WR/TE) first, then any
other positions worth surfacing. Skip articles that don't yield any
player-specific takeaway.

If a player's name appears only as a last name in the article, use only
the last name in your bullet — never invent a first name.

Do not include a header, intro, or trailer. Output only the bullet list.
"""


def _build_article_block(items: list[NewsItem]) -> tuple[str, list[dict[str, Any]]]:
    """Build the prompt body and matching numbered source list.

    Returns:
        (prompt_text, numbered_sources). `numbered_sources` is the list
        attached to the section's `sources` field so the renderer footer
        can show clickable [N] links — order matches the [N] in prompt.
    """
    blocks: list[str] = []
    numbered: list[dict[str, Any]] = []

    for idx, item in enumerate(items, start=1):
        body = (item.full_text or item.summary or "").strip()
        if len(body) > ARTICLE_BODY_CHARS:
            body = body[:ARTICLE_BODY_CHARS].rstrip() + "…"

        author = item.author or "(unknown author)"
        published = item.published.isoformat() if item.published else ""

        blocks.append(
            f"[{idx}] Title: {item.title}\n"
            f"Author: {author}\n"
            f"Published: {published}\n"
            f"URL: {item.url}\n"
            f"Body:\n{body}".rstrip()
        )

        numbered.append({
            "num": idx,
            "title": item.title,
            "url": item.url,
            "source": "FantasyPoints",
            "author": item.author,
            "published": published,
        })

    return "\n\n---\n\n".join(blocks), numbered


def build_fp_section(
    items: list[NewsItem],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    date_label: str = "",
) -> Optional[dict[str, Any]]:
    """Summarize FantasyPoints articles into the daily report's FP section.

    Args:
        items: NewsItems collected by collectors.fantasypoints_collector.
        client: Optional pre-resolved LLM client. Built from active config when None.
        usage_tracker: Optional shared usage tracker. When supplied, this call's
            tokens accumulate into the existing daily total.
        date_label: Free-form label for logging.

    Returns:
        {"summary": str, "sources": [...], "count": int} or None when items is empty.
    """
    if not items:
        return None

    client, runtime = _resolve_client_and_runtime(client)
    if usage_tracker is None:
        usage_tracker = _init_usage_tracker(runtime)

    prompt_body, numbered_sources = _build_article_block(items)
    prompt = PROMPT_INSTRUCTIONS + "\n\nArticles:\n\n" + prompt_body

    logger.info(
        "Building FantasyPoints section: %d articles (%s)",
        len(items),
        date_label or "no label",
    )

    try:
        summary = _call_model(
            client,
            prompt,
            runtime,
            max_tokens=MAX_OUTPUT_TOKENS,
            usage_tracker=usage_tracker,
            usage_label="fantasypoints_section",
            # Constrained-format bullet extraction; high reasoning often
            # eats the entire token budget before producing visible text.
            reasoning_effort="medium",
        )
    except Exception as e:
        logger.error("FantasyPoints section LLM call failed: %s", e)
        return None

    summary = (summary or "").strip()
    if not summary:
        logger.warning("FantasyPoints section: LLM returned empty output.")
        return None

    return {
        "summary": summary,
        # `numbered_sources` powers the dashboard's `[N]` citation
        # auto-linkifier (mirrors Team Notes / League-Wide). `sources`
        # is the same list so the standalone HTML report's footer
        # bullet list still renders.
        "numbered_sources": numbered_sources,
        "sources": numbered_sources,
        "count": len(items),
    }
