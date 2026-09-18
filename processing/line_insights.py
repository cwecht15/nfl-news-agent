"""What in the market is worth acting on — read from ``data/odds/<season>/wkNN.json``.

The Line Movement page used to show every number that moved. The questions it
exists to answer are narrower:

1. **Game environment.** Has a team's market-implied total moved, and does
   the projection sheet's own spread / O-U (which drives every player
   projection in that game) still agree with the market?
2. **Player lines vs your projection.** A line moving *toward* your number
   confirms it; one moving *away* from it — or already flagged RED / AMBER
   by the odds repo's calibrated bands — is the one to re-check.
3. **The latest pull.** What changed in the most recent odds pull, derived
   from stored state so it is never blank between pulls.

Pure functions over the week file, no Sheets reads — the dashboard calls them
on every rerun. Movement thresholds are the collector's (``odds.thresholds``).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from collectors.odds_collector import (
    DEFAULT_THRESHOLDS, RATE_STATS, STAT_LABEL, _diff_game, _diff_prop, _market_implied,
)

# A team total moving a point, or the sheet sitting a point off the market,
# is worth a look; smaller is book noise. Overridable in odds.insights.
DEFAULT_INSIGHTS = {"team_total_move": 1.0, "sheet_gap": 1.0}

FLAG_RANK = {"RED": 0, "AMBER": 1, "MKT-ONLY": 2}


def _thresholds(settings: Optional[dict]) -> dict:
    odds = (settings or {}).get("odds", {}) or {}
    return odds.get("thresholds") or DEFAULT_THRESHOLDS


def _insights_cfg(settings: Optional[dict]) -> dict:
    odds = (settings or {}).get("odds", {}) or {}
    return {**DEFAULT_INSIGHTS, **(odds.get("insights") or {})}


def _r(v: Optional[float], nd: int = 1) -> Optional[float]:
    return None if v is None else round(v, nd)


def team_environment(week_data: Optional[dict], settings: Optional[dict] = None,
                     played: Optional[set[str]] = None) -> list[dict]:
    """One row per team: implied total open -> now, and the sheet's implied
    total against the market's. ``notable`` marks rows past either threshold.
    Sorted with the notable rows first, biggest gap or move first. Teams in
    ``played`` (news abbreviations; their game is over) are left out.
    """
    cfg = _insights_cfg(settings)
    played = played or set()
    rows: list[dict] = []
    for key, g in ((week_data or {}).get("games") or {}).items():
        cur, opened, sheet = g.get("current") or {}, g.get("opened") or {}, g.get("sheet") or {}
        now = _market_implied(cur.get("spread_home"), cur.get("total"))
        at_open = _market_implied(opened.get("spread_home"), opened.get("total"))
        on_sheet = _market_implied(sheet.get("spread_home"), sheet.get("ou"))
        for side, other in (("home", "away"), ("away", "home")):
            team, opp = g.get(side), g.get(other)
            if not team or team in played:
                continue
            move = (now[side] - at_open[side]) if None not in (now[side], at_open[side]) else None
            gap = (on_sheet[side] - now[side]) if None not in (on_sheet[side], now[side]) else None
            notable = ((move is not None and abs(move) >= cfg["team_total_move"])
                       or (gap is not None and abs(gap) >= cfg["sheet_gap"]))
            rows.append({
                "team": team, "opp": opp, "home": side == "home", "game": key,
                "kickoff_et": g.get("kickoff_et", ""),
                "implied_open": at_open[side], "implied_now": now[side], "implied_move": _r(move),
                "sheet_implied": on_sheet[side], "sheet_gap": _r(gap),
                "spread_open": opened.get("spread_home"), "spread_now": cur.get("spread_home"),
                "total_open": opened.get("total"), "total_now": cur.get("total"),
                "fp_flag": sheet.get("fp_flag") or "",
                "notable": notable,
            })
    rows.sort(key=lambda r: (not r["notable"],
                             -max(abs(r["sheet_gap"] or 0), abs(r["implied_move"] or 0))))
    return rows


def player_watchlist(week_data: Optional[dict], settings: Optional[dict] = None,
                     include_toward: bool = False,
                     played: Optional[set[str]] = None) -> list[dict]:
    """Player x stat lines worth re-checking against your projection.

    In: anything the odds repo flagged RED / AMBER / MKT-ONLY, plus any line
    that moved at least its movement threshold since it opened *away* from
    your number. ``include_toward`` adds the confirming moves too. THIN
    markets (too few books) are left out, as the collector does, and so are
    players whose game is already over (``played``, news abbreviations).
    """
    thr_all = (_thresholds(settings).get("props") or {})
    played = played or set()
    rows: list[dict] = []
    for p in ((week_data or {}).get("props") or {}).values():
        if p.get("thin") or p.get("team") in played:
            continue
        cur = (p.get("current") or {}).get("mkt_mu")
        opened = (p.get("opened") or {}).get("mkt_mu")
        ours = p.get("ours")
        if cur is None:
            continue
        stat = p.get("stat", "")
        thr = float(thr_all.get(stat) or 0) or None
        move = (cur - opened) if opened is not None else None
        gap_now = (cur - ours) if ours is not None else None
        gap_open = (opened - ours) if (ours is not None and opened is not None) else None
        trend = ""
        if move and thr and abs(move) >= thr and gap_now is not None and gap_open is not None:
            trend = "away" if abs(gap_now) > abs(gap_open) else "toward"
        flag = p.get("flag") or ""
        if not (flag in FLAG_RANK or trend == "away" or (include_toward and trend == "toward")):
            continue
        rows.append({
            "player": p.get("player", ""), "pos": p.get("pos", ""), "team": p.get("team", ""),
            "opp": p.get("opp", ""), "gsis_id": p.get("gsis_id", ""),
            "stat": stat, "stat_label": STAT_LABEL.get(stat, stat),
            "ours": ours, "market_open": opened, "market_now": cur,
            "move": move, "gap_now": gap_now, "trend": trend, "flag": flag,
            "line": (p.get("current") or {}).get("cons_line"),
            # Gap in units of the stat's movement threshold, so a 30-yard
            # passing gap and a half-catch gap rank on one scale.
            "score": (abs(gap_now) / thr) if (gap_now is not None and thr) else 0.0,
        })
    rows.sort(key=lambda r: (FLAG_RANK.get(r["flag"], 3), r["trend"] != "away", -r["score"]))
    return rows


def _from_side(spread_home: Optional[float], home: bool) -> Optional[float]:
    """Home spread -> this team's spread (negative = favored)."""
    if spread_home is None:
        return None
    return spread_home if home else -spread_home


