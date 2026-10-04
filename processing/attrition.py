"""Positional injury attrition — which depth-chart slots each team is down.

The Injury Report lists every injured player, but it cannot say that a team
is down its CB1 *and* CB2, or that its WR1 went on IR two weeks ago. OurLads
can't either: it moves an IR'd player into its ``IR`` bucket and promotes the
backup, so the current chart hides that a starter is missing at all.

So the slots are frozen once, from the depth chart just before Week 1
(:func:`build_baseline` → ``data/attrition/<season>/baseline.json``), and every
run asks of each baseline player: is he available THIS week?

* QB/RB/WR/TE ranks come from the Week-1 projection sheet (the user's own
  pecking order — OurLads' LWR/RWR/SWR rows don't rank receivers).
* OL / IDL / EDGE / LB / CB / S ranks come from the OurLads chart. Its page
  ends with a practice-squad block reusing plain labels (WR, C, DT, LB, CB,
  S, ED ...), which the stored snapshot can't tell apart from a team that
  uses the same label up top — so every OurLads player must be on the 53
  per nflverse on the baseline date.
* A starter who also returns kicks was stored under ``KR``/``PR`` (the
  snapshot keeps one row per name and the return rows come last), leaving
  his real slot empty. :func:`build_baseline` puts a returner whose nflverse
  position fits back into the empty starting slot and logs it.

Status sources, most severe wins: roster state (IR/PUP/NFI/SUS — this is
what remembers earlier weeks' losses), this week's inactives, this week's
injury report (OUT/D/Q, and a non-rest DNP before designations exist).
A player traded/released/sent to the practice squad is "departed": shown
greyed, never scored. Players lost before Week 1 are not in the baseline.

Pure functions; the only I/O is in the ``load_*`` helpers and the CLI.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from config_loader import get_data_dir, get_settings
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

# Display order of the units (heat-map columns).
UNITS = ["QB", "RB", "WR", "TE", "OL", "IDL", "EDGE", "LB", "CB", "S"]
SKILL_UNITS = ("QB", "RB", "WR", "TE")
# Prefix used in a slot label ("DT1", "EDGE2", "CB3"); OL starters keep their spot.
UNIT_LABEL = {"IDL": "DT", "EDGE": "EDGE", "LB": "LB", "CB": "CB", "S": "S",
              "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "OL": "OL"}

OL_SPOTS = ["LT", "LG", "C", "RG", "RT"]
# OurLads row order on the page — the tie-break inside a unit at equal depth.
ROW_ORDER = OL_SPOTS + ["OT", "OG",
                        "LDE", "LDT", "DT", "NT", "RDT", "RDE", "DE", "WDE", "SDE",
                        "ED", "RUSH", "LOLB", "OLB", "ROLB",
                        "LILB", "MLB", "MIKE", "ILB", "RILB", "WLB", "WILL", "SLB", "SAM", "LB",
                        "LCB", "RCB", "CB", "SCB", "NB", "SS", "FS", "S"]
_ROW_RANK = {r: i for i, r in enumerate(ROW_ORDER)}
_DE_ROWS = {"LDE", "RDE", "DE", "WDE", "SDE"}
# Rows that mean a 3-4 / odd front: their DEs line up inside.
_ODD_FRONT_EDGE_ROWS = {"LOLB", "ROLB", "OLB", "RUSH", "ED"}
_FIXED_UNIT = {
    **{r: "OL" for r in OL_SPOTS + ["OT", "OG"]},
    **{r: "IDL" for r in ("LDT", "RDT", "DT", "NT")},
    **{r: "EDGE" for r in ("ED", "RUSH", "LOLB", "ROLB", "OLB")},
    **{r: "LB" for r in ("LILB", "RILB", "MLB", "MIKE", "ILB", "WLB", "WILL", "SLB", "SAM", "LB")},
    **{r: "CB" for r in ("LCB", "RCB", "CB", "SCB", "NB")},
    **{r: "S" for r in ("SS", "FS", "S")},
}
_RETURN_ROWS = {"KR", "PR"}
# Labels OurLads reuses in its practice-squad block at the foot of the page.
# A player on one of these rows counts only when nflverse confirms he is on
# the 53; a name nflverse can't match is kept on any other row (the 09-08
# nflverse file simply lacked Harrison Smith, Joshua Metellus, Vega Ioane).
_PS_BLOCK_ROWS = {"WR", "OT", "OG", "C", "TE", "QB", "RB", "ED", "DT", "LB", "CB", "S"}
# nflverse depth_chart_position → unit, for returner repair.
_DCP_UNIT = {"CB": "CB", "DB": "CB", "SS": "S", "FS": "S", "S": "S"}
# Starting rows a unit is expected to have, for detecting a row that lost
# its starter entirely to a KR/PR overwrite.
_PAIRED_ROWS = {"CB": [("LCB", "RCB")], "S": [("SS", "FS")]}

_RESERVE = {"IR", "PUP", "NFI", "SUS"}
_GONE = {"FA", "RET", "CUT", "EXE", "PS", "DEV", "UFA", "TRD"}
_NOT_INJURY_RELATED = re.compile(r"\bnir\b|not injury[- ]related|\brest\b|\bpersonal\b", re.IGNORECASE)
_STATUS_TEXT = {"IR": "IR", "PUP": "PUP", "NFI": "NFI", "SUS": "suspended",
                "INACTIVE": "inactive", "OUT": "out", "D": "doubtful", "Q": "questionable",
                "DNP": "DNP"}

DEFAULTS = {
    "baseline_depth_date": "2026-09-09",
    "baseline_proj_snapshot": "wk01/primary/2026-09-08",
    "baseline_nflverse_date": "2026-09-08",
    "skill_starters": {"QB": 1, "RB": 1, "WR": 3, "TE": 1},
    "skill_extra_depth": 2,          # backups kept per skill unit beyond the starters
    "max_depth": 2,                  # OurLads columns kept (1 = starter, 2 = first backup)
    "weights": {"IR": 1.0, "PUP": 1.0, "NFI": 1.0, "SUS": 1.0, "INACTIVE": 1.0,
                "OUT": 1.0, "D": 0.75, "DNP": 0.5, "Q": 0.35},
    "slot_weights": {"starter": 1.0, "backup1": 0.5, "deeper": 0.25},
    "levels": {"severe": 1.75, "notable": 0.9},
    "unit_levels": {"QB": {"severe": 0.9, "notable": 0.5}},
    "notes": {"own_units": ["QB", "RB", "WR", "TE", "OL"], "opp_max_units": 3},
}


def config(settings: Optional[dict] = None) -> dict:
    cfg = dict(DEFAULTS)
    user = ((settings if settings is not None else get_settings()) or {}).get("attrition") or {}
    for k, v in user.items():
        cfg[k] = {**cfg[k], **v} if isinstance(cfg.get(k), dict) and isinstance(v, dict) else v
    return cfg


def name_key(name: str) -> str:
    from collectors.nflverse_roster_collector import name_key as _nk
    return _nk(name or "")


def _short(name: str) -> str:
    """Last name for prompt lines; keeps suffixes off ("Metchie III" → Metchie)."""
    parts = [p for p in (name or "").split() if p.rstrip(".").upper() not in {"JR", "SR", "II", "III", "IV", "V"}]
    return parts[-1] if parts else (name or "")


def _readable(name: str) -> str:
    """OurLads writes some names in caps ("DAVID MOORE"); title-case those."""
    return name.title() if name and name.isupper() else name


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------
def _nflverse_index(nflverse: dict) -> tuple[dict, dict]:
    by_key: dict[str, list[dict]] = defaultdict(list)
    by_last: dict[tuple, list[dict]] = defaultdict(list)
    for gsis, p in ((nflverse or {}).get("players") or {}).items():
        rec = {**p, "gsis_id": p.get("gsis_id") or gsis, "team_news": to_news(p.get("team", ""), "nflverse")}
        k = p.get("name_key") or name_key(p.get("name", ""))
        by_key[k].append(rec)
        toks = k.split()
        if toks:
            by_last[(rec["team_news"], toks[-1], toks[0][:1])].append(rec)
    return by_key, by_last


def _match_nflverse(name: str, team: str, by_key: dict, by_last: dict) -> Optional[dict]:
    k = name_key(name)
    cands = by_key.get(k) or []
    same = [c for c in cands if c["team_news"] == team]
    if len(same) == 1:
        return same[0]
    if len(cands) == 1 and not same:
        return None  # a namesake on another club, not this player
    toks = k.split()
    if toks:
        alt = by_last.get((team, toks[-1], toks[0][:1])) or []
        if len(alt) == 1:
            return alt[0]
    return None


def _team_units(rows: dict[str, list[dict]]) -> dict[str, str]:
    """Row label → unit for one team. DE rows sit inside in an odd front."""
    odd = bool(_ODD_FRONT_EDGE_ROWS & set(rows))
    out = {}
    for r in rows:
        if r in _DE_ROWS:
            out[r] = "IDL" if odd else "EDGE"
        elif r in _FIXED_UNIT:
            out[r] = _FIXED_UNIT[r]
    return out


def build_baseline(depth_chart: dict, proj_players: dict, nflverse: dict,
                   overrides: Optional[dict] = None, settings: Optional[dict] = None,
                   meta: Optional[dict] = None) -> dict:
    """Freeze each team's Week-1 slots. Returns the baseline file body."""
    cfg = config(settings)
    by_key, by_last = _nflverse_index(nflverse)
    max_depth = int(cfg["max_depth"])
    repairs: list[str] = []
    teams: dict[str, list[dict]] = defaultdict(list)

    # --- OL / defense from OurLads -------------------------------------
    per_team_rows: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    returners: dict[str, list[dict]] = defaultdict(list)
    for p in (depth_chart or {}).values():
        team = to_news(p.get("team", ""), "ourlads")
        # Snapshots from 2026-10 on keep the rows a name was displaced from
        # in `also` (page order); his first unit row is his real slot.
        rows_seen = list(p.get("also") or []) + [{"pos": p.get("pos"), "depth": p.get("depth")}]
        first = next((r for r in rows_seen if str(r.get("pos") or "").upper() in _FIXED_UNIT
                      or str(r.get("pos") or "").upper() in _DE_ROWS), None)
        if first:
            p = {**p, "pos": first["pos"], "depth": first["depth"]}
        row = str(p.get("pos") or "").upper()
        if row not in _FIXED_UNIT and row not in _DE_ROWS and row not in _RETURN_ROWS:
            continue
        nv = _match_nflverse(p.get("name", ""), team, by_key, by_last)
        if nv and nv.get("status") != "ACT":
            continue  # practice squad / reserve / cut — not on the 53
        if not nv and (row in _PS_BLOCK_ROWS or row in _RETURN_ROWS):
            continue  # can't tell the 53's row from the practice-squad block's
        nv = nv or {}
        rec = {"name": _readable(p.get("name", "")), "row": row, "depth": int(p.get("depth") or 9), "nv": nv}
        if row in _RETURN_ROWS:
            returners[team].append(rec)
        elif rec["depth"] <= max_depth:
            per_team_rows[team][row].append(rec)

    for team, rows in per_team_rows.items():
        # Returner repair: a starting row with no depth-1 player (or the
        # missing half of LCB/RCB, SS/FS) gets a returner of that position.
        for unit, pairs in _PAIRED_ROWS.items():
            for pair in pairs:
                present = [r for r in pair if r in rows]
                if not present:
                    continue
                for r in pair:
                    if any(x["depth"] == 1 for x in rows.get(r, [])):
                        continue
                    pick = next((x for x in sorted(returners.get(team, []), key=lambda x: x["depth"])
                                 if _DCP_UNIT.get(x["nv"].get("depth_chart_position", "")) == unit
                                 and not any(x["name"] == y["name"] for rr in rows.values() for y in rr)), None)
                    if pick:
                        rows[r].append({**pick, "row": r, "depth": 1, "repaired": True})
                        repairs.append(f"{team} {r}1 <- {pick['name']} (was {pick['row']}{pick['depth']})")

        units = _team_units(rows)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for r, recs in rows.items():
            u = units.get(r)
            if u:
                grouped[u].extend(recs)
        for unit, recs in grouped.items():
            recs.sort(key=lambda x: (x["depth"], _ROW_RANK.get(x["row"], 99)))
            starters = [x for x in recs if x["depth"] == 1]
            for i, x in enumerate(recs, 1):
                if unit == "OL" and x["depth"] == 1 and x["row"] in OL_SPOTS:
                    label = x["row"]
                elif unit == "OL":
                    label = f"OL{i}"
                else:
                    label = f"{UNIT_LABEL[unit]}{i}"
                teams[team].append(_entry(team, unit, label, i, x["depth"] == 1, x["name"],
                                          x["nv"].get("gsis_id"), row=x["row"], repaired=x.get("repaired", False),
                                          starters=len(starters)))

    # --- skill positions from the Week-1 sheet --------------------------
    skill_starters = cfg["skill_starters"]
    extra = int(cfg["skill_extra_depth"])
    by_team_pos: dict[tuple, list[dict]] = defaultdict(list)
    for p in (proj_players or {}).values():
        if str(p.get("status") or "").lower() != "active":
            continue
        pos = str(p.get("pos") or "").upper()
        if pos not in SKILL_UNITS:
            continue
        by_team_pos[(to_news(p.get("team", ""), "proj"), pos)].append(p)
    for (team, pos), ps in by_team_pos.items():
        ps.sort(key=lambda p: (p.get("depth") or 99, p.get("slot") or 999))
        n = int(skill_starters.get(pos, 1))
        for i, p in enumerate(ps[: n + extra], 1):
            teams[team].append(_entry(team, pos, f"{pos}{i}", i, i <= n, p.get("name", ""),
                                      p.get("player_id"), starters=n))

    # --- manual overrides: {team: {label: "Player Name"}} ----------------
    for team, labels in (overrides or {}).items():
        if team.startswith("_"):
            continue
        for label, name in (labels or {}).items():
            slot = next((e for e in teams[team] if e["label"] == label), None)
            nv = _match_nflverse(name, team, by_key, by_last)
            if slot:
                slot.update(name=name, name_key=name_key(name), gsis_id=(nv or {}).get("gsis_id"), override=True)
            repairs.append(f"{team} {label} := {name} (override)")

    for team in teams:
        teams[team].sort(key=lambda e: (UNITS.index(e["unit"]), e["rank"]))
    return {**(meta or {}), "repairs": repairs, "teams": dict(sorted(teams.items()))}


