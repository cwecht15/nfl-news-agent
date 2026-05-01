"""Source health monitoring.

Tracks per-source success/failure rates over time.
Surfaces persistent issues as warnings in the dashboard.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HEALTH_FILE = Path(__file__).parent.parent / "data" / "source_health.json"
FAILURE_THRESHOLD = 3  # consecutive failures before warning


def _load_health() -> dict:
    """Load the source health history."""
    if not HEALTH_FILE.exists():
        return {"sources": {}}
    try:
        with open(HEALTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, KeyError):
        return {"sources": {}}


def _save_health(data: dict):
    """Persist the source health history."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HEALTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def record_source_result(
    source_name: str,
    item_count: int,
    error: str = "",
    low_volume: bool = False,
):
    """Record a collection result for a source.

    Args:
        source_name: The source identifier (e.g., "ESPN NFL", "NFL.com Transactions").
        item_count: Number of items collected (0 if failed).
        error: Error message if the source failed.
        low_volume: When True, a 0-item run with no error is treated as a
            quiet-day success rather than a failure. Use for sources that
            legitimately produce nothing on some days (beat writers,
            YouTube press conferences) so the alert system doesn't fire on
            normal lulls. Default False keeps the strict behavior for RSS,
            Web, Reddit, ESPN — where 0 items really does mean something
            broke.
    """
    data = _load_health()
    sources = data.setdefault("sources", {})
    entry = sources.setdefault(source_name, {
        "total_runs": 0,
        "total_failures": 0,
        "consecutive_failures": 0,
        "last_success": None,
        "last_failure": None,
        "last_error": "",
        "history": [],
    })

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry["total_runs"] += 1

    is_failure = bool(error) or (not low_volume and item_count == 0)

    if is_failure:
        entry["total_failures"] += 1
        entry["consecutive_failures"] += 1
        entry["last_failure"] = now
        entry["last_error"] = error or "returned 0 items"
        entry["history"].append({"date": now, "status": "fail", "items": 0, "error": error})
    else:
        entry["consecutive_failures"] = 0
        entry["last_success"] = now
        entry["last_error"] = ""
        entry["history"].append({"date": now, "status": "ok", "items": item_count})

    # Keep last 30 entries
    entry["history"] = entry["history"][-30:]
    _save_health(data)


def get_health_alerts() -> list[dict]:
    """Return alerts for sources with persistent failures."""
    data = _load_health()
    alerts = []

    for source_name, entry in data.get("sources", {}).items():
        consecutive = entry.get("consecutive_failures", 0)
        if consecutive >= FAILURE_THRESHOLD:
            last_success = entry.get("last_success", "never")
            alerts.append({
                "source": source_name,
                "severity": "warning",
                "message": (
                    f"{source_name} has failed {consecutive} consecutive times. "
                    f"Last success: {last_success}. "
                    f"Last error: {entry.get('last_error', 'unknown')}"
                ),
            })

    return alerts


def get_health_summary() -> dict[str, dict]:
    """Return the full health summary for dashboard display."""
    data = _load_health()
    return data.get("sources", {})
