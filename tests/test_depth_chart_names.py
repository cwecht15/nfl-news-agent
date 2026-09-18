"""OurLads name parsing — the 2026-09-11 markup change.

OurLads started appending a ``span.dc-key`` (draft "24/1", "CF25", "U/NYJ",
position code "WR^") and an injury badge span to every player cell, and
``get_text()`` glued both onto the first name ("Rome24/1Q Odunze"). The
snapshots scraped 09-11 .. 09-17 carry those names and are healed on read.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from collectors import depth_chart_collector as dcc

LIVE_TABLE = """<html><body><table>
<tr><td>Pos</td><td>No</td><td>Player 1</td><td>No</td><td>Player 2</td></tr>
<tr><td>LWR</td><td>15</td>
  <td><a class="" href="https://www.ourlads.com/nfldepthcharts/player/53237/">Odunze, Rome</a> <span class="dc-key">24/1</span><span class="badge badge-danger bad-ps">Q</span></td>
  <td>9</td>
  <td><a class="" href="https://www.ourlads.com/nfldepthcharts/player/54601/">Walker, Jahdae</a> <span class="dc-key">CF25</span></td></tr>
<tr><td>RWR</td><td>10</td>
  <td><a class="" href="https://www.ourlads.com/nfldepthcharts/player/54186/">Burden III, Luther</a> <span class="dc-key">25/2</span></td>
  <td></td>
  <td><a class="" href="https://www.ourlads.com/nfldepthcharts/player/0/"></a> <span class="dc-key"></span></td></tr>
