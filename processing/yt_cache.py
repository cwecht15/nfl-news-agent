"""Durable disk cache for generated YouTube Report sections.

The dashboard's `@st.cache_data` is an in-memory cache that dies on every
Streamlit redeploy (which happens ~daily when the pipeline commits data).
This adds a disk layer keyed by the inputs that fully determine the output
— the date range and the exact set of video IDs summarized — so re-opening
a previously generated range is instant and spends no LLM tokens.

Locally the cache persists indefinitely; on Streamlit Cloud it survives
until the next redeploy. Cache files are deliberately NOT committed by the
CI workflow, so they never bloat the repo.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from config_loader import get_data_dir


def make_key(
    start_iso: str,
    end_iso: str,
    team_filter: Iterable[str],
    video_ids: Iterable[str],
) -> str:
    """Stable cache key from the inputs that determine the summary output.

    Order-independent in `team_filter` / `video_ids` (they're sorted), so
    the same selection produces the same key regardless of UI ordering.
    """
    payload = json.dumps(
        {
            "start": start_iso,
            "end": end_iso,
            "teams": sorted(team_filter or []),
            "videos": sorted(video_ids or []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _path(key: str) -> Path:
    return get_data_dir("yt_reports") / f"{key}.json"


def load(key: str) -> Optional[dict[str, Any]]:
    """Return the cached record ({"generated_at", "meta", "section"}) or None."""
    path = _path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(key: str, section: dict[str, Any], meta: Optional[dict[str, Any]] = None) -> None:
    """Persist a generated section under the given key."""
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": meta or {},
        "section": section,
    }
    with open(_path(key), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
