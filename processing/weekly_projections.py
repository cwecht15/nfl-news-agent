"""Weekly in-season projection snapshots + diffs.

Why this exists separately from ``scripts/snapshot_projections.py``: that
script is the *offseason* tracker (one preseason sheet, fixed rows, writes
``data/projections/``) and the Projections dashboard page assumes preseason
semantics for everything under that tree. In-season the user works in two
weekly sheets (``primary`` plus a ``secondary`` copy used on Tuesday to start
the next week while MNF is pending), each with its own ``Current Week`` cell
and a different tab layout. So this module:

* reads the four weekly tabs (``Working_Game_Proj``, ``Working_Player_Proj``,
  ``Player_Projections``, ``Working_Kicker_Proj``) with **label-located**
  header rows rather than fixed row numbers;
* writes week-scoped snapshots under a separate root
  ``data/weekly_projections/<season>/wk<NN>/<sheet>/<date>/`` so the
  offseason tree is never touched;
* diffs each snapshot against the most recent *earlier* snapshot for the
  same (week, sheet) — falling back to the other sheet's latest for that
  week, which is how Tuesday's secondary work becomes Wednesday's main-sheet
  baseline;
* keeps its own ``changelog.csv`` and an ``active.json`` pointer so the
  projection audit and the dashboard can find "this week's working sheet"
  without a gspread client.

Pure helpers from the offseason script are reused unchanged
(``_build_player_col_map`` for Adj detection + duplicate-label
disambiguation, ``_safe_float``, ``diff_snapshots``, ``diff_fantasy``).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings
from processing import season as season_mod
from scripts.snapshot_projections import (
    SKIP_HEADERS,
    _build_player_col_map,
    _safe_float,
    diff_fantasy,
    diff_snapshots,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KINDS: tuple[str, ...] = ("players", "games", "output", "kickers")
SHEET_LABELS: tuple[str, ...] = ("primary", "secondary")
DEFAULT_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")

# Projection-style abbreviations, in the order the 32 team blocks appear in
# Working_Player_Proj. Used to strip the trailing team token from "Name TEAM".
PROJ_TEAMS: tuple[str, ...] = (
    "ARZ", "ATL", "BLT", "BUF", "CAR", "CHI", "CIN", "CLV", "DAL", "DEN", "DET",
    "GB", "HST", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA", "MIN", "NE", "NO",
    "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
)

# The weekly tabs carry two extra historical/reference columns the preseason
# helper doesn't know about. They are blanked before the shared col-map
# builder sees the header (blank cells are skipped), so SKIP_HEADERS in the
# offseason script stays untouched.
WEEKLY_SKIP_HEADERS: frozenset[str] = frozenset(SKIP_HEADERS) | {"L1", "2023"}

# Working_Player_Proj fixed identity columns (0-indexed)
P_TEAM, P_HOME, P_OPP, P_SLOT, P_STATUS, P_ID, P_POS, P_NAME = 0, 2, 3, 4, 5, 6, 7, 8
P_METRIC_START = 9  # col J

# Working_Game_Proj (header row 4 → index 3)
G_HEADER_ROW = 3
G_TEAM, G_HOME_AWAY, G_OPP = 1, 2, 3
G_METRIC_START = 4  # col E

# Working_Kicker_Proj (header row 2 → index 1)
K_HEADER_ROW = 1
K_ID, K_NAME, K_POS, K_TEAM = 1, 2, 3, 4
K_METRIC_START = 5  # col F

CHANGELOG_FIELDS: tuple[str, ...] = (
    "date", "season", "week", "sheet", "kind", "key", "label", "type",
    "metric", "old_value", "new_value", "details",
)

_BAD_IDS = {"", "#N/A", "#REF!", "N/A", "-"}
# Game rows carry a 2-3 letter abbreviation in col B; the tab also has
# league summary rows at the bottom ("League Σ/32", "Target 23-25").
_TEAM_ABBR_RE = re.compile(r"^[A-Z]{2,3}$")


# ---------------------------------------------------------------------------
# Paths (module-level so tests can monkeypatch ``_base_dir``)
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    """Root of the weekly tree. Tests patch this to a tmp dir."""
    return get_data_dir("weekly_projections")


def _week_dir(season: int, week: int, sheet: str) -> Path:
    return _base_dir() / str(season) / f"wk{int(week):02d}" / sheet


def snapshot_dir(season: int, week: int, sheet: str, date_str: str) -> Path:
    """``data/weekly_projections/<season>/wk<NN>/<sheet>/<date>/`` (not created)."""
    return _week_dir(season, week, sheet) / date_str


def _rel_dir(path: Path) -> str:
    """Portable (posix, base-relative) form of a snapshot dir for pointers."""
    try:
        return path.relative_to(_base_dir()).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_dir(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else _base_dir() / p


def _changelog_path() -> Path:
    return _base_dir() / "changelog.csv"


def _active_path(season: int) -> Path:
    return _base_dir() / str(season) / "active.json"


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def _cell(row: list, idx: int) -> str:
    return str(row[idx]).strip() if idx < len(row) and row[idx] is not None else ""


def _col_letter(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA (spreadsheet column letters)."""
    letters = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _int_or_none(val: str) -> Optional[int]:
    val = (val or "").strip()
    if not val:
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


