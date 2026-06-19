"""Durable disk cache for generated Twitter Report sections.

Mirror of processing/podcast_cache.py for the Twitter tab: a disk layer under
the dashboard's in-memory @st.cache_data, keyed by the inputs that fully
determine the output (date range + team filter + the exact set of tweet IDs
summarized), so re-opening a previously generated range is instant and spends
no LLM tokens. Cache files are gitignored and never committed.
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
    tweet_ids: Iterable[str],
) -> str:
    payload = json.dumps(
        {
            "start": start_iso,
            "end": end_iso,
            "teams": sorted(team_filter or []),
            "tweets": sorted(tweet_ids or []),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _path(key: str) -> Path:
    return get_data_dir("twitter_reports") / f"{key}.json"


def load(key: str) -> Optional[dict[str, Any]]:
    path = _path(key)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(key: str, section: dict[str, Any], meta: Optional[dict[str, Any]] = None) -> None:
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": meta or {},
        "section": section,
    }
    with open(_path(key), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False)