</table></body></html>"""


def test_cell_name_text_ignores_markers():
    cells = BeautifulSoup(LIVE_TABLE, "html.parser").find_all("td")
    assert dcc._cell_name_text(cells[7]) == "Odunze, Rome"
    assert dcc._cell_name_text(cells[9]) == "Walker, Jahdae"
    # A cell without a player link: spans are dropped, the rest is the name
    plain = BeautifulSoup('<td>Smith, John <span class="dc-key">U/NYJ</span></td>', "html.parser").td
    assert dcc._cell_name_text(plain) == "Smith, John"


def test_scrape_team_yields_clean_names(monkeypatch):
    class Resp:
        text = LIVE_TABLE

        def raise_for_status(self):
            pass

    monkeypatch.setattr(dcc.requests, "get", lambda url, **kw: Resp())
    players = dcc.scrape_team("CHI")
    assert [(p["name"], p["pos"], p["depth"]) for p in players] == [
        ("Rome Odunze", "LWR", 1), ("Jahdae Walker", "LWR", 2), ("Luther Burden III", "RWR", 1),
    ]


@pytest.mark.parametrize("tagged,clean", [
    ("Rome24/1Q Odunze", "Rome Odunze"),
    ("JahdaeCF25 Walker", "Jahdae Walker"),
    ("BrycenSF25 Tremayne", "Brycen Tremayne"),
    ("ROBERTU/Mia HUNT", "ROBERT HUNT"),
    ("JohnU/NYJ Metchie III", "John Metchie III"),
    ("FeleipeU/Atl Franks", "Feleipe Franks"),
    ("CoreyW/Bal Bullock", "Corey Bullock"),
    ("MarkCC/Mia Smith", "Mark Smith"),
    ("JAMESRB^ CONNER", "JAMES CONNER"),
    ("HAROLDOLB^ LANDRY III", "HAROLD LANDRY III"),
    ("JDILB^ Bertrand", "JD Bertrand"),
    ("VJS^ Payne", "VJ Payne"),
    ("Mitchell25/5 Evans", "Mitchell Evans"),
    ("Ja'Tavion24/4 Sanders", "Ja'Tavion Sanders"),
    ("SamS Hecht", "Sam Hecht"),                 # letters-only tag after a lowercase letter
    ("JaylenO Smith", "Jaylen Smith"),           # bare injury badge
    # clean names are never touched
    ("DJ Moore", "DJ Moore"),
    ("JAMES CONNER", "JAMES CONNER"),
    ("DeMarcus Lawrence", "DeMarcus Lawrence"),
    ("Marvin Harrison Jr.", "Marvin Harrison Jr."),
])
def test_clean_tagged_name(tagged, clean):
    assert dcc.clean_tagged_name(tagged) == clean


def test_clean_tagged_name_all_caps_needs_a_reference():
    # "SAMC" could be a name; only a clean snapshot can say it is Sam + C
    assert dcc.clean_tagged_name("SAMC MUSTIPHER") == "SAMC MUSTIPHER"
    assert dcc.clean_tagged_name("SAMC MUSTIPHER", {"sam mustipher"}) == "SAM MUSTIPHER"
    assert dcc.clean_tagged_name("BRETTOG TOTH", {"brett toth"}) == "BRETT TOTH"


def _dc(name, team="CHI", pos="LWR", depth=1, generic="WR"):
    return {"name": name, "pos": pos, "generic_pos": generic, "depth": depth, "team": team}


def _tagged_snapshot():
    names = [f"Player{c}24/1 Last{c}" for c in "ABCDEFGHIJKL"] + ["SAMC MUSTIPHER", "Rome24/1Q Odunze"]
    return {n.lower(): _dc(n, pos="IR" if n.startswith("SAMC") else "LWR") for n in names}


def test_heal_snapshot_leaves_clean_snapshots_alone():
    clean = {"dj moore": _dc("DJ Moore"), "samc x": _dc("SAMC X")}
    assert dcc._heal_snapshot(clean) is clean


def test_diff_against_a_tagged_snapshot_is_not_a_mass_turnover():
    prev = _tagged_snapshot()
    cur = {f"player{c} last{c}".lower(): _dc(f"Player{c} Last{c}") for c in "ABCDEFGHIJKL"}
    cur["sam mustipher"] = _dc("SAM MUSTIPHER", pos="IR")
    cur["rome odunze"] = _dc("Rome Odunze", depth=2)
    changes = dcc.diff_depth_charts(cur, prev)
    assert [(c["type"], c["name"]) for c in changes] == [("demoted", "Rome Odunze")]
    depth, status = dcc.split_reserve_changes(changes)
    assert status == []            # nobody "left IR" because the name format changed


def test_pipeline_path_loader_then_diff_resolves_all_caps(tmp_path, monkeypatch):
    """run_daily loads the prior snapshot through the loader (which heals
    without a reference, so the digit/slash tags are already gone) and then
    diffs; the all-caps reserve-list names must still resolve."""
    import json
    monkeypatch.setattr(dcc, "DEPTH_CHART_DIR", tmp_path)
    (tmp_path / "2026-09-17.json").write_text(json.dumps(_tagged_snapshot()), encoding="utf-8")
    prev = dcc.load_latest_depth_charts(before_date="2026-09-18")
    assert "samc mustipher" in prev                 # ambiguous without a reference
    cur = {f"player{c} last{c}".lower(): _dc(f"Player{c} Last{c}") for c in "ABCDEFGHIJKL"}
    cur["sam mustipher"] = _dc("SAM MUSTIPHER", pos="IR")
    cur["rome odunze"] = _dc("Rome Odunze")
    assert dcc.diff_depth_charts(cur, prev) == []


def test_heal_collision_keeps_the_later_entry_like_the_scraper():
    # A player on IR can also sit in his position's table; the scraper's
    # dict keeps the later (reserve-list) row, so healing must too.
    tagged = _tagged_snapshot()
    tagged["james17/3 conner"] = _dc("James17/3 Conner", team="ARZ", pos="RB", generic="RB")
    tagged["jamesrb^ conner"] = _dc("JAMESRB^ CONNER", team="ARZ", pos="IR", generic="IR")
    healed = dcc._heal_snapshot(tagged)
    assert healed["james conner"]["pos"] == "IR"


def test_diff_between_clean_snapshots_is_untouched():
    prev = {"jc latham": _dc("JC Latham", pos="LT", generic="OL"), "dj moore": _dc("DJ Moore")}
    cur = {"j latham": _dc("J Latham", pos="LT", generic="OL"), "dj moore": _dc("DJ Moore")}
    # "JC" -> "J" + "C" would need 'C' to be a glued marker; clean data is never re-keyed
    types = sorted(c["type"] for c in dcc.diff_depth_charts(cur, prev))
    assert types == ["added", "removed"]


def test_loaders_heal_legacy_files(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(dcc, "DEPTH_CHART_DIR", tmp_path)
    (tmp_path / "2026-09-17.json").write_text(json.dumps(_tagged_snapshot()), encoding="utf-8")
    healed = dcc.load_depth_chart_by_date("2026-09-17")
    assert "rome odunze" in healed and healed["rome odunze"]["name"] == "Rome Odunze"
    assert dcc.load_latest_depth_charts()["playerc lastc"]["name"] == "PlayerC LastC"