def _strip_team_suffix(name: str, team: str = "") -> str:
    """'Josh Allen BUF' → 'Josh Allen'. Only strips a trailing token that is
    the row's own team or a known projection abbreviation, so 'Robert
    Griffin III' keeps its suffix."""
    tokens = (name or "").split()
    if len(tokens) > 1:
        last = tokens[-1].upper()
        if (team and last == team.upper()) or last in PROJ_TEAMS:
            return " ".join(tokens[:-1])
    return " ".join(tokens)


def _mask_skip_headers(header: list) -> list[str]:
    """Copy of the header with weekly-only reference columns blanked."""
    return ["" if _cell(header, i) in WEEKLY_SKIP_HEADERS else str(c) for i, c in enumerate(header)]


def _unique_labels(col_map: list[tuple[int, str, bool]]) -> list[tuple[int, str, bool]]:
    """Guarantee distinct metric labels.

    ``_build_player_col_map`` disambiguates the duplicate "YPA Adj" pair by
    group, but the weekly tabs also repeat plain labels ("Lock", "RuTD",
    "FG Atts") that have no group prefix to lean on. Any label still shared
    after that pass gets its column letter appended so nothing silently
    overwrites another metric in the snapshot dict.
    """
    counts = Counter(label for _, label, _ in col_map)
    out = []
    for idx, label, is_adj in col_map:
        if counts[label] > 1:
            label = f"{label} ({_col_letter(idx)})"
        out.append((idx, label, is_adj))
    return out


def _build_col_map(header: list, col_start: int) -> list[tuple[int, str, bool]]:
    masked = _mask_skip_headers(header)
    return _unique_labels(_build_player_col_map(masked, col_start=col_start, col_end=len(masked)))


def _metrics(row: list, col_map: list[tuple[int, str, bool]]) -> dict[str, Any]:
    return {label: _safe_float(_cell(row, idx)) for idx, label, _ in col_map if idx < len(row)}


def _header_signature(header: list, start: int) -> tuple[str, ...]:
    sig = [_cell(header, i) for i in range(start, len(header))]
    while sig and not sig[-1]:
        sig.pop()
    return tuple(sig)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _settings(settings: Optional[dict]) -> dict:
    return settings if settings is not None else get_settings()


def _positions_from_settings(settings: Optional[dict]) -> list[str]:
    cfg = season_mod.get_in_season_projection_settings(settings)
    pos = cfg.get("positions") or list(DEFAULT_POSITIONS)
    return [str(p).strip().upper() for p in pos]


# ---------------------------------------------------------------------------
# Sheet meta
# ---------------------------------------------------------------------------


def read_sheet_meta(gc, sheet_id: str, settings: Optional[dict] = None) -> dict:
    """{'season', 'week'} from ``Working_Game_Proj!C1:C2`` (thin wrapper)."""
    return season_mod.read_sheet_week(gc, sheet_id, settings)


def meta_from_game_values(values: list[list[str]]) -> dict:
    """Same as :func:`read_sheet_meta` but from an already-fetched grid."""
    season = _int_or_none(_cell(values[0], 2)) if values else None
    week = _int_or_none(_cell(values[1], 2)) if len(values) > 1 else None
    return {"season": season, "week": week}


# ---------------------------------------------------------------------------
# Parsers (pure; take the raw grid from ``get_all_values()``)
# ---------------------------------------------------------------------------


def _is_player_header(row: list) -> bool:
    return _cell(row, P_ID) == "ID" and _cell(row, P_POS) == "Pos"


