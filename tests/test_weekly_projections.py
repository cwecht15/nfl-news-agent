"""Tests for processing.weekly_projections (in-season weekly snapshots).

All offline: parsers run on synthetic grids shaped like the live
``Working_Player_Proj`` / ``Working_Game_Proj`` / ``Player_Projections`` /
``Working_Kicker_Proj`` tabs, and the orchestration test drives
``run_weekly_snapshot`` through a fake gspread client with the data root
monkeypatched to ``tmp_path``.
"""

from __future__ import annotations

import copy
import csv
import json
import logging

import pytest

from processing import weekly_projections as wp

# ---------------------------------------------------------------------------
# Synthetic grids
# ---------------------------------------------------------------------------

# Metric header from col J (index 9) on. Includes:
#   - reference columns that must be skipped (2025, L1, 2023)
#   - the duplicate "YPA Adj" pair (Scramble group / Pass group)
#   - a duplicate plain "Lock" pair that has no group to lean on
PLAYER_METRIC_HEADER = [
    "DB Share", "2025", "Sack Rate", "Sack Adj", "Scrm YPA", "YPA Adj", "L1",
    "Aimed Atts", "YPA", "YPA Adj", "Lock", "2023", "Lock", "Mkt PassAtt",
]
# Values aligned with PLAYER_METRIC_HEADER
BRISSETT_METRICS = ["100.0%", "89.0%", "6.0%", "0.0", "5.1", "0.0", "1", "30", "7.2", "0.0", "", "9", "", "31.5"]
LOVE_METRICS = ["0.0%", "N/A", "", "", "", "", "", "", "", "", "", "", "", ""]
ALLEN_METRICS = ["100.0%", "95.0%", "4.0%", "0.0", "6.4", "0.2", "1", "34", "8.1", "0.1", "1", "9", "", "35.0"]


def _player_header(team: str, home: str, opp: str) -> list[str]:
    return [team, "", home, opp, "", "", "ID", "Pos", ""] + PLAYER_METRIC_HEADER


def _player_row(team, home, opp, slot, status, gsis, pos, name, metrics) -> list[str]:
    return [team, "", home, opp, slot, status, gsis, pos, name] + list(metrics)


def _placeholder(team, home, opp, slot, pos="") -> list[str]:
    return [team, "", home, opp, slot, "", "", pos, ""] + ["N/A"] * len(PLAYER_METRIC_HEADER)


def make_player_grid(brissett_sack_adj="0.0", love_status="PS", allen_opp="NYJ") -> list[list[str]]:
    bris = list(BRISSETT_METRICS)
    bris[3] = brissett_sack_adj
    return [
        ["Working_Player_Proj", "", "", "", "", "", "", "", ""],
        ["", "", "", "", "", "", "", "", ""],
        # --- ARZ block (away at LAC) ---
        _player_header("ARZ", "LAC", "LAC"),
        _player_row("ARZ", "LAC", "LAC", "1", "Active", "00-0033119", "QB", "Jacoby Brissett ARZ", bris),
        _placeholder("ARZ", "LAC", "LAC", "1", "QB"),
        _placeholder("ARZ", "LAC", "LAC", "1", "QB"),
        _placeholder("ARZ", "LAC", "LAC", "1"),
        _player_row("ARZ", "LAC", "LAC", "2", love_status, "00-0041027", "RB", "Jeremiyah Love ARZ", LOVE_METRICS),
        _player_row("ARZ", "LAC", "LAC", "3", "IR", "00-0099999", "DL", "Some Lineman ARZ", LOVE_METRICS),
        ["ARZ", "", "LAC", "LAC", "14", "", "TEAM", "", "", "100.0%"],
        # stray row after the TEAM terminator must be ignored
        _player_row("ARZ", "LAC", "LAC", "", "", "00-0011111", "QB", "Ghost Player ARZ", LOVE_METRICS),
        ["", "", "", "", "", "", "", "", ""],
        # --- BUF block (home vs NYJ) ---
        _player_header("BUF", "BUF", allen_opp),
        _player_row("BUF", "BUF", allen_opp, "1", "Active", "00-0034857", "QB", "Josh Allen BUF", ALLEN_METRICS),
        _placeholder("BUF", "BUF", allen_opp, "1", "QB"),
        ["BUF", "", "BUF", allen_opp, "14", "", "TEAM", "", "", "100.0%"],
    ]