def game_card(week_data: Optional[dict], team: str) -> Optional[dict]:
    """``team``'s game this week, from its own side: spread (negative =
    favored), total, implied totals for both clubs — open, now, and on your
    sheet — the sharp book, and one history row per posted-line change."""
    for key, g in ((week_data or {}).get("games") or {}).items():
        if team not in (g.get("home"), g.get("away")):
            continue
        home = g.get("home") == team
        side, other = ("home", "away") if home else ("away", "home")
        cur, opened = g.get("current") or {}, g.get("opened") or {}
        sheet, sharp = g.get("sheet") or {}, g.get("sharp") or {}
        imp_now = _market_implied(cur.get("spread_home"), cur.get("total"))
        imp_open = _market_implied(opened.get("spread_home"), opened.get("total"))
        imp_sheet = _market_implied(sheet.get("spread_home"), sheet.get("ou"))
        history = []
        for h in g.get("history") or []:
            imp = _market_implied(h.get("spread_home"), h.get("total"))
            history.append({
                "at": h.get("at"), "spread": _from_side(h.get("spread_home"), home),
                "total": h.get("total"), "implied": imp[side], "opp_implied": imp[other],
                "ml": h.get("home_ml" if home else "away_ml"),
            })
        return {
            "game": key, "team": team, "opp": g.get(other), "home": home,
            "kickoff_et": g.get("kickoff_et", ""),
            "spread_open": _from_side(opened.get("spread_home"), home),
            "spread_now": _from_side(cur.get("spread_home"), home),
            "total_open": opened.get("total"), "total_now": cur.get("total"),
            "implied_open": imp_open[side], "implied_now": imp_now[side],
            "opp_implied_open": imp_open[other], "opp_implied_now": imp_now[other],
            "ml_now": cur.get("home_ml" if home else "away_ml"),
            "sheet_spread": _from_side(sheet.get("spread_home"), home), "sheet_total": sheet.get("ou"),
            "sheet_implied": imp_sheet[side], "fp_flag": sheet.get("fp_flag") or "",
            "sharp_spread": _from_side(sharp.get("spread_home"), home), "sharp_total": sharp.get("total"),
            "history": history,
        }
    return None


STAT_ORDER = {s: i for i, s in enumerate(STAT_LABEL)}
POS_ORDER = {"QB": 0, "RB": 1, "FB": 2, "WR": 3, "TE": 4, "K": 5}


def display_value(stat: str, v: Optional[float]) -> Optional[float]:
    """A market or projected value the way a person reads it.

    Anytime TD is stored as an expected-TD rate (lambda); shown as the chance
    of scoring, P = 1 - e^-lambda, in percent — "56%" reads, "0.82" does not.
    Everything else is the stat's own mean.
    """
    if v is None:
        return None
    if stat in RATE_STATS:
        return 100.0 * (1.0 - math.exp(-max(float(v), 0.0)))
    return float(v)


def stat_display_label(stat: str) -> str:
    return "Anytime TD %" if stat in RATE_STATS else STAT_LABEL.get(stat, stat)