def parse_player_blocks(
    values: list[list[str]],
    positions: Optional[Iterable[str]] = None,
) -> dict[str, dict]:
    """``Working_Player_Proj`` → ``{gsis: {player_id, name, team, pos, slot,
    depth, status, home, home_away, opp, metrics}}``.

    The tab is 32 team blocks, each with its own header row (``G=="ID"`` and
    ``H=="Pos"``). The column map is built from the first header; later
    headers are compared from col J on (cols A–D carry the block's team /
    home / opp and legitimately differ) and a warning is logged on drift.
    Rows with a blank GSIS are placeholders; ``G=="TEAM"`` ends a block.

    ``slot`` is the raw ``#`` column, which is a *global* running row number
    (1..N across the whole tab), so it shifts for everyone below an inserted
    row. ``depth`` is the derived 1-based order of the player within their
    team block and position (QB1, RB2, ...) — use that for depth-chart
    questions.

    ``positions=None`` keeps the default fantasy set; pass an empty list to
    keep every position.
    """
    keep = set(DEFAULT_POSITIONS) if positions is None else {p.upper() for p in positions}
    players: dict[str, dict] = {}
    col_map: Optional[list[tuple[int, str, bool]]] = None
    first_sig: tuple[str, ...] = ()
    in_block = False
    depth_by_pos: Counter = Counter()

    for row_idx, row in enumerate(values):
        if _is_player_header(row):
            if col_map is None:
                col_map = _build_col_map(row, P_METRIC_START)
                first_sig = _header_signature(row, P_METRIC_START)
            else:
                sig = _header_signature(row, P_METRIC_START)
                if sig != first_sig:
                    logger.warning(
                        "Working_Player_Proj header drift in %s block (row %d): "
                        "metric columns differ from the first block",
                        _cell(row, P_TEAM) or "?", row_idx + 1,
                    )
            in_block = True
            depth_by_pos = Counter()
            continue

        if not in_block or col_map is None:
            continue

        gsis = _cell(row, P_ID)
        if gsis == "TEAM":
            in_block = False
            continue
        if gsis in _BAD_IDS:
            continue

        pos = _cell(row, P_POS).upper()
        depth_by_pos[pos] += 1
        if keep and pos not in keep:
            continue

        team = _cell(row, P_TEAM).upper()
        home = _cell(row, P_HOME).upper()
        if gsis in players:
            logger.warning("Duplicate GSIS %s in Working_Player_Proj (%s); keeping first", gsis, team)
            continue

        players[gsis] = {
            "player_id": gsis,
            "name": _strip_team_suffix(_cell(row, P_NAME), team),
            "team": team,
            "pos": pos,
            "slot": _int_or_none(_cell(row, P_SLOT)),
            "depth": depth_by_pos[pos],
            "status": _cell(row, P_STATUS),
            "home": home,
            "home_away": ("Home" if home == team else "Away") if home and team else "",
            "opp": _cell(row, P_OPP).upper(),
            "metrics": _metrics(row, col_map),
        }

    if col_map is None:
        logger.warning("Working_Player_Proj: no header row (G=='ID', H=='Pos') found")
    return players


def _find_game_header(values: list[list[str]]) -> int:
    for i, row in enumerate(values[:10]):
        if _cell(row, G_TEAM) == "Team" and _cell(row, G_OPP) == "Opp":
            return i
    return G_HEADER_ROW


def parse_game_rows(values: list[list[str]]) -> dict[str, dict]:
    """``Working_Game_Proj`` → ``{team: {team, home_away, opp, metrics}}``.

    Rows with a team abbreviation in col B are game rows; blank separator
    rows, the per-game totals row (values only in K / AM, no team) and the
    league summary rows at the bottom ("League Σ/32", "Δ vs target") are
    skipped. Col C has no header label in the sheet, so it is forced to
    "Home/Away".
    """
    if not values:
        return {}
    h_idx = _find_game_header(values)
    header = list(values[h_idx])
    if len(header) <= G_HOME_AWAY:
        header += [""] * (G_HOME_AWAY + 1 - len(header))
    header[G_HOME_AWAY] = header[G_HOME_AWAY] or "Home/Away"
    col_map = _build_col_map(header, G_METRIC_START)

    games: dict[str, dict] = {}
    for row in values[h_idx + 1:]:
        team = _cell(row, G_TEAM).upper()
        if not team:
            continue
        if not _TEAM_ABBR_RE.match(team):
            logger.debug("Skipping non-team row in Working_Game_Proj: %r", _cell(row, G_TEAM))
            continue
        if team in games:
            logger.warning("Duplicate team row %s in Working_Game_Proj; keeping first", team)
            continue
        games[team] = {
            "team": team,
            "home_away": _cell(row, G_HOME_AWAY),
            "opp": _cell(row, G_OPP).upper(),
            "metrics": _metrics(row, col_map),
        }
    return games


