"""Market lines and player-prop movement (in-season only).

Read-only view of the **NFL Odds** project (`Projects/NFL Odds`, repo
`cwecht15/nfl-odds`). That project pulls The Odds API, prices every market
against these same weekly projection sheets, and publishes the result to
Google Sheets. Nothing here calls a betting API — no key, no credits. The
news agent's own service account already has read access to both books.

Three reads, all gspread:

* **Game lines** — the odds sheet's ``SB_GameLines`` tab: consensus
  spread / total / moneylines across ~39 books, Pinnacle as the sharp
  reference, and (already computed there) the projection sheet's own line
  plus the ``FP Flag`` that fires when the two disagree. Current pull only,
  overwritten each time — so the per-game series is built and kept here.
* **Player props + anytime TD** — the "NFL Market History" workbook's
  ``<season>_W<ww>`` tab: one row per pull x player x stat, GSIS-keyed,
  carrying our projection (``ours``), the market-implied mean (``mkt_mu``),
  the consensus line and the Market_Check verdict (``flag``). This is a real
  per-pull history and needs no snapshotting.
* **Freshness** — ``Pull_Status`` row 3 (row 2 is Kalshi/Polymarket, row 4
  the projections watcher): when the last successful pull ran and which week
  it priced.

Output: ``data/odds/<season>/wk<NN>.json`` — one accumulating file per week:

    {"season": 2026, "week": 1, "updated_at": ..., "pull": {...},
     "games": {"SF@LAR": {"opened", "current", "sharp", "sheet", "implied",
                          "history": [...]}},
     "props": {"<gsis>|<stat>": {"opened", "previous", "current", "ours",
                                 "flag", ...}},
     "changes": [ typed change records ]}

Two things the sources make necessary:

* **Partial pulls.** The CI ``anytime_td`` mode logs only ``player_anytime_td``
  rows under a new pull id, so "the previous pull" has to be resolved per
  ``(gsis_id, stat)`` — globally it would look like every other prop vanished.
* **Re-prices reuse the odds' timestamp** and game lines are not rewritten by
  an anytime-TD pull, so the per-game history appends on a *content* change,
  never on a clock reading.

CLI:
    python -m collectors.odds_collector [--date YYYY-MM-DD] [--week N] [--season YYYY] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_data_dir, get_settings
from processing import season as season_mod
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

ET_ZONE = season_mod.ET_ZONE

# Internal stat key -> label used in report bullets and on the dashboard.
# Mirrors ML_STAT_NAME in the odds repo (Sportsbook/sportsbook_odds/present.py).
STAT_LABEL = {
    "pass_att": "Pass Att",
    "completions": "Completions",
    "pass_yds": "Pass Yds",
    "pass_tds": "Pass TD",
    "ints": "INT",
    "rush_att": "Rush Att",
    "rush_yds": "Rush Yds",
    "rush_tds": "Rush TD",
    "receptions": "Rec",
    "rec_yds": "Rec Yds",
    "rec_tds": "Rec TD",
    "rush_rec_yds": "Rush+Rec Yds",
    "anytime_td": "Anytime TD rate",
}

# ``mkt_mu`` for anytime_td is the implied expected-TD rate (lambda), NOT a
# probability — the odds repo labels the column "Anytime TD (rate)". A rate of
# 1.27 means 1.27 expected TDs (P(score) = 72%), so it must never be rendered
# as a percentage.
RATE_STATS = {"anytime_td"}

DEFAULT_THRESHOLDS = {
    "spread": 0.5,
    "total": 1.0,
    "moneyline": 15,
    "props": {
        "pass_yds": 8.0, "pass_att": 1.5, "completions": 1.0, "pass_tds": 0.15,
        "ints": 0.1, "rush_att": 1.5, "rush_yds": 6.0, "rush_tds": 0.06,
        "receptions": 0.5, "rec_yds": 6.0, "rec_tds": 0.05,
        "rush_rec_yds": 8.0, "anytime_td": 0.10,
    },
    # Floor for a "quoted but not projected" bullet, per stat. A yardage or
    # reception line has no floor — having one at all means expected volume.
    "market_only_min": {"anytime_td": 0.20, "rush_tds": 0.20, "rec_tds": 0.20, "pass_tds": 0.50},
}

CHANGE_TYPES = (
    "spread_move", "total_move", "ml_move",
    "prop_move", "prop_new", "sheet_drift", "market_only",
)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _base_dir() -> Path:
    return get_data_dir("odds")


def week_file_path(season: int, week: int) -> Path:
    d = _base_dir() / str(season)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"wk{int(week):02d}.json"


def load_week_file(season: int, week: int) -> Optional[dict]:
    p = week_file_path(season, week)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_week_file(data: dict) -> Path:
    p = week_file_path(int(data["season"]), int(data["week"]))
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def latest_week_file(season: int, before_week: Optional[int] = None) -> Optional[dict]:
    """Most recent week file for the season (used when this week has no pull yet)."""
    d = _base_dir() / str(season)
    if not d.exists():
        return None
    weeks = []
    for p in d.glob("wk*.json"):
        try:
            w = int(p.stem[2:])
        except ValueError:
            continue
        if before_week is None or w <= before_week:
            weeks.append(w)
    if not weeks:
        return None
    return load_week_file(season, max(weeks))


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------


def _cfg(settings: Optional[dict] = None) -> dict:
    return (settings or get_settings()).get("odds", {}) or {}


def _f(v: Any) -> Optional[float]:
    """Sheet cell -> float. Handles '', '-', em dashes, '+101', '64%', '1,234'."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("−", "-")
    if not s or s in {"-", "–", "—", "N/A", "n/a"}:
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1]
    if s.startswith("+"):
        s = s[1:]
    try:
        f = float(s)
    except ValueError:
        return None
    return f / 100.0 if pct else f