def make_game_grid(week: int = 1) -> list[list[str]]:
    return [
        ["", "", "2026"],
        ["", "", str(week)],
        [],
        ["", "Team", "", "Opp", "Proj Points", "Implied Tot", "Plays", "Plays Adj", "2025", "L5", "Pass Rate", "Pass Rate Adj"],
        ["1", "SEA", "Home", "NE", "24.0", "24.0", "61.9", "0.0", "60", "61", "55.0%", "0.0"],
        ["", "NE", "Away", "SEA", "20.9", "20.5", "61.2", "0.0", "62", "60", "58.0%", "0.0"],
        ["", "", "", "", "", "", "123.1", ""],            # totals row: no team
        [],                                                # separator
        ["2", "LA", "Home", "SF", "26.6", "26.0", "63.9", "0.5", "64", "63", "60.0%", "0.0"],
        ["", "SF", "Away", "LA", "23.5", "22.5", "62.2", "0.5", "61", "62", "52.0%", "0.0"],
        ["", "", "", "", "", "", "126.1", ""],
        [],
        # league summary rows at the bottom of the live tab (text in col B)
        ["", "League Σ/32", "", "", "22.87"],
        ["", "Target 23-25", "", "", "22.63"],
        ["", "Δ vs target", "", "", "+1.1%"],
    ]


def make_output_grid(brissett_rank="30", brissett_ppr="15.0", farrell_rank="72") -> list[list[str]]:
    return [
        ["#", "ID", "Name", "Pos", "Team", "Opp", "PPR", "POS Rank", "DBs", "Aimed Atts"],
        ["418", "00-0036887", "Luke Farrell SF", "TE", "SF", "LA", "1.2", farrell_rank, "0.0", "0.0"],
        ["464", "", "", "", "", "", "", "", "", ""],
        ["30", "00-0034975", "Justice Hill BLT", "RB", "BLT", "IND", "5.9", "47", "0.0", "0.0"],
        ["5", "00-0031234", "Some Receiver KC", "WR", "KC", "LAC", "18.4", "12", "55", "0"],
        ["12", "00-0033119", "Jacoby Brissett ARZ", "QB", "ARZ", "LAC", brissett_ppr, brissett_rank, "62", "33"],
    ]


def make_kicker_grid() -> list[list[str]]:
    return [
        [],
        ["#", "ID", "NAME", "POS", "Team", "FGA", "2025", "2024", "Total FGM", "XPM Rate", "XPM Rate Adj", "Career"],
        ["7", "00-0038567", "Chad Ryland ARZ", "K", "ARZ", "1.9", "1.9", "N/A", "1.6", "0.95", "0.0", "0.9"],
        ["8", "#N/A", "Unknown K", "K", "BUF", "2.0", "", "", "1.7", "0.9", "0.0", ""],
        ["20", "00-0025565", "Nick Folk ATL", "K", "ATL", "2.0", "1.8", "N/A", "1.7", "0.97", "0.0", "0.9"],
    ]


# ---------------------------------------------------------------------------
# parse_player_blocks
# ---------------------------------------------------------------------------


def test_parse_player_blocks_identity_and_filters():
    players = wp.parse_player_blocks(make_player_grid())

    # 3 real rows kept: placeholders, the DL row (position filter), the row
    # after TEAM and the terminator itself are all dropped.
    assert set(players) == {"00-0033119", "00-0041027", "00-0034857"}

    bris = players["00-0033119"]
    assert bris["name"] == "Jacoby Brissett"          # trailing team token stripped
    assert bris["team"] == "ARZ"
    assert bris["pos"] == "QB"
    assert bris["slot"] == 1
    assert bris["status"] == "Active"
    assert bris["home"] == "LAC" and bris["opp"] == "LAC"
    assert bris["home_away"] == "Away"

    love = players["00-0041027"]
    assert love["status"] == "PS" and love["slot"] == 2

    allen = players["00-0034857"]
    assert allen["name"] == "Josh Allen"
    assert allen["home_away"] == "Home" and allen["opp"] == "NYJ"
    # depth = order within team block + position (independent of the global #)
    assert bris["depth"] == 1 and love["depth"] == 1 and allen["depth"] == 1


