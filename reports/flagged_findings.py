"""Storage for user-flagged findings across daily reports.

Two flag modes share the same JSON store at data/flagged_findings.json,
distinguished by the entry's `mode` field:

- `handbook` (default, backward-compat for legacy entries): the long-
  running "Internal FP Handbook". Flags persist forever, accumulate
  across all daily reports, support category + author attribution.

- `daily`: the "Daily Site Report". Scoped per-report-date — the
  Flagged tab and PDF export filter by report_date so flags from one
  day don't bleed into another. Captures team + note only (no
  category, no author).

Flag IDs are deterministic. Handbook IDs are hashed from
`{date}|{section}|{content}` (unchanged from before, so existing
handbook entries keep their IDs). Daily IDs prepend `daily|` to the
hash input so the same content can be flagged in both modes without
collision.
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

MODE_HANDBOOK = "handbook"
MODE_DAILY = "daily"
ALL_MODES: tuple[str, ...] = (MODE_HANDBOOK, MODE_DAILY)


def _store_path() -> Path:
    return DATA_DIR / "flagged_findings.json"


def _flag_id(
    report_date: str,
    section: str,
    content: str,
    mode: str = MODE_HANDBOOK,
) -> str:
    """Deterministic flag ID. Handbook hash is unprefixed so legacy
    entries keep their existing IDs; daily hash prepends `daily|` so
    the same content can be flagged in both modes simultaneously
    without collision."""
    if mode == MODE_DAILY:
        payload = f"daily|{report_date}|{section}|{content}"
    else:
        payload = f"{report_date}|{section}|{content}"
    h = hashlib.sha256(payload.encode("utf-8"))
    return h.hexdigest()[:12]


def _normalize_mode(mode: str) -> str:
    return MODE_DAILY if mode == MODE_DAILY else MODE_HANDBOOK


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


def load_flags_by_mode(mode: str) -> list[dict[str, Any]]:
    """Return flags whose `mode` matches. Legacy entries (no `mode`
    field) are treated as handbook."""
    target = _normalize_mode(mode)
    out = []
    for f in load_flags():
        entry_mode = _normalize_mode(f.get("mode") or MODE_HANDBOOK)
        if entry_mode == target:
            out.append(f)
    return out


def _save(flags: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"flags": flags}, f, indent=2, ensure_ascii=False)


def get_flag_id(
    report_date: str,
    section: str,
    content: str,
    mode: str = MODE_HANDBOOK,
) -> str:
    return _flag_id(report_date, section, content, mode=_normalize_mode(mode))


def is_flagged(
    report_date: str,
    section: str,
    content: str,
    mode: str = MODE_HANDBOOK,
) -> bool:
    fid = _flag_id(report_date, section, content, mode=_normalize_mode(mode))
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
    category: str = "",
    note: str = "",
    team: str = "",
    sources: list[dict[str, Any]] | None = None,
    flagged_by: str = "",
    mode: str = MODE_HANDBOOK,
) -> dict[str, Any]:
    """Insert or update a flag. Returns the stored entry.

    `mode` selects between the long-running Handbook flags (default)
    and the per-day Daily Site Report flags. Handbook flags carry
    category + flagged_by; daily flags ignore those fields entirely.
    Last writer wins on the relevant fields per mode. The flag ID is
    derived from (mode, date, section, content), so the same content
    can carry one flag in each mode independently.
    """
    mode = _normalize_mode(mode)
    fid = _flag_id(report_date, section, content, mode=mode)
    flags = load_flags()
    now = datetime.now().isoformat(timespec="seconds")
    sources = sources or []
    flagged_by = (flagged_by or "").strip()
    for f in flags:
        if f.get("id") == fid:
            f["section_label"] = section_label
            f["team"] = team
            f["note"] = note
            f["mode"] = mode
            if sources:
                f["sources"] = sources
            if mode == MODE_HANDBOOK:
                f["category"] = category
                if flagged_by:
                    f["flagged_by"] = flagged_by
            f["updated_at"] = now
            _save(flags)
            return f
    entry: dict[str, Any] = {
        "id": fid,
        "report_date": report_date,
        "section": section,
        "section_label": section_label,
        "team": team,
        "content": content,
        "note": note,
        "sources": sources,
        "flagged_at": now,
        "mode": mode,
    }
    if mode == MODE_HANDBOOK:
        entry["category"] = category
        entry["flagged_by"] = flagged_by
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
