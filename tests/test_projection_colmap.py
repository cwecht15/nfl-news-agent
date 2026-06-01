"""Tests for scripts.snapshot_projections._build_player_col_map.

The Google Sheet has two columns literally named "YPA Adj" (one under the
Scramble group, one under the Pass group). The col-map builder must
disambiguate them by prefixing with the preceding group name, and must skip
historical/reference columns (2025, 2024, Career, L10, L5).
"""

import scripts.snapshot_projections as sp


def _header_with(entries: dict[int, str]) -> list[str]:
    """Build a header row of the right width with names at given indices."""
    row = [""] * (sp.PLAYER_COL_END + 2)
    for idx, name in entries.items():
        row[idx] = name
    return row


def test_duplicate_ypa_adj_disambiguated_by_group():
    start = sp.PLAYER_COL_START
    header = _header_with({
        start + 0: "Scramble",
        start + 1: "YPA Adj",   # belongs to Scramble group
        start + 2: "Pass",
        start + 3: "YPA Adj",   # belongs to Pass group
    })
    col_map = _build = sp._build_player_col_map(header)
    labels = [label for _, label, _ in col_map]

    assert "Scramble YPA Adj" in labels
    assert "Pass YPA Adj" in labels
    # The bare ambiguous label should not survive for the duplicated column.
    assert labels.count("YPA Adj") == 0


def test_adjustment_flag_set():
    # A non-duplicated "YPA Adj" is NOT prefixed (disambiguation only fires
    # for collisions), but its is_adj flag must still be True.
    start = sp.PLAYER_COL_START
    header = _header_with({
        start + 0: "Scramble",
        start + 1: "YPA Adj",
    })
    col_map = sp._build_player_col_map(header)
    by_label = {label: is_adj for _, label, is_adj in col_map}
    assert by_label["Scramble"] is False
    assert by_label["YPA Adj"] is True


def test_skip_headers_excluded():
    start = sp.PLAYER_COL_START
    header = _header_with({
        start + 0: "Scramble",
        start + 1: "2025",          # historical — skipped
        start + 2: "Career",        # reference — skipped
        start + 3: "Pass YPA",
    })
    col_map = sp._build_player_col_map(header)
    labels = [label for _, label, _ in col_map]
    assert "2025" not in labels
    assert "Career" not in labels
    assert "Scramble" in labels
    assert "Pass YPA" in labels


def test_non_duplicate_label_passed_through():
    start = sp.PLAYER_COL_START
    header = _header_with({
        start + 0: "Completions",
        start + 1: "Pass Yards",
    })
    col_map = sp._build_player_col_map(header)
    labels = [label for _, label, _ in col_map]
    assert labels == ["Completions", "Pass Yards"]


def test_column_indices_preserved():
    start = sp.PLAYER_COL_START
    header = _header_with({start + 0: "Scramble", start + 5: "Pass YPA"})
    col_map = sp._build_player_col_map(header)
    indices = {label: idx for idx, label, _ in col_map}
    assert indices["Scramble"] == start + 0
    assert indices["Pass YPA"] == start + 5