def parse_output(values: list[list[str]]) -> dict[str, dict]:
    """``Player_Projections`` → ``{id: {name, pos, team, opp, ppr, pos_rank,
    half_ppr, ppr_g, stats}}``.

    ``POS Rank`` is numeric in this sheet ("72"); it is re-synthesized as
    ``"TE72"`` so the offseason ``diff_fantasy`` / ``_parse_rank_number``
    work unchanged. ``half_ppr`` / ``ppr_g`` don't exist here and are None.
    """
    if not values:
        return {}
    header = [_cell(values[0], i) for i in range(len(values[0]))]
    idx = {h.lower(): i for i, h in enumerate(header) if h}

    def col(*names: str) -> Optional[int]:
        for n in names:
            if n.lower() in idx:
                return idx[n.lower()]
        return None

    c_id, c_name, c_pos = col("ID"), col("Name"), col("Pos")
    c_team, c_opp, c_ppr = col("Team"), col("Opp"), col("PPR")
    c_rank = col("POS Rank", "Pos Rank", "Rank")
    if c_id is None:
        logger.warning("Player_Projections: no ID column in header row")
        return {}
    identity = {c for c in (col("#"), c_id, c_name, c_pos, c_team, c_opp, c_ppr, c_rank) if c is not None}
    stat_cols = [(i, h) for i, h in enumerate(header) if h and i not in identity]

    out: dict[str, dict] = {}
    for row in values[1:]:
        pid = _cell(row, c_id)
        if pid in _BAD_IDS:
            continue
        pos = _cell(row, c_pos).upper() if c_pos is not None else ""
        team = _cell(row, c_team).upper() if c_team is not None else ""
        rank_raw = _cell(row, c_rank) if c_rank is not None else ""
        rank_num = _int_or_none(rank_raw)
        if rank_num is not None:
            pos_rank = f"{pos}{rank_num}"
        else:
            pos_rank = rank_raw  # already "WR12"-style, or blank
        if pid in out:
            logger.warning("Duplicate ID %s in Player_Projections; keeping first", pid)
            continue
        out[pid] = {
            "name": _strip_team_suffix(_cell(row, c_name), team) if c_name is not None else "",
            "pos": pos,
            "team": team,
            "opp": _cell(row, c_opp).upper() if c_opp is not None else "",
            "ppr": _safe_float(_cell(row, c_ppr)) if c_ppr is not None else None,
            "pos_rank": pos_rank,
            "half_ppr": None,
            "ppr_g": None,
            "stats": {h: _safe_float(_cell(row, i)) for i, h in stat_cols},
        }
    return out


def _find_kicker_header(values: list[list[str]]) -> int:
    for i, row in enumerate(values[:10]):
        if _cell(row, K_ID) == "ID":
            return i
    return K_HEADER_ROW


def parse_kickers(values: list[list[str]]) -> dict[str, dict]:
    """``Working_Kicker_Proj`` → same shape as :func:`parse_player_blocks`
    (``pos`` "K", ``slot`` None, ``status`` ""). Rows whose ID is ``#N/A``
    (unmatched lookup) are skipped.
    """
    if not values:
        return {}
    h_idx = _find_kicker_header(values)
    col_map = _build_col_map(values[h_idx], K_METRIC_START)

    kickers: dict[str, dict] = {}
    for row in values[h_idx + 1:]:
        pid = _cell(row, K_ID)
        if pid in _BAD_IDS:
            continue
        team = _cell(row, K_TEAM).upper()
        if pid in kickers:
            logger.warning("Duplicate ID %s in Working_Kicker_Proj; keeping first", pid)
            continue
        kickers[pid] = {
            "player_id": pid,
            "name": _strip_team_suffix(_cell(row, K_NAME), team),
            "team": team,
            "pos": _cell(row, K_POS).upper() or "K",
            "slot": None,
            "depth": None,
            "status": "",
            "home": "",
            "home_away": "",
            "opp": "",
            "metrics": _metrics(row, col_map),
        }
    return kickers


# ---------------------------------------------------------------------------
# Fetch + snapshot
# ---------------------------------------------------------------------------


def fetch_sheet_tables(gc, sheet_id: str, settings: Optional[dict] = None) -> dict[str, list[list[str]]]:
    """Raw grids for the four weekly tabs: {games, players, output, kickers}."""
    cfg = season_mod.get_in_season_projection_settings(settings)
    sh = gc.open_by_key(sheet_id)
    tabs = {
        "games": cfg.get("game_sheet", "Working_Game_Proj"),
        "players": cfg.get("player_sheet", "Working_Player_Proj"),
        "output": cfg.get("output_sheet", "Player_Projections"),
        "kickers": cfg.get("kicker_sheet", "Working_Kicker_Proj"),
    }
    return {kind: sh.worksheet(title).get_all_values() for kind, title in tabs.items()}


