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

from datetime import datetime, timedelta, timezone
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


# Anytime TD is shown as implied TDs — the expected-TD rate the NFL Odds
# project derives from the price. It is the unit the projection sheet uses
# (Rush TD 0.7 + Rec TD 0.2), so "your proj" compares to it directly. It is
# a count, not a probability; P(scores) = 1 - e^-rate if ever needed.
TD_MIN_MOVE = 0.02        # smaller than this does not show at two decimals

# Stats read to two decimals (counts well under 1); everything else to one.
FINE_STATS = {"anytime_td", "pass_tds", "rush_tds", "rec_tds", "ints"}


def display_value(stat: str, v: Optional[float]) -> Optional[float]:
    """A market or projected value as the tables show it (the stat's own mean)."""
    return None if v is None else float(v)


def stat_display_label(stat: str) -> str:
    return "Implied TDs" if stat in RATE_STATS else STAT_LABEL.get(stat, stat)


def prop_table(week_data: Optional[dict], settings: Optional[dict] = None, *,
               team: Optional[str] = None, played: Optional[set[str]] = None,
               only_moved: bool = True, include_thin: bool = False,
               min_move_pct: Optional[float] = None,
               baseline: str = "open", since: Optional[str] = None) -> list[dict]:
    """Player lines, one row per player x stat, in comparable units.

    ``move_pct`` is the move since open relative to the opening value, so a
    half-catch move (4.5 -> 5.2 receptions, +14%) and a 17-yard move (245 ->
    228 passing yards, -7%) rank on one scale. ``only_moved`` keeps lines
    whose raw move reached the stat's movement threshold (``odds.thresholds``),
    which is what keeps a 0.02 -> 0.04 TD rate from reading as +100%.
    ``min_move_pct`` replaces that with a plain percent cut — "moved at least
    5%" — which is what a person scanning a team means by *moved*: the report
    thresholds hide a 6-yard (10%) rushing move. Anytime TD also needs a full
    ``TD_MIN_MOVE`` (0.02), so 0.030 -> 0.032 implied TDs (+7%) is not a move.
    ``vs_you_pct`` is the market relative to your projection. THIN markets
    (too few books) are left out unless ``include_thin``.

    Rows sort by ``size`` — the raw move in units of the stat's threshold —
    not by ``move_pct``: implied TDs going 0.03 -> 0.12 is +300% and would bury
    every yardage line, while in threshold units it ranks with its peers.

    ``baseline`` / ``since`` choose what the ``open`` column measures from
    (see :func:`team_movement`); the default is the week's opening line.
    """
    thr_all = _thresholds(settings).get("props") or {}
    played = played or set()
    bctx = _baseline_ctx(week_data, baseline, since)
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
        opened = _prop_base(p, bctx)
        prev = (p.get("previous") or {}).get("mkt_mu") if p.get("previous") else None
        thr = float(thr_all.get(stat) or 0)
        raw_move = (cur - opened) if opened is not None else None
        moved = bool(raw_move is not None and thr and abs(raw_move) >= thr)
        now_d, open_d = display_value(stat, cur), display_value(stat, opened)
        move_pct = ((now_d - open_d) / open_d * 100.0) if open_d else None
        if min_move_pct is not None:
            moved = (move_pct is not None and abs(move_pct) >= min_move_pct
                     and (stat not in RATE_STATS or abs(now_d - open_d) >= TD_MIN_MOVE))
            if min_move_pct > 0 and not moved:
                continue
        elif only_moved and not moved:
            continue
        you_d, prev_d = display_value(stat, p.get("ours")), display_value(stat, prev)
        rows.append({
            "player": p.get("player", ""), "pos": p.get("pos", ""), "team": p.get("team", ""),
            "opp": p.get("opp", ""), "gsis_id": p.get("gsis_id", ""), "stat": stat,
            "stat_label": stat_display_label(stat),
            "you": you_d, "book_line": (p.get("current") or {}).get("cons_line"),
            "open": open_d, "now": now_d,
            "move": (now_d - open_d) if open_d is not None else None,
            "move_pct": move_pct,
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


# ---------------------------------------------------------------------------
# Movement from a chosen baseline — "what moved", kept apart from "is my sheet
# off the market". The page used to fold both into one "notable" list, so a
# team could be listed without having moved and a move under a point vanished
# into a collapsed expander.
# ---------------------------------------------------------------------------

#: open = since the line opened this week; window = since the previous day's
#: report (``since`` = its generated_at); last = the most recent pull only.
BASELINES = ("open", "window", "last")


def _ts(v: Any) -> Optional[datetime]:
    """ISO string -> aware datetime. The odds files mix ``-04:00`` pull times
    with ``+00:00`` / ``Z`` ones, so they are compared as datetimes, never as
    strings."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _latest_read(log: list[dict]) -> Optional[datetime]:
    """When the most recent *new* pull was first read — the pull_log entry
    whose pull ids differ from the entry before it (an entry can also be a
    re-read of the same pull that only carried flag changes)."""
    for i in range(len(log) - 1, -1, -1):
        ids = (log[i].get("pulled_at"), log[i].get("props_pull_id"))
        before = (log[i - 1].get("pulled_at"), log[i - 1].get("props_pull_id")) if i else None
        if ids != before:
            return _ts(log[i].get("seen_at"))
    return None


def _baseline_ctx(week_data: Optional[dict], baseline: str, since: Optional[str]) -> dict:
    """Resolve a baseline to one cutoff: "what had been read by time X".

    Every baseline but ``open`` is a cutoff on when this repo first *read* a
    pull (``pull_log[].seen_at``), not when the odds repo took it — the rule
    the report's Line Movement section uses. So a 9:41 AM pull first read at
    11:41 AM, after the morning report, is still news in "since yesterday's
    report". ``last`` is the cutoff just before the most recent read, which
    covers every pull that read brought in: an odds run often lands a full
    prop pull and an anytime-TD merge pull minutes apart, and the latest
    ``props_pull_id`` alone would name only the second.
    """
    week_data = week_data or {}
    log = week_data.get("pull_log") or []
    reads = sorted(t for t in (_ts(e.get("seen_at")) for e in log) if t)
    cutoff: Optional[datetime] = None
    kind = baseline if baseline in BASELINES else "open"
    if kind == "window":
        cutoff = _ts(since)
    elif kind == "last":
        latest = _latest_read(log)
        cutoff = (latest - timedelta(microseconds=1)) if latest else None
    if cutoff is None:
        kind = "open"      # no earlier report / no pull log: the whole week is new
    return {"kind": kind, "cutoff": cutoff, "reads": reads}


def _known_by(at: Any, bctx: dict) -> bool:
    """Had the pull stamped ``at`` been read by the cutoff?

    A pull is first read by the first run at or after it, so its read time is
    the earliest ``seen_at`` not before its own timestamp; with no such run
    on record (a file from before the pull log) its own time stands in.
    """
    when = _ts(at)
    if when is None:
        return False
    read = next((r for r in bctx["reads"] if r >= when), when)
    return read <= bctx["cutoff"]


def _game_base(g: dict, bctx: dict) -> dict:
    """The posted line at the baseline (``spread_home`` / ``total`` / ``at``)."""
    opened, cur = g.get("opened") or {}, g.get("current") or {}
    if bctx["kind"] == "open":
        return opened
    hist = g.get("history") or []
    known = [h for h in hist if _known_by(h.get("at"), bctx)]
    if known:
        return known[-1]
    if not hist and _known_by(cur.get("at"), bctx):
        return cur
    return opened


def _prop_base(p: dict, bctx: dict) -> Optional[float]:
    """The market mean at the baseline. Week files written before props kept a
    ``history`` can only tell whether the current value predates the cutoff,
    and otherwise fall back to the line at open."""
    opened = (p.get("opened") or {}).get("mkt_mu")
    if bctx["kind"] == "open":
        return opened
    cur = p.get("current") or {}
    hist = p.get("history")
    if hist:
        known = [h for h in hist if _known_by(h[0], bctx)]
        return known[-1][1] if known else opened
    if _known_by(cur.get("at"), bctx):
        return cur.get("mkt_mu")
    prev = p.get("previous") or {}
    if prev and _known_by(prev.get("at"), bctx):
        return prev.get("mkt_mu")
    return opened


def _team_implied(spread_team: Optional[float], total: Optional[float]) -> Optional[float]:
    if spread_team is None or total is None:
        return None
    return total / 2.0 - spread_team / 2.0


def team_movement(week_data: Optional[dict], settings: Optional[dict] = None, *,
                  baseline: str = "open", since: Optional[str] = None,
                  played: Optional[set[str]] = None) -> list[dict]:
    """Every team's market-implied total at the baseline and now, biggest move first.

    A team's implied total is ``total/2 - spread/2`` from its own side, so its
    move splits exactly into ``from_total`` (half the total's move) and
    ``from_spread`` (minus half its spread's move). That split is the "why":
    a total dropping two points costs both sides one; a spread moving two
    points toward a side gives it one and takes one from the opponent.
    """
    bctx = _baseline_ctx(week_data, baseline, since)
    played = played or set()
    rows: list[dict] = []
    for key, g in ((week_data or {}).get("games") or {}).items():
        base, cur = _game_base(g, bctx), g.get("current") or {}
        for side in ("home", "away"):
            team, opp = g.get(side), g.get("away" if side == "home" else "home")
            if not team or team in played:
                continue
            home = side == "home"
            sp_base = _from_side(base.get("spread_home"), home)
            sp_now = _from_side(cur.get("spread_home"), home)
            tot_base, tot_now = base.get("total"), cur.get("total")
            imp_base, imp_now = _team_implied(sp_base, tot_base), _team_implied(sp_now, tot_now)
            move = (imp_now - imp_base) if None not in (imp_base, imp_now) else None
            from_total = ((tot_now - tot_base) / 2.0) if None not in (tot_now, tot_base) else None
            from_spread = (-(sp_now - sp_base) / 2.0) if None not in (sp_now, sp_base) else None
            rows.append({
                "team": team, "opp": opp, "home": home, "game": key,
                "kickoff_et": g.get("kickoff_et", ""),
                "base": _r(imp_base, 2), "now": _r(imp_now, 2), "move": _r(move, 2),
                "from_total": _r(from_total, 2), "from_spread": _r(from_spread, 2),
                "spread_base": sp_base, "spread_now": sp_now,
                "total_base": tot_base, "total_now": tot_now,
                "base_at": base.get("at"),
            })
    rows.sort(key=lambda r: (-abs(r["move"] or 0), r["team"]))
    return rows


def why_text(row: dict) -> str:
    """'total -1.0, spread +0.5' — the parts of a team-total move, zeros left out."""
    parts = []
    for label, key in (("total", "from_total"), ("spread", "from_spread")):
        v = row.get(key)
        if v is not None and abs(v) >= 0.01:
            parts.append(f"{label} {v:+.2f}".rstrip("0").rstrip("."))
    return ", ".join(parts)


def sheet_vs_market(week_data: Optional[dict], settings: Optional[dict] = None,
                    played: Optional[set[str]] = None) -> list[dict]:
    """Teams whose projection-sheet implied total sits ``sheet_gap``+ off the
    market's — a state, not a movement, so it takes no baseline."""
    limit = _insights_cfg(settings)["sheet_gap"]
    rows = [r for r in team_environment(week_data, settings, played=played)
            if r["sheet_gap"] is not None and abs(r["sheet_gap"]) >= limit]
    rows.sort(key=lambda r: (-abs(r["sheet_gap"]), r["team"]))
    return rows


def player_stat_moves(week_data: Optional[dict], settings: Optional[dict] = None, *,
                      baseline: str = "open", since: Optional[str] = None,
                      played: Optional[set[str]] = None, include_thin: bool = False) -> list[dict]:
    """One row per player x stat whose market moved since the baseline.

    ``size`` is the move in units of the stat's movement threshold
    (``odds.thresholds.props``) — the one scale on which half a catch and
    eight passing yards compare. ``vs_you`` says whether the move took the
    market further from your projection (``away``) or closer (``toward``).
    """
    thr_all = _thresholds(settings).get("props") or {}
    bctx = _baseline_ctx(week_data, baseline, since)
    played = played or set()
    rows: list[dict] = []
    for p in ((week_data or {}).get("props") or {}).values():
        if p.get("team") in played or (p.get("thin") and not include_thin):
            continue
        now = (p.get("current") or {}).get("mkt_mu")
        base = _prop_base(p, bctx)
        if now is None or base is None or now == base:
            continue
        stat = p.get("stat", "")
        thr = float(thr_all.get(stat) or 0)
        move = now - base
        ours = p.get("ours")
        vs_you = ""
        if ours is not None:
            vs_you = "away" if abs(now - ours) > abs(base - ours) else "toward"
        rows.append({
            "player": p.get("player", ""), "pos": p.get("pos", ""), "team": p.get("team", ""),
            "opp": p.get("opp", ""), "gsis_id": p.get("gsis_id", ""), "stat": stat,
            "stat_label": stat_display_label(stat), "ours": ours, "base": base, "now": now,
            "move": move, "move_pct": (move / base * 100.0) if base else None,
            "size": (abs(move) / thr) if thr else 0.0, "vs_you": vs_you,
            "flag": p.get("flag") or "", "book_line": (p.get("current") or {}).get("cons_line"),
        })
    rows.sort(key=lambda r: -r["size"])
    return rows


def _fmt_stat(stat: str, v: Optional[float], signed: bool = False) -> str:
    if v is None:
        return ""
    nd = 2 if stat in FINE_STATS else 1
    return f"{v:+.{nd}f}" if signed else f"{v:.{nd}f}"


def player_movement(week_data: Optional[dict], settings: Optional[dict] = None, *,
                    baseline: str = "open", since: Optional[str] = None,
                    played: Optional[set[str]] = None, min_size: float = 1.0,
                    include_thin: bool = False) -> list[dict]:
    """Moved lines grouped into one row per player, biggest move first.

    Only movement puts a player here — a RED / AMBER flag on a line that has
    not moved belongs to the market-vs-projection view, not to "what moved".
    Correlated stats (receptions and receiving yards) read as one story on
    one row instead of scattering the player down the table.
    """
    by_player: dict[str, list[dict]] = {}
    for m in player_stat_moves(week_data, settings, baseline=baseline, since=since,
                               played=played, include_thin=include_thin):
        if m["size"] < min_size or (m["stat"] in RATE_STATS and abs(m["move"]) < TD_MIN_MOVE):
            continue
        by_player.setdefault(m["gsis_id"] or f"{m['player']}|{m['team']}", []).append(m)

    out: list[dict] = []
    for stats in by_player.values():
        stats.sort(key=lambda m: (-m["size"], STAT_ORDER.get(m["stat"], 99)))
        first = stats[0]
        ups = {m["move"] > 0 for m in stats}
        trends = {m["vs_you"] for m in stats if m["vs_you"]}
        flags = [m["flag"] for m in stats if m["flag"] in FLAG_RANK]
        out.append({
            "player": first["player"], "pos": first["pos"], "team": first["team"],
            "opp": first["opp"], "gsis_id": first["gsis_id"],
            "size": first["size"],
            "direction": "up" if ups == {True} else "down" if ups == {False} else "mixed",
            "vs_you": trends.pop() if len(trends) == 1 else ("mixed" if trends else ""),
            "flag": min(flags, key=FLAG_RANK.get) if flags else "",
            "summary": " · ".join(
                f"{m['stat_label']} {_fmt_stat(m['stat'], m['base'])}→{_fmt_stat(m['stat'], m['now'])} "
                f"({_fmt_stat(m['stat'], m['move'], signed=True)})" for m in stats),
            "stats": stats,
        })
    out.sort(key=lambda r: (-r["size"], r["player"]))
    return out
