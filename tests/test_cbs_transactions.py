"""CBS transaction log — the elevation fallback behind ESPN."""

from collectors import cbs_transactions_collector as cbs
from processing.roster_events import normalize_espn_elevations


def _row(team, name, tx):
    return (f'<tr><td><span class="TeamName"><a>{team}</a></span></td>'
            f'<td><span class="CellPlayerName--short"><a>X. {name.split()[-1]}</a></span>'
            f'<span class="CellPlayerName--long"><a>{name}</a></span></td><td>{tx}</td></tr>')


PAGE = (
    '<div class="TableBase"><h4 class="TableBase-title TableBase-title--large">Saturday, October 3, 2026</h4>'
    '<table><thead><tr><th>Team</th><th>Player</th><th>Transaction</th></tr></thead><tbody>'
    + _row("BUF", "Kani Walker", "Active/prac. squad")
    + _row("JAC", "Jalen McLeod", "Active/prac. squad")
    + _row("SF", "Sebastian Valdez", "Active/prac. squad")
    + _row("DEN", "Adam Prentice", "Active/prac. squad")
    + _row("BAL", "Ethan Pocic", "On IR Knee")
    + _row("CAR", "Ja'seem Reed", "Active/prac. squad")
    + '</tbody></table></div>'
    '<div class="TableBase"><h4 class="TableBase-title">Monday, September 28, 2026</h4>'
    '<table><tbody>' + _row("NE", "Old Move", "Active/prac. squad") + '</tbody></table></div>'
)

STATE = {
    "players": {
        "1": {"name": "Kani Walker", "team": "BUF", "status": "PS", "pos": "DB"},
        "2": {"name": "Jalen McLeod", "team": "JAX", "status": "PS", "pos": "LB"},
        "3": {"name": "Sebastian Valdez", "team": "SF", "status": "PS", "pos": "DL"},
        "4": {"name": "Adam Prentice", "team": "DEN", "status": "PS", "pos": "RB"},
        "6": {"name": "Ja'Seem Reed", "team": "CAR", "status": "ACT", "pos": "WR"},
        "7": {"name": "Old Move", "team": "NE", "status": "PS", "pos": "DB"},
    },
    "by_name": {"kani walker": "1", "jalen mcleod": "2", "sebastian valdez": "3",
                "adam prentice": "4", "ja'seem reed": "6", "old move": "7"},
}

ESPN = [
    {"date": "2026-10-03T07:00Z", "team": {"abbreviation": "SF"},
     "description": "Waived DL DL Viliami Fehoko. Signed DL Sebastian Valdez to the practice squad."},
    {"date": "2026-10-03T07:00Z", "team": {"abbreviation": "DEN"},
     "description": "Promoted FB Adam Prentice to the active roster."},
]


def test_parse_page_reads_each_days_table_and_maps_teams():
    rows = cbs.parse_page(PAGE)
    assert {"date": "2026-10-03", "team": "JAX", "name": "Jalen McLeod",
            "transaction": "Active/prac. squad"} in rows
    assert {r["date"] for r in rows} == {"2026-10-03", "2026-09-28"}


def test_only_practice_squad_players_espn_has_not_described_become_elevations():
    got = cbs.elevation_candidates(cbs.parse_page(PAGE), STATE, ESPN, cutoff="2026-10-01")
    names = {(e["team"], e["name"]) for e in got}
    # Valdez (signed TO the PS) and Prentice (promoted) are ESPN's to describe;
    # Reed is already on the 53; Pocic is an IR move; Old Move is out of window.
    assert names == {("BUF", "Kani Walker"), ("JAX", "Jalen McLeod")}
    assert all(e["event_type"] == "ps_elevated" and e["source"] == "cbs" for e in got)


def test_cbs_rows_enter_the_ledger_as_reported():
    got = cbs.elevation_candidates(cbs.parse_page(PAGE), STATE, ESPN, cutoff="2026-10-01")
    events = normalize_espn_elevations(got, "2026-10-03")
    assert {e["confidence"] for e in events} == {"reported"}
    assert {e["source_kind"] for e in events} == {"cbs"}
    espn = normalize_espn_elevations([{"date": "2026-10-03", "team": "DET", "name": "Jalen Mills",
                                       "pos": "DB", "event_type": "ps_elevated"}], "2026-10-03")
    assert espn[0]["confidence"] == "confirmed" and espn[0]["source_kind"] == "espn"


def test_fetch_failure_is_non_fatal():
    class Boom:
        def get(self, *a, **kw):
            raise RuntimeError("down")

    assert cbs.fetch_page(session=Boom()) == ""
    assert cbs.parse_page("") == []


def test_nickname_resolves_to_the_one_ps_player_with_that_last_name_and_initial():
    page = ('<h4 class="TableBase-title">Saturday, October 3, 2026</h4><table><tbody>'
            + _row("ARI", "Cameron Robertson", "Active/prac. squad") + '</tbody></table>')
    state = {"players": {"9": {"name": "Cam Robertson", "team": "ARI", "status": "PS", "pos": "DB"}},
             "by_name": {"cam robertson": "9"}}
    got = cbs.elevation_candidates(cbs.parse_page(page), state, [], cutoff="2026-10-01")
    assert [(e["team"], e["name"]) for e in got] == [("ARI", "Cam Robertson")]
    state["players"]["10"] = {"name": "Chris Robertson", "team": "ARI", "status": "PS", "pos": "WR"}
    assert cbs.elevation_candidates(cbs.parse_page(page), state, [], cutoff="2026-10-01") == []