def prop_table(week_data: Optional[dict], settings: Optional[dict] = None, *,
               team: Optional[str] = None, played: Optional[set[str]] = None,
               only_moved: bool = True, include_thin: bool = False) -> list[dict]:
    """Player lines, one row per player x stat, in comparable units.

    ``move_pct`` is the move since open relative to the opening value, so a
    half-catch move (4.5 -> 5.2 receptions, +14%) and a 17-yard move (245 ->
    228 passing yards, -7%) rank on one scale. ``only_moved`` keeps lines
    whose raw move reached the stat's movement threshold (``odds.thresholds``),
    which is what keeps a 0.02 -> 0.04 TD rate from reading as +100%.
    ``vs_you_pct`` is the market relative to your projection. THIN markets
    (too few books) are left out unless ``include_thin``.

    Rows sort by ``size`` — the raw move in units of the stat's threshold —
    not by ``move_pct``: a TD chance going 3% -> 12% is +300% and would bury
    every yardage line, while in threshold units it ranks with its peers.
    """
    thr_all = _thresholds(settings).get("props") or {}
    played = played or set()
    rows: list[dict] = []
    for p in ((week_data or {}).get("props") or {}).values():
        if team and p.get("team") != team:
            continue
        if p.get("team") in played or (p.get("thin") and not include_thin):
            continue
        stat = p.get("stat", "")
        cur = (p.get("current") or {}).get("mkt_mu")
        if cur is None:
            continue
        opened = (p.get("opened") or {}).get("mkt_mu")
        prev = (p.get("previous") or {}).get("mkt_mu") if p.get("previous") else None
        thr = float(thr_all.get(stat) or 0)
        raw_move = (cur - opened) if opened is not None else None
        moved = bool(raw_move is not None and thr and abs(raw_move) >= thr)
        if only_moved and not moved:
            continue
        now_d, open_d = display_value(stat, cur), display_value(stat, opened)
        you_d, prev_d = display_value(stat, p.get("ours")), display_value(stat, prev)
        rows.append({
            "player": p.get("player", ""), "pos": p.get("pos", ""), "team": p.get("team", ""),
            "opp": p.get("opp", ""), "gsis_id": p.get("gsis_id", ""), "stat": stat,
            "stat_label": stat_display_label(stat),
            "you": you_d, "book_line": (p.get("current") or {}).get("cons_line"),
            "open": open_d, "now": now_d,
            "move": (now_d - open_d) if open_d is not None else None,
            "move_pct": ((now_d - open_d) / open_d * 100.0) if open_d else None,
            "last_pull": (now_d - prev_d) if prev_d is not None else None,
            "vs_you_pct": ((now_d - you_d) / you_d * 100.0) if you_d else None,
            "flag": p.get("flag") or "", "thin": bool(p.get("thin")), "moved": moved,
            "size": (abs(raw_move) / thr) if (raw_move is not None and thr) else 0.0,
        })
    rows.sort(key=lambda r: -r["size"])
    return rows


def latest_pull_moves(week_data: Optional[dict], settings: Optional[dict] = None) -> list[dict]:
    """What moved in the most recent pull versus the one before it.

    Derived from stored state rather than a run's diff, so it answers "what
    did the last pull change" at any time — including hours after the run
    that first saw it. Games compare their last two history entries when the
    latest one is this pull; props compare ``previous`` -> ``current`` for
    the keys this pull refreshed (an anytime-TD pull refreshes only those).
    """
    week_data = week_data or {}
    thresholds = _thresholds(settings)
    pull = week_data.get("pull") or {}
    out: list[dict] = []
    for g in (week_data.get("games") or {}).values():
        hist = g.get("history") or []
        if len(hist) < 2 or hist[-1].get("at") != pull.get("pulled_at"):
            continue
        before, after = hist[-2], hist[-1]
        fresh = {"away": g.get("away"), "home": g.get("home"),
                 **{k: after.get(k) for k in ("spread_home", "total", "home_ml", "away_ml")}}
        out.extend(_diff_game({"current": before, "opened": g.get("opened") or {}}, fresh, thresholds))
    props_pull = pull.get("props_pull_id")
    for p in (week_data.get("props") or {}).values():
        cur, prev = p.get("current") or {}, p.get("previous")
        if not prev or not props_pull or cur.get("at") != props_pull:
            continue
        out.extend(c for c in _diff_prop({"current": prev}, p, thresholds) if c["type"] == "prop_move")
    out.sort(key=lambda c: -float(c.get("magnitude") or 0))
    return out


def pull_batches(week_data: Optional[dict]) -> list[dict]:
    """Logged pulls, newest first — ``[{"pulled_at", "seen_at", "changes"}]``."""
    return list(reversed((week_data or {}).get("pull_log") or []))


def summary_counts(week_data: Optional[dict], settings: Optional[dict] = None,
                   played: Optional[set[str]] = None) -> dict[str, Any]:
    teams = team_environment(week_data, settings, played=played)
    players = player_watchlist(week_data, settings, played=played)
    return {
        "teams_notable": sum(1 for t in teams if t["notable"]),
        "sheet_off": sum(1 for t in teams
                         if t["sheet_gap"] is not None
                         and abs(t["sheet_gap"]) >= _insights_cfg(settings)["sheet_gap"]),
        "players": len(players),
        "players_red": sum(1 for p in players if p["flag"] == "RED"),
        "players_away": sum(1 for p in players if p["trend"] == "away"),
        # What the page lists by default: RED, or moving away from you (one line can be both).
        "players_recheck": sum(1 for p in players if p["flag"] == "RED" or p["trend"] == "away"),
    }