def test_parse_player_blocks_depth_is_per_team_and_position():
    grid = [
        _player_header("ARZ", "LAC", "LAC"),
        _player_row("ARZ", "LAC", "LAC", "1", "Active", "00-1", "QB", "QB One ARZ", LOVE_METRICS),
        _player_row("ARZ", "LAC", "LAC", "2", "Active", "00-2", "RB", "RB One ARZ", LOVE_METRICS),
        _placeholder("ARZ", "LAC", "LAC", "2", "RB"),
        _player_row("ARZ", "LAC", "LAC", "3", "PS", "00-3", "RB", "RB Two ARZ", LOVE_METRICS),
        _player_row("ARZ", "LAC", "LAC", "4", "Active", "00-4", "WR", "WR One ARZ", LOVE_METRICS),
        ["ARZ", "", "LAC", "LAC", "14", "", "TEAM", "", ""],
        _player_header("BUF", "BUF", "NYJ"),
        # the global # keeps counting (42..) but depth restarts per block
        _player_row("BUF", "BUF", "NYJ", "42", "Active", "00-5", "QB", "QB Buf BUF", LOVE_METRICS),
        _player_row("BUF", "BUF", "NYJ", "43", "Active", "00-6", "RB", "RB Buf BUF", LOVE_METRICS),
        ["BUF", "", "BUF", "NYJ", "14", "", "TEAM", "", ""],
    ]
    players = wp.parse_player_blocks(grid)
    depth = {pid: (p["team"], p["pos"], p["slot"], p["depth"]) for pid, p in players.items()}
    assert depth["00-1"] == ("ARZ", "QB", 1, 1)
    assert depth["00-2"] == ("ARZ", "RB", 2, 1)
    assert depth["00-3"] == ("ARZ", "RB", 3, 2)   # placeholder rows do not consume depth
    assert depth["00-4"] == ("ARZ", "WR", 4, 1)
    assert depth["00-5"] == ("BUF", "QB", 42, 1)
    assert depth["00-6"] == ("BUF", "RB", 43, 1)


def test_parse_player_blocks_metric_labels():
    players = wp.parse_player_blocks(make_player_grid())
    labels = set(players["00-0033119"]["metrics"])

    # reference columns skipped (incl. the weekly-only L1 / 2023)
    assert not labels & {"2025", "L1", "2023"}
    # duplicate "YPA Adj" pair disambiguated by group/section
    assert "Scrm YPA Adj" in labels
    assert "Pass YPA Adj" in labels
    assert "YPA Adj" not in labels
    # duplicate plain "Lock" pair kept distinct (column letter suffix)
    locks = sorted(l for l in labels if l.startswith("Lock"))
    assert len(locks) == 2 and locks[0] != locks[1]
    # Lock / Mkt columns stay inside metrics
    assert "Mkt PassAtt" in labels

    m = players["00-0033119"]["metrics"]
    assert m["DB Share"] == 100.0       # percent parsed to float
    assert m["Sack Adj"] == 0.0
    assert m["Mkt PassAtt"] == 31.5


def test_parse_player_blocks_positions_filter_and_keep_all():
    grid = make_player_grid()
    only_qb = wp.parse_player_blocks(grid, positions=["QB"])
    assert set(only_qb) == {"00-0033119", "00-0034857"}
    everything = wp.parse_player_blocks(grid, positions=[])
    assert "00-0099999" in everything  # DL row kept when no filter


def test_parse_player_blocks_warns_on_header_drift(caplog):
    grid = make_player_grid()
    drifted = ["DAL", "", "DAL", "PHI", "", "", "ID", "Pos", ""] + PLAYER_METRIC_HEADER[:-1] + ["Something Else"]
    grid += [
        drifted,
        _player_row("DAL", "DAL", "PHI", "1", "Active", "00-0055555", "QB", "Dak Prescott DAL", ALLEN_METRICS),
        ["DAL", "", "DAL", "PHI", "14", "", "TEAM", "", ""],
    ]
    with caplog.at_level(logging.WARNING, logger="processing.weekly_projections"):
        players = wp.parse_player_blocks(grid)
    assert "00-0055555" in players
    assert any("header drift" in r.message and "DAL" in r.message for r in caplog.records)