def parse_sheet_tables(tables: dict[str, list[list[str]]], settings: Optional[dict] = None) -> dict:
    """Parse fetched grids → {"meta": {season, week}, players, games, output, kickers}."""
    positions = _positions_from_settings(settings)
    return {
        "meta": meta_from_game_values(tables.get("games", [])),
        "players": parse_player_blocks(tables.get("players", []), positions),
        "games": parse_game_rows(tables.get("games", [])),
        "output": parse_output(tables.get("output", [])),
        "kickers": parse_kickers(tables.get("kickers", [])),
    }


def fetch_sheet(gc, label: str, sheet_id: str, settings: Optional[dict] = None) -> dict:
    """Read + parse one weekly sheet without writing anything."""
    parsed = parse_sheet_tables(fetch_sheet_tables(gc, sheet_id, settings), settings)
    meta = parsed["meta"]
    if meta.get("week") is None:
        # C1:C2 unreadable from the grid (e.g. short rows) — one more call.
        try:
            meta.update({k: v for k, v in read_sheet_meta(gc, sheet_id, settings).items() if v is not None})
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read Current Week for %s sheet: %s", label, e)
    if meta.get("season") is None:
        meta["season"] = season_mod.get_season_year(settings)
    meta["sheet"] = label
    meta["spreadsheet_id"] = sheet_id
    meta["counts"] = {kind: len(parsed[kind]) for kind in KINDS}
    return parsed


def write_snapshot(parsed: dict, date_str: str, run: str = "am") -> dict:
    """Write players/games/output/kickers + meta.json for a parsed sheet.
    Returns the (completed) meta dict."""
    meta = dict(parsed["meta"])
    if meta.get("week") is None:
        raise ValueError(f"{meta.get('sheet', '?')} sheet has no Current Week (C2) — cannot place snapshot")
    target = snapshot_dir(meta["season"], meta["week"], meta["sheet"], date_str)
    target.mkdir(parents=True, exist_ok=True)
    for kind in KINDS:
        with open(target / f"{kind}.json", "w", encoding="utf-8") as f:
            json.dump(parsed[kind], f, indent=2, ensure_ascii=False)
    meta.update({
        "date": date_str,
        "run": run,
        "snapshot_at": _now_iso(),
        "dir": _rel_dir(target),
        "counts": {kind: len(parsed[kind]) for kind in KINDS},
    })
    with open(target / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info(
        "Saved %s weekly snapshot wk%02d (%s): %s",
        meta["sheet"], meta["week"], date_str,
        ", ".join(f"{k}={v}" for k, v in meta["counts"].items()),
    )
    parsed["meta"] = meta
    return meta


def snapshot_sheet(gc, label: str, sheet_id: str, date_str: str,
                   settings: Optional[dict] = None, run: str = "am") -> dict:
    """Read the four tabs of one sheet and write the snapshot files.
    Returns {"meta", "players", "games", "output", "kickers"}."""
    parsed = fetch_sheet(gc, label, sheet_id, settings)
    write_snapshot(parsed, date_str, run=run)
    return parsed


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read %s: %s", path, e)
        return None


def _looks_like_date(name: str) -> bool:
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


def list_snapshot_dates(season: int, week: int, sheet: str) -> list[str]:
    """Ascending ISO dates that have a snapshot dir for (season, week, sheet)."""
    d = _week_dir(season, week, sheet)
    if not d.exists():
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and _looks_like_date(p.name)
        and any((p / f"{k}.json").exists() for k in (*KINDS, "meta"))
    )


def _other_sheet(sheet: str) -> str:
    return "secondary" if sheet == "primary" else "primary"


