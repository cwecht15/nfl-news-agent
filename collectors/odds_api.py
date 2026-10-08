"""Direct Odds API pulls: cheap game lines, rate-limited player props.

Until 2026-10 the news agent only *read* the sheets the NFL Odds project
(``Projects/NFL Odds``, repo ``cwecht15/nfl-odds``) writes, so market freshness
was that project's schedule (Tue 9a / Thu 4p / Sat 9p / Sun / Mon ET). Books
post most props Tue-Wed, after the Tuesday pull, and a Wednesday-afternoon
report saw a 30h+ old pull marked stale — which silenced the audit's market
checks and the Team Notes game line. The key is the 20,000-credit/month plan,
so two kinds of direct pull are affordable:

* **Game lines** (:func:`pull_game_lines`) — one ``/odds`` call for the whole
  week, ``h2h,spreads,totals`` x ``us,us2,eu`` = **9 credits**. Folded into
  ``data/odds/<season>/wkNN.json`` through ``odds_collector.merge_into_week``
  in exactly the shape ``read_game_lines`` produces, so every consumer reads it
  unchanged. Runs automatically in the scheduled runs listed in
  ``odds.api.game_lines.auto_runs`` and from a dashboard button.
* **Player props** (:func:`pull_props`) — NOT priced here. The NFL Odds project
  owns prop pricing (name matching, calibrated bands, Best Bets, the history
  workbook), so this dispatches *its* ``pull-sportsbook.yml`` (~550 credits for
  a full refresh), waits for it, and lets the normal sheet read pick the new
  pull up. Rate-limited by :func:`props_budget` from a committed ledger.

Every call is recorded in ``data/odds/api_usage.json`` (the ledger), which is
what makes the props limits enforceable server-side: the dashboard only
dispatches ``refresh.yml``; this module, running on Actions, decides.

Imports only ``requests`` + stdlib + this repo's modules: it sits on the
refresh path, and ``tests/test_slim_requirements.py`` holds that path to
``requirements-refresh.txt``. No pandas — medians come from ``statistics``.

Every public function is non-fatal: it returns a dict with ``ok`` / ``reason``
and never raises, like ``odds_collector.collect_odds``.

CLI:
    python collectors/odds_api.py --quota
    python collectors/odds_api.py --lines [--dry-run]
    python collectors/odds_api.py --props [--mode refresh|anytime_td] [--dry-run] [--force]
    (all accept --date / --week / --season)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collectors import odds_collector as oc
from config_loader import get_settings, get_teams
from processing import season as season_mod
from processing.team_abbr import to_news, to_proj

logger = logging.getLogger(__name__)

ET_ZONE = season_mod.ET_ZONE

LEDGER_NAME = "api_usage.json"
LEDGER_KEEP = 300

# What a full props refresh is assumed to cost when checking the credit floor
# before dispatching it (the NFL Odds README puts a full refresh at ~550).
PROPS_EST_CREDITS = 600
# A failed props run can still have spent its credits; one that reports at
# least this many counts toward the limits like a successful one.
PROPS_SPENT_FLOOR = 100

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "base_url": "https://api.the-odds-api.com/v4",
    "sport": "americanfootball_nfl",
    "key_env": "ODDS_API_KEY",
    "min_remaining_credits": 1000,
    "game_lines": {
        "regions": ["us", "us2", "eu"],
        "markets": ["h2h", "spreads", "totals"],
        "sharp_book": "pinnacle",
        "fp_flag": {"spread": 1.0, "total": 1.5},
        "auto_runs": ["am", "pm", "injuries", "gameday"],
        "auto_min_gap_minutes": 60,
    },
    "props": {
        "repo": "cwecht15/nfl-odds",
        "workflow": "pull-sportsbook.yml",
        "ref": "main",
        "mode": "refresh",
        "token_env": "NFL_ODDS_GH_TOKEN",
        "wait_minutes": 15,
        "min_gap_hours": 6,
        "max_per_day": 2,
        "max_per_week": 4,
        "min_remaining_credits": 7000,
    },
}

PROPS_MODES = ("refresh", "anytime_td")

_GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Config + seams
# ---------------------------------------------------------------------------


def api_cfg(settings: Optional[dict] = None) -> dict:
    """``odds.api`` merged over :data:`DEFAULTS` (one level of nesting)."""
    raw = ((settings or get_settings()).get("odds", {}) or {}).get("api") or {}
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k not in ("game_lines", "props")}}
    out["game_lines"] = {**DEFAULTS["game_lines"], **(raw.get("game_lines") or {})}
    out["props"] = {**DEFAULTS["props"], **(raw.get("props") or {})}
    return out


def _requests():
    """Seam so tests can swap the HTTP client without patching sys.modules."""
    import requests
    return requests


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key(cfg: dict) -> str:
    return (os.environ.get(cfg.get("key_env") or "ODDS_API_KEY") or "").strip()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=ET_ZONE)


def _et_label(dt: Optional[datetime]) -> str:
    if not dt:
        return "?"
    return dt.astimezone(ET_ZONE).strftime("%a %I:%M %p ET").replace(" 0", " ")


# ---------------------------------------------------------------------------
# A1. Client + quota
# ---------------------------------------------------------------------------


class OddsApiError(RuntimeError):
    def __init__(self, status: int, path: str, body: str = ""):
        self.status = status
        super().__init__(f"Odds API HTTP {status} for {path}: {body[:200]}")


def parse_quota(headers: Any) -> dict:
    """``{"last", "used", "remaining"}`` from the response headers (ints or None).

    Mirrors NFL Odds ``client.Quota.update``: every response, free or paid,
    carries ``x-requests-last`` (what this call cost), ``-used`` and
    ``-remaining`` for the month.
    """
    def _n(name: str) -> Optional[int]:
        try:
            v = (headers or {}).get(name)
        except AttributeError:
            v = None
        if v is None:
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    return {"last": _n("x-requests-last"), "used": _n("x-requests-used"),
            "remaining": _n("x-requests-remaining")}


def fetch(path: str, params: Optional[dict] = None, *, settings: Optional[dict] = None,
          key: Optional[str] = None, attempts: int = 3) -> tuple[Any, dict]:
    """``GET {base_url}/{path}`` -> ``(json, quota)``. Raises :class:`OddsApiError`.

    Retries 429 / 5xx / connection errors with backoff. The key goes in the
    query string (that is how The Odds API authenticates) and is never logged:
    error text carries the path, not the URL.
    """
    cfg = api_cfg(settings)
    key = key or _api_key(cfg)
    if not key:
        raise OddsApiError(0, path, f"no {cfg.get('key_env')}")
    requests = _requests()
    url = f"{str(cfg['base_url']).rstrip('/')}/{path.lstrip('/')}"
    q = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    q["apiKey"] = key
    last_err: Optional[Exception] = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=q, timeout=(10, 60))
        except Exception as e:  # noqa: BLE001 - connection / timeout
            last_err = OddsApiError(0, path, type(e).__name__)
        else:
            quota = parse_quota(getattr(r, "headers", {}) or {})
            if r.status_code == 200:
                return r.json(), quota
            last_err = OddsApiError(r.status_code, path, getattr(r, "text", "") or "")
            if r.status_code != 429 and r.status_code < 500:
                raise last_err
        if i < attempts - 1:
            _sleep(2.0 * (2 ** i))
    raise last_err or OddsApiError(0, path)


def fetch_quota(settings: Optional[dict] = None) -> dict:
    """Free ``GET /sports`` just for the quota headers.

    ``{"used", "remaining", "last", "at"}`` or ``{"error": ...}`` — never raises.
    """
    cfg = api_cfg(settings)
    if not _api_key(cfg):
        return {"error": f"no {cfg.get('key_env')}"}
    try:
        _body, quota = fetch("sports", settings=settings)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {**quota, "at": _iso(_now())}


# ---------------------------------------------------------------------------
# A2. Game lines
# ---------------------------------------------------------------------------


def team_lookup() -> dict[str, str]:
    """``{"washington commanders": "WAS", ...}`` plus nickname-only keys."""
    out: dict[str, str] = {}
    for t in get_teams():
        name = str(t.get("name") or "").strip().lower()
        if not name:
            continue
        out[name] = t["abbr"]
        out.setdefault("~" + name.split()[-1], t["abbr"])
    return out


def resolve_team(name: str, lookup: Optional[dict[str, str]] = None) -> str:
    """Odds API full team name -> news abbr; fallback on the last word ("Commanders")."""
    lookup = lookup if lookup is not None else team_lookup()
    n = str(name or "").strip().lower()
    if not n:
        return ""
    if n in lookup:
        return lookup[n]
    return lookup.get("~" + n.split()[-1], "")


def _kickoff_utc(row: dict) -> Optional[datetime]:
    """Schedule row (``date`` + ET ``time`` like "8:15 PM") -> UTC datetime."""
    try:
        d = date.fromisoformat(str(row.get("date")))
    except (TypeError, ValueError):
        return None
    t = str(row.get("time") or "").strip()
    hh, mm = 13, 0
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])", t)
    if m:
        hh = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        mm = int(m.group(2))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET_ZONE).astimezone(timezone.utc)


def week_schedule(season: int, week: int, settings: Optional[dict] = None,
                  schedule: Optional[list[dict]] = None) -> list[dict]:
    """This week's games in news abbrevs: ``[{"away", "home", "kickoff"}]``.

    The cached schedule (``data/schedule/<season>.json``) is in the projection
    dialect (ARZ / BLT / LA ...), so it is normalised here once.
    """
    if schedule is None:
        schedule = season_mod.load_schedule(settings=settings, season=season) or []
    out = []
    for g in schedule:
        if int(g.get("week") or 0) != int(week):
            continue
        out.append({"away": to_news(str(g.get("away") or ""), "proj"),
                    "home": to_news(str(g.get("home") or ""), "proj"),
                    "kickoff": _kickoff_utc(g)})
    return out


def events_window(rows: list[dict]) -> tuple[Optional[str], Optional[str]]:
    """``[earliest kickoff - 36h, latest kickoff + 6h]`` as API ``Z`` strings."""
    ks = [r["kickoff"] for r in rows if r.get("kickoff")]
    if not ks:
        return None, None
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return ((min(ks) - timedelta(hours=36)).strftime(fmt),
            (max(ks) + timedelta(hours=6)).strftime(fmt))


def filter_events(events: list[dict], rows: list[dict],
                  lookup: Optional[dict[str, str]] = None) -> list[dict]:
    """Keep the events whose ``(away, home)`` pair is a game of this week.

    Returns ``[{"id", "away", "home", "commence_time"}]``. Anything else in the
    window (a neighbouring week's TNF, a misnamed team) is dropped, never
    guessed at — it would write a game key that does not belong to the week.
    """
    lookup = lookup if lookup is not None else team_lookup()
    pairs = {(r["away"], r["home"]) for r in rows}
    out = []
    for ev in events or []:
        away = resolve_team(ev.get("away_team"), lookup)
        home = resolve_team(ev.get("home_team"), lookup)
        if (away, home) in pairs:
            out.append({"id": ev.get("id"), "away": away, "home": home,
                        "commence_time": ev.get("commence_time")})
        else:
            logger.info("Odds API event %s @ %s is not a Week game here - skipped",
                        ev.get("away_team"), ev.get("home_team"))
    return out


def american_to_prob(price: float) -> float:
    price = float(price)
    return 100.0 / (price + 100.0) if price > 0 else -price / (-price + 100.0)


def devig_pair(p_a: float, p_b: float) -> tuple[float, float]:
    s = p_a + p_b
    return (p_a / s, p_b / s) if s > 0 else (p_a, p_b)


def _median(vals: list) -> Optional[float]:
    vals = [float(v) for v in vals if v is not None]
    return float(statistics.median(vals)) if vals else None


def _best_line(quotes: list[tuple], prefer: str) -> tuple[Optional[float], Optional[float]]:
    """Bettor-friendliest ``(point, price)``: ``high`` = largest point, ``low`` = smallest;
    ties by the higher price (NFL Odds ``compare._best_line``)."""
    qs = [(p, pr) for p, pr, _b in quotes if p is not None and pr is not None]
    if not qs:
        return None, None
    if prefer == "low":
        return min(qs, key=lambda q: (q[0], -q[1]))
    return max(qs, key=lambda q: (q[0], q[1]))


def _ml_str(v: Optional[float]) -> str:
    return "" if v is None else f"{int(round(v))}"


def _et(iso: Any) -> str:
    dt = _parse_dt(iso)
    if not dt:
        return str(iso or "")[:16]
    return dt.astimezone(ET_ZONE).strftime("%a %m/%d %I:%M %p")


def _sheet_block(spread_home: Optional[float], total: Optional[float],
                 sheet_spread: Optional[float], sheet_ou: Optional[float], fp_cfg: dict) -> dict:
    """The sheet-vs-market block ``read_game_lines`` reads off SB_GameLines' FP columns."""
    sd = (round(spread_home - sheet_spread, 2)
          if spread_home is not None and sheet_spread is not None else None)
    td = round(total - sheet_ou, 2) if total is not None and sheet_ou is not None else None
    flag = ""
    if sd is not None and abs(sd) >= float(fp_cfg.get("spread", 1.0)):
        flag = "FP-SPREAD"
    if td is not None and abs(td) >= float(fp_cfg.get("total", 1.5)):
        flag = "FP-BOTH" if flag else "FP-TOTAL"
    return {"spread_home": sheet_spread, "ou": sheet_ou,
            "fp_spread_delta": sd, "fp_total_delta": td, "fp_flag": flag}


def build_game_row(event: dict, away: str, home: str, *, sheet_game: Optional[dict] = None,
                   prev_game: Optional[dict] = None, settings: Optional[dict] = None) -> dict:
    """One ``/odds`` event -> the dict ``odds_collector.read_game_lines`` returns.

    ``sheet_game`` is the active weekly snapshot's game row for the home team
    (``metrics["Spread"]`` / ``metrics["O/U"]`` = the sheet's own line). Without
    it the stored game's sheet line is kept and only the deltas re-measured —
    the sheet did not change just because this pull could not read it.
    """
    cfg = api_cfg(settings)
    gl = cfg["game_lines"]
    home_name, away_name = event.get("home_team"), event.get("away_team")
    sp_home, sp_away, over, under, ml_home, ml_away = [], [], [], [], [], []
    books: set[str] = set()
    sharp_key = str(gl.get("sharp_book") or "pinnacle").lower()
    sharp: dict[str, Any] = {"book": "", "spread_home": None, "total": None,
                             "home_ml": None, "home_p": None}
    sharp_away_ml = None
    for bk in event.get("bookmakers") or []:
        bkey = str(bk.get("key") or "")
        is_sharp = bkey.lower() == sharp_key
        for mkt in bk.get("markets") or []:
            mk = mkt.get("key")
            for oc_ in mkt.get("outcomes") or []:
                name, price, point = oc_.get("name"), oc_.get("price"), oc_.get("point")
                price = float(price) if price is not None else None
                point = float(point) if point is not None else None
                if price is None:
                    continue
                books.add(bkey)
                if mk == "spreads" and name == home_name:
                    sp_home.append((point, price, bkey))
                    if is_sharp:
                        sharp["spread_home"] = point
                elif mk == "spreads" and name == away_name:
                    sp_away.append((point, price, bkey))
                elif mk == "totals" and name == "Over":
                    over.append((point, price, bkey))
                    if is_sharp:
                        sharp["total"] = point
                elif mk == "totals" and name == "Under":
                    under.append((point, price, bkey))
                elif mk == "h2h" and name == home_name:
                    ml_home.append((None, price, bkey))
                    if is_sharp:
                        sharp["home_ml"] = price
                elif mk == "h2h" and name == away_name:
                    ml_away.append((None, price, bkey))
                    if is_sharp:
                        sharp_away_ml = price
    if any(sharp[k] is not None for k in ("spread_home", "total", "home_ml")):
        sharp["book"] = sharp_key
    if sharp["home_ml"] is not None and sharp_away_ml is not None:
        sharp["home_p"] = round(devig_pair(american_to_prob(sharp["home_ml"]),
                                           american_to_prob(sharp_away_ml))[0], 3)

    spread = _median([q[0] for q in sp_home])
    total = _median([q[0] for q in over])
    hs = _best_line(sp_home, "high")
    as_ = _best_line(sp_away, "high")
    ov = _best_line(over, "low")
    un = _best_line(under, "high")
    best_hml = max((q[1] for q in ml_home), default=None)
    best_aml = max((q[1] for q in ml_away), default=None)

    if sheet_game:
        m = sheet_game.get("metrics") or {}
        s_sp, s_ou = oc._f(m.get("Spread")), oc._f(m.get("O/U"))
    else:
        prev_sheet = (prev_game or {}).get("sheet") or {}
        s_sp, s_ou = prev_sheet.get("spread_home"), prev_sheet.get("ou")

    return {
        "key": f"{away}@{home}",
        "away": away,
        "home": home,
        "kickoff_et": _et(event.get("commence_time")),
        "spread_home": spread,
        "total": total,
        "home_ml": _median([q[1] for q in ml_home]),
        "away_ml": _median([q[1] for q in ml_away]),
        "n_books": len(books),
        "best": {
            "home_spread": f"{hs[0]:+g} ({hs[1]:+.0f})" if hs[0] is not None else "",
            "away_spread": f"{as_[0]:+g} ({as_[1]:+.0f})" if as_[0] is not None else "",
            "over": f"{ov[0]:g} ({ov[1]:+.0f})" if ov[0] is not None else "",
            "under": f"{un[0]:g} ({un[1]:+.0f})" if un[0] is not None else "",
            "home_ml": _ml_str(best_hml),
            "away_ml": _ml_str(best_aml),
        },
        "sharp": sharp,
        "sheet": _sheet_block(spread, total, s_sp, s_ou, gl.get("fp_flag") or {}),
        "implied": oc._market_implied(spread, total),
    }


def _sheet_games(season: int, week: int) -> dict:
    """The active weekly snapshot's games, or {} when it is another week's."""
    try:
        from processing.weekly_projections import load_active_snapshot
        snap = load_active_snapshot(season) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("Weekly snapshot unavailable for the sheet line: %s", e)
        return {}
    meta = snap.get("meta") or {}
    try:
        if int(meta.get("week") or 0) != int(week):
            return {}
    except (TypeError, ValueError):
        return {}
    return snap.get("games") or {}


def _resolve_week(date_str: Optional[str], season: Optional[int], week: Optional[int],
                  settings: dict) -> tuple[int, Optional[int], list]:
    season = season or season_mod.get_season_year(settings)
    schedule = season_mod.load_schedule(settings=settings, season=season) or []
    if week is None:
        week = season_mod.week_from_date(schedule, date_str or season_mod.today_et()) if schedule else None
    return season, week, schedule


def last_api_lines_at(ledger: Optional[dict] = None) -> Optional[datetime]:
    """When the last successful game-lines pull ran (any season/week)."""
    ledger = ledger if ledger is not None else load_ledger()
    ts = [_parse_dt(e.get("at")) for e in ledger.get("pulls") or []
          if e.get("kind") == "game_lines" and e.get("ok")]
    ts = [t for t in ts if t]
    return max(ts) if ts else None


def pull_game_lines(date_str: Optional[str] = None, season: Optional[int] = None,
                    week: Optional[int] = None, settings: Optional[dict] = None,
                    log: Optional[logging.Logger] = None, *, run: str = "manual",
                    requested_by: str = "pipeline", dry_run: bool = False,
                    now: Optional[datetime] = None) -> dict:
    """Pull this week's spreads / totals / moneylines (9 credits) into the week file.

    Never raises. Returns ``{"ok", "reason", "games", "changes", "credits_used",
    "credits_remaining", "file", "events"}``.
    """
    log = log or logger
    result: dict[str, Any] = {"ok": False, "reason": "", "games": 0, "changes": [],
                              "credits_used": 0, "credits_remaining": None, "file": None,
                              "events": [], "season": season, "week": week}
    try:
        settings = settings or get_settings()
        cfg = api_cfg(settings)
        gl = cfg["game_lines"]
        if not cfg.get("enabled", True):
            result["reason"] = "odds.api.enabled is false"
            return result
        if not _api_key(cfg):
            result["reason"] = f"no {cfg.get('key_env')} - skipping"
            return result
        season, week, schedule = _resolve_week(date_str, season, week, settings)
        result["season"], result["week"] = season, week
        if not week:
            result["reason"] = "no NFL week for this date"
            return result
        rows = week_schedule(season, week, settings, schedule=schedule)
        if not rows:
            result["reason"] = f"no schedule rows for Week {week}"
            return result

        # 1. Budget: the quota call is free.
        quota = fetch_quota(settings)
        if quota.get("error"):
            result["reason"] = f"quota check failed: {quota['error']}"
            return result
        result["credits_remaining"] = quota.get("remaining")
        ok, why = lines_budget(quota, settings)
        if not ok:
            result["reason"] = why
            return result

        # 2. Events (free), filtered to this week's schedule.
        start, end = events_window(rows)
        sport = cfg["sport"]
        events, _q = fetch(f"sports/{sport}/events",
                           {"dateFormat": "iso", "commenceTimeFrom": start, "commenceTimeTo": end},
                           settings=settings)
        lookup = team_lookup()
        matched = filter_events(events or [], rows, lookup)
        result["events"] = matched
        now_dt = now or _now()
        started = sum(1 for r in rows if r.get("kickoff") and r["kickoff"] <= now_dt)
        if not 12 <= len(matched) <= 16 and not started:
            log.warning("Odds API: %d events matched Week %s (schedule has %d games)",
                        len(matched), week, len(rows))
        if not matched:
            result["reason"] = f"no Odds API events matched Week {week}"
            return result
        n_cost = len(gl.get("regions") or []) * len(gl.get("markets") or [])
        if dry_run:
            for e in matched:
                log.info("  %s@%s  %s", e["away"], e["home"], _et(e["commence_time"]))
            result["ok"] = True
            result["reason"] = (f"dry run - {len(matched)} events matched Week {week}; "
                                f"would spend {n_cost} credits")
            return result

        # 3. Odds (paid).
        odds, q = fetch(f"sports/{sport}/odds", {
            "regions": ",".join(gl.get("regions") or []),
            "markets": ",".join(gl.get("markets") or []),
            "oddsFormat": "american", "dateFormat": "iso",
            "eventIds": ",".join(str(e["id"]) for e in matched),
        }, settings=settings)
        result["credits_used"] = q.get("last") or 0
        result["credits_remaining"] = q.get("remaining")

        # 4. Rows in read_game_lines' shape.
        prev = oc.load_week_file(season, week)
        prev_games = (prev or {}).get("games") or {}
        sheet_games = _sheet_games(season, week)
        by_id = {e["id"]: e for e in matched}
        games: list[dict] = []
        for ev in odds or []:
            m = by_id.get(ev.get("id"))
            if not m:
                continue
            key = f"{m['away']}@{m['home']}"
            games.append(build_game_row(
                ev, m["away"], m["home"], sheet_game=sheet_games.get(to_proj(m["home"])),
                prev_game=prev_games.get(key), settings=settings))
        games = [g for g in games if g["spread_home"] is not None or g["total"] is not None]

        # 5. Merge. The stored pull fields describe the NFL Odds project's
        # pull (props) and stay as they are; only the games half is new.
        now_iso = _iso(now_dt)
        stored = (prev or {}).get("pull") or {}
        meta = {k: stored.get(k) for k in ("pulled_at", "props_pull_id", "week_reported",
                                           "stale_reason", "age_hours")}
        meta.update({
            "games_at": now_dt.astimezone(ET_ZONE).isoformat(timespec="seconds"),
            "games_source": "api", "games_age_hours": 0.0, "games_stale_reason": "",
            "thresholds": oc._cfg(settings).get("thresholds") or oc.DEFAULT_THRESHOLDS,
            "pull_source": "api",
            "credits_used": result["credits_used"],
            "credits_remaining": result["credits_remaining"],
        })
        if games:
            data, changes = oc.merge_into_week(prev, season, week, games, {}, meta, now_iso)
            result["file"] = str(oc.save_week_file(data))
            result["games"] = len(games)
            result["changes"] = changes

        # 6. Ledger — the credits were spent whether or not a row survived.
        append_ledger({
            "at": now_iso, "kind": "game_lines", "mode": "", "season": season, "week": week,
            "run": run, "requested_by": requested_by,
            "credits_used": result["credits_used"], "credits_remaining": result["credits_remaining"],
            "ok": bool(games), "run_url": "", "note": "" if games else "no priced games returned",
        }, quota={"used": q.get("used"), "remaining": q.get("remaining")})
        result["ok"] = bool(games)
        result["reason"] = (f"{len(games)} games, {len(result['changes'])} changes"
                            if games else "the odds call returned no priced games")
    except Exception as e:  # noqa: BLE001 - the pipeline must keep going
        log.warning("Game-lines pull failed (non-fatal): %s", e)
        result["reason"] = str(e)
    return result


# ---------------------------------------------------------------------------
# C. Ledger + budget (no network)
# ---------------------------------------------------------------------------


def ledger_path() -> Path:
    return oc._base_dir() / LEDGER_NAME


def load_ledger() -> dict:
    p = ledger_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("pulls", [])
    data.setdefault("quota", {})
    return data


def append_ledger(entry: dict, quota: Optional[dict] = None) -> dict:
    """Append one pull (keeping the last :data:`LEDGER_KEEP`) and refresh the quota."""
    data = load_ledger()
    try:
        data["pulls"] = (list(data.get("pulls") or []) + [entry])[-LEDGER_KEEP:]
        if quota and quota.get("remaining") is not None:
            data["quota"] = {"at": entry.get("at") or _iso(_now()),
                             "used": quota.get("used"), "remaining": quota.get("remaining")}
        ledger_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not write the Odds API ledger: %s", e)
    return data


def lines_budget(quota: Optional[dict], settings: Optional[dict] = None) -> tuple[bool, str]:
    """``remaining >= odds.api.min_remaining_credits`` (unknown counts as allowed)."""
    cfg = api_cfg(settings)
    floor = int(cfg.get("min_remaining_credits", 1000))
    remaining = (quota or {}).get("remaining")
    if remaining is None:
        return True, "credits remaining unknown"
    if remaining < floor:
        return False, f"only {remaining:,} credits left (floor {floor:,})"
    return True, f"{remaining:,} credits left"


def _counts_against(e: dict) -> bool:
    return e.get("kind") == "props" and (
        e.get("ok") or float(e.get("credits_used") or 0) >= PROPS_SPENT_FLOOR)


def props_budget(ledger: Optional[dict], project_last_pull_at: Optional[str],
                 settings: Optional[dict] = None, now: Optional[datetime] = None,
                 season_week: Optional[tuple[int, int]] = None,
                 remaining: Optional[float] = None) -> dict:
    """May a props pull be dispatched now? Pure; shared by the dashboard and the server.

    Rules (``odds.api.props``): the last props pull — ours or the NFL Odds
    project's own scheduled one — is at least ``min_gap_hours`` old; fewer than
    ``max_per_day`` today (ET) and ``max_per_week`` this NFL week; and the
    credits left after a full pull stay above ``min_remaining_credits``.
    ``remaining`` overrides the ledger's cached quota (the server passes a live
    reading). A failed run that still spent credits counts like a success.
    """
    cfg = api_cfg(settings)["props"]
    ledger = ledger or {}
    now = now or _now()
    now_et = now.astimezone(ET_ZONE)
    entries = [e for e in ledger.get("pulls") or [] if _counts_against(e)]
    times = [(_parse_dt(e.get("at")), e) for e in entries]
    times = [(t, e) for t, e in times if t]

    today = sum(1 for t, _e in times if t.astimezone(ET_ZONE).date() == now_et.date())
    this_week = 0
    if season_week:
        this_week = sum(1 for _t, e in times
                        if (e.get("season"), e.get("week")) == tuple(season_week))
    if remaining is None:
        remaining = (ledger.get("quota") or {}).get("remaining")

    gap = timedelta(hours=float(cfg.get("min_gap_hours", 6)))
    max_day, max_week = int(cfg.get("max_per_day", 2)), int(cfg.get("max_per_week", 4))
    floor = int(cfg.get("min_remaining_credits", 7000))

    blocks: list[str] = []
    next_at: Optional[datetime] = None
    last_ours = max((t for t, _e in times), default=None)
    if last_ours and now < last_ours + gap:
        next_at = last_ours + gap
        blocks.append(f"last props pull was {_et_label(last_ours)}")
    proj = _parse_dt(project_last_pull_at)
    if proj and now < proj + gap:
        cand = proj + gap
        next_at = max(next_at, cand) if next_at else cand
        blocks.append(f"the NFL Odds project last pulled {_et_label(proj)}")
    if today >= max_day:
        midnight = datetime.combine(now_et.date() + timedelta(days=1), datetime.min.time(),
                                    tzinfo=ET_ZONE)
        next_at = max(next_at, midnight) if next_at else midnight
        blocks.insert(0, f"{today} of {max_day} today used")
    week_blocked = bool(season_week) and this_week >= max_week
    if week_blocked:
        blocks.insert(0, f"{this_week} of {max_week} this week used")
    if remaining is not None and remaining - PROPS_EST_CREDITS < floor:
        blocks.insert(0, f"only {int(remaining):,} credits left (keeping {floor:,} in reserve)")

    allowed = not blocks
    if allowed:
        reason = (f"{today} of {max_day} today, {this_week} of {max_week} this week used"
                  + (f"; {int(remaining):,} credits left" if remaining is not None else ""))
    else:
        reason = "; ".join(blocks)
        if week_blocked:
            reason += "; resets next NFL week"
        elif next_at and not (remaining is not None and remaining - PROPS_EST_CREDITS < floor):
            reason += f"; next allowed {_et_label(next_at)}"
    return {"allowed": allowed, "reason": reason,
            "next_at": _iso(next_at) if next_at and not allowed else None,
            "today": today, "this_week": this_week, "remaining": remaining,
            "max_per_day": max_day, "max_per_week": max_week}


# ---------------------------------------------------------------------------
# A3. Props via the NFL Odds workflow
# ---------------------------------------------------------------------------


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _github_token(cfg: dict) -> str:
    """``NFL_ODDS_GH_TOKEN`` or, locally only, ``gh auth token``.

    Not on Actions: there ``gh`` is authenticated as this repo's GITHUB_TOKEN,
    which cannot dispatch a workflow in another repository.
    """
    tok = (os.environ.get(cfg["props"].get("token_env") or "NFL_ODDS_GH_TOKEN") or "").strip()
    if tok or os.environ.get("GITHUB_ACTIONS"):
        return tok
    try:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - no gh on PATH
        return ""


def _find_props_run(runs: list[dict], dispatched_at: datetime) -> Optional[dict]:
    """Newest run created at/after the dispatch (30 s of clock skew allowed)."""
    cutoff = dispatched_at - timedelta(seconds=30)
    cands = [(r, _parse_dt(r.get("created_at"))) for r in runs or []]
    cands = [(r, t) for r, t in cands if t and t >= cutoff]
    if not cands:
        return None
    return max(cands, key=lambda c: c[1])[0]


def _credits_from_status(season: int) -> Optional[int]:
    """Fallback: '... · 548 credits · 14849 remaining · ...' from Pull_Status row 3."""
    try:
        status = oc.read_pull_status(oc._client(), season)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"([\d.]+) credits · ([\d.]+) remaining", status.get("detail") or "")
    return int(float(m.group(1))) if m else None


