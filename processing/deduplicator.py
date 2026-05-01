"""Story Deduplication.

Groups news items that cover the same event/story across multiple sources.
Uses embedding-based cosine similarity when sentence-transformers is available,
falling back to SequenceMatcher for title-based dedup.
"""

import logging
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import NewsItem

logger = logging.getLogger(__name__)

# Similarity thresholds
SIMILARITY_THRESHOLD = 0.6          # SequenceMatcher fallback
EMBEDDING_SIMILARITY_THRESHOLD = 0.75  # Cosine similarity for embeddings
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"

_embedding_model = None
_embedding_available: Optional[bool] = None


def _get_embedding_model():
    """Load and cache the sentence-transformers model."""
    global _embedding_model, _embedding_available

    if _embedding_available is False:
        return None

    if _embedding_model is not None:
        return _embedding_model

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.info(
            "sentence-transformers not installed — using SequenceMatcher fallback. "
            "Install with: pip install sentence-transformers"
        )
        _embedding_available = False
        return None

    try:
        logger.info("Loading embedding model '%s' for deduplication...", EMBEDDING_MODEL_NAME)
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        _embedding_available = True
        return _embedding_model
    except Exception as e:
        # Model load can fail for many reasons (HF Hub rate limit, network
        # blip, cache corruption). Log the actual error so the intermittent
        # failures stop looking like "package missing".
        logger.warning(
            "Embedding model load failed (%s: %s) — using SequenceMatcher fallback.",
            type(e).__name__, e,
        )
        _embedding_available = False
        return None


def _compute_embeddings(titles: list[str]):
    """Compute embeddings for a list of titles. Returns None if unavailable."""
    model = _get_embedding_model()
    if model is None:
        return None
    return model.encode(titles, normalize_embeddings=True)


def _cosine_similarity(a, b) -> float:
    """Compute cosine similarity between two normalized embedding vectors."""
    return float(a @ b)


def _normalize_title(title: str) -> str:
    """Normalize a title for comparison."""
    title = title.lower()
    # Remove common prefixes/suffixes
    title = re.sub(r"^(breaking|report|sources?|per|via)\s*:?\s*", "", title)
    # Remove punctuation
    title = re.sub(r"[^\w\s]", " ", title)
    # Collapse whitespace
    title = re.sub(r"\s+", " ", title).strip()
    return title


def _text_similarity(a: str, b: str) -> float:
    """Compute similarity ratio between two strings."""
    return SequenceMatcher(None, a, b).ratio()


def _keyword_overlap(norm_a: str, norm_b: str) -> float:
    """Compute keyword overlap ratio between two normalized titles."""
    stop = {"the", "a", "an", "to", "for", "in", "on", "of",
            "and", "with", "is", "at", "by", "from", "nfl"}
    words_a = set(norm_a.split()) - stop
    words_b = set(norm_b.split()) - stop
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


_TRANSACTION_STOP = {
    "the", "a", "an", "to", "for", "in", "on", "of", "and", "with", "is",
    "at", "by", "from", "nfl", "sign", "signs", "signed", "signing",
    "trade", "traded", "trades", "release", "released", "waive", "waived",
    "claim", "claimed", "free", "agent", "unrestricted", "restricted",
    "exclusive", "rights", "deal", "contract", "extension", "tender",
}

# Team-related words to exclude from player-name matching
_TEAM_WORDS: set[str] | None = None