def _row_map(header: list[str]) -> dict[str, int]:
    """{header text: column index}. Access by NAME — the odds repo adds columns."""
    return {str(h).strip(): i for i, h in enumerate(header) if str(h).strip()}


def _cell(row: list, idx: dict[str, int], name: str) -> str:
    i = idx.get(name)
    if i is None or i >= len(row):
        return ""
    return str(row[i]).strip()


def _num(row: list, idx: dict[str, int], name: str) -> Optional[float]:
    return _f(_cell(row, idx, name))


def parse_status_time(text: str, season: int, now: Optional[datetime] = None) -> Optional[str]:
    """'Thu Sep 10, 12:50 PM ET' -> ISO ET timestamp.

    The sheet omits the year, so it is taken from ``season`` and rolled back
    when that would put the pull far in the future (a January playoff pull
    printed against a September-dated season).
    """
    if not text:
        return None
    s = re.sub(r"\s*ET\s*$", "", str(text).strip())
    m = re.search(r"([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])", s)
    if not m:
        return None
    mon, day, hh, mm, ampm = m.groups()
    try:
        month = datetime.strptime(mon[:3], "%b").month
    except ValueError:
        return None
    hour = int(hh) % 12 + (12 if ampm.lower() == "pm" else 0)
    now = now or datetime.now(ET_ZONE)
    for year in (season, season + 1, season - 1):
        try:
            dt = datetime(year, month, int(day), hour, int(mm), tzinfo=ET_ZONE)
        except ValueError:
            continue
        if -370 < (now - dt).days < 40:
            return dt.isoformat(timespec="minutes")
    return None


def _age_hours(iso_ts: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET_ZONE)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def _fmt_line(v: Optional[float]) -> str:
    """-3.75 -> '-3.75', 48.0 -> '48'."""
    if v is None:
        return "?"
    return f"{v:g}"


def _fmt_spread(v: Optional[float]) -> str:
    """Always signed: '+3.5' is the home underdog, '-3.5' the home favourite.
    Unsigned '3.5' reads as "favoured by 3.5", which is the opposite."""
    if v is None:
        return "?"
    return f"{v:+g}"


