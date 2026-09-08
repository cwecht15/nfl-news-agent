"""Phase-aware data source for the Projections dashboard page.

The page was written against the preseason snapshot layout
(``data/projections/<date>/{players,fantasy,teams}.json`` + one
``changelog.csv``). In-season the pipeline snapshots the weekly sheets into
``data/weekly_projections/<season>/wk<NN>/<sheet>/<date>/`` instead, with
different file names and changelog ``kind`` labels. This module presents
both layouts through one small interface so the page's tabs keep working
unchanged:

    src = get_projection_source()
    src.dates()               # newest first
    src.load(date, "players" | "fantasy" | "teams")
    src.changelog()           # rows with kind in {player, fantasy, team}
    src.week_for_date(date)   # None offseason

In-season mapping: ``players`` = players.json + kickers.json, ``fantasy`` =
output.json (weekly PPR + POS Rank), ``teams`` = games.json. When both
sheets were snapshotted on a date (Tuesdays) the one showing the higher
Current Week wins, tie → primary — the same rule the pipeline uses.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_settings
from processing import season as season_mod

PRESEASON_DIR = PROJECT_ROOT / "data" / "projections"
WEEKLY_DIR = PROJECT_ROOT / "data" / "weekly_projections"

# weekly changelog kind → preseason kind the page filters on
_KIND_MAP = {"players": "player", "kickers": "player", "games": "team", "output": "fantasy"}


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class PreseasonSource:
    mode = "offseason"
    label = "Preseason sheet"
    points_label = "season PPR"

    def __init__(self, base: Optional[Path] = None):
        self.base = base or PRESEASON_DIR   # resolved at call time (tests monkeypatch the module dirs)

    def dates(self) -> list[str]:
        if not self.base.exists():
            return []
        return sorted([d.name for d in self.base.iterdir() if d.is_dir()], reverse=True)

    def load(self, date: str, kind: str) -> Optional[dict]:
        return _read_json(self.base / date / f"{kind}.json")

    def changelog(self) -> list[dict]:
        path = self.base / "changelog.csv"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def week_for_date(self, date: str) -> Optional[int]:
        return None

    def dates_same_week(self, date: str) -> list[str]:
        return self.dates()


class WeeklySource:
    mode = "in_season"
    points_label = "this week's PPR"

    def __init__(self, season: int, base: Optional[Path] = None):
        self.season = season
        self.base = (base or WEEKLY_DIR) / str(season)
        self._index: Optional[dict[str, dict]] = None

    # -- index: date -> chosen snapshot dir (+ week/sheet) -------------------
    def _build_index(self) -> dict[str, dict]:
        idx: dict[str, dict] = {}
        if not self.base.exists():
            return idx
        for wk_dir in self.base.glob("wk*"):
            try:
                week = int(wk_dir.name[2:])
            except ValueError:
                continue
            for sheet_dir in wk_dir.iterdir():
                if not sheet_dir.is_dir():
                    continue
                sheet = sheet_dir.name
                for date_dir in sheet_dir.iterdir():
                    if not date_dir.is_dir() or not (date_dir / "players.json").exists():
                        continue
                    cand = {"dir": date_dir, "week": week, "sheet": sheet}
                    cur = idx.get(date_dir.name)
                    if cur is None or week > cur["week"] or (week == cur["week"] and sheet == "primary" and cur["sheet"] != "primary"):
                        idx[date_dir.name] = cand
        return idx

    @property
    def index(self) -> dict[str, dict]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    @property
    def label(self) -> str:
        dates = self.dates()
        if not dates:
            return "Weekly sheet"
        info = self.index[dates[0]]
        return f"Weekly sheet · Week {info['week']} · {info['sheet']}"

    def dates(self) -> list[str]:
        return sorted(self.index, reverse=True)

    def week_for_date(self, date: str) -> Optional[int]:
        info = self.index.get(date)
        return info["week"] if info else None

    def sheet_for_date(self, date: str) -> Optional[str]:
        info = self.index.get(date)
        return info["sheet"] if info else None

    def dates_same_week(self, date: str) -> list[str]:
        wk = self.week_for_date(date)
        return sorted(d for d, i in self.index.items() if i["week"] == wk)

    def load(self, date: str, kind: str) -> Optional[dict]:
        info = self.index.get(date)
        if not info:
            return None
        d = info["dir"]
        if kind == "players":
            players = _read_json(d / "players.json") or {}
            kickers = _read_json(d / "kickers.json") or {}
            merged = dict(players)
            for gid, rec in kickers.items():
                merged.setdefault(gid, {**rec, "pos": rec.get("pos") or "K"})
            return merged or None
        if kind == "fantasy":
            return _read_json(d / "output.json")
        if kind == "teams":
            return _read_json(d / "games.json")
        return _read_json(d / f"{kind}.json")

    def changelog(self) -> list[dict]:
        path = self.base.parent / "changelog.csv"
        if not path.exists():
            return []
        rows: list[dict] = []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if str(row.get("season") or "") not in ("", str(self.season)):
                    continue
                info = self.index.get(row.get("date", ""))
                # keep only the sheet the page shows for that date
                if info and row.get("sheet") and row["sheet"] != info["sheet"]:
                    continue
                row = dict(row)
                row["kind"] = _KIND_MAP.get(row.get("kind", ""), row.get("kind", ""))
                rows.append(row)
        return rows


def get_projection_source(settings: Optional[dict] = None):
    """WeeklySource in-season (when weekly snapshots exist), else PreseasonSource."""
    settings = settings or get_settings()
    if season_mod.is_in_season(settings):
        src = WeeklySource(season_mod.get_season_year(settings))
        if src.dates():
            return src
    return PreseasonSource()