def test_parse_player_blocks_no_drift_warning_when_only_identity_cols_differ(caplog):
    # ARZ and BUF headers differ in cols A-D (team/home/opp) — that's expected.
    with caplog.at_level(logging.WARNING, logger="processing.weekly_projections"):
        wp.parse_player_blocks(make_player_grid())
    assert not any("header drift" in r.message for r in caplog.records)


def test_strip_team_suffix_keeps_roman_numerals():
    assert wp._strip_team_suffix("Robert Griffin III", "WAS") == "Robert Griffin III"
    assert wp._strip_team_suffix("Josh Allen BUF", "BUF") == "Josh Allen"
    assert wp._strip_team_suffix("Puka Nacua LA", "LA") == "Puka Nacua"
    assert wp._strip_team_suffix("Ka'imi Fairbairn HST", "") == "Ka'imi Fairbairn"


# ---------------------------------------------------------------------------
# parse_game_rows
# ---------------------------------------------------------------------------


def test_parse_game_rows_skips_separators_totals_and_summary_rows():
    games = wp.parse_game_rows(make_game_grid())
    assert set(games) == {"SEA", "NE", "LA", "SF"}  # no totals / summary rows
    sea = games["SEA"]
    assert sea["home_away"] == "Home"
    assert sea["opp"] == "NE"
    assert sea["metrics"]["Proj Points"] == 24.0
    assert sea["metrics"]["Plays Adj"] == 0.0
    assert sea["metrics"]["Pass Rate"] == 55.0
    assert "2025" not in sea["metrics"] and "L5" not in sea["metrics"]
    assert games["NE"]["home_away"] == "Away"


def test_game_header_adj_detection_and_forced_home_away_label():
    header = list(make_game_grid()[3])
    header[2] = header[2] or "Home/Away"
    col_map = wp._build_col_map(header, wp.G_METRIC_START)
    flags = {label: is_adj for _, label, is_adj in col_map}
    assert flags["Plays Adj"] is True
    assert flags["Pass Rate Adj"] is True
    assert flags["Plays"] is False
    # identity columns are not metrics
    assert "Team" not in flags and "Opp" not in flags and "Home/Away" not in flags


# ---------------------------------------------------------------------------
# parse_output / parse_kickers
# ---------------------------------------------------------------------------


def test_parse_output_synthesizes_pos_rank_and_skips_blank_rows():
    out = wp.parse_output(make_output_grid())
    assert set(out) == {"00-0036887", "00-0034975", "00-0031234", "00-0033119"}
    wr = out["00-0031234"]
    assert wr["pos_rank"] == "WR12"
    assert wr["name"] == "Some Receiver"
    assert wr["team"] == "KC" and wr["opp"] == "LAC"
    assert wr["ppr"] == 18.4
    assert wr["half_ppr"] is None and wr["ppr_g"] is None
    assert wr["stats"] == {"DBs": 55.0, "Aimed Atts": 0.0}
    assert out["00-0036887"]["pos_rank"] == "TE72"

    from scripts.snapshot_projections import _parse_rank_number
    assert _parse_rank_number(wr["pos_rank"]) == 12


def test_parse_kickers_shape_and_na_ids():
    kickers = wp.parse_kickers(make_kicker_grid())
    assert set(kickers) == {"00-0038567", "00-0025565"}
    k = kickers["00-0038567"]
    assert k["name"] == "Chad Ryland" and k["team"] == "ARZ" and k["pos"] == "K"
    assert k["slot"] is None and k["status"] == ""
    assert k["metrics"]["FGA"] == 1.9
    assert k["metrics"]["XPM Rate Adj"] == 0.0
    assert "2025" not in k["metrics"] and "Career" not in k["metrics"]


# ---------------------------------------------------------------------------
# diff_context
# ---------------------------------------------------------------------------


