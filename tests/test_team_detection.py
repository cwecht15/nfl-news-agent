"""Tests for collectors.rss_collector._detect_teams.

The tricky requirement: team abbreviations are only matched as explicit
uppercase tokens, so "CAR" the team isn't found in "car crash" and "WAS"
isn't found in the word "was". Full names and nicknames match
case-insensitively as whole words.
"""

from collectors.rss_collector import _detect_teams, _contains_alias


def test_full_name_match(fake_teams_by_abbr):
    found = _detect_teams("The Kansas City Chiefs won", fake_teams_by_abbr)
    assert "KC" in found


def test_nickname_match(fake_teams_by_abbr):
    found = _detect_teams("The Eagles signed a tackle", fake_teams_by_abbr)
    assert "PHI" in found


def test_nickname_match_case_insensitive(fake_teams_by_abbr):
    found = _detect_teams("the eagles had a good draft", fake_teams_by_abbr)
    assert "PHI" in found


def test_abbreviation_matched_as_uppercase_token(fake_teams_by_abbr):
    found = _detect_teams("Trade alert: KC gets a pick", fake_teams_by_abbr)
    assert "KC" in found


def test_lowercase_word_car_not_matched_as_team(fake_teams_by_abbr):
    """'car' inside 'car crash' must NOT be detected as Carolina."""
    found = _detect_teams("There was a car crash near the stadium", fake_teams_by_abbr)
    assert "CAR" not in found


def test_lowercase_word_was_not_matched_as_team(fake_teams_by_abbr):
    """'was' must NOT be detected as Washington."""
    found = _detect_teams("He was traded last week", fake_teams_by_abbr)
    assert "WAS" not in found


def test_panthers_nickname_still_matches(fake_teams_by_abbr):
    found = _detect_teams("The Panthers drafted a QB", fake_teams_by_abbr)
    assert "CAR" in found


def test_multiple_teams_detected(fake_teams_by_abbr):
    found = _detect_teams("Chiefs and Eagles agree to a trade", fake_teams_by_abbr)
    assert "KC" in found
    assert "PHI" in found


def test_no_team_returns_empty(fake_teams_by_abbr):
    assert _detect_teams("League announces new schedule format", fake_teams_by_abbr) == []


def test_contains_alias_whole_word_only():
    assert _contains_alias("the eagles flew", "eagles") is True
    # substring inside another word should not match
    assert _contains_alias("beagles are dogs", "eagles") is False