def _latest_for_sheet(season: int, week: int, sheet: str, kind: str,
                      before_date: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    dates = list_snapshot_dates(season, week, sheet)
    if before_date:
        dates = [d for d in dates if d < before_date]
    for d in reversed(dates):
        data = _load_json(snapshot_dir(season, week, sheet, d) / f"{kind}.json")
        if data is not None:
            return data, d
    return None, None


def latest_weekly_snapshot(
    season: int,
    week: int,
    sheet: str,
    kind: str,
    before_date: Optional[str] = None,
) -> tuple[Optional[dict], Optional[dict]]:
    """(data, meta) of the most recent ``kind`` snapshot for (season, week,
    sheet), strictly before ``before_date`` when given.

    If that sheet has no earlier snapshot for the week, the *other* sheet's
    latest for the same week is used (Tuesday's secondary work is the
    natural baseline for Wednesday's main sheet). The returned meta carries
    ``basis_sheet`` / ``basis_date`` / ``fallback`` so callers can record it.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown snapshot kind {kind!r}")
    for candidate, fallback in ((sheet, False), (_other_sheet(sheet), True)):
        data, d = _latest_for_sheet(season, week, candidate, kind, before_date)
        if data is None:
            continue
        meta = _load_json(snapshot_dir(season, week, candidate, d) / "meta.json") or {
            "season": season, "week": week, "sheet": candidate, "date": d,
        }
        meta = dict(meta)
        meta.update({"basis_sheet": candidate, "basis_date": d, "fallback": fallback})
        if fallback:
            logger.info(
                "No earlier %s snapshot for %s wk%02d — diffing against %s (%s)",
                kind, sheet, week, candidate, d,
            )
        return data, meta
    return None, None


# ---------------------------------------------------------------------------
# Context diff (roster-ish signals the metric diff doesn't capture)
# ---------------------------------------------------------------------------


def _ident(p: dict, pid: str) -> dict:
    return {
        "player_id": p.get("player_id", pid),
        "name": p.get("name", pid),
        "team": p.get("team", ""),
        "pos": p.get("pos", ""),
    }


def _depth_or_slot(p: dict) -> Any:
    """Depth (order within team+pos) when the snapshot has it, else raw slot.
    The raw ``#`` is a global row number and would flag everyone below an
    inserted row, so ``slot_changes`` are computed on depth."""
    return p.get("depth") if p.get("depth") is not None else p.get("slot")


def diff_context(cur_players: dict, prev_players: dict) -> dict:
    """Status flips, depth/slot moves, adds/removes and opponent changes
    between two ``players`` snapshots. Each record carries player identity
    plus ``old`` / ``new`` where applicable."""
    out: dict[str, list[dict]] = {
        "status_changes": [], "slot_changes": [], "added": [], "removed": [], "opp_changes": [],
    }
    prev_players = prev_players or {}
    for pid, cur in cur_players.items():
        prev = prev_players.get(pid)
        if prev is None:
            out["added"].append({**_ident(cur, pid), "status": cur.get("status", ""),
                                 "slot": cur.get("slot"), "depth": cur.get("depth")})
            continue
        if cur.get("status") != prev.get("status"):
            out["status_changes"].append({**_ident(cur, pid), "old": prev.get("status"), "new": cur.get("status")})
        if _depth_or_slot(cur) != _depth_or_slot(prev):
            out["slot_changes"].append({**_ident(cur, pid), "old": _depth_or_slot(prev), "new": _depth_or_slot(cur)})
        if cur.get("opp") != prev.get("opp"):
            out["opp_changes"].append({**_ident(cur, pid), "old": prev.get("opp"), "new": cur.get("opp")})
    for pid, prev in prev_players.items():
        if pid not in cur_players:
            out["removed"].append({**_ident(prev, pid), "status": prev.get("status", ""),
                                   "slot": prev.get("slot"), "depth": prev.get("depth")})
    return out


# ---------------------------------------------------------------------------
# Active pointer
# ---------------------------------------------------------------------------


def write_active_pointer(season: int, info: dict) -> Path:
    """``data/weekly_projections/<season>/active.json`` = {date, sheet, week, dir, snapshot_at, ...}."""
    path = _active_path(season)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": info.get("date"),
        "sheet": info.get("sheet"),
        "week": info.get("week"),
        "dir": info.get("dir"),
        "snapshot_at": info.get("snapshot_at") or _now_iso(),
    }
    for k, v in info.items():
        payload.setdefault(k, v)
    payload["season"] = season
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return path


def load_active_pointer(season: int) -> Optional[dict]:
    return _load_json(_active_path(season))


def load_active_snapshot(season: Optional[int] = None) -> Optional[dict]:
    """{"meta", "players", "games", "output", "kickers"} for the active
    sheet's latest snapshot, or None when the pointer / dir is missing."""
    season = season if season is not None else season_mod.get_season_year()
    ptr = load_active_pointer(season)
    if not ptr or not ptr.get("dir"):
        return None
    d = _resolve_dir(ptr["dir"])
    if not d.exists():
        logger.warning("Active weekly snapshot dir missing: %s", d)
        return None
    meta = _load_json(d / "meta.json") or {
        "season": season, "week": ptr.get("week"), "sheet": ptr.get("sheet"), "date": ptr.get("date"),
    }
    snap = {"meta": meta}
    for kind in KINDS:
        snap[kind] = _load_json(d / f"{kind}.json") or {}
    return snap


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------


def _fmt(v: Any) -> Any:
    return "" if v is None else v


def _changelog_rows(change: dict) -> list[dict]:
    """One change record → one or more changelog rows.

    ``diff_fantasy`` records carry ``ppr_old/ppr_new`` and ``rank_old/
    rank_new`` instead of ``metric/old/new``; they are flattened here so the
    CSV stays readable for the ``--diff`` view and the dashboard."""
    base = {"key": change.get("key", ""), "label": change.get("label", ""), "type": change.get("type", "")}
    if change.get("type") != "fantasy_change":
        return [{**base, "metric": change.get("metric", ""), "old_value": _fmt(change.get("old")),
                 "new_value": _fmt(change.get("new")), "details": change.get("details", "")}]
    rows = []
    for metric, key in (("PPR", "ppr"), ("Half PPR", "half_ppr"), ("PPR/G", "ppr_g")):
        if f"{key}_old" in change or f"{key}_new" in change:
            rows.append({**base, "metric": metric, "old_value": _fmt(change.get(f"{key}_old")),
                         "new_value": _fmt(change.get(f"{key}_new")), "details": ""})
    if "rank_old" in change or "rank_new" in change:
        rows.append({**base, "metric": "POS Rank", "old_value": _fmt(change.get("rank_old")),
                     "new_value": _fmt(change.get("rank_new")),
                     "details": "adjusted" if change.get("adjusted") else ""})
    if not rows:
        rows.append({**base, "metric": "", "old_value": "", "new_value": "", "details": ""})
    return rows


def write_weekly_changelog(changes: list[dict], date_str: str, season: int, week: int,
                           sheet: str, kind: str) -> None:
    """Append to ``data/weekly_projections/changelog.csv`` (header once)."""
    if not changes:
        return
    path = _changelog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CHANGELOG_FIELDS))
        if write_header:
            writer.writeheader()
        n = 0
        for change in changes:
            for row in _changelog_rows(change):
                writer.writerow({
                    "date": date_str, "season": season, "week": week, "sheet": sheet, "kind": kind, **row,
                })
                n += 1
    logger.info("Logged %d %s/%s changes (wk%02d) to %s", n, sheet, kind, week, path)