def _entry(team, unit, label, rank, starter, name, gsis, *, row="", repaired=False, starters=0) -> dict:
    return {"team": team, "unit": unit, "label": label, "rank": rank, "starter": bool(starter),
            "name": name, "name_key": name_key(name), "gsis_id": gsis, "row": row,
            "repaired": repaired, "unit_starters": starters}


def baseline_path(season: int) -> Path:
    return get_data_dir("attrition") / str(season) / "baseline.json"


def load_baseline(season: int) -> Optional[dict]:
    try:
        return json.loads(baseline_path(season).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# This week's status
# ---------------------------------------------------------------------------
def _state_rec(state: dict, gsis: Optional[str], key: str) -> Optional[dict]:
    players = (state or {}).get("players") or {}
    if gsis and gsis in players:
        return players[gsis]
    gid = ((state or {}).get("by_name") or {}).get(key)
    return players.get(gid) if gid else None


def _injury_index(injury_week: Optional[dict]) -> dict[tuple, dict]:
    out = {}
    for team, rec in ((injury_week or {}).get("teams") or {}).items():
        players = (rec or {}).get("players") or {}
        if isinstance(players, dict):
            players = list(players.values())
        for p in players:
            if isinstance(p, dict) and not p.get("cleared"):
                out[(team, name_key(p.get("name", "")))] = p
    return out


def _inactive_index(inactives_weeks: dict[int, dict]) -> dict[int, set]:
    """{week: {gsis or "team|name_key", ...}} — one entry per inactive player."""
    out: dict[int, set] = {}
    for wk, data in (inactives_weeks or {}).items():
        s: set = set()
        for g in ((data or {}).get("games") or {}).values():
            for team, t in ((g or {}).get("teams") or {}).items():
                for p in (t or {}).get("inactives") or []:
                    if p.get("gsis_id"):
                        s.add(p["gsis_id"])
                    s.add(f"{to_news(p.get('team') or team, 'espn')}|{p.get('name_key') or name_key(p.get('name', ''))}")
        out[int(wk)] = s
    return out


def _inactive(idx: set, e: dict) -> bool:
    return bool(idx) and ((e.get("gsis_id") and e["gsis_id"] in idx) or f"{e['team']}|{e['name_key']}" in idx)


def player_status(e: dict, state: dict, injuries: dict, inactive_now: set, cfg: dict,
                  schedule: Optional[list] = None) -> dict:
    """Most severe availability signal for one baseline player."""
    w = cfg["weights"]
    st = _state_rec(state, e.get("gsis_id"), e["name_key"])
    out = {"code": "", "weight": 0.0, "detail": "", "since": "", "departed": False}
    if st:
        status, team = str(st.get("status") or "").upper(), to_news(st.get("team", ""))
        if status in _RESERVE:
            since = st.get("ir_date") or (st.get("last_event") or {}).get("date") or ""
            out.update(code=status, weight=float(w.get(status, 1.0)), since=since)
            if since and schedule:
                from processing.season import week_from_date
                wk = week_from_date(schedule, since)
                out["since_week"] = wk
            if st.get("earliest_return_week"):
                out["return_week"] = st["earliest_return_week"]
            if st.get("designated_return_date"):
                out["detail"] = "designated to return"
            return out
        if team and team != e["team"]:
            out.update(departed=True, detail=f"now {team}" if status in ("ACT", "PS", "IR") else status.lower())
            return out
        if status in _GONE:
            out.update(departed=True, detail={"PS": "practice squad", "DEV": "practice squad"}.get(status, status.lower()))
            return out
    cands = []
    if _inactive(inactive_now, e):
        cands.append(("INACTIVE", ""))
    p = injuries.get((e["team"], e["name_key"]))
    if p:
        code = str(p.get("game_status") or "").strip().upper()
        injury = str(p.get("injury") or "").strip()
        if code in ("OUT", "O"):
            cands.append(("OUT", injury))
        elif code in ("D", "Q"):
            cands.append((code, injury))
        else:
            practice = p.get("practice") or {}
            if isinstance(practice, dict) and practice:
                last = str(practice[max(practice)] or "").strip().upper()
                if last == "DNP" and not _NOT_INJURY_RELATED.search(injury):
                    cands.append(("DNP", injury))
    if cands:
        code, detail = max(cands, key=lambda c: float(w.get(c[0], 0)))
        out.update(code=code, weight=float(w.get(code, 0)), detail=detail.lower())
    return out


def _slot_weight(e: dict, cfg: dict) -> float:
    sw = cfg["slot_weights"]
    if e["starter"]:
        return float(sw["starter"])
    return float(sw["backup1"] if e["rank"] == (e.get("unit_starters") or 0) + 1 else sw["deeper"])


def _level(unit: str, score: float, cfg: dict) -> str:
    lv = {**cfg["levels"], **(cfg.get("unit_levels") or {}).get(unit, {})}
    if score >= float(lv["severe"]):
        return "severe"
    if score >= float(lv["notable"]):
        return "notable"
    return "mild" if score > 0 else "none"


def _next_up(e: dict, latest_depth_by_team: dict, current_skill: dict) -> str:
    if e["unit"] in SKILL_UNITS:
        cur = current_skill.get((e["team"], e["unit"], e["rank"]))
        return cur if cur and name_key(cur) != e["name_key"] else ""
    row = e.get("row")
    # Only a starter's row has a meaningful "who holds it now"; a backup's
    # row's depth-1 is just the starter who was there all along.
    if not row or not e.get("starter"):
        return ""
    for p in latest_depth_by_team.get(e["team"], []):
        if p.get("pos") == row and int(p.get("depth") or 0) == 1 and name_key(p.get("name", "")) != e["name_key"]:
            return _readable(p.get("name", ""))
    return ""


def build_attrition(baseline: dict, state: Optional[dict], injury_week: Optional[dict],
                    inactives_weeks: Optional[dict[int, dict]], week: Optional[int],
                    latest_depth: Optional[dict] = None, current_proj: Optional[dict] = None,
                    schedule: Optional[list] = None, settings: Optional[dict] = None) -> dict:
    """``{team: {unit: {...}}}`` for every baseline team.

    Each unit: ``score`` (Σ status weight × slot weight), ``level``,
    ``starters`` / ``starters_down`` (full-weight equivalents), ``slots`` (every
    baseline slot with its status) and ``down`` (the affected slots only,
    departed included so a gap is explained).
    """
    cfg = config(settings)
    injuries = _injury_index(injury_week)
    inactive_by_week = _inactive_index(inactives_weeks or {})
    inactive_now = inactive_by_week.get(int(week), set()) if week else set()

    latest_by_team: dict[str, list] = defaultdict(list)
    for p in (latest_depth or {}).values():
        latest_by_team[to_news(p.get("team", ""), "ourlads")].append(p)
    current_skill: dict[tuple, str] = {}
    grouped_cur: dict[tuple, list] = defaultdict(list)
    for p in (current_proj or {}).values():
        if str(p.get("status") or "").lower() == "active" and str(p.get("pos") or "").upper() in SKILL_UNITS:
            grouped_cur[(to_news(p.get("team", ""), "proj"), p["pos"].upper())].append(p)
    for (team, pos), ps in grouped_cur.items():
        ps.sort(key=lambda p: (p.get("depth") or 99, p.get("slot") or 999))
        for i, p in enumerate(ps, 1):
            current_skill[(team, pos, i)] = p.get("name", "")

    out: dict[str, dict] = {}
    for team, entries in ((baseline or {}).get("teams") or {}).items():
        units: dict[str, dict] = {}
        for e in entries:
            s = player_status(e, state or {}, injuries, inactive_now, cfg, schedule)
            missed = sorted(wk for wk, idx in inactive_by_week.items()
                            if (week is None or wk < int(week)) and _inactive(idx, e))
            slot = {**{k: e[k] for k in ("label", "name", "gsis_id", "starter", "rank", "row")},
                    **s, "slot_weight": _slot_weight(e, cfg), "missed_weeks": missed}
            slot["impact"] = 0.0 if s["departed"] else round(slot["slot_weight"] * s["weight"], 3)
            slot["next_up"] = _next_up(e, latest_by_team, current_skill) if (s["weight"] or s["departed"]) else ""
            u = units.setdefault(e["unit"], {"unit": e["unit"], "slots": [], "score": 0.0,
                                             "starters": 0, "starters_down": 0.0})
            u["slots"].append(slot)
            u["score"] += slot["impact"]
            if e["starter"]:
                u["starters"] += 1
                if not s["departed"]:
                    u["starters_down"] += s["weight"]
        for unit, u in units.items():
            u["score"] = round(u["score"], 3)
            u["starters_down"] = round(u["starters_down"], 2)
            u["level"] = _level(unit, u["score"], cfg)
            u["down"] = [s for s in u["slots"] if s["weight"] > 0 or s["departed"]]
        out[team] = units
    return out


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------
def slot_status_text(s: dict, short: bool = False) -> str:
    if s.get("departed"):
        return s.get("detail") or "departed"
    txt = _STATUS_TEXT.get(s.get("code", ""), s.get("code", "").lower())
    if s.get("code") in _RESERVE and s.get("since_week"):
        txt += f" wk{s['since_week']}" if short else f" since Wk{s['since_week']}"
    return txt


def unit_cell(u: Optional[dict]) -> str:
    """Compact heat-map text: "CB1·CB2(Q)"."""
    if not u:
        return ""
    bits = []
    for s in u.get("down") or []:
        if s.get("departed") or not s.get("weight"):
            continue
        code = s.get("code", "")
        bits.append(s["label"] + ("" if s["weight"] >= 1 else f"({code})"))
    return "·".join(bits)


def _slot_phrase(s: dict) -> str:
    return f"{s['label']} {_short(s['name'])} ({slot_status_text(s, short=True)})"


def own_team_line(units: dict, cfg: dict) -> str:
    """"down: WR1 Moore (IR wk2), WR3 Palmer (questionable)" — skill + OL units
    at notable or worse, plus any skill starter fully out."""
    bits = []
    for unit in cfg["notes"]["own_units"]:
        u = units.get(unit)
        if not u:
            continue
        hit = [s for s in u["down"] if not s["departed"] and s["weight"] > 0]
        if u["level"] in ("notable", "severe"):
            bits += [_slot_phrase(s) for s in hit]
        elif unit in SKILL_UNITS:
            bits += [_slot_phrase(s) for s in hit if s["starter"] and s["weight"] >= 1]
    return ", ".join(bits)


def opponent_line(units: dict, cfg: dict) -> str:
    """"CB1 Banks (IR wk2), CB2 Adebo (out); S1 Belton (doubtful)" — the
    worst units at notable or above, at most ``opp_max_units``."""
    ranked = sorted((u for u in units.values() if u["level"] in ("notable", "severe")),
                    key=lambda u: -u["score"])[: int(cfg["notes"]["opp_max_units"])]
    return "; ".join(", ".join(_slot_phrase(s) for s in u["down"] if not s["departed"] and s["weight"] > 0)
                     for u in ranked)


# ---------------------------------------------------------------------------
# Loading (pipeline + CLI; the dashboard has its own cached loaders)
# ---------------------------------------------------------------------------
def load_inactives_weeks(season: int) -> dict[int, dict]:
    out = {}
    d = get_data_dir("inactives") / str(season)
    for p in d.glob("wk*.json"):
        try:
            out[int(p.stem[2:])] = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    return out


def load_current_proj(season: int) -> Optional[dict]:
    """players.json of the snapshot ``active.json`` points at (dir is
    relative to data/weekly_projections, e.g. "2026/wk04/primary/2026-09-30")."""
    base = get_data_dir("weekly_projections")
    try:
        ptr = json.loads((base / str(season) / "active.json").read_text(encoding="utf-8"))
        return json.loads((base / ptr["dir"] / "players.json").read_text(encoding="utf-8"))
    except (OSError, KeyError, json.JSONDecodeError):
        return None


def compute_for_week(season: int, week: int, settings: Optional[dict] = None) -> Optional[dict]:
    """Load every input from disk and run :func:`build_attrition`."""
    baseline = load_baseline(season)
    if not baseline:
        return None
    from collectors.depth_chart_collector import load_latest_depth_charts
    from collectors.injury_report_collector import load_week_file
    from processing.season import load_schedule
    try:
        state = json.loads((get_data_dir("roster") / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = None
    return build_attrition(baseline, state, load_week_file(season, week), load_inactives_weeks(season),
                           week, latest_depth=load_latest_depth_charts(),
                           current_proj=load_current_proj(season),
                           schedule=load_schedule(season=season), settings=settings)


if __name__ == "__main__":
    import argparse

    from processing.season import get_season_context

    ap = argparse.ArgumentParser(description="Print positional attrition for a team")
    ap.add_argument("--team", help="news-style abbr (default: every team with a notable unit)")
    ap.add_argument("--week", type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ctx = get_season_context()
    wk = args.week or ctx.week
    res = compute_for_week(ctx.season, wk)
    if res is None:
        sys.exit("No baseline — run scripts/build_attrition_baseline.py first")
    cfg = config()
    for team in sorted(res):
        if args.team and team != args.team.upper():
            continue
        units = res[team]
        if not args.team and not any(u["level"] in ("notable", "severe") for u in units.values()):
            continue
        print(f"\n{team}  (week {wk})")
        for unit in UNITS:
            u = units.get(unit)
            if not u or not u["down"]:
                continue
            print(f"  {unit:5} {u['level']:8} score {u['score']:.2f}  starters down {u['starters_down']}/{u['starters']}")
            for s in u["down"]:
                nxt = f"  -> {s['next_up']}" if s.get("next_up") else ""
                print(f"      {s['label']:6} {s['name']:28} {slot_status_text(s)}{nxt}")
