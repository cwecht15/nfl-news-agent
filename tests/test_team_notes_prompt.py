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
    lines = sm._in_season_game_lines()
    assert lines["BUF"] == "Week 3: BUF visits KC on Sunday 2026-09-27 4:25 PM"
    assert lines["KC"] == "Week 3: KC hosts BUF on Sunday 2026-09-27 4:25 PM"
    assert lines["DAL"] == "Week 3: DAL is on bye this week"
    assert sm._game_line(lines, "BUF").startswith("Week 3") and sm._game_line(None, "BUF") is None
