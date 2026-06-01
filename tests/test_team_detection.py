"""Tests for collectors.rss_collector._detect_teams.

The tricky requirement: team abbreviations are only matched as explicit
uppercase tokens, so "CAR" the team isn't found in "car crash" and "WAS"
isn't found in the word "was". Full names and nicknames match
case-insensitively as whole words.
"""

from collectors.rss_collector import (
    _detect_teams,
    _contains_alias,
    _strip_caption_boilerplate,
    detect_teams_for_item,
)


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


# ── Over-tagging fix: body-based detection should not pollute team pools ──

def test_allow_abbr_flag_gates_bare_abbreviations(fake_teams_by_abbr):
    assert "MIN" in _detect_teams("MIN gets the ball", fake_teams_by_abbr)
    # In long bodies bare abbreviations are noise — disabled.
    assert "MIN" not in _detect_teams("MIN gets the ball", fake_teams_by_abbr, allow_abbr=False)


def test_strip_caption_boilerplate_removes_getty_dateline():
    raw = ("MINNEAPOLIS, MINNESOTA - OCTOBER 19, 2025: A.J. Brown of the Eagles "
           "warms up. (Photo by John Doe/Getty Images) The Patriots are close to a deal.")
    out = _strip_caption_boilerplate(raw)
    assert "MINNEAPOLIS, MINNESOTA - OCTOBER 19" not in out
    assert "Photo by" not in out
    assert "Patriots are close to a deal" in out


def test_sbn_feed_body_mention_does_not_add_other_team(fake_teams_by_abbr):
    """A Bills blog post that mentions the Vikings in the body stays BUF-only."""
    teams = detect_teams_for_item(
        title="Bills offensive line takes a step forward",
        summary="Camp notes from Orchard Park.",
        body="The unit looked sharp, even against the Minnesota Vikings last fall.",
        feed_teams=["BUF"],
        teams_by_abbr=fake_teams_by_abbr,
    )
    assert teams == ["BUF"]


def test_sbn_feed_title_cross_team_is_added(fake_teams_by_abbr):
    teams = detect_teams_for_item(
        title="Bills vs Vikings: Week 1 preview",
        summary="", body="", feed_teams=["BUF"], teams_by_abbr=fake_teams_by_abbr,
    )
    assert "BUF" in teams and "MIN" in teams


def test_national_single_body_mention_not_tagged(fake_teams_by_abbr):
    """National feed: Eagles in title, a lone 'Vikings' deep in body → PHI only."""
    teams = detect_teams_for_item(
        title="Eagles trade A.J. Brown talks heat up",
        summary="Philadelphia weighs offers.",
        body="One rumored suitor floated a swap involving the Vikings, sources said.",
        feed_teams=[],
        teams_by_abbr=fake_teams_by_abbr,
    )
    assert "PHI" in teams
    assert "MIN" not in teams


def test_national_repeated_lead_mention_is_tagged(fake_teams_by_abbr):
    teams = detect_teams_for_item(
        title="Around the league: roster moves",
        summary="",
        body="The Vikings made news today. The Vikings signed a veteran guard.",
        feed_teams=[],
        teams_by_abbr=fake_teams_by_abbr,
    )
    assert "MIN" in teams


def test_national_title_mention_is_tagged(fake_teams_by_abbr):
    teams = detect_teams_for_item(
        title="Vikings hire new general manager",
        summary="", body="", feed_teams=[], teams_by_abbr=fake_teams_by_abbr,
    )
    assert "MIN" in teams


def test_getty_dateline_on_other_team_story_stays_clean(fake_teams_by_abbr):
    """Patriots story whose lead is a Getty caption naming MIN/PHI → only NE."""
    teams = detect_teams_for_item(
        title="Patriots nearing a deal",
        summary="",
        body=("MINNEAPOLIS, MINNESOTA - OCTOBER 19, 2025: A.J. Brown of the Eagles "
              "warms up. (Photo by John Doe/Getty Images) New England is close."),
        feed_teams=["NE"],
        teams_by_abbr=fake_teams_by_abbr,
    )
    assert teams == ["NE"]