def _fmt_ml(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{int(round(v)):+d}"


def _fmt_stat(stat: str, v: Optional[float]) -> str:
    if v is None:
        return "?"
    if stat in RATE_STATS:
        return f"{v:.2f}"
    return f"{v:.1f}" if abs(v) >= 1 else f"{v:.2f}"


# ---------------------------------------------------------------------------
# Sheet reads
# ---------------------------------------------------------------------------


def _client():
    from processing.sheet_reconciliation import _get_gspread_client
    return _get_gspread_client()


def _with_retry(fn, *, attempts: int = 3, base_delay: float = 5.0):
    """Run a Sheets read, backing off on 429.

    Three reads per run is nothing, but the local task, the cloud pipeline and
    a Streamlit visitor all share one project quota ("Read requests per minute
    per user"), so a burst can collide. A failure after the last attempt is
    re-raised and handled by ``collect_odds``'s non-fatal wrapper.
    """
    import time
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - gspread APIError has no stable type here
            if "429" not in str(e) or i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            logger.warning("Sheets read rate-limited, retrying in %.0fs", delay)
            time.sleep(delay)


def read_pull_status(gc, season: int, settings: Optional[dict] = None,
                     now: Optional[datetime] = None) -> dict:
    """{'pulled_at', 'week_reported', 'detail', 'status'} from Pull_Status row 3."""
    cfg = _cfg(settings)
    out: dict[str, Any] = {"pulled_at": None, "week_reported": None, "detail": "", "status": ""}
    sh = _with_retry(lambda: gc.open_by_key(cfg.get("odds_sheet_id")))
    ws = sh.worksheet(cfg.get("status_tab", "Pull_Status"))
    values = _with_retry(ws.get_all_values)
    row_no = int(cfg.get("status_row", 3))
    if len(values) < row_no:
        return out
    row = values[row_no - 1]
    out["status"] = row[1].strip() if len(row) > 1 else ""
    last_pull = row[2].strip() if len(row) > 2 else ""
    out["detail"] = row[3].strip() if len(row) > 3 else ""
    # A re-price keeps the original odds' timestamp: "odds from Thu Sep 10, 9:52 AM ET".
    m = re.search(r"odds from ([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2},\s*[\d:]+\s*[AaPp][Mm]\s*ET)",
                  out["detail"])
    out["pulled_at"] = parse_status_time(m.group(1) if m else last_pull, season, now)
    wm = re.search(r"Wk\s*(\d{1,2})", out["detail"])
    if wm:
        out["week_reported"] = int(wm.group(1))
    return out


def read_game_lines(gc, settings: Optional[dict] = None) -> list[dict]:
    """SB_GameLines -> one dict per game, news-style abbrevs."""
    cfg = _cfg(settings)
    sh = _with_retry(lambda: gc.open_by_key(cfg.get("odds_sheet_id")))
    ws = sh.worksheet(cfg.get("game_lines_tab", "SB_GameLines"))
    values = _with_retry(ws.get_all_values)
    if len(values) < 2:
        return []
    idx = _row_map(values[0])
    games: list[dict] = []
    for row in values[1:]:
        away = to_news(_cell(row, idx, "Away"), "proj")
        home = to_news(_cell(row, idx, "Home"), "proj")
        if not away or not home:
            continue
        spread = _num(row, idx, "Cons Spread (Home)")
        total = _num(row, idx, "Cons Total")
        games.append({
            "key": f"{away}@{home}",
            "away": away,
            "home": home,
            "kickoff_et": _cell(row, idx, "Kickoff (ET)"),
            "spread_home": spread,
            "total": total,
            "home_ml": _num(row, idx, "Cons Home ML"),
            "away_ml": _num(row, idx, "Cons Away ML"),
            "n_books": _num(row, idx, "Books"),
            "best": {
                "home_spread": _cell(row, idx, "Best Home Spread"),
                "away_spread": _cell(row, idx, "Best Away Spread"),
                "over": _cell(row, idx, "Best Over"),
                "under": _cell(row, idx, "Best Under"),
                "home_ml": _cell(row, idx, "Best Home ML"),
                "away_ml": _cell(row, idx, "Best Away ML"),
            },
            "sharp": {
                "book": _cell(row, idx, "Sharp Book"),
                "spread_home": _num(row, idx, "Sharp Spread"),
                "total": _num(row, idx, "Sharp Total"),
                "home_ml": _num(row, idx, "Sharp Home ML"),
                "home_p": _num(row, idx, "Sharp Home %"),
            },
            "sheet": {
                "spread_home": _num(row, idx, "Sheet Spread"),
                "ou": _num(row, idx, "Sheet O/U"),
                "fp_spread_delta": _num(row, idx, "FP Spread Δ"),
                "fp_total_delta": _num(row, idx, "FP Total Δ"),
                "fp_flag": _cell(row, idx, "FP Flag"),
            },
            "implied": _market_implied(spread, total),
        })
    return games


def _market_implied(spread_home: Optional[float], total: Optional[float]) -> dict:
    """Market-implied team totals from the consensus spread and total."""
    if spread_home is None or total is None:
        return {"home": None, "away": None}
    home = total / 2.0 - spread_home / 2.0
    return {"home": round(home, 1), "away": round(total - home, 1)}


def read_prop_history(gc, season: int, week: int, settings: Optional[dict] = None) -> list[dict]:
    """The history workbook's ``<season>_W<ww>`` tab -> raw per-pull rows.

    Returns [] when the tab does not exist yet (start of a week, before the
    first pull) or has been trimmed away — never raises for that case.
    """
    cfg = _cfg(settings)
    sheet_id = cfg.get("history_sheet_id")
    if not sheet_id:
        return []
    tab = f"{cfg.get('history_tab_prefix', '')}{season}_W{int(week):02d}"
    sh = _with_retry(lambda: gc.open_by_key(sheet_id))
    try:
        ws = sh.worksheet(tab)
    except Exception:  # gspread.WorksheetNotFound and friends
        logger.info("Market history tab %s not present yet", tab)
        return []
    values = _with_retry(ws.get_all_values)
    if len(values) < 2:
        return []
    idx = _row_map(values[0])
    rows: list[dict] = []
    for row in values[1:]:
        gsis = _cell(row, idx, "gsis_id")
        stat = _cell(row, idx, "stat")
        pulled = _cell(row, idx, "pulled_at") or _cell(row, idx, "pull_id")
        if not gsis or not stat or not pulled:
            continue
        team = _cell(row, idx, "team_sheet")
        team = to_news(team, "proj") if team else to_news(_cell(row, idx, "team"), "nflverse")
        rows.append({
            "pulled_at": pulled,
            "gsis_id": gsis,
            "player": _cell(row, idx, "player"),
            "team": team,
            "opp": to_news(_cell(row, idx, "opp"), "proj"),
            "pos": _cell(row, idx, "pos"),
            "stat": stat,
            "ours": _num(row, idx, "ours"),
            "mkt_mu": _num(row, idx, "mkt_mu"),
            "cons_line": _num(row, idx, "cons_line"),
            "n_books": int(_num(row, idx, "n_books") or 0),
            "delta": _num(row, idx, "delta"),
            "pct": _num(row, idx, "pct"),
            "flag": _cell(row, idx, "flag"),
            "game": _cell(row, idx, "game"),
            "event_id": _cell(row, idx, "event_id"),
            "kickoff_utc": _cell(row, idx, "kickoff_utc"),
        })
    rows.sort(key=lambda r: r["pulled_at"])
    return rows


def collapse_prop_history(rows: list[dict], min_books: int = 2) -> dict[str, dict]:
    """Per-pull rows -> {"<gsis>|<stat>": {opened, previous, current, ...}}.

    ``previous`` is the pull before the latest **for that key**, because a
    ``--merge`` pull logs only the markets it refreshed.
    """
    by_key: dict[str, list[dict]] = {}
    for r in rows:
        if r["mkt_mu"] is None:
            continue
        by_key.setdefault(f"{r['gsis_id']}|{r['stat']}", []).append(r)

    out: dict[str, dict] = {}
    for key, entries in by_key.items():
        entries.sort(key=lambda r: r["pulled_at"])
        first, last = entries[0], entries[-1]
        prev = entries[-2] if len(entries) > 1 else None

        def _snap(r: Optional[dict]) -> Optional[dict]:
            if r is None:
                return None
            return {"mkt_mu": r["mkt_mu"], "cons_line": r["cons_line"],
                    "n_books": r["n_books"], "at": r["pulled_at"]}

        out[key] = {
            "gsis_id": last["gsis_id"], "player": last["player"], "team": last["team"],
            "opp": last["opp"], "pos": last["pos"], "stat": last["stat"],
            "game": last["game"], "event_id": last["event_id"],
            "ours": last["ours"], "flag": last["flag"],
            "delta": last["delta"], "pct": last["pct"],
            "thin": last["flag"] == "THIN" or last["n_books"] < min_books,
            "pulls": len(entries),
            "opened": _snap(first), "previous": _snap(prev), "current": _snap(last),
        }
    return out


# ---------------------------------------------------------------------------
# Merge + diff
# ---------------------------------------------------------------------------


def _change(ctype: str, *, team: str = "", player: str = "", gsis_id: str = "",
            pos: str = "", stat: str = "", game: str = "", basis: str = "",
            old: Any = None, new: Any = None, magnitude: float = 0.0,
            message: str = "", ours: Any = None) -> dict:
    return {
        "type": ctype, "team": team, "player": player, "gsis_id": gsis_id,
        "pos": pos, "stat": stat, "stat_label": STAT_LABEL.get(stat, stat) if stat else "",
        "game": game, "basis": basis, "ours": ours,
        "old": old, "new": new, "magnitude": round(magnitude, 3), "message": message,
    }


def _same_line(a: Optional[dict], b: dict) -> bool:
    if not a:
        return False
    return all(a.get(k) == b.get(k) for k in ("spread_home", "total", "home_ml", "away_ml"))


def _diff_game(prev_game: Optional[dict], g: dict, thresholds: dict) -> list[dict]:
    """Changes between the stored state and the freshly-read game line."""
    changes: list[dict] = []
    first_run = prev_game is None
    base = (prev_game or {}).get("current") if not first_run else None
    opened = (prev_game or {}).get("opened") or {}
    # On the first read of a week there is no stored state; the reference is
    # the opening line captured in the same read, so nothing has "moved" yet.
    if base is None:
        return changes
    label = f"{g['away']}@{g['home']}"

    sp_thr = float(thresholds.get("spread", 0.5))
    old_sp, new_sp = base.get("spread_home"), g.get("spread_home")
    if old_sp is not None and new_sp is not None and abs(new_sp - old_sp) >= sp_thr:
        fav = g["home"] if new_sp < old_sp else g["away"]
        open_sp = opened.get("spread_home")
        extra = (f", opened {_fmt_spread(open_sp)}"
                 if open_sp is not None and open_sp not in (old_sp, new_sp) else "")
        changes.append(_change(
            "spread_move", team=g["home"], game=label, basis="since last report",
            old=old_sp, new=new_sp, magnitude=abs(new_sp - old_sp) / sp_thr,
            message=f"**{label}** {g['home']} {_fmt_spread(new_sp)} "
                    f"(was {_fmt_spread(old_sp)}{extra}) — "
                    f"{abs(new_sp - old_sp):g} toward {fav}",
        ))

    tot_thr = float(thresholds.get("total", 1.0))
    old_t, new_t = base.get("total"), g.get("total")
    if old_t is not None and new_t is not None and abs(new_t - old_t) >= tot_thr:
        direction = "up" if new_t > old_t else "down"
        open_t = opened.get("total")
        extra = (f", opened {_fmt_line(open_t)}"
                 if open_t is not None and open_t not in (old_t, new_t) else "")
        changes.append(_change(
            "total_move", team=g["home"], game=label, basis="since last report",
            old=old_t, new=new_t, magnitude=abs(new_t - old_t) / tot_thr,
            message=f"**{label}** total {_fmt_line(new_t)}, {direction} "
                    f"{abs(new_t - old_t):g} from {_fmt_line(old_t)}{extra}",
        ))

    ml_thr = float(thresholds.get("moneyline", 15))
    for side, key in (("home", "home_ml"), ("away", "away_ml")):
        old_m, new_m = base.get(key), g.get(key)
        if old_m is None or new_m is None or abs(new_m - old_m) < ml_thr:
            continue
        changes.append(_change(
            "ml_move", team=g[side], game=label, basis="since last report",
            old=old_m, new=new_m, magnitude=abs(new_m - old_m) / ml_thr,
            message=f"**{g[side]}** moneyline {_fmt_ml(new_m)} (was {_fmt_ml(old_m)})",
        ))

    return changes


def _diff_prop(prev_prop: Optional[dict], p: dict, thresholds: dict) -> list[dict]:
    """Changes for one player x stat. First sight of a week diffs open -> now."""
    stat = p["stat"]
    thr = float((thresholds.get("props") or {}).get(stat, 0) or 0)
    cur = p.get("current") or {}
    new_mu = cur.get("mkt_mu")
    if new_mu is None or p.get("thin"):
        return []

    label = STAT_LABEL.get(stat, stat)
    name = p.get("player") or "?"

    if prev_prop is None:
        opened = p.get("opened") or {}
        old_mu = opened.get("mkt_mu")
        if old_mu is None or p.get("pulls", 1) < 2:
            # anytime_td is quoted as a price, not an O/U line, so cons_line
            # is blank for it — show the implied rate instead of nothing.
            line = cur.get("cons_line")
            line_txt = (f" at {_fmt_line(line)}" if line is not None
                        else f" implying {_fmt_stat(stat, new_mu)}")
            return [_change(
                "prop_new", team=p.get("team", ""), player=name, gsis_id=p.get("gsis_id", ""),
                pos=p.get("pos", ""), stat=stat, game=p.get("game", ""), basis="new this week",
                old=None, new=new_mu, magnitude=0.5, ours=p.get("ours"),
                message=f"**{name}** ({p.get('pos', '')}, {p.get('team', '')}) "
                        f"{label} line opened{line_txt}",
            )]
        basis = "since the line opened"
    else:
        old_mu = ((prev_prop.get("current") or {}).get("mkt_mu"))
        basis = "since last report"
        if old_mu is None:
            return []

    move = new_mu - old_mu
    if thr <= 0 or abs(move) < thr:
        return []
    direction = "up" if move > 0 else "down"
    ours = p.get("ours")
    ours_txt = ""
    if ours is not None:
        ours_txt = f"; we project {_fmt_stat(stat, ours)}"
    return [_change(
        "prop_move", team=p.get("team", ""), player=name, gsis_id=p.get("gsis_id", ""),
        pos=p.get("pos", ""), stat=stat, game=p.get("game", ""), basis=basis,
        old=old_mu, new=new_mu, magnitude=abs(move) / thr, ours=ours,
        message=f"**{name}** ({p.get('pos', '')}, {p.get('team', '')}) {label} "
                f"{_fmt_stat(stat, new_mu)}, {direction} from {_fmt_stat(stat, old_mu)} "
                f"{basis}{ours_txt}",
    )]


def _flag_state_prefix(key: str) -> str:
    return key.split("|", 1)[0]


def _flag_changes(games: list[dict], props: dict[str, dict],
                  prev_flags: Optional[dict] = None,
                  min_market_only: Optional[dict] = None) -> tuple[list[dict], dict]:
    """Verdicts the odds repo already computed: sheet drift and market-only stats.

    These are *state*, not movement — a sheet line that drifted stays drifted
    until someone edits the sheet. Re-reporting them every run would fill a
    section about what changed with things that did not. So each is emitted
    only when it first appears or its value changes, and the current state is
    returned to be stored for the next comparison. The Projection Audit is
    where the standing version lives, with dismissals.

    ``MKT-ONLY`` fires when ``ours is None or ours <= 0``, so it means "we
    project nothing for this stat" — which covers both a player with no row on
    the weekly sheet and one who is on it projected for zero. Only the audit
    (which can see the sheet rows) separates those two.

    The returned state covers only the sources that actually returned rows;
    ``merge_into_week`` carries the rest forward. A tab that is missing or
    mid-write returns ``[]`` rather than an error, and wholesale-replacing the
    state with it would replay every flag on the next successful run.
    """
    prev_flags = prev_flags or {}
    min_market_only = min_market_only or {}
    out: list[dict] = []
    state: dict[str, Any] = {}

    for g in games:
        sheet = g.get("sheet") or {}
        flag = str(sheet.get("fp_flag") or "")
        label = f"{g['away']}@{g['home']}"
        key = f"sheet_drift|{label}"
        if not flag:
            continue
        state[key] = flag
        if prev_flags.get(key) == flag:
            continue
        out.append(_change(
            "sheet_drift", team=g["home"], game=label, basis="vs the sheet",
            old=sheet.get("spread_home"), new=g.get("spread_home"),
            magnitude=abs(sheet.get("fp_spread_delta") or 0) + abs(sheet.get("fp_total_delta") or 0),
            message=f"**{label}**: your sheet has {_fmt_spread(sheet.get('spread_home'))} / "
                    f"{_fmt_line(sheet.get('ou'))}, market {_fmt_spread(g.get('spread_home'))} / "
                    f"{_fmt_line(g.get('total'))} ({flag})",
        ))

    for p in props.values():
        if p.get("flag") != "MKT-ONLY" or p.get("thin"):
            continue
        stat = p["stat"]
        mkt = (p.get("current") or {}).get("mkt_mu")
        # Nearly every active skill player carries an anytime-TD price; a bare
        # low TD quote is not evidence of anything.
        if mkt is None or float(mkt) < float(min_market_only.get(stat, 0) or 0):
            continue
        key = f"market_only|{p.get('gsis_id')}|{stat}"
        state[key] = True
        if prev_flags.get(key):
            continue
        out.append(_change(
            "market_only", team=p.get("team", ""), player=p.get("player", ""),
            gsis_id=p.get("gsis_id", ""), pos=p.get("pos", ""), stat=stat,
            game=p.get("game", ""), basis="no projection",
            old=None, new=mkt, magnitude=1.0, ours=p.get("ours"),
            message=f"Market quotes **{p.get('player', '?')}** "
                    f"({p.get('pos', '')}, {p.get('team', '')}) "
                    f"{STAT_LABEL.get(stat, stat)} {_fmt_stat(stat, mkt)} "
                    f"— the sheet projects none",
        ))
    return out, state


def merge_into_week(data: Optional[dict], season: int, week: int, games: list[dict],
                    props: dict[str, dict], meta: dict, now_iso: str) -> tuple[dict, list[dict]]:
    """Fold a fresh read into the week file. Returns ``(data, changes)``."""
    data = json.loads(json.dumps(data)) if data else {}
    data.setdefault("season", season)
    data.setdefault("week", week)
    prev_flags: dict = dict(data.get("flags_seen") or {})
    prev_games: dict[str, dict] = dict(data.get("games") or {})
    prev_props: dict[str, dict] = dict(data.get("props") or {})
    thresholds = meta.get("thresholds") or DEFAULT_THRESHOLDS

    changes: list[dict] = []
    new_games: dict[str, dict] = {}
    for g in games:
        key = g["key"]
        prev = prev_games.get(key)
        changes.extend(_diff_game(prev, g, thresholds))
        current = {"spread_home": g["spread_home"], "total": g["total"],
                   "home_ml": g["home_ml"], "away_ml": g["away_ml"],
                   "n_books": g["n_books"], "at": meta.get("pulled_at") or now_iso}
        history = list((prev or {}).get("history") or [])
        # Content dedupe: a re-price reuses the odds' timestamp and an
        # anytime-TD pull does not rewrite this tab at all.
        if not history or not _same_line(history[-1], current):
            history.append(current)
        new_games[key] = {
            "away": g["away"], "home": g["home"], "kickoff_et": g["kickoff_et"],
            "opened": (prev or {}).get("opened") or dict(current),
            "current": current,
            "best": g["best"], "sharp": g["sharp"], "sheet": g["sheet"],
            "implied": g["implied"],
            "history": history[-40:],
        }
    # Games already stored but missing from this read (tab mid-write) survive.
    for key, g in prev_games.items():
        new_games.setdefault(key, g)

    new_props: dict[str, dict] = {}
    for key, p in props.items():
        changes.extend(_diff_prop(prev_props.get(key), p, thresholds))
        new_props[key] = p
    for key, p in prev_props.items():
        new_props.setdefault(key, p)

    flag_changes, flag_state = _flag_changes(
        games, props, prev_flags=prev_flags,
        min_market_only=(meta.get("thresholds") or {}).get("market_only_min") or {},
    )
    changes.extend(flag_changes)

    order = {t: i for i, t in enumerate(CHANGE_TYPES)}
    changes.sort(key=lambda c: (order.get(c["type"], 99), -c.get("magnitude", 0)))

    data["games"] = new_games
    data["props"] = new_props
    # Only the sources that returned rows may retire their flags; an empty read
    # is "we did not see", not "it cleared".
    carried = {k: v for k, v in prev_flags.items()
               if (_flag_state_prefix(k) == "sheet_drift" and not games)
               or (_flag_state_prefix(k) == "market_only" and not props)}
    data["flags_seen"] = {**carried, **flag_state}
    data["changes"] = changes
    data["pull"] = {k: meta.get(k) for k in
                    ("pulled_at", "props_pull_id", "week_reported", "stale_reason", "age_hours")}
    data["updated_at"] = now_iso
    return data, changes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def collect_odds(date_str: Optional[str] = None, week: Optional[int] = None,
                 season: Optional[int] = None, settings: Optional[dict] = None,
                 gc=None, now: Optional[datetime] = None,
                 write: bool = True) -> dict:
    """Read the odds sheets and fold them into this week's file. Never raises."""
    settings = settings or get_settings()
    cfg = _cfg(settings)
    date_str = date_str or season_mod.today_et()
    result: dict[str, Any] = {
        "date": date_str, "season": season, "week": week, "file": None,
        "games": 0, "props": 0, "changes": [], "pull": {}, "errors": [],
    }
    try:
        season = season or season_mod.get_season_year(settings)
        result["season"] = season
        if week is None:
            schedule = season_mod.load_schedule(settings=settings, season=season)
            week = season_mod.week_from_date(schedule, date_str) if schedule else None
        result["week"] = week
        if not week:
            result["errors"].append("no NFL week for this date - skipped")
            return result

        gc = gc or _client()
        now_dt = now or datetime.now(timezone.utc)
        status = read_pull_status(gc, season, settings, now=now_dt.astimezone(ET_ZONE))
        games = read_game_lines(gc, settings)
        rows = read_prop_history(gc, season, week, settings)
        min_books = int(cfg.get("min_books", 2))
        props = collapse_prop_history(rows, min_books=min_books)

        stale_reason = ""
        reported = status.get("week_reported")
        if reported is not None and reported != week:
            stale_reason = (f"the last odds pull priced Week {reported}; "
                            f"the news agent is on Week {week}")
        age = _age_hours(status.get("pulled_at"), now_dt)
        max_age = float(cfg.get("max_pull_age_hours", 30))
        if not stale_reason and age is not None and age > max_age:
            stale_reason = f"last odds pull was {age:.0f}h ago"

        meta = {
            "pulled_at": status.get("pulled_at"),
            "props_pull_id": rows[-1]["pulled_at"] if rows else None,
            "week_reported": reported,
            "stale_reason": stale_reason,
            "age_hours": round(age, 1) if age is not None else None,
            "thresholds": cfg.get("thresholds") or DEFAULT_THRESHOLDS,
        }

        prev = load_week_file(season, week)
        now_iso = now_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        pull_meta = {k: meta.get(k) for k in
                     ("pulled_at", "props_pull_id", "week_reported", "stale_reason", "age_hours")}
        if stale_reason:
            # SB_GameLines holds only the newest pull, so a wrong-week read is
            # next week's slate. Merging it would write games that do not
            # belong to this week — new keys, opening lines, history rows — and
            # every flag computed off it would be about the wrong matchups.
            # Keep what is stored, record why nothing moved, write nothing new.
            result["pull"] = pull_meta
            result["errors"].append(stale_reason)
            if prev is None:
                return result
            prev["pull"] = pull_meta
            prev["updated_at"] = now_iso
            prev["changes"] = []
            result["games"] = len(prev.get("games") or {})
            result["props"] = len(prev.get("props") or {})
            result["data"] = prev
            if write:
                result["file"] = str(save_week_file(prev))
            return result

        data, changes = merge_into_week(prev, season, week, games, props, meta, now_iso)

        result["games"] = len(data.get("games") or {})
        result["props"] = len(data.get("props") or {})
        result["changes"] = changes
        result["pull"] = data.get("pull") or {}
        if write:
            result["file"] = str(save_week_file(data))
        result["data"] = data
    except Exception as e:  # noqa: BLE001 — the pipeline must keep going
        logger.exception("Odds collection failed")
        result["errors"].append(str(e))
    return result


def game_line_for_team(week_data: Optional[dict], team: str) -> Optional[dict]:
    """The stored game entry a news-style team abbreviation plays in."""
    for g in ((week_data or {}).get("games") or {}).values():
        if team in (g.get("home"), g.get("away")):
            return g
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Read market lines / prop movement published by the NFL Odds project.")
    ap.add_argument("--date", default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="read and print, don't write the week file")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    res = collect_odds(args.date, week=args.week, season=args.season, write=not args.dry_run)
    pull = res.get("pull") or {}
    print(f"Week {res['week']}: {res['games']} games, {res['props']} player-stats, "
          f"{len(res['changes'])} changes")
    print(f"  odds pulled: {pull.get('pulled_at') or 'unknown'}"
          f"{'  [' + pull['stale_reason'] + ']' if pull.get('stale_reason') else ''}")
    for c in res["changes"][:40]:
        print(f"  [{c['type']:12s}] {c['message']}")
    for e in res["errors"]:
        print("  error:", e)
    if res.get("file"):
        print("written:", res["file"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