def _get_team_words() -> set[str]:
    """Lazily load team name words so transactions dedup ignores them."""
    global _TEAM_WORDS
    if _TEAM_WORDS is not None:
        return _TEAM_WORDS
    try:
        import yaml
        teams_path = Path(__file__).parent.parent / "config" / "teams.yaml"
        with open(teams_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        words = set()
        for team in data.get("teams", []):
            words.add(team["abbr"].lower())
            for w in team["name"].lower().split():
                words.add(w)
        _TEAM_WORDS = words
    except Exception:
        _TEAM_WORDS = set()
    return _TEAM_WORDS


def _extract_player_words(norm_title: str) -> set[str]:
    """Extract likely player-name words from a normalized transaction title.

    Strips transaction verbs, common words, and team names so only player
    name fragments remain.
    """
    all_stop = _TRANSACTION_STOP | _get_team_words()
    return set(norm_title.split()) - all_stop


def _transaction_name_overlap(norm_a: str, norm_b: str) -> bool:
    """Check if two transaction titles refer to the same player.

    Requires at least 2 shared player-name words (first + last), or if
    either title only has 1 name word (e.g. a mononymous player), requires
    that single word to match.
    """
    words_a = _extract_player_words(norm_a)
    words_b = _extract_player_words(norm_b)
    shared = words_a & words_b
    min_name_words = min(len(words_a), len(words_b))
    if min_name_words <= 1:
        return len(shared) >= 1 and len(shared) == min_name_words
    return len(shared) >= 2


def deduplicate(items: list[NewsItem]) -> list[list[NewsItem]]:
    """Group news items by story.

    Uses embedding cosine similarity when sentence-transformers is available,
    otherwise falls back to SequenceMatcher title similarity. Also checks
    team-based keyword overlap as a secondary signal.

    Transaction items are only deduped against other items that share at least
    one player name keyword, since structurally similar transaction titles
    (e.g. two "Exclusive Rights Signing" entries) can fool similarity metrics.

    Args:
        items: List of NewsItem objects from all sources.

    Returns:
        List of groups. Each group is a list of related NewsItems.
    """
    if not items:
        return []

    # Sort by published date (earliest first)
    sorted_items = sorted(items, key=lambda x: x.published)
    norm_titles = [_normalize_title(item.title) for item in sorted_items]

    # Try embedding-based similarity
    embeddings = _compute_embeddings(norm_titles)
    use_embeddings = embeddings is not None

    if use_embeddings:
        logger.info("Using embedding-based deduplication (%s).", EMBEDDING_MODEL_NAME)
    else:
        logger.info("Using SequenceMatcher-based deduplication.")

    groups: list[list[NewsItem]] = []
    assigned: set[int] = set()

    for i, item in enumerate(sorted_items):
        if i in assigned:
            continue

        group = [item]
        assigned.add(i)

        for j in range(i + 1, len(sorted_items)):
            if j in assigned:
                continue

            other = sorted_items[j]

            # Never merge two distinct transactions — their titles look
            # similar ("X: Team (Exclusive Rights Signing)") but each is a
            # separate event.  Only allow merging when a meaningful proper-
            # noun word appears in both titles (i.e. the same transaction
            # reported by two sources).
            if (getattr(item, "category", None) == "transaction"
                    or getattr(other, "category", None) == "transaction"):
                if not _transaction_name_overlap(norm_titles[i], norm_titles[j]):
                    continue

            # Primary: embedding or text similarity
            if use_embeddings:
                sim = _cosine_similarity(embeddings[i], embeddings[j])
                threshold = EMBEDDING_SIMILARITY_THRESHOLD
            else:
                sim = _text_similarity(norm_titles[i], norm_titles[j])
                threshold = SIMILARITY_THRESHOLD

            if sim >= threshold:
                group.append(other)
                assigned.add(j)
                continue

            # Secondary: same team + keyword overlap
            if (item.teams and other.teams
                    and set(item.teams) & set(other.teams)):
                overlap = _keyword_overlap(norm_titles[i], norm_titles[j])
                if overlap >= 0.5:
                    group.append(other)
                    assigned.add(j)

        groups.append(group)

    logger.info(
        "Deduplicated %d items into %d story groups.",
        len(items), len(groups),
    )
    return groups


# Source prefixes that are mostly aggregation / fan-blog coverage rather
# than original reporting. When a story group contains both a primary
# outlet (ESPN, Pro Football Talk, CBS, NFL.com, The Athletic, named
# beat writers) and one of these, the primary outlet should be the
# group's representative — even when the aggregator's body text is
# longer. Order doesn't matter; we just check `startswith`.
_NON_PRIMARY_SOURCE_PREFIXES: tuple[str, ...] = (
    "SBN ",        # SBN team blogs — mostly commentary on insider scoops
    "SI team ",    # SI per-team pages — mix, but mostly secondary
    "r/nfl",       # Reddit user-submitted pointers, not original reporting
)


def _is_primary_source(source: str) -> bool:
    if not source:
        return False
    return not any(source.startswith(p) for p in _NON_PRIMARY_SOURCE_PREFIXES)


def pick_primary(group: list[NewsItem]) -> NewsItem:
    """Pick the best item from a group to use as the primary source.

    Two-tier ranking:
      1. Original-reporting outlets (ESPN, Pro Football Talk, CBS Sports,
         NFL.com, The Athletic, beat writers — anything not on the
         non-primary prefix list) outrank aggregator/blog coverage (SBN
         team blogs, SI team pages, Reddit). When SBN is just commenting
         on an ESPN scoop, we cite ESPN.
      2. Within the same tier: longest summary (more body for the LLM),
         then earliest published.
    """
    return max(
        group,
        key=lambda x: (
            1 if _is_primary_source(x.source) else 0,
            len(x.summary or ""),
            -x.published.timestamp(),
        ),
    )


def flatten_groups(groups: list[list[NewsItem]]) -> list[NewsItem]:
    """Flatten groups back to a list, keeping only primary items.

    Secondary sources are noted in the primary item's summary.
    """
    results = []
    for group in groups:
        primary = pick_primary(group)
        if len(group) > 1:
            other_sources = [
                item.source for item in group if item is not primary
            ]
            primary.summary += f" [Also reported by: {', '.join(other_sources)}]"
        results.append(primary)
    return results


if __name__ == "__main__":
    # Test with sample data
    from datetime import datetime, timezone

    samples = [
        NewsItem("Chiefs sign WR Marquise Brown", "", "ESPN", "rss",
                 datetime.now(timezone.utc), "KC adds receiver", teams=["KC"]),
        NewsItem("Marquise Brown signing with Kansas City Chiefs", "", "PFT", "rss",
                 datetime.now(timezone.utc), "WR headed to KC", teams=["KC"]),
        NewsItem("Eagles trade for draft pick", "", "ESPN", "rss",
                 datetime.now(timezone.utc), "PHI moves up", teams=["PHI"]),
    ]

    groups = deduplicate(samples)
    for i, group in enumerate(groups):
        print(f"Group {i + 1}:")
        for item in group:
            print(f"  [{item.source}] {item.title}")
        print()