def read_weekly_changelog() -> list[dict]:
    """All rows of the weekly changelog (empty list when absent)."""
    path = _changelog_path()
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _empty_context() -> dict:
    return {"status_changes": [], "slot_changes": [], "added": [], "removed": [], "opp_changes": []}


def _adjusted_ids(changes: list[dict]) -> set[str]:
    ids: set[str] = set()
    for c in changes:
        if c.get("type") == "added":
            ids.add(c.get("key", ""))
        elif c.get("type") == "metric_change" and "adj" in str(c.get("metric", "")).lower():
            ids.add(c.get("key", ""))
    ids.discard("")
    return ids


def _diff_sheet(snap: dict, date_str: str) -> tuple[dict, dict, list[dict], dict]:
    """Diff one fresh snapshot against its baseline(s).

    Returns (counts_by_kind, diff_basis_by_kind, fantasy_changes, context)."""
    meta = snap["meta"]
    season, week, label = meta["season"], meta["week"], meta["sheet"]
    counts: dict[str, int] = {}
    basis: dict[str, dict] = {}
    adjusted: set[str] = set()
    context = _empty_context()

    for kind in ("players", "kickers", "games"):
        prev, prev_meta = latest_weekly_snapshot(season, week, label, kind, before_date=date_str)
        if prev is None:
            counts[kind] = 0
            basis[kind] = None
            logger.info("No earlier %s snapshot for %s wk%02d — baseline", kind, label, week)
            continue
        basis[kind] = {k: prev_meta.get(k) for k in ("basis_sheet", "basis_date", "fallback")}
        changes = diff_snapshots(snap[kind], prev, "game" if kind == "games" else "player")
        counts[kind] = len(changes)
        if kind in ("players", "kickers"):
            adjusted |= _adjusted_ids(changes)
        if kind == "players":
            context = diff_context(snap["players"], prev)
        write_weekly_changelog(changes, date_str, season, week, label, kind)

    prev_out, prev_meta = latest_weekly_snapshot(season, week, label, "output", before_date=date_str)
    fantasy_changes: list[dict] = []
    if prev_out is None:
        counts["output"] = 0
        basis["output"] = None
        logger.info("No earlier output snapshot for %s wk%02d — baseline", label, week)
    else:
        basis["output"] = {k: prev_meta.get(k) for k in ("basis_sheet", "basis_date", "fallback")}
        fantasy_changes = diff_fantasy(snap["output"], prev_out, adjusted_ids=adjusted)
        counts["output"] = len(fantasy_changes)
        write_weekly_changelog(fantasy_changes, date_str, season, week, label, "output")

    return counts, basis, fantasy_changes, context


