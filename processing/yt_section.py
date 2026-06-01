"""Build the YouTube-only report section.

Used by:
- `scripts/run_daily.py` when invoked with `--include-yt-section`, to attach
  a YT block to the locally-generated daily report.
- `dashboard/pages/yt_report.py` for on-demand date-range reports rendered
  directly in the dashboard.

The section has two subsections:
- Press-conference summary (one prose block, scored across all transcripts).
- Per-team transcript bullets (same prompt rules as the news-side Team Notes).

Returns an empty dict when transcripts is empty so callers can `if yt:`.
"""

import logging
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import Transcript
from processing.summarizer import (
    _init_usage_tracker,
    _resolve_client_and_runtime,
    summarize_press_conferences,
    summarize_team_highlights_from_transcripts,
)

logger = logging.getLogger(__name__)


def build_yt_section(
    transcripts: list[Transcript],
    client: Optional[Any] = None,
    usage_tracker: Optional[dict[str, Any]] = None,
    date_label: str = "",
    pre_filtered: bool = False,
) -> dict:
    """Summarize transcripts into the YouTube report section.

    Args:
        transcripts: All transcripts to summarize. Press-conf scoring trims
            internally; the per-team subsection sees the same selection.
        client: Optional pre-resolved LLM client. When None, builds one
            from the active provider config.
        usage_tracker: Optional shared usage tracker. When None, this
            function creates its own and includes it in the result so
            callers (e.g. the dashboard tab) can render token cost.
        date_label: Free-form label for logging / display (e.g.
            "2026-05-04" or "2026-04-25 → 2026-05-01").
        pre_filtered: When True, the caller has already curated the
            transcript list (e.g. user picked specific videos). Skip the
            internal press-relevance filter and use every transcript.

    Returns:
        {
            "press_conferences": {"summary": str, "count": int,
                                   "sources": [{"team","title","url","channel"}]},
            "team_notes": {team_abbr: {"summary": str,
                                        "numbered_sources": [...]}},
            "date_label": str,
            "transcript_count": int,
            "llm_usage": {...},  # only when this function owns the tracker
        }
        or {} when transcripts is empty.
    """
    if not transcripts:
        return {}

    client, runtime = _resolve_client_and_runtime(client)

    own_tracker = usage_tracker is None
    if own_tracker:
        usage_tracker = _init_usage_tracker(runtime)

    logger.info(
        "Building YT section: %d transcripts (%s)",
        len(transcripts),
        date_label or "no label",
    )

    press_summary, press_count, press_sources = summarize_press_conferences(
        transcripts,
        client,
        usage_tracker=usage_tracker,
        pre_filtered=pre_filtered,
    )

    team_notes = summarize_team_highlights_from_transcripts(
        transcripts,
        client,
        usage_tracker=usage_tracker,
        pre_filtered=pre_filtered,
    )

    result: dict[str, Any] = {
        "press_conferences": {
            "summary": press_summary,
            "count": press_count,
            "sources": press_sources,
        },
        "team_notes": team_notes,
        "date_label": date_label,
        "transcript_count": len(transcripts),
    }
    if own_tracker:
        result["llm_usage"] = usage_tracker
    return result