def test_diff_context():
    prev = wp.parse_player_blocks(make_player_grid(love_status="PS", allen_opp="NYJ"))
    cur = wp.parse_player_blocks(make_player_grid(love_status="Active", allen_opp="MIA"))
    # depth change + add + remove (the raw slot is a global row number and is
    # deliberately ignored — shift it too to prove that)
    cur["00-0041027"]["depth"] = 2
    cur["00-0034857"]["slot"] = 99
    cur["00-0077777"] = {**cur["00-0034857"], "player_id": "00-0077777", "name": "New Guy"}
    del cur["00-0033119"]

    ctx = wp.diff_context(cur, prev)
    assert ctx["status_changes"] == [{
        "player_id": "00-0041027", "name": "Jeremiyah Love", "team": "ARZ", "pos": "RB",
        "old": "PS", "new": "Active",
    }]
    assert [(c["player_id"], c["old"], c["new"]) for c in ctx["slot_changes"]] == [("00-0041027", 1, 2)]
    assert [(c["player_id"], c["old"], c["new"]) for c in ctx["opp_changes"]] == [("00-0034857", "NYJ", "MIA")]
    assert [c["player_id"] for c in ctx["added"]] == ["00-0077777"]
    assert [c["player_id"] for c in ctx["removed"]] == ["00-0033119"]


def test_diff_context_falls_back_to_slot_without_depth():
    prev = {"a": {"player_id": "a", "name": "A", "team": "KC", "pos": "WR", "slot": 3, "status": "Active"}}
    cur = {"a": {"player_id": "a", "name": "A", "team": "KC", "pos": "WR", "slot": 4, "status": "Active"}}
    ctx = wp.diff_context(cur, prev)
    assert [(c["old"], c["new"]) for c in ctx["slot_changes"]] == [(3, 4)]


def test_diff_context_handles_empty_previous():
    cur = wp.parse_player_blocks(make_player_grid())
    ctx = wp.diff_context(cur, {})
    assert len(ctx["added"]) == 3
    assert ctx["removed"] == [] and ctx["status_changes"] == []


# ---------------------------------------------------------------------------
# Snapshot lookups (tmp data root)
# ---------------------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "_base_dir", lambda: tmp_path)
    return tmp_path


def _write_snapshot(season, week, sheet, date_str, players=None, output=None):
    d = wp.snapshot_dir(season, week, sheet, date_str)
    d.mkdir(parents=True, exist_ok=True)
    (d / "players.json").write_text(json.dumps(players or {}), encoding="utf-8")
    if output is not None:
        (d / "output.json").write_text(json.dumps(output), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({
        "season": season, "week": week, "sheet": sheet, "date": date_str,
    }), encoding="utf-8")
    return d


def test_latest_weekly_snapshot_same_sheet_and_before_date(data_root):
    _write_snapshot(2026, 1, "primary", "2026-09-08", {"a": 1})
    _write_snapshot(2026, 1, "primary", "2026-09-09", {"a": 2})
    data, meta = wp.latest_weekly_snapshot(2026, 1, "primary", "players")
    assert data == {"a": 2} and meta["basis_date"] == "2026-09-09" and meta["fallback"] is False
    data, meta = wp.latest_weekly_snapshot(2026, 1, "primary", "players", before_date="2026-09-09")
    assert data == {"a": 1} and meta["basis_date"] == "2026-09-08"
    assert wp.list_snapshot_dates(2026, 1, "primary") == ["2026-09-08", "2026-09-09"]


def test_latest_weekly_snapshot_falls_back_to_other_sheet(data_root):
    # Tuesday: week 2 was started on the secondary sheet. Wednesday: primary
    # flips to week 2 with no earlier primary snapshot for that week.
    _write_snapshot(2026, 2, "secondary", "2026-09-15", {"x": "tue"})
    data, meta = wp.latest_weekly_snapshot(2026, 2, "primary", "players", before_date="2026-09-16")
    assert data == {"x": "tue"}
    assert meta["fallback"] is True
    assert meta["basis_sheet"] == "secondary" and meta["basis_date"] == "2026-09-15"
    # a kind the other sheet doesn't have either → nothing
    assert wp.latest_weekly_snapshot(2026, 2, "primary", "output", before_date="2026-09-16") == (None, None)
    # same-sheet snapshot wins over fallback once it exists
    _write_snapshot(2026, 2, "primary", "2026-09-16", {"x": "wed"})
    data, meta = wp.latest_weekly_snapshot(2026, 2, "primary", "players", before_date="2026-09-17")
    assert data == {"x": "wed"} and meta["fallback"] is False