def pull_props(season: Optional[int] = None, week: Optional[int] = None,
               settings: Optional[dict] = None, log: Optional[logging.Logger] = None, *,
               mode: Optional[str] = None, run: str = "manual", requested_by: str = "pipeline",
               force: bool = False, dry_run: bool = False, date_str: Optional[str] = None,
               now: Optional[datetime] = None) -> dict:
    """Dispatch the NFL Odds project's prop pull, wait for it, record the cost.

    Never raises. The caller then runs the ordinary sheet read
    (``run_odds_step`` -> ``collect_odds``), which sees the new pull.
    """
    log = log or logger
    result: dict[str, Any] = {"ok": False, "reason": "", "mode": mode, "budget": {},
                              "conclusion": "", "run_url": "", "credits_used": None,
                              "credits_remaining": None, "dispatched": False}
    try:
        settings = settings or get_settings()
        cfg = api_cfg(settings)
        pcfg = cfg["props"]
        mode = mode or pcfg.get("mode") or "refresh"
        result["mode"] = mode
        if not cfg.get("enabled", True):
            result["reason"] = "odds.api.enabled is false"
            return result
        if mode not in PROPS_MODES:
            result["reason"] = f"unknown props mode {mode!r} (use {' / '.join(PROPS_MODES)})"
            return result
        season, week, _schedule = _resolve_week(date_str, season, week, settings)
        now_dt = now or _now()

        # 1. Budget — live quota when we can read it, the ledger's otherwise.
        quota = fetch_quota(settings)
        before_used = quota.get("used")
        ledger = load_ledger()
        stored = oc.load_week_file(season, week) if week else None
        verdict = props_budget(ledger, ((stored or {}).get("pull") or {}).get("pulled_at"),
                               settings, now=now_dt,
                               season_week=(season, week) if week else None,
                               remaining=quota.get("remaining"))
        result["budget"] = verdict
        result["credits_remaining"] = verdict.get("remaining")
        if not verdict["allowed"] and not force:
            result["reason"] = f"budget: {verdict['reason']}"
            return result
        if dry_run:
            result["ok"] = verdict["allowed"] or force
            result["reason"] = (f"dry run - {'allowed' if verdict['allowed'] else 'forced'}: "
                                f"{verdict['reason']}; would dispatch {pcfg['repo']} "
                                f"{pcfg['workflow']} mode={mode}")
            return result

        # 2. Dispatch.
        token = _github_token(cfg)
        if not token:
            result["reason"] = f"no {pcfg.get('token_env')} (and no local gh login)"
            return result
        requests = _requests()
        base = f"{_GITHUB_API}/repos/{pcfg['repo']}/actions/workflows/{pcfg['workflow']}"
        dispatched_at = _now()
        r = requests.post(f"{base}/dispatches", headers=_gh_headers(token),
                          json={"ref": pcfg.get("ref") or "main", "inputs": {"mode": mode}},
                          timeout=20)
        if r.status_code not in (200, 201, 204):
            result["reason"] = (f"dispatch failed: GitHub {r.status_code} "
                                f"{(getattr(r, 'text', '') or '')[:160]}")
            return result
        result["dispatched"] = True
        log.info("Dispatched %s %s (mode=%s); waiting for it", pcfg["repo"], pcfg["workflow"], mode)

        # 3. Find + wait.
        deadline = _clock() + 60.0 * float(pcfg.get("wait_minutes", 15))
        found: Optional[dict] = None
        while True:
            try:
                rr = requests.get(f"{base}/runs", headers=_gh_headers(token),
                                  params={"event": "workflow_dispatch", "per_page": 10},
                                  timeout=20)
                if rr.status_code == 200:
                    found = _find_props_run((rr.json() or {}).get("workflow_runs") or [],
                                            dispatched_at) or found
            except Exception as e:  # noqa: BLE001 - keep polling
                log.info("Polling the props run failed once: %s", e)
            if found and found.get("status") == "completed":
                break
            if _clock() >= deadline:
                break
            _sleep(20)

        result["run_url"] = (found or {}).get("html_url") or ""
        result["conclusion"] = (found or {}).get("conclusion") or ""
        completed = bool(found) and found.get("status") == "completed"
        note = "" if completed else ("timed out waiting for the run" if found
                                     else "dispatched run never appeared")

        # 4. Ledger: credits from the quota delta, else Pull_Status.
        after = fetch_quota(settings)
        used = None
        if after.get("used") is not None and before_used is not None:
            used = int(after["used"]) - int(before_used)
        elif completed:
            used = _credits_from_status(season)
        result["credits_used"] = used
        result["credits_remaining"] = after.get("remaining", result["credits_remaining"])
        ok = completed and result["conclusion"] == "success"
        append_ledger({
            "at": _iso(dispatched_at), "kind": "props", "mode": mode, "season": season,
            "week": week, "run": run, "requested_by": requested_by,
            "credits_used": used, "credits_remaining": result["credits_remaining"],
            "ok": ok, "run_url": result["run_url"], "note": note,
        }, quota=after if not after.get("error") else None)
        result["ok"] = ok
        result["reason"] = (f"props pull {result['conclusion'] or 'incomplete'}"
                            + (f" ({note})" if note else "")
                            + (f", {used} credits" if used is not None else ""))
    except Exception as e:  # noqa: BLE001
        log.warning("Props pull failed (non-fatal): %s", e)
        result["reason"] = str(e)
    return result