def _record_diff_basis(meta: dict, basis: dict) -> None:
    """Persist which snapshot each kind was diffed against into meta.json."""
    meta["diff_basis"] = basis
    d = _resolve_dir(meta.get("dir", "")) if meta.get("dir") else None
    if d and d.exists():
        with open(d / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)


def run_weekly_snapshot(
    gc,
    date_str: str,
    ctx=None,
    settings: Optional[dict] = None,
    sheets: Optional[list[str]] = None,
    run: str = "am",
) -> dict:
    """Snapshot + diff the weekly sheet(s) for ``date_str``.

    ``sheets`` defaults to ``["primary"]`` plus ``"secondary"`` on the
    configured secondary weekdays (Tuesday). Each sheet is non-fatal: a
    read failure is logged and recorded under ``errors``; only when *no*
    sheet could be read does this raise. The active working sheet is the
    one with the higher Current Week (tie → primary) and is written to
    ``active.json``.
    """
    settings = _settings(settings)
    cfg = season_mod.get_in_season_projection_settings(settings)
    sheet_ids: dict = cfg.get("sheets", {}) or {}

    if sheets is None:
        read_secondary = (
            bool(getattr(ctx, "read_secondary", False)) if ctx is not None
            else season_mod.read_secondary_today(date_str, settings)
        )
        sheets = ["primary"] + (["secondary"] if read_secondary else [])
    if "both" in sheets:
        sheets = list(SHEET_LABELS)

    result: dict = {
        "date": date_str,
        "run": run,
        "week": None,
        "active_sheet": None,
        "active_dir": None,
        "sheet_weeks": {},
        "snapshots": {},
        "rank_movers": [],
        "rank_movers_by_sheet": {},
        "context_changes": {},
        "changes": {},
        "diff_basis": {},
        "errors": {},
    }

    snaps: dict[str, dict] = {}
    for label in sheets:
        sid = sheet_ids.get(label)
        if not sid:
            logger.warning("projections.in_season.sheets.%s not configured — skipping", label)
            result["errors"][label] = "not configured"
            continue
        try:
            snap = snapshot_sheet(gc, label, sid, date_str, settings, run=run)
        except Exception as e:  # noqa: BLE001 — per-sheet failures must not kill the run
            logger.exception("Weekly snapshot failed for %s sheet: %s", label, e)
            result["errors"][label] = str(e)
            continue
        snaps[label] = snap
        meta = snap["meta"]
        result["sheet_weeks"][label] = meta["week"]
        result["snapshots"][label] = meta
        try:
            counts, basis, fantasy_changes, context = _diff_sheet(snap, date_str)
        except Exception as e:  # noqa: BLE001
            logger.exception("Weekly diff failed for %s sheet: %s", label, e)
            result["errors"][label] = f"diff: {e}"
            counts, basis, fantasy_changes, context = (
                {k: 0 for k in KINDS}, {k: None for k in KINDS}, [], _empty_context(),
            )
        result["changes"][label] = counts
        result["diff_basis"][label] = basis
        result["context_changes"][label] = context
        result["rank_movers_by_sheet"][label] = [c for c in fantasy_changes if c.get("adjusted")]
        _record_diff_basis(meta, basis)

    if not snaps:
        raise RuntimeError(
            "No weekly projections sheet could be read: "
            + "; ".join(f"{k}: {v}" for k, v in result["errors"].items())
        )

    metas = {label: {"season": s["meta"]["season"], "week": s["meta"]["week"]} for label, s in snaps.items()}
    week, active = season_mod.resolve_current_week(metas, None, date_str)
    if active not in snaps:  # defensive: resolve_current_week only returns labels it was given
        active = next(iter(snaps))
        week = snaps[active]["meta"]["week"]
    active_meta = snaps[active]["meta"]
    season = active_meta["season"]

    result["week"] = week
    result["active_sheet"] = active
    result["active_dir"] = active_meta.get("dir")
    result["rank_movers"] = result["rank_movers_by_sheet"].get(active, [])
    write_active_pointer(season, {
        "date": date_str,
        "sheet": active,
        "week": week,
        "dir": active_meta.get("dir"),
        "snapshot_at": active_meta.get("snapshot_at"),
        "run": run,
        "sheet_weeks": result["sheet_weeks"],
    })
    logger.info(
        "Weekly snapshot done: week %s, active sheet %s (%s)",
        week, active, ", ".join(f"{k}=wk{v}" for k, v in result["sheet_weeks"].items()),
    )
    return result