def test_latest_weekly_snapshot_missing_week(data_root):
    assert wp.latest_weekly_snapshot(2026, 9, "primary", "players") == (None, None)


def test_latest_weekly_snapshot_rejects_unknown_kind(data_root):
    with pytest.raises(ValueError):
        wp.latest_weekly_snapshot(2026, 1, "primary", "teams")


# ---------------------------------------------------------------------------
# Changelog + active pointer
# ---------------------------------------------------------------------------


def test_changelog_header_written_once(data_root):
    changes = [
        {"key": "00-1", "label": "A", "type": "metric_change", "metric": "Sack Adj", "old": 0.0, "new": 0.5},
        {"key": "00-2", "label": "B", "type": "added", "details": "New player entry"},
    ]
    wp.write_weekly_changelog(changes, "2026-09-09", 2026, 1, "primary", "players")
    wp.write_weekly_changelog(changes[:1], "2026-09-10", 2026, 1, "primary", "players")
    wp.write_weekly_changelog([], "2026-09-11", 2026, 1, "primary", "players")  # no-op

    path = data_root / "changelog.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(wp.CHANGELOG_FIELDS)
    assert sum(1 for l in lines if l.startswith("date,")) == 1
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 3
    assert rows[0]["sheet"] == "primary" and rows[0]["week"] == "1" and rows[0]["kind"] == "players"
    assert rows[0]["metric"] == "Sack Adj" and rows[0]["old_value"] == "0.0" and rows[0]["new_value"] == "0.5"
    assert rows[1]["type"] == "added" and rows[1]["details"] == "New player entry"
    assert rows[2]["date"] == "2026-09-10"
    assert wp.read_weekly_changelog() == rows


def test_changelog_flattens_fantasy_changes(data_root):
    change = {
        "key": "00-1", "label": "A", "type": "fantasy_change", "pos": "WR", "team": "KC",
        "ppr_old": 10.0, "ppr_new": 12.5, "rank_old": "WR20", "rank_new": "WR15", "adjusted": True,
    }
    wp.write_weekly_changelog([change], "2026-09-09", 2026, 1, "primary", "output")
    rows = wp.read_weekly_changelog()
    metrics = {r["metric"]: (r["old_value"], r["new_value"], r["details"]) for r in rows}
    assert metrics["PPR"] == ("10.0", "12.5", "")
    assert metrics["POS Rank"] == ("WR20", "WR15", "adjusted")


def test_active_pointer_roundtrip(data_root):
    d = _write_snapshot(2026, 1, "primary", "2026-09-08", {"p": {"name": "X"}}, output={"p": {"ppr": 1}})
    path = wp.write_active_pointer(2026, {"date": "2026-09-08", "sheet": "primary", "week": 1, "dir": wp._rel_dir(d)})
    assert path == data_root / "2026" / "active.json"
    ptr = wp.load_active_pointer(2026)
    assert ptr["sheet"] == "primary" and ptr["week"] == 1 and ptr["dir"] == "2026/wk01/primary/2026-09-08"
    assert ptr["snapshot_at"]
    snap = wp.load_active_snapshot(2026)
    assert snap["players"] == {"p": {"name": "X"}}
    assert snap["output"] == {"p": {"ppr": 1}}
    assert snap["games"] == {} and snap["kickers"] == {}
    assert snap["meta"]["week"] == 1


def test_load_active_snapshot_missing(data_root):
    assert wp.load_active_pointer(2026) is None
    assert wp.load_active_snapshot(2026) is None


# ---------------------------------------------------------------------------
# run_weekly_snapshot end-to-end with a fake gspread client
# ---------------------------------------------------------------------------

