"""Practice-squad elevations from CBS Sports' transaction log — the second
source behind ESPN (in-season).

ESPN's feed (``collectors/espn_transactions_collector.py``) is the only
source that *names* an elevation, but it publishes clubs gradually: at
7:40 PM ET on 2026-10-03 it carried Saturday elevations for 10 clubs while
CBS's page already listed ~30, at

    https://www.cbssports.com/nfl/transactions/

one ``<table>`` per day under a ``TableBase-title`` heading ("Saturday,
October 3, 2026"), rows ``Team | Player | Transaction``.

CBS's label is ``Active/prac. squad`` — a move between the 53 and the
practice squad in *either* direction and of either kind: elevations, true
promotions (DEN's Adam Prentice, "Promoted ... to the active roster" per
ESPN), even a signing *to* the practice squad (SF's Sebastian Valdez). So a
row only becomes an elevation here when

* roster state has that player on THAT club's practice squad right now, and
* ESPN names him in none of that club's rows in the window. ESPN wins
  whenever it mentions a player at all, because it says which kind of move
  it was — and the PS check alone lets through a player just signed to the
  PS (Valdez) or signed off it to the 53 ("Signed WR Alex Bachman ... to the
  active roster", which roster state still shows as PS).

What survives is recorded ``confidence: reported`` — visible on the
dashboard, but ``elevations_used`` (the 3-per-season cap) only advances once
ESPN's own row confirms it through ``roster_events.confirm_reported``.

CLI:
    python collectors/cbs_transactions_collector.py [--days N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config_loader import get_settings
from processing.team_abbr import to_news

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.cbssports.com/nfl/transactions/"
DEFAULT_LOOKBACK_DAYS = 2
DEFAULT_TIMEOUT = 30
# CBS serves its page to a browser UA (unlike ESPN's JSON APIs, which 403 one).
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
_MOVE_LABEL = "active/prac. squad"


def fetch_page(url: str = DEFAULT_URL, timeout: int = DEFAULT_TIMEOUT,
               session: Optional[requests.Session] = None) -> str:
    sess = session or requests.Session()
    try:
        resp = sess.get(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:  # noqa: BLE001 — non-fatal, like every other collector
        logger.warning("CBS transactions fetch failed: %s", e)
        return ""


def _day(title: str) -> str:
    try:
        return datetime.strptime(" ".join(title.split()), "%A, %B %d, %Y").date().isoformat()
    except ValueError:
        return ""


def parse_page(html: str) -> list[dict]:
    """Every row on the page: ``[{date, team, name, transaction}]``."""
    out: list[dict] = []
    soup = BeautifulSoup(html or "", "html.parser")
    for table in soup.find_all("table"):
        title = table.find_previous(class_="TableBase-title")
        day = _day(title.get_text(" ", strip=True)) if title else ""
        if not day:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            long_name = tds[1].find(class_="CellPlayerName--long")
            name = (long_name or tds[1]).get_text(" ", strip=True)
            team = to_news(tds[0].get_text(strip=True), "cbs")
            if name and team:
                out.append({"date": day, "team": team, "name": name,
                            "transaction": tds[2].get_text(" ", strip=True)})
    return out


def _name_key(name: str) -> str:
    from collectors.nflverse_roster_collector import name_key
    return name_key(name)


def _norm_text(text: str) -> str:
    return " ".join(str(text or "").replace(".", "").replace("’", "'").lower().split())


def espn_mentions(espn_rows: list[dict], cutoff: str) -> dict[str, str]:
    """news-style team -> every ESPN description for it since ``cutoff``, normalized."""
    out: dict[str, str] = {}
    for r in espn_rows or []:
        if str(r.get("date") or "")[:10] < cutoff:
            continue
        team = to_news(str((r.get("team") or {}).get("abbreviation") or ""), "espn")
        out[team] = out.get(team, "") + " | " + _norm_text(r.get("description"))
    return out


def elevation_candidates(rows: list[dict], state: Optional[dict], espn_rows: Optional[list[dict]],
                         cutoff: str) -> list[dict]:
    """CBS move rows that can only be an elevation, in the ESPN row shape.

    A player ESPN mentions for the same club in the window is left to ESPN.
    """
    mentioned = espn_mentions(espn_rows or [], cutoff)
    players = (state or {}).get("players") or {}
    by_name = (state or {}).get("by_name") or {}
    out, seen = [], set()
    for r in rows:
        if r["date"] < cutoff or not r["transaction"].lower().startswith(_MOVE_LABEL):
            continue
        key = _name_key(r["name"])
        if (r["team"], key) in seen or (key and key in mentioned.get(r["team"], "")):
            continue
        rec = players.get(by_name.get(key) or "") or players.get(f"name:{key}") or {}
        if str(rec.get("status") or "").upper() != "PS" or to_news(rec.get("team") or "") != r["team"]:
            continue  # not on this club's practice squad: a promotion, a PS signing, or unknown
        seen.add((r["team"], key))
        out.append({
            "date": r["date"], "team": r["team"], "name": rec.get("name") or r["name"],
            "pos": rec.get("pos") or "", "event_type": "ps_elevated", "source": "cbs",
            "detail": f"CBS: {r['name']} — {r['transaction']}",
        })
    return out


def collect_cbs_elevations(date_str: Optional[str] = None, state: Optional[dict] = None,
                           espn_rows: Optional[list[dict]] = None,
                           settings: Optional[dict] = None,
                           session: Optional[requests.Session] = None) -> list[dict]:
    """Elevations CBS lists that ESPN has not yet, inside the lookback window.
    Returns ``[]`` — never raises — when disabled or unreachable."""
    cfg = (((settings or get_settings()).get("roster", {}) or {}).get("elevations", {}) or {}).get("cbs", {}) or {}
    if not cfg.get("enabled", True):
        return []
    if state is None:
        from config_loader import get_data_dir
        try:
            state = json.loads((get_data_dir("roster") / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
    html = fetch_page(cfg.get("url", DEFAULT_URL), int(cfg.get("timeout", DEFAULT_TIMEOUT)), session)
    rows = parse_page(html)
    from processing.season import today_et
    today = date.fromisoformat(date_str or today_et())
    cutoff = (today - timedelta(days=int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS)))).isoformat()
    found = elevation_candidates(rows, state, espn_rows, cutoff)
    logger.info("CBS elevations: %d not yet in ESPN (%d move rows since %s, %d rows on the page, newest %s)",
                len(found), sum(1 for r in rows if r["date"] >= cutoff
                                and r["transaction"].lower().startswith(_MOVE_LABEL)),
                cutoff, len(rows), max((r["date"] for r in rows), default="none"))
    return found


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Practice-squad elevations from CBS Sports' transaction log.")
    ap.add_argument("--date", default=None)
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    settings.setdefault("roster", {}).setdefault("elevations", {}).setdefault("cbs", {})["lookback_days"] = args.days
    from collectors.espn_transactions_collector import fetch_transactions
    for e in collect_cbs_elevations(args.date, espn_rows=fetch_transactions(), settings=settings):
        print(f"  {e['date']}  {e['team']:4} {e['pos']:4} {e['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
