"""scripts.auto_backfill_youtube — recovery from an untracked-file pull collision.

Nothing here shells out to git: ``run_git`` is replaced with a fake that
returns scripted ``CompletedProcess`` objects, the same way collector tests
replace their fetch functions.
"""

import subprocess

import pytest

from scripts import auto_backfill_youtube as abf


COLLISION_STDERR = (
    "From https://github.com/cwecht15/nfl-news-agent\n"
    " * branch            master     -> FETCH_HEAD\n"
    "error: The following untracked working tree files would be overwritten by merge:\n"
    "\tdata/inactives/espn_athletes.json\n"
    "Please move or remove them before you merge.\n"
    "Aborting\n"
)


def _cp(cmd, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, rc, stdout, stderr)


class FakeGit:
    """Records every git invocation and answers from a per-subcommand script."""

    def __init__(self, pull_results):
        self.calls: list[list[str]] = []
        self._pulls = list(pull_results)

    def __call__(self, cmd, logger):
        self.calls.append(cmd)
        if cmd[1] == "pull":
            return self._pulls.pop(0) if self._pulls else _cp(cmd)
        if cmd[1] == "diff" and "--diff-filter=U" in cmd:
            return _cp(cmd)                 # no autostash conflict
        if cmd[1] == "diff" and "--cached" in cmd:
            return _cp(cmd, rc=1)           # rc 1 == there are staged changes
        return _cp(cmd)

    def pull_count(self):
        return sum(1 for c in self.calls if c[1] == "pull")

    def ran(self, *prefix):
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A fake project root holding the three paths push_to_github stages."""
    (tmp_path / "data" / "raw" / "2026-09-10").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "2026-09-10" / "youtube.json").write_text("[]", encoding="utf-8")
    (tmp_path / "data" / "transcripts" / "2026-09-10").mkdir(parents=True)
    (tmp_path / "data" / "youtube_seen.json").write_text("{}", encoding="utf-8")
    collided = tmp_path / "data" / "inactives" / "espn_athletes.json"
    collided.parent.mkdir(parents=True)
    collided.write_text('{"local": 1}', encoding="utf-8")
    monkeypatch.setattr(abf, "PROJECT_ROOT", tmp_path)
    return tmp_path


def test_untracked_collision_is_moved_aside_and_pull_retried(repo, caplog):
    import logging

    collided = repo / "data" / "inactives" / "espn_athletes.json"
    git = FakeGit([_cp(["git", "pull"], rc=1, stderr=COLLISION_STDERR), _cp(["git", "pull"])])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(abf, "run_git", git)
        with caplog.at_level(logging.WARNING):
            assert abf.push_to_github("2026-09-10", logging.getLogger("t")) is True

    assert not collided.exists(), "the blocking file should have been moved aside"
    assert git.pull_count() == 2, "the pull should be retried exactly once"
    assert git.ran("git", "add", "-f"), "the push should proceed past the pull"
    assert git.ran("git", "push", "origin", "master")
    # The displaced copy is recoverable, so the log must name where it went.
    assert "nfl-pull-collision-" in caplog.text


def test_unrelated_pull_failure_is_not_retried(repo):
    import logging

    git = FakeGit([_cp(["git", "pull"], rc=1, stderr="fatal: couldn't find remote ref master\n")])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(abf, "run_git", git)
        assert abf.push_to_github("2026-09-10", logging.getLogger("t")) is False

    assert git.pull_count() == 1, "a non-collision failure must not be retried"
    assert not git.ran("git", "add", "-f")


def test_parser_handles_both_merge_and_checkout_wording():
    for verb in ("merge", "checkout"):
        text = (
            f"error: The following untracked working tree files would be overwritten by {verb}:\n"
            "\tdata/inactives/espn_athletes.json\n"
            "\tdata/roster/new.json\n"
            "Please move or remove them before you merge.\n"
        )
        assert abf._parse_untracked_collision(text) == [
            "data/inactives/espn_athletes.json",
            "data/roster/new.json",
        ]


def test_parser_returns_nothing_for_unrelated_output():
    assert abf._parse_untracked_collision("fatal: couldn't find remote ref master") == []
    assert abf._parse_untracked_collision("") == []


def test_recovery_refuses_paths_outside_the_repo(repo):
    import logging

    text = (
        "error: The following untracked working tree files would be overwritten by merge:\n"
        "\t../../evil.json\n"
        "Please move or remove them before you merge.\n"
    )
    # Nothing inside the repo survives validation, so there is nothing to recover.
    assert abf._recover_untracked_collision(text, logging.getLogger("t")) is False