# ---------------------------------------------------------------------------
# A4. CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Direct Odds API pulls (game lines) and "
                                             "NFL Odds prop-pull dispatch.")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--quota", action="store_true", help="print credits used / remaining (free)")
    what.add_argument("--lines", action="store_true", help="pull this week's game lines (9 credits)")
    what.add_argument("--props", action="store_true", help="dispatch the NFL Odds props pull")
    ap.add_argument("--mode", choices=PROPS_MODES, default=None)
    ap.add_argument("--dry-run", action="store_true", help="spend nothing")
    ap.add_argument("--force", action="store_true", help="props: ignore the budget")
    ap.add_argument("--date", default=None)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--season", type=int, default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.quota:
        q = fetch_quota()
        if q.get("error"):
            print("error:", q["error"])
            return 1
        print(f"Odds API credits: {q.get('used')} used, {q.get('remaining')} remaining")
        return 0
    if args.lines:
        res = pull_game_lines(args.date, season=args.season, week=args.week, run="manual",
                              requested_by="cli", dry_run=args.dry_run)
        print(f"Week {res.get('week')}: {'ok' if res['ok'] else 'NOT ok'} - {res['reason']}")
        if not args.dry_run and res.get("credits_used"):
            print(f"  credits used {res['credits_used']}, remaining {res['credits_remaining']}")
        for c in (res.get("changes") or [])[:30]:
            print(f"  [{c['type']:12s}] {c['message']}")
        if res.get("file"):
            print("written:", res["file"])
        return 0 if res["ok"] else 1
    res = pull_props(args.season, args.week, mode=args.mode, run="manual", requested_by="cli",
                     force=args.force, dry_run=args.dry_run, date_str=args.date)
    print(f"Props ({res.get('mode')}): {'ok' if res['ok'] else 'NOT ok'} - {res['reason']}")
    if res.get("run_url"):
        print("run:", res["run_url"])
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