SETTINGS = {
    "season": {"phase": "in_season", "year": 2026, "secondary_weekdays": ["Tue"]},
    "projections": {"in_season": {
        "sheets": {"primary": "PRIMARY_ID", "secondary": "SECONDARY_ID"},
        "game_sheet": "Working_Game_Proj",
        "player_sheet": "Working_Player_Proj",
        "output_sheet": "Player_Projections",
        "kicker_sheet": "Working_Kicker_Proj",
        "positions": ["QB", "RB", "WR", "TE", "K"],
    }},
}


class _FakeWS:
    def __init__(self, values):
        self._values = values

    def get_all_values(self):
        return copy.deepcopy(self._values)

    def get_values(self, rng):
        assert rng == "C1:C2"
        return [[self._values[0][2]], [self._values[1][2]]]


class _FakeBook:
    def __init__(self, tabs):
        self._tabs = tabs

    def worksheet(self, title):
        if title not in self._tabs:
            raise KeyError(title)
        return _FakeWS(self._tabs[title])


class FakeGC:
    def __init__(self):
        self.books: dict[str, dict] = {}

    def set(self, key, week=1, **grid_kwargs):
        player_kwargs = {k: v for k, v in grid_kwargs.items() if k in ("brissett_sack_adj", "love_status", "allen_opp")}
        output_kwargs = {k: v for k, v in grid_kwargs.items() if k in ("brissett_rank", "brissett_ppr", "farrell_rank")}
        self.books[key] = {
            "Working_Game_Proj": make_game_grid(week),
            "Working_Player_Proj": make_player_grid(**player_kwargs),
            "Player_Projections": make_output_grid(**output_kwargs),
            "Working_Kicker_Proj": make_kicker_grid(),
        }

    def open_by_key(self, key):
        if key not in self.books:
            raise RuntimeError(f"no such spreadsheet {key}")
        return _FakeBook(self.books[key])


def test_run_weekly_snapshot_baseline_then_diff(data_root):
    gc = FakeGC()
    gc.set("PRIMARY_ID", week=1)
    gc.set("SECONDARY_ID", week=1)

    # Tuesday → both sheets by default; both wk1 → tie → primary active.
    r1 = wp.run_weekly_snapshot(gc, "2026-09-08", settings=SETTINGS)
    assert r1["week"] == 1 and r1["active_sheet"] == "primary"
    assert r1["sheet_weeks"] == {"primary": 1, "secondary": 1}
    assert r1["changes"]["primary"] == {"players": 0, "kickers": 0, "games": 0, "output": 0}
    assert r1["rank_movers"] == [] and r1["errors"] == {}
    snap_dir = data_root / "2026" / "wk01" / "primary" / "2026-09-08"
    assert {p.name for p in snap_dir.iterdir()} == {"players.json", "games.json", "output.json", "kickers.json", "meta.json"}
    meta = json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["counts"] == {"players": 3, "games": 4, "output": 4, "kickers": 2}
    assert meta["diff_basis"]["players"] is None
    assert (data_root / "2026" / "wk01" / "secondary" / "2026-09-08" / "meta.json").exists()
    assert wp.load_active_pointer(2026)["dir"] == "2026/wk01/primary/2026-09-08"
    assert not (data_root / "changelog.csv").exists()  # nothing to log on the baseline

    # Wednesday: primary only (weekday rule); an Adj tweak moves Brissett's
    # rank, Love flips PS → Active, Farrell's rank moves without an Adj.
    gc.set("PRIMARY_ID", week=1, brissett_sack_adj="0.5", love_status="Active",
           brissett_rank="25", brissett_ppr="16.0", farrell_rank="70")
    r2 = wp.run_weekly_snapshot(gc, "2026-09-09", settings=SETTINGS)
    assert list(r2["snapshots"]) == ["primary"]
    assert r2["active_sheet"] == "primary" and r2["week"] == 1
    assert r2["changes"]["primary"]["players"] == 1        # Sack Adj metric change
    assert r2["changes"]["primary"]["output"] == 2         # Brissett + Farrell fantasy changes
    assert r2["diff_basis"]["primary"]["players"] == {
        "basis_sheet": "primary", "basis_date": "2026-09-08", "fallback": False,
    }
    ctx = r2["context_changes"]["primary"]
    assert [(c["name"], c["old"], c["new"]) for c in ctx["status_changes"]] == [("Jeremiyah Love", "PS", "Active")]
    movers = r2["rank_movers"]
    assert len(movers) == 1 and movers[0]["key"] == "00-0033119"
    assert movers[0]["rank_old"] == "QB30" and movers[0]["rank_new"] == "QB25" and movers[0]["adjusted"] is True

    rows = wp.read_weekly_changelog()
    kinds = {(r["kind"], r["type"], r["metric"]) for r in rows}
    assert ("players", "metric_change", "Sack Adj") in kinds
    assert ("output", "fantasy_change", "POS Rank") in kinds
    assert all(r["sheet"] == "primary" and r["week"] == "1" and r["date"] == "2026-09-09" for r in rows)


