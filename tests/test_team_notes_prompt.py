"""Team Notes prompts — offseason text unchanged, in-season variant carries game context."""

from processing import summarizer as sm
from processing.season import SeasonContext


def _offseason_single(team, item_block):
    return f"""Decide whether this item contains real, actionable NFL news for {team}, then either write a team note or skip.

Real news = roster moves, injury updates, contract talks, draft strategy signals, coaching decisions, front-office quotes with substance — and the item must name a {team} player, coach, or executive and say something specific about them.
NOT real news = mock draft rankings, historical trivia, uniform reveals, podcast promos, general previews with no new information, paywalled excerpts that only describe what the article will cover, items where the only {team}-related "subject" is the journalist or outlet, items where you would have to write "the excerpt does not specify any {team} player / decision / detail."

If noteworthy: write 1-2 sentences covering what happened and why it matters, and end with the citation [1].
If NOT noteworthy: respond with exactly "SKIP" and nothing else. When in doubt, SKIP — a missing team note is far better than a bullet that admits it has no {team} content.

Today's item:
{item_block}"""


def test_offseason_single_prompt_unchanged():
    assert sm._team_note_prompt_single("BUF", "[1] x", None) == _offseason_single("BUF", "[1] x")


def test_offseason_multi_prompt_unchanged():
    p = sm._team_note_prompt_multi("BUF", "[1] x", None)
    assert p.startswith("Write a bulleted team note for BUF based only on today's items.")
    assert "ORDERED BY FANTASY IMPACT (most roster-relevant first)" in p
    assert "1. Direct fantasy-relevant role/usage change at a skill position" in p
    assert "Game context" not in p and "THIS WEEK" not in p
    assert p.endswith("Today's items:\n[1] x")


def test_in_season_prompts_carry_game_context():
    line = "Week 3: BUF visits KC on Sunday 2026-09-27 4:25 PM"
    single = sm._team_note_prompt_single("BUF", "[1] x", line)
    multi = sm._team_note_prompt_multi("BUF", "[1] x", line)
    assert line in single and line in multi
    assert "ORDERED BY IMPACT ON THIS WEEK'S PROJECTIONS" in multi
    assert "1. Usage / role changes at a skill position" in multi
    assert "Do NOT write a bullet that merely restates the schedule" in multi
    assert "[1] x" in multi and multi.endswith("{item_block}") is False
    # citations + no-invention rules survive in the in-season variant
    assert "NEVER invent a player's first name" in multi and "[N] citation" in multi


def test_game_lines_none_in_offseason(monkeypatch):
    monkeypatch.setattr("processing.season.get_season_context",
                        lambda *a, **k: SeasonContext("offseason", 2026, None, None, {}, "2026-05-01", "Fri", False))
    assert sm._in_season_game_lines() is None


