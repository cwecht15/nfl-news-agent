"""Team abbreviation dialects.

Three dialects are in play across the pipeline:

* **news** — `config/teams.yaml` (ARI / BAL / CLE / HOU / LAR / LV / JAX …).
  Used by every collector, NewsItem.teams, OurLads output (except ARZ).
* **proj** — the projection sheets (ARZ / BLT / CLV / HST / LA). Also the
  master "2026 Depth Chart" sheet and the weekly in-season sheets.
* **per-source quirks** — OurLads slugs use ``ARZ``; nflverse uses ``LA``
  for the Rams; NFL.com's injury page uses ``AZ`` for Arizona.

Everything funnels through :func:`to_news` / :func:`to_proj` so the rest of
the code never hardcodes a special case again. The canonical news↔proj map
stays in ``scripts.transaction_reconciler`` (unchanged; imported here).
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.transaction_reconciler import NEWS_TO_PROJ_TEAM, PROJ_TO_NEWS_TEAM

# Per-source → news-style corrections. Anything not listed passes through.
SOURCE_TO_NEWS: dict[str, dict[str, str]] = {
    "news": {},
    "proj": dict(PROJ_TO_NEWS_TEAM),
    "ourlads": {"ARZ": "ARI"},
    "nflverse": {"LA": "LAR"},
    "nflcom": {"AZ": "ARI", "LA": "LAR"},
    "rotowire": {"LA": "LAR", "ARZ": "ARI", "JAC": "JAX", "WSH": "WAS"},
}

# Explicit reverse maps for the sources we also *write* abbreviations for.
NEWS_TO_SOURCE: dict[str, dict[str, str]] = {
    "news": {},
    "proj": dict(NEWS_TO_PROJ_TEAM),
    "ourlads": {"ARI": "ARZ"},
    "nflverse": {"LAR": "LA"},
}


def to_news(abbr: str, source: str = "news") -> str:
    """Normalize ``abbr`` from ``source``'s dialect to news-style."""
    if not abbr:
        return ""
    a = abbr.strip().upper()
    return SOURCE_TO_NEWS.get(source, {}).get(a, a)


def to_proj(abbr: str, source: str = "news") -> str:
    """Normalize ``abbr`` from ``source``'s dialect to projection-style."""
    news = to_news(abbr, source)
    return NEWS_TO_PROJ_TEAM.get(news, news)


def from_news(abbr: str, target: str) -> str:
    """Convert a news-style abbreviation into ``target``'s dialect."""
    if not abbr:
        return ""
    a = abbr.strip().upper()
    return NEWS_TO_SOURCE.get(target, {}).get(a, a)


@lru_cache(maxsize=1)
def _nickname_index() -> dict[str, str]:
    """{lowercase nickname or full name: news abbr} from teams.yaml."""
    from config_loader import get_teams

    idx: dict[str, str] = {}
    for t in get_teams():
        abbr = t["abbr"]
        name = t.get("name", "")
        if name:
            idx[name.lower()] = abbr
            # "New England Patriots" -> "patriots"; "Washington Commanders" -> "commanders"
            idx[name.split()[-1].lower()] = abbr
    # Common alternates that don't match the last word rule
    idx.setdefault("49ers", "SF")
    idx.setdefault("niners", "SF")
    idx.setdefault("football team", "WAS")
    return idx


def nickname_to_news(text: str) -> str:
    """Resolve a nickname or full team name ("Patriots", "Los Angeles Rams")
    to a news-style abbreviation. Returns "" when unknown."""
    if not text:
        return ""
    key = " ".join(text.strip().lower().split())
    idx = _nickname_index()
    if key in idx:
        return idx[key]
    last = key.split()[-1] if key else ""
    return idx.get(last, "")


def same_team(a: str, b: str, source_a: str = "news", source_b: str = "news") -> bool:
    """True when two abbreviations (possibly in different dialects) refer to the same club."""
    return to_news(a, source_a) == to_news(b, source_b) and bool(to_news(a, source_a))
