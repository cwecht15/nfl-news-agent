"""Resolve RSS feed URLs for the configured NFL podcasts via the iTunes API.

Apple's public, unauthenticated Search API returns each podcast's real RSS
`feedUrl` (plus publisher name and collection id). This one-off helper takes
the curated name->team list below, queries iTunes for each, and prints
ready-to-paste YAML for `config/sources.yaml` plus the top alternates so
ambiguous matches (and the two uncertain team tags) can be eyeballed.

Usage:
    python scripts/resolve_podcast_feeds.py            # human-readable report
    python scripts/resolve_podcast_feeds.py --yaml     # emit the `podcasts:` block
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

# name -> NFL team abbreviation, or "NFL" for national / league-wide shows.
# "?" tags are unconfirmed and should be verified against the resolved
# publisher/description before committing.
PODCASTS: list[tuple[str, str]] = [
    ("NFL: The Insiders", "NFL"),
    ("The Adam Schefter Podcast", "NFL"),
    ("The MMQB NFL Podcast", "NFL"),
    ("Drive Time with Travis Wingfield", "MIA"),
    ("Guilty As Charged Podcast", "LAC"),
    ("Welcome to Detroit - A Detroit Lions podcast", "DET"),
    ("The James Boyd Podcast", "IND"),
    ("Pats Interference podcast", "NE"),
    ("Giants Nation Show", "NYG"),
    ("NFL Daily with Gregg Rosenthal", "NFL"),
    ("Processing Blue: A Panthers Podcast", "CAR"),
    ("The Athletic Football Show: A show about the NFL", "NFL"),
    ("NFL: Move the Sticks with Daniel Jeremiah & Bucky Brooks", "NFL"),
    ("The Matt Verderame Show", "NFL"),
    ("Eagle Eye: A Philadelphia Eagles Podcast", "PHI"),
    ("Hops With Pop", "LAC"),
    ("The Alec Lewis Show", "MIN"),
    ("The Playcallers", "NFL"),
    ("Go Birds!", "PHI"),
    ("Love of the Star: A Dallas Cowboys Podcast", "DAL"),
    ("Orange and Blue Today", "DEN?"),
    ("First & Tenn", "TEN"),
    ("All 32 NFL Podcast w/ Mike Giardi", "NFL"),
    ("Twentyman in the Huddle", "DET"),
    ("NewOrleans.Football: Saints Podcast", "NO"),
    ("The New York Football Podcast: A show about the New York Giants", "NYG"),
    ("The John Keim Report", "WAS"),
    ("Seahawks Man 2 Man: A show about the Seattle Seahawks", "SEA"),
    ("Breaking Big Blue with Jordan Raanan", "NYG"),
    ("Between the Tackles", "?"),
    ("Take The North: A Chicago Bears Podcast", "CHI"),
    ("UnCovering the Birds with Jeff McLane", "PHI"),
    ("One Bills Live", "BUF"),
    ("The Ringer's Philly Special", "PHI"),
    ("Hoge & Jahns: a show about the Chicago Bears", "CHI"),
    ("The Football GM: A show about the NFL", "NFL"),
    ("Jeff Legwold ESPN Broncos Reports", "DEN"),
    ("The Broncos Podcast with Troy Renck", "DEN"),
]

SEARCH = "https://itunes.apple.com/search"


def _search(term: str, limit: int = 4) -> list[dict]:
    qs = urllib.parse.urlencode({
        "term": term,
        "media": "podcast",
        "entity": "podcast",
        "limit": limit,
    })
    req = urllib.request.Request(
        f"{SEARCH}?{qs}",
        headers={"User-Agent": "nfl-news-agent/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("results", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yaml", action="store_true", help="Emit the podcasts: YAML block")
    ap.add_argument("--delay", type=float, default=0.7)
    args = ap.parse_args()

    resolved: list[dict] = []
    for name, team in PODCASTS:
        try:
            results = _search(name)
        except Exception as e:
            print(f"!! {name}: search failed ({e})", file=sys.stderr)
            results = []
        time.sleep(args.delay)

        top = results[0] if results else {}
        resolved.append({
            "name": name,
            "team": team,
            "feed_url": top.get("feedUrl", ""),
            "itunes_id": top.get("collectionId", ""),
            "matched": top.get("collectionName", ""),
            "artist": top.get("artistName", ""),
            "alts": results[1:],
        })

        if not args.yaml:
            flag = "  <-- CONFIRM" if "?" in team else ""
            print(f"\n# {name}  [team={team}]{flag}")
            if not top:
                print("  (no match)")
                continue
            print(f"  matched : {top.get('collectionName')}  by {top.get('artistName')}")
            print(f"  feed_url: {top.get('feedUrl')}")
            print(f"  itunes  : {top.get('collectionId')}")
            for a in results[1:]:
                print(f"    alt: {a.get('collectionName')} / {a.get('artistName')} -> {a.get('feedUrl')}")

    if args.yaml:
        print("podcasts:")
        for r in resolved:
            team = r["team"].replace("?", "")
            print(f'  - name: "{r["name"].replace(chr(34), "")}"')
            print(f'    team: "{team or "NFL"}"')
            print(f'    feed_url: "{r["feed_url"]}"')
            if r["itunes_id"]:
                print(f"    itunes_id: {r['itunes_id']}")

    missing = [r["name"] for r in resolved if not r["feed_url"]]
    print(f"\n# Resolved {len(resolved) - len(missing)}/{len(resolved)} feeds.",
          file=sys.stderr)
    if missing:
        print("# No feed for: " + "; ".join(missing), file=sys.stderr)


if __name__ == "__main__":
    main()