def test_game_lines_in_season(monkeypatch):
    sched = [
        {"week": 3, "away": "BUF", "home": "KC", "date": "2026-09-27", "day": "Sunday", "time": "4:25 PM"},
        {"week": 3, "away": "NE", "home": "SEA", "date": "2026-09-27", "day": "Sunday", "time": "1:00 PM"},
    ]
    monkeypatch.setattr("processing.season.get_season_context",
                        lambda *a, **k: SeasonContext("in_season", 2026, 3, "primary", {"primary": 3}, "2026-09-23", "Wed", False))
    monkeypatch.setattr("processing.season.load_schedule", lambda *a, **k: sched)
    # These two append " — …" tails from on-disk week files. Neutralize them so
    # this test pins the SCHEDULE formatting rather than silently breaking the
    # day data/odds/2026/wk03.json or data/injuries/2026/wk03.json lands on disk.
    monkeypatch.setattr(sm, "_append_market_context", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_append_opponent_injuries", lambda *a, **k: None)
    lines = sm._in_season_game_lines()
    assert lines["BUF"] == "Week 3: BUF visits KC on Sunday 2026-09-27 4:25 PM"
    assert lines["KC"] == "Week 3: KC hosts BUF on Sunday 2026-09-27 4:25 PM"
    assert lines["DAL"] == "Week 3: DAL is on bye this week"
    assert sm._game_line(lines, "BUF").startswith("Week 3") and sm._game_line(None, "BUF") is None


# ── In-season Team Notes must own the role angle of a status change ──────────

def test_in_season_prompt_requires_the_role_consequence():
    """The old rule said "mention ONLY to add the role angle" and the model read
    that as permission to stay silent — Puka Nacua's hip DNP drew 0 bullets on
    2026-09-19 while sitting at LAR.sources[0]. It is now an obligation."""
    p = sm._team_note_prompt_multi("LAR", "[1] x", "Week 3: LAR visits NYG")
    assert "MUST write that bullet" in p
    assert "USAGE event, not merely a status event" in p
    # The beneficiary, not just the injured player, is a legal bullet subject.
    assert "keyed to the BENEFICIARY" in p
    # Non-skill bullets are capped — LAR spent 3 of 5 on defense that day.
    assert "AT MOST ONE such bullet" in p
    # The status line itself still belongs to the injury sections.
    assert "do NOT restate the STATUS LINE itself" in p


def test_in_season_single_gate_accepts_status_with_role_consequence():
    p = sm._team_note_prompt_single("LAR", "[1] x", "Week 3: LAR visits NYG")
    assert "A status change WITH a role consequence is real news" in p
    # ...and the offseason gate is untouched by that addition.
    assert "A status change WITH a role consequence" not in sm._team_note_prompt_single("LAR", "[1] x", None)


# ── Opponent injury context ─────────────────────────────────────────────────

def _players(*records):
    """On disk `players` is a dict keyed by normalized name, not a list —
    iterating it naively yields name strings and silently finds no injuries."""
    return {r["name"].lower(): r for r in records}


def _week_file():
    return {"teams": {
        "LAR": {"opp": "NYG", "players": {}},
        "NYG": {"opp": "LAR", "players": _players(
            {"name": "Deonte Banks", "pos": "CB", "injury": "Calf", "game_status": "Q"},
            {"name": "Paulson Adebo", "pos": "CB", "injury": "Knee", "game_status": "O"},
            {"name": "Gone Guy", "pos": "S", "injury": "Rib", "game_status": "O", "cleared": "2026-09-19"},
            {"name": "No Designation", "pos": "LB", "injury": "Ankle", "game_status": ""},
        )},
    }}


def test_opponent_injuries_accepts_a_player_list_too(monkeypatch):
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: {"teams": {
        "LAR": {"opp": "NYG", "players": []},
        "NYG": {"opp": "LAR", "players": [
            {"name": "Paulson Adebo", "pos": "CB", "injury": "Knee", "game_status": "O"},
        ]},
    }})
    lines = {"LAR": "Week 3: LAR visits NYG"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["LAR"].endswith("NYG injury report: Paulson Adebo (CB, knee) out")


def test_opponent_injuries_appended_most_severe_first(monkeypatch):
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: _week_file())
    lines = {"LAR": "Week 3: LAR visits NYG"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    # Out before questionable; cleared and undesignated players are left out.
    assert lines["LAR"] == (
        "Week 3: LAR visits NYG — NYG injury report: "
        "Paulson Adebo (CB, knee) out, Deonte Banks (CB, calf) questionable"
    )
    assert "Gone Guy" not in lines["LAR"] and "No Designation" not in lines["LAR"]


def test_opponent_dnp_without_designation_counts_but_not_rest_days(monkeypatch):
    """Adebo practised DNP all week, drew no designation, then went to IR —
    dropping him hid the story that thinned the Giants' secondary. But a
    veteran resting ("NIR - Rest") is load management, not availability."""
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: {"teams": {
        "LAR": {"opp": "NYG", "players": {}},
        "NYG": {"opp": "LAR", "players": _players(
            {"name": "Paulson Adebo", "pos": "CB", "injury": "Knee", "game_status": "",
             "practice": {"2026-09-17": "DNP", "2026-09-18": "DNP"}},
            {"name": "Rested Vet", "pos": "DT", "injury": "NIR - Rest", "game_status": "",
             "practice": {"2026-09-18": "DNP"}},
            {"name": "Full Go", "pos": "WR", "injury": "Hand", "game_status": "",
             "practice": {"2026-09-18": "FP"}},
        )},
    }})
    lines = {"LAR": "Week 3: LAR visits NYG"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["LAR"].endswith("NYG injury report: Paulson Adebo (CB, knee) did not practice")
    assert "Rested Vet" not in lines["LAR"] and "Full Go" not in lines["LAR"]


def test_designated_player_keeps_a_rest_tagged_injury(monkeypatch):
    """The NIR filter gates only the no-designation fallback — a Questionable
    tag is a real signal whatever the injury string says."""
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: {"teams": {
        "HOU": {"opp": "CIN", "players": {}},
        "CIN": {"opp": "HOU", "players": _players(
            {"name": "B.J. Hill", "pos": "DT", "injury": "NIR - Rest / Achilles", "game_status": "Q"},
        )},
    }})
    lines = {"HOU": "Week 3: HOU hosts CIN"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert "B.J. Hill" in lines["HOU"] and "questionable" in lines["HOU"]


def test_opponent_injuries_cap_reports_the_remainder(monkeypatch):
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: {"teams": {
        "GB": {"opp": "NYJ", "players": {}},
        "NYJ": {"opp": "GB", "players": _players(*[
            {"name": f"Player {i}", "pos": "CB", "injury": "Knee", "game_status": "Q"}
            for i in range(7)
        ])},
    }})
    lines = {"GB": "Week 3: GB visits NYJ"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["GB"].count("questionable") == sm._OPP_INJURY_MAX
    assert lines["GB"].endswith("+3 more")


def test_bye_team_never_gets_an_opponent_report(monkeypatch):
    """The collector skips a bye team's page, so `opp` can still hold last
    week's matchup — appending it would invent a game that isn't played."""
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: {"teams": {
        "DAL": {"opp": "PHI", "players": {}},
        "PHI": {"opp": "DAL", "players": _players(
            {"name": "Someone", "pos": "CB", "injury": "Knee", "game_status": "O"},
        )},
    }})
    lines = {"DAL": f"Week 3: DAL{sm._BYE_MARKER}"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["DAL"] == f"Week 3: DAL{sm._BYE_MARKER}"


def test_opponent_injuries_noop_without_week_file(monkeypatch):
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file", lambda *a, **k: None)
    lines = {"LAR": "Week 3: LAR visits NYG"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["LAR"] == "Week 3: LAR visits NYG"


def test_opponent_injuries_never_raises(monkeypatch):
    """Context is a bonus, never a blocker — a malformed file must not kill the run."""
    monkeypatch.setattr("collectors.injury_report_collector.load_week_file",
                        lambda *a, **k: {"teams": {"LAR": "not-a-dict"}})
    lines = {"LAR": "Week 3: LAR visits NYG"}
    sm._append_opponent_injuries(lines, SeasonContext("in_season", 2026, 3, "primary", {}, "2026-09-23", "Wed", False))
    assert lines["LAR"] == "Week 3: LAR visits NYG"
