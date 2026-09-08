"""Projection audit — "are the right guys projected this week?"

Cross-checks the active weekly projection sheet against everything else
the agent knows in-season:

* roster state (``data/roster/state.json`` — nflverse baseline + NFL.com /
  OurLads / insider-report events),
* the latest nflverse roster snapshot (who is actually on the 53 / PS),
* the current week's injury report (``data/injuries/<season>/wk<NN>.json``),
* the latest OurLads depth chart (to rank "missing" players by depth),
* the schedule (opponent / bye sanity).

Every alert carries a stable ``key`` scoped to the week, so a dismissal
expires naturally when the sheet rolls to the next week. Dismissals live in
``data/projections/audit_dismissals.json`` (same mechanics as the Depth
Chart Manager's ``sheet_recon_dismissals.json``).

Runs as Step 5d of the morning pipeline and inside ``scripts/run_afternoon.py``;
results are written to ``data/audit/<date>-<run>.json`` and rendered as the
"Projection Audit" report section. CLI:

    python -m processing.projection_audit [--date YYYY-MM-DD] [--run am|pm] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings
from processing import season as season_mod
from processing.team_abbr import to_news, to_proj

logger = logging.getLogger(__name__)

DISMISSALS_PATH = PROJECT_ROOT / "data" / "projections" / "audit_dismissals.json"

RESERVE_STATUSES = {"IR", "PUP", "NFI", "SUS", "RET", "EXE"}
NOT_ON_53 = RESERVE_STATUSES | {"PS", "FA", "CUT"}

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Sheet Status column → roster-state status family
SHEET_STATUS_FAMILY = {
    "ACTIVE": "ACT",
    "PS": "PS",
    "IR": "IR",
    "PUP": "PUP",
    "NFI": "NFI",
    "SUS": "SUS",
}


# ---------------------------------------------------------------------------
# Dismissals
# ---------------------------------------------------------------------------


def load_dismissals(path: Path = DISMISSALS_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_dismissals(dismissals: dict[str, dict], path: Path = DISMISSALS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dismissals, indent=2, ensure_ascii=False), encoding="utf-8")


def dismiss(key: str, note: str = "", path: Path = DISMISSALS_PATH) -> None:
    d = load_dismissals(path)
    d[key] = {"dismissed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "note": note}
    save_dismissals(d, path)


def undismiss(key: str, path: Path = DISMISSALS_PATH) -> None:
    d = load_dismissals(path)
    if key in d:
        del d[key]
        save_dismissals(d, path)


def filter_dismissed(alerts: list[dict], dismissals: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    active, dismissed = [], []
    for a in alerts:
        (dismissed if a.get("key") in dismissals else active).append(a)
    return active, dismissed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name_key(name: str) -> str:
    from processing.sheet_reconciliation import _normalize_name
    return _normalize_name(name or "")


def _alert(atype: str, severity: str, *, player: str = "", gsis_id: Optional[str] = None,
           pos: str = "", team: str = "", sheet: str = "", week: Optional[int] = None,
           message: str, evidence: Optional[dict] = None, key_tail: str = "") -> dict:
    ident = gsis_id or _name_key(player) or team
    key = f"{atype}|{ident}|{week}" + (f"|{key_tail}" if key_tail else "")
    return {
        "type": atype, "key": key, "severity": severity,
        "player": player, "gsis_id": gsis_id, "pos": pos, "team": team,
        "sheet": sheet, "week": week, "message": message, "evidence": evidence or {},
    }


def _state_players(state: Optional[dict]) -> dict[str, dict]:
    return dict((state or {}).get("players") or {})


def _state_lookup(state: Optional[dict], gsis_id: Optional[str], name_key: str) -> Optional[dict]:
    players = _state_players(state)
    if gsis_id and gsis_id in players:
        return players[gsis_id]
    by_name = (state or {}).get("by_name") or {}
    gid = by_name.get(name_key)
    if gid and gid in players:
        return players[gid]
    return players.get(f"name:{name_key}")


def _ppr(rec: Optional[dict]) -> float:
    if not rec:
        return 0.0
    v = rec.get("ppr")
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _slot(rec: dict) -> Optional[int]:
    """Per-team depth order (1 = first at the position on the sheet).

    The sheet's ``#`` column (stored as ``slot``) is a global running row
    number, so it is NOT used; ``depth`` is derived by the weekly parser.
    """
    v = rec.get("depth")
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _week_window(schedule: list[dict], week: int) -> tuple[Optional[str], Optional[str]]:
    """ISO (start, end) of an NFL week: Tuesday before the first game → Monday after the last."""
    games = season_mod.games_for_week(schedule, week)
    if not games:
        return None, None
    from datetime import timedelta
    first = min(date.fromisoformat(g["date"]) for g in games)
    last = max(date.fromisoformat(g["date"]) for g in games)
    start = first
    while start.weekday() != 1:
        start -= timedelta(days=1)
    end = last
    while end.weekday() != 0:
        end += timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Input loading (all optional / soft)
# ---------------------------------------------------------------------------


def load_inputs(ctx, date_str: str, settings: Optional[dict] = None) -> dict:
    settings = settings or get_settings()
    inputs: dict[str, Any] = {
        "snapshot": None, "state": None, "nflverse": None, "nflverse_date": None,
        "injuries": None, "ourlads": None, "schedule": [], "inactives": {}, "errors": [],
    }
    try:
        from processing.weekly_projections import load_active_snapshot
        inputs["snapshot"] = load_active_snapshot(ctx.season)
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"weekly snapshot: {e}")
    try:
        from processing.roster_events import load_state
        inputs["state"] = load_state()
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"roster state: {e}")
    try:
        from collectors.nflverse_roster_collector import latest_nflverse_snapshot
        players, nd = latest_nflverse_snapshot()
        inputs["nflverse"], inputs["nflverse_date"] = players, nd
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"nflverse: {e}")
    try:
        from collectors.injury_report_collector import load_week_file
        if ctx.week:
            inputs["injuries"] = load_week_file(ctx.season, ctx.week)
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"injury report: {e}")
    try:
        from collectors.depth_chart_collector import load_latest_depth_charts
        inputs["ourlads"] = load_latest_depth_charts() or {}
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"ourlads: {e}")
    try:
        inputs["schedule"] = season_mod.load_schedule(settings=settings, season=ctx.season)
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"schedule: {e}")
    try:
        from collectors.inactives_collector import inactive_players_for_week
        inputs["inactives"] = inactive_players_for_week(ctx.season, ctx.week) if ctx.week else {}
    except Exception as e:  # noqa: BLE001
        inputs["errors"].append(f"inactives: {e}")
    return inputs


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _sheet_rows(snapshot: dict, positions: set[str]) -> dict[str, dict]:
    """Merge players + kickers into one {gsis: row} map with a ``kind`` tag."""
    rows: dict[str, dict] = {}
    for gid, rec in (snapshot.get("players") or {}).items():
        if positions and str(rec.get("pos", "")).upper() not in positions:
            continue
        rows[gid] = {**rec, "kind": "player"}
    for gid, rec in (snapshot.get("kickers") or {}).items():
        if positions and "K" not in positions:
            continue
        rows.setdefault(gid, {**rec, "pos": rec.get("pos") or "K", "kind": "kicker"})
    return rows


def check_sheet_vs_roster(rows: dict[str, dict], output: dict, state: Optional[dict],
                          nflverse: Optional[dict], week: int, sheet: str) -> list[dict]:
    alerts: list[dict] = []
    nfv = nflverse or {}
    for gid, rec in rows.items():
        name = rec.get("name") or gid
        pos = str(rec.get("pos") or "")
        sheet_team = to_proj(str(rec.get("team") or ""), "proj")
        sheet_status = str(rec.get("status") or "").strip().upper()
        nk = _name_key(name)
        st = _state_lookup(state, gid if gid.startswith("00-") else None, nk)
        nv = nfv.get(gid) if gid.startswith("00-") else None
        roster_status = (st or {}).get("status") or (
            {"ACT": "ACT", "DEV": "PS", "RES": "IR", "CUT": "FA", "RET": "RET", "EXE": "EXE"}.get(
                (nv or {}).get("status", ""), None)
        )
        roster_team_news = (st or {}).get("team") or (nv or {}).get("team")
        roster_team = to_proj(roster_team_news, "news") if roster_team_news else None
        ppr = _ppr((output or {}).get(gid))
        slot = _slot(rec)
        source = (st or {}).get("status_source") or ("nflverse" if nv else None)

        if not roster_status:
            continue

        # 1. Projected as active but not on the 53
        if roster_status in NOT_ON_53 and sheet_status in ("", "ACTIVE") and (ppr > 0 or (slot is not None and slot <= 3)):
            sev = SEVERITY_ERROR if roster_status in RESERVE_STATUSES or roster_status == "FA" else SEVERITY_WARNING
            since = (st or {}).get("status_since")
            erw = (st or {}).get("earliest_return_week")
            msg = (
                f"{name} ({pos}, {sheet_team}) is projected {'active' if not sheet_status else sheet_status.title()}"
                + (f" ({ppr:.1f} PPR)" if ppr else "")
                + f" but roster status is {roster_status}"
                + (f" (since {since})" if since else "")
                + (f"; eligible Wk {erw}" if erw else "")
            )
            alerts.append(_alert(
                "status_conflict", sev, player=name, gsis_id=gid, pos=pos, team=sheet_team,
                sheet=sheet, week=week,
                message=msg,
                evidence={"roster_status": roster_status, "status_source": source, "sheet_status": sheet_status,
                          "ppr": ppr, "slot": slot, "earliest_return_week": (st or {}).get("earliest_return_week")},
            ))
        # 2. Sheet Status column stale (PS promoted, IR activated, etc.)
        elif sheet_status and SHEET_STATUS_FAMILY.get(sheet_status) and SHEET_STATUS_FAMILY[sheet_status] != roster_status:
            fam = SHEET_STATUS_FAMILY[sheet_status]
            if not (fam in RESERVE_STATUSES and roster_status in RESERVE_STATUSES):
                alerts.append(_alert(
                    "sheet_status_stale", SEVERITY_WARNING, player=name, gsis_id=gid, pos=pos,
                    team=sheet_team, sheet=sheet, week=week,
                    message=f"{name} ({pos}, {sheet_team}) sheet Status is {sheet_status} but roster says {roster_status}"
                            f"{' - promoted to the active roster' if fam == 'PS' and roster_status == 'ACT' else ''}"
                            f"{' - activated' if fam in RESERVE_STATUSES and roster_status == 'ACT' else ''}",
                    evidence={"roster_status": roster_status, "status_source": source, "sheet_status": sheet_status, "ppr": ppr},
                ))

        # 3. Wrong team
        if roster_team and sheet_team and roster_team != sheet_team and roster_status not in ("FA", "RET"):
            alerts.append(_alert(
                "wrong_team", SEVERITY_ERROR, player=name, gsis_id=gid, pos=pos, team=sheet_team,
                sheet=sheet, week=week, key_tail=roster_team,
                message=f"{name} ({pos}) is on the {sheet_team} sheet but the roster has him on {roster_team}",
                evidence={"sheet_team": sheet_team, "roster_team": roster_team, "status_source": source},
            ))
    return alerts


MIN_TEAM_BLOCK_ROWS = 8   # a team block with fewer rows is being (re)built, not missing players


def check_missing_active(rows: dict[str, dict], nflverse: Optional[dict], ourlads: Optional[dict],
                         positions: set[str], week: int, sheet: str, bye_teams: set[str],
                         state: Optional[dict] = None) -> list[dict]:
    """Active-roster QB/RB/WR/TE/K (nflverse) with no row on their team's sheet.

    A team whose block holds fewer than MIN_TEAM_BLOCK_ROWS player rows is
    reported once as ``team_block_incomplete`` instead of once per player —
    on editing days the sheet is often mid-rebuild when a run reads it.
    """
    alerts: list[dict] = []
    if not nflverse:
        return alerts
    rows_per_team: dict[str, int] = {}
    for r in rows.values():
        if r.get("kind") == "player":
            t = to_proj(str(r.get("team") or ""), "proj")
            rows_per_team[t] = rows_per_team.get(t, 0) + 1
    incomplete = {t for t, n in rows_per_team.items() if n < MIN_TEAM_BLOCK_ROWS}
    for t in sorted(incomplete):
        if t in bye_teams:
            continue
        alerts.append(_alert(
            "team_block_incomplete", SEVERITY_WARNING, team=t, sheet=sheet, week=week,
            message=f"{t} has only {rows_per_team[t]} player rows on the sheet - block looks unfinished"
                    f" (per-player missing checks skipped for {t})",
            evidence={"rows": rows_per_team[t], "min_rows": MIN_TEAM_BLOCK_ROWS},
        ))
    sheet_ids = set(rows)
    sheet_name_keys = {(_name_key(r.get("name", "")), to_proj(str(r.get("team") or ""), "proj")) for r in rows.values()}
    ourlads = ourlads or {}
    for gid, p in nflverse.items():
        if p.get("status") != "ACT":
            continue
        pos = str(p.get("pos") or "").upper()
        if pos not in positions:
            continue
        team_proj = to_proj(str(p.get("team") or ""), "news")
        if team_proj in bye_teams or team_proj in incomplete:
            continue
        nk = p.get("name_key") or _name_key(p.get("name", ""))
        # Roster state carries official NFL.com moves nflverse hasn't caught up with yet
        st = _state_lookup(state, gid, nk)
        if st and st.get("status") and st["status"] != "ACT":
            continue
        if gid in sheet_ids or (nk, team_proj) in sheet_name_keys:
            continue
        dc = ourlads.get((p.get("name") or "").lower()) or {}
        if not dc:
            # try the OurLads name key variants
            for k, v in ourlads.items():
                if _name_key(k) == nk and to_proj(str(v.get("team") or ""), "ourlads") == team_proj:
                    dc = v
                    break
        depth = dc.get("depth")
        dc_pos = str(dc.get("pos") or "").upper()
        dc_generic = str(dc.get("generic_pos") or "").upper()
        # Fullbacks / returners / reserve buckets are never projected rows.
        if dc_pos in ("FB", "KR", "PR", "H", "LS", "KO") or dc_generic in ("FB", "RET") or dc_pos in RESERVE_STATUSES:
            continue
        if pos == "K":
            sev = SEVERITY_WARNING
        elif pos == "QB":
            # The sheet only projects the starter; a missing QB2/QB3 is by design.
            if depth != 1:
                continue
            sev = SEVERITY_ERROR
        elif depth is None:
            sev = SEVERITY_INFO
        elif depth <= 2:
            sev = SEVERITY_WARNING
        elif depth == 3:
            sev = SEVERITY_INFO
        else:
            continue
        alerts.append(_alert(
            "missing_active", sev, player=p.get("name", ""), gsis_id=gid, pos=pos, team=team_proj,
            sheet=sheet, week=week,
            message=f"{p.get('name')} ({pos}, {team_proj}) is on the active roster"
                    f"{f' - OurLads {dc_pos or pos} #{depth}' if depth else ''} but has no row on the sheet",
            evidence={"ourlads_depth": depth, "ourlads_pos": dc_pos, "nflverse_status": p.get("status"),
                      "status_abbr": p.get("status_abbr")},
        ))
    return alerts


def check_injuries(rows: dict[str, dict], output: dict, injuries: Optional[dict], week: int, sheet: str) -> list[dict]:
    alerts: list[dict] = []
    if not injuries:
        return alerts
    idx: dict[tuple[str, str], dict] = {}
    for team, tdata in (injuries.get("teams") or {}).items():
        for nk, p in (tdata.get("players") or {}).items():
            idx[(nk, to_proj(team, "news"))] = p
    for gid, rec in rows.items():
        name = rec.get("name") or gid
        nk = _name_key(name)
        team_proj = to_proj(str(rec.get("team") or ""), "proj")
        p = idx.get((nk, team_proj))
        if not p:
            continue
        gs = str(p.get("game_status") or "").upper()
        ppr = _ppr((output or {}).get(gid))
        practice = p.get("practice") or {}
        latest = practice[max(practice)] if practice else ""
        if gs in ("OUT", "D") and ppr > 0:
            sev = SEVERITY_ERROR if gs == "OUT" else SEVERITY_WARNING
            alerts.append(_alert(
                "out_but_projected", sev, player=name, gsis_id=gid, pos=str(rec.get("pos") or ""),
                team=team_proj, sheet=sheet, week=week, key_tail=gs,
                message=f"{name} ({rec.get('pos')}, {team_proj}) is {'OUT' if gs == 'OUT' else 'Doubtful'}"
                        f" ({p.get('injury') or 'injury'}) but projected for {ppr:.1f} PPR",
                evidence={"game_status": gs, "injury": p.get("injury"), "practice": practice, "ppr": ppr},
            ))
        elif gs == "" and latest == "DNP" and ppr > 0 and len(practice) >= 2:
            alerts.append(_alert(
                "dnp_but_projected", SEVERITY_INFO, player=name, gsis_id=gid, pos=str(rec.get("pos") or ""),
                team=team_proj, sheet=sheet, week=week,
                message=f"{name} ({rec.get('pos')}, {team_proj}) did not practice on the latest report"
                        f" ({p.get('injury') or 'injury'}); projected {ppr:.1f} PPR",
                evidence={"practice": practice, "injury": p.get("injury"), "ppr": ppr},
            ))
    return alerts


def check_inactives(rows: dict[str, dict], output: dict, inactives: Optional[dict], week: int, sheet: str) -> list[dict]:
    """Declared game-day inactives that still carry projected points."""
    alerts: list[dict] = []
    if not inactives:
        return alerts
    for gid, rec in rows.items():
        name = rec.get("name") or gid
        team_news = to_news(str(rec.get("team") or ""), "proj")
        p = inactives.get((team_news, _name_key(name)))
        if not p:
            continue
        ppr = _ppr((output or {}).get(gid))
        if ppr <= 0:
            continue
        team_proj = to_proj(team_news, "news")
        alerts.append(_alert(
            "inactive_but_projected", SEVERITY_ERROR, player=name, gsis_id=gid, pos=str(rec.get("pos") or ""),
            team=team_proj, sheet=sheet, week=week,
            message=f"{name} ({rec.get('pos')}, {team_proj}) is INACTIVE for {p.get('game') or 'this week'}"
                    f" ({p.get('phase')}) but projected for {ppr:.1f} PPR",
            evidence={"game": p.get("game"), "phase": p.get("phase"), "ppr": ppr},
        ))
    return alerts


def check_elevations(rows: dict[str, dict], state: Optional[dict], schedule: list[dict], week: int,
                     sheet: str, max_elev: int) -> list[dict]:
    alerts: list[dict] = []
    if not state:
        return alerts
    start, end = _week_window(schedule, week)
    sheet_ids = set(rows)
    sheet_name_keys = {_name_key(r.get("name", "")) for r in rows.values()}
    for gid, p in _state_players(state).items():
        dates = [d for d in (p.get("elevation_dates") or []) if start and end and start <= d[:10] <= end]
        name = p.get("name", "")
        nk = p.get("name_key") or _name_key(name)
        team_proj = to_proj(str(p.get("team") or ""), "news")
        on_sheet = gid in sheet_ids or nk in sheet_name_keys
        if dates and not on_sheet:
            alerts.append(_alert(
                "elevated_not_projected", SEVERITY_WARNING, player=name, gsis_id=gid if gid.startswith("00-") else None,
                pos=str(p.get("pos") or ""), team=team_proj, sheet=sheet, week=week,
                message=f"{name} ({p.get('pos')}, {team_proj}) was elevated from the practice squad"
                        f" ({dates[-1][:10]}) but has no row on the sheet",
                evidence={"elevation_dates": dates, "elevations_used": p.get("elevations_used")},
            ))
        used = int(p.get("elevations_used") or 0)
        if used >= max_elev and p.get("status") == "PS":
            alerts.append(_alert(
                "elevation_limit", SEVERITY_INFO, player=name, gsis_id=gid if gid.startswith("00-") else None,
                pos=str(p.get("pos") or ""), team=team_proj, sheet=sheet, week=week,
                message=f"{name} ({p.get('pos')}, {team_proj}) has used {used}/{max_elev} elevations -"
                        f" must be signed to the 53 to play again",
                evidence={"elevations_used": used, "elevation_dates": p.get("elevation_dates")},
            ))
    return alerts


def check_schedule(snapshot: dict, rows: dict[str, dict], output: dict, schedule: list[dict],
                   week: int, sheet: str) -> list[dict]:
    alerts: list[dict] = []
    if not schedule:
        return alerts
    byes = season_mod.teams_on_bye(schedule, week)
    games = snapshot.get("games") or {}
    for team, g in games.items():
        tp = to_proj(team, "proj")
        sheet_opp = to_proj(str(g.get("opp") or ""), "proj")
        expected = season_mod.opponent(schedule, tp, week)
        if tp in byes:
            continue  # handled via player rows below
        if expected and sheet_opp and sheet_opp != expected["opp"]:
            alerts.append(_alert(
                "opp_mismatch", SEVERITY_ERROR, team=tp, sheet=sheet, week=week, key_tail=sheet_opp,
                message=f"{tp} sheet opponent is {sheet_opp} but the Week {week} schedule says {expected['opp']}",
                evidence={"sheet_opp": sheet_opp, "schedule_opp": expected["opp"], "game_date": expected["date"]},
            ))
    # bye teams with projected points
    by_team_ppr: dict[str, float] = {}
    for gid, rec in rows.items():
        tp = to_proj(str(rec.get("team") or ""), "proj")
        if tp in byes:
            by_team_ppr[tp] = by_team_ppr.get(tp, 0.0) + _ppr((output or {}).get(gid))
    for tp, total in by_team_ppr.items():
        if total > 0:
            alerts.append(_alert(
                "bye_projected", SEVERITY_ERROR, team=tp, sheet=sheet, week=week,
                message=f"{tp} is on bye in Week {week} but its players carry {total:.1f} PPR on the sheet",
                evidence={"ppr_total": round(total, 1)},
            ))
    return alerts


def check_ir_returns(rows: dict[str, dict], state: Optional[dict], week: int, sheet: str) -> list[dict]:
    alerts: list[dict] = []
    for gid, p in _state_players(state).items():
        if p.get("status") not in ("IR", "PUP", "NFI"):
            continue
        erw = p.get("earliest_return_week")
        designated = p.get("designated_return_date")
        if (erw is not None and erw == week) or designated:
            name = p.get("name", "")
            team_proj = to_proj(str(p.get("team") or ""), "news")
            on_sheet = gid in rows
            alerts.append(_alert(
                "ir_return_window", SEVERITY_INFO, player=name, gsis_id=gid if gid.startswith("00-") else None,
                pos=str(p.get("pos") or ""), team=team_proj, sheet=sheet, week=week,
                message=f"{name} ({p.get('pos')}, {team_proj}) on {p.get('status')}"
                        f"{' - designated to return ' + str(designated)[:10] if designated else ''}"
                        f"{f' - first eligible Week {erw}' if erw else ''}"
                        f"{' (already on the sheet)' if on_sheet else ''}",
                evidence={"status": p.get("status"), "earliest_return_week": erw,
                          "designated_return_date": designated, "on_sheet": on_sheet},
            ))
    return alerts


def check_unconfirmed(state: Optional[dict], date_str: str, window_days: int, week: int, sheet: str) -> list[dict]:
    alerts: list[dict] = []
    today = date.fromisoformat(date_str)
    for gid, p in _state_players(state).items():
        for ev in p.get("pending") or []:
            d = str(ev.get("date") or "")[:10]
            try:
                age = (today - date.fromisoformat(d)).days
            except ValueError:
                continue
            if age >= window_days:
                name = p.get("name", "")
                alerts.append(_alert(
                    "unconfirmed_report", SEVERITY_INFO, player=name, gsis_id=gid if gid.startswith("00-") else None,
                    pos=str(p.get("pos") or ""), team=to_proj(str(p.get("team") or ""), "news"),
                    sheet=sheet, week=week, key_tail=str(ev.get("event_type") or ""),
                    message=f"{name}: reported {str(ev.get('event_type') or '').replace('_', ' ')} on {d}"
                            f" ({str(ev.get('source') or '').replace('news:', '')}) still unconfirmed after {age} days",
                    evidence={"event": ev, "days_pending": age},
                ))
    return alerts


def check_stale_secondary(ctx, week: int, sheet: str) -> list[dict]:
    if not getattr(ctx, "read_secondary", False):
        return []
    sw = getattr(ctx, "sheet_weeks", {}) or {}
    if "secondary" in sw and "primary" in sw and sw["secondary"] <= sw["primary"]:
        return [_alert(
            "stale_secondary", SEVERITY_INFO, team="", sheet=sheet, week=week,
            message=f"It's {ctx.weekday}: the secondary sheet is still on Week {sw['secondary']}"
                    f" (main sheet Week {sw['primary']}) - next week's copy hasn't been started",
            evidence=dict(sw),
        )]
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_audit(ctx, date_str: Optional[str] = None, run: str = "am",
              settings: Optional[dict] = None, inputs: Optional[dict] = None,
              write: bool = True) -> dict:
    """Run every check and write ``data/audit/<date>-<run>.json``.

    Returns ``{"date","run","season","week","sheet","alerts","dismissed","errors","counts"}``.
    Never raises - a missing input simply skips the checks that need it.
    """
    settings = settings or get_settings()
    date_str = date_str or ctx.today or date.today().isoformat()
    cfg = settings.get("projection_audit", {}) or {}
    positions = {str(p).upper() for p in (cfg.get("positions") or ["QB", "RB", "WR", "TE", "K"])}
    max_elev = int((settings.get("roster", {}) or {}).get("max_elevations", 3))
    window_days = int((settings.get("roster", {}) or {}).get("confirm_window_days", 3))

    inputs = inputs if inputs is not None else load_inputs(ctx, date_str, settings)
    snapshot = inputs.get("snapshot") or {}
    meta = snapshot.get("meta") or {}
    week = meta.get("week") or ctx.week
    sheet = meta.get("sheet") or ctx.active_sheet or "primary"
    schedule = inputs.get("schedule") or []
    errors = list(inputs.get("errors") or [])

    alerts: list[dict] = []
    if snapshot and week:
        rows = _sheet_rows(snapshot, positions)
        output = snapshot.get("output") or {}
        byes = season_mod.teams_on_bye(schedule, week) if schedule else set()
        alerts += check_sheet_vs_roster(rows, output, inputs.get("state"), inputs.get("nflverse"), week, sheet)
        alerts += check_missing_active(rows, inputs.get("nflverse"), inputs.get("ourlads"), positions, week, sheet, byes,
                                       state=inputs.get("state"))
        alerts += check_injuries(rows, output, inputs.get("injuries"), week, sheet)
        alerts += check_inactives(rows, output, inputs.get("inactives"), week, sheet)
        alerts += check_elevations(rows, inputs.get("state"), schedule, week, sheet, max_elev)
        alerts += check_schedule(snapshot, rows, output, schedule, week, sheet)
        alerts += check_ir_returns(rows, inputs.get("state"), week, sheet)
        alerts += check_unconfirmed(inputs.get("state"), date_str, window_days, week, sheet)
        alerts += check_stale_secondary(ctx, week, sheet)
    else:
        errors.append("no active weekly snapshot - sheet checks skipped")

    # de-dupe by key (a player can trip the same check twice via name/gsis paths)
    seen: set[str] = set()
    unique: list[dict] = []
    for a in alerts:
        if a["key"] in seen:
            continue
        seen.add(a["key"])
        unique.append(a)
    sev_rank = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
    unique.sort(key=lambda a: (sev_rank.get(a["severity"], 9), a.get("team") or "", a.get("player") or ""))

    active, dismissed = filter_dismissed(unique, load_dismissals())
    counts: dict[str, int] = {}
    for a in active:
        counts[a["type"]] = counts.get(a["type"], 0) + 1

    result = {
        "date": date_str, "run": run, "season": ctx.season, "week": week, "sheet": sheet,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "inputs": {
            "snapshot_date": meta.get("date"), "nflverse_date": inputs.get("nflverse_date"),
            "injury_week_updated": (inputs.get("injuries") or {}).get("updated_at"),
            "roster_state_updated": (inputs.get("state") or {}).get("updated_at"),
        },
        "alerts": active, "dismissed": dismissed, "errors": errors, "counts": counts,
    }
    if write:
        out_dir = get_data_dir("audit")
        path = out_dir / f"{date_str}-{run}.json"
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        result["file"] = str(path)
    return result


def latest_audit(before_date: Optional[str] = None) -> Optional[dict]:
    """Most recent ``data/audit/*.json`` (pm sorts after am for the same day)."""
    d = get_data_dir("audit")
    files = sorted(p for p in d.glob("*.json"))
    if before_date:
        files = [p for p in files if p.name[:10] < before_date]
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _main() -> int:
    ap = argparse.ArgumentParser(description="Audit the active weekly projection sheet.")
    ap.add_argument("--date", default=None)
    ap.add_argument("--run", default="cli", help="label written into the output filename")
    ap.add_argument("--json", action="store_true", help="print the full JSON result")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ctx = season_mod.get_season_context(today=args.date)
    if not ctx.in_season:
        print("season.phase is offseason - nothing to audit. Set season.phase: in_season in config/settings.yaml.")
        return 0
    res = run_audit(ctx, args.date or ctx.today, run=args.run, write=not args.no_write)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    print(f"Week {res['week']} | sheet {res['sheet']} | {len(res['alerts'])} open alerts, {len(res['dismissed'])} dismissed")
    if res["errors"]:
        print("inputs missing:", "; ".join(res["errors"]))
    for a in res["alerts"]:
        print(f"  [{a['severity']:7s}] {a['type']:24s} {a['message']}")
    if res.get("file"):
        print("written:", res["file"])
    return 0


if __name__ == "__main__":
    sys.exit(_main())