def test_run_weekly_snapshot_secondary_week_flip_and_fallback(data_root):
    gc = FakeGC()
    gc.set("PRIMARY_ID", week=1)
    gc.set("SECONDARY_ID", week=1)
    wp.run_weekly_snapshot(gc, "2026-09-08", settings=SETTINGS)

    # Tuesday of week 2: the secondary already shows week 2 → it is active.
    gc.set("SECONDARY_ID", week=2, brissett_sack_adj="0.3")
    r_tue = wp.run_weekly_snapshot(gc, "2026-09-15", settings=SETTINGS)
    assert r_tue["sheet_weeks"] == {"primary": 1, "secondary": 2}
    assert r_tue["week"] == 2 and r_tue["active_sheet"] == "secondary"
    assert r_tue["diff_basis"]["secondary"]["players"] is None  # first wk2 snapshot
    ptr = wp.load_active_pointer(2026)
    assert ptr["sheet"] == "secondary" and ptr["dir"] == "2026/wk02/secondary/2026-09-15"

    # Wednesday: primary flipped to week 2; it has no earlier wk2 snapshot so
    # the diff basis is Tuesday's secondary snapshot.
    gc.set("PRIMARY_ID", week=2, brissett_sack_adj="0.6")
    r_wed = wp.run_weekly_snapshot(gc, "2026-09-16", settings=SETTINGS)
    assert r_wed["active_sheet"] == "primary" and r_wed["week"] == 2
    basis = r_wed["diff_basis"]["primary"]["players"]
    assert basis == {"basis_sheet": "secondary", "basis_date": "2026-09-15", "fallback": True}
    assert r_wed["changes"]["primary"]["players"] == 1   # 0.3 -> 0.6
    meta = json.loads((data_root / "2026" / "wk02" / "primary" / "2026-09-16" / "meta.json").read_text(encoding="utf-8"))
    assert meta["diff_basis"]["players"]["fallback"] is True


def test_run_weekly_snapshot_sheet_failure_is_non_fatal(data_root):
    gc = FakeGC()
    gc.set("PRIMARY_ID", week=1)  # secondary missing → open_by_key raises
    r = wp.run_weekly_snapshot(gc, "2026-09-08", settings=SETTINGS, sheets=["primary", "secondary"])
    assert r["active_sheet"] == "primary"
    assert "secondary" in r["errors"] and "primary" not in r["errors"]


def test_run_weekly_snapshot_raises_when_nothing_readable(data_root):
    gc = FakeGC()
    with pytest.raises(RuntimeError):
        wp.run_weekly_snapshot(gc, "2026-09-08", settings=SETTINGS)


def test_run_weekly_snapshot_explicit_sheets_and_ctx(data_root):
    gc = FakeGC()
    gc.set("PRIMARY_ID", week=1)
    gc.set("SECONDARY_ID", week=1)

    class Ctx:
        read_secondary = False

    r = wp.run_weekly_snapshot(gc, "2026-09-08", ctx=Ctx(), settings=SETTINGS)
    assert list(r["snapshots"]) == ["primary"]
    r = wp.run_weekly_snapshot(gc, "2026-09-09", settings=SETTINGS, sheets=["both"])
    assert set(r["snapshots"]) == {"primary", "secondary"}
