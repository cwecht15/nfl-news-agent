"""Storage for user-flagged findings across daily reports.

Items in any daily report (bullets, paragraphs, tables, team highlights)
can be flagged with a category and optional note. The store is a single
JSON file at data/flagged_findings.json. Flag IDs are deterministic so
re-flagging the same item is idempotent.
"""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import DATA_DIR

CATEGORIES = ["Injury", "Depth Chart", "Coaching", "Player Take"]


def _store_path() -> Path:
    return DATA_DIR / "flagged_findings.json"


def _flag_id(report_date: str, section: str, content: str) -> str:
    h = hashlib.sha256(f"{report_date}|{section}|{content}".encode("utf-8"))
    return h.hexdigest()[:12]


def load_flags() -> list[dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    return data.get("flags", [])


def _save(flags: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"flags": flags}, f, indent=2, ensure_ascii=False)


def get_flag_id(report_date: str, section: str, content: str) -> str:
    return _flag_id(report_date, section, content)


def is_flagged(report_date: str, section: str, content: str) -> bool:
    fid = _flag_id(report_date, section, content)
    return any(f.get("id") == fid for f in load_flags())


def get_flag(flag_id: str) -> dict[str, Any] | None:
    for f in load_flags():
        if f.get("id") == flag_id:
            return f
    return None


def add_or_update_flag(
    report_date: str,
    section: str,
    section_label: str,
    content: str,
    category: str,
    note: str = "",
    team: str = "",
    sources: list[dict[str, Any]] | None = None,
    flagged_by: str = "",
) -> dict[str, Any]:
    """Insert or update a flag. Returns the stored entry.

    `flagged_by` is the visitor's display name for attribution. Last
    writer wins: re-flagging the same item with a different name
    overwrites the prior attribution. We deliberately don't keep a
    history of every flagger because the flag ID is deterministic on
    (date, section, content) — a true "everyone who's ever flagged this"
    list would inflate JSON size and isn't useful for the small invited
    group this is built for.
    """
    fid = _flag_id(report_date, section, content)
    flags = load_flags()
    now = datetime.now().isoformat(timespec="seconds")
    sources = sources or []
    flagged_by = (flagged_by or "").strip()
    for f in flags:
        if f.get("id") == fid:
            f["category"] = category
            f["note"] = note
            f["section_label"] = section_label
            f["team"] = team
            if sources:
                f["sources"] = sources
            if flagged_by:
                f["flagged_by"] = flagged_by
            f["updated_at"] = now
            _save(flags)
            return f
    entry = {
        "id": fid,
        "report_date": report_date,
        "section": section,
        "section_label": section_label,
        "category": category,
        "team": team,
        "content": content,
        "note": note,
        "sources": sources,
        "flagged_at": now,
        "flagged_by": flagged_by,
    }
    flags.append(entry)
    _save(flags)
    return entry


def update_flag_fields(flag_id: str, **fields: Any) -> bool:
    flags = load_flags()
    for f in flags:
        if f.get("id") == flag_id:
            f.update(fields)
            f["updated_at"] = datetime.now().isoformat(timespec="seconds")
            _save(flags)
            return True
    return False


def remove_flag(flag_id: str) -> bool:
    flags = load_flags()
    new_flags = [f for f in flags if f.get("id") != flag_id]
    if len(new_flags) == len(flags):
        return False
    _save(new_flags)
    return True
