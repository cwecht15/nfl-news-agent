"""dashboard._workflow_dispatch — dispatching refresh.yml and reading its run.

Every test is offline: the PAT and the HTTP client are both patched, so nothing
here can start a real GitHub Actions run.
"""

from pathlib import Path

import pytest
import yaml

from dashboard import _workflow_dispatch as wd
from scripts import run_afternoon

WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "refresh.yml"


class FakeResponse:
    def __init__(self, status_code, json_body=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeRequests:
    """Records calls and replays queued responses."""

    def __init__(self, post=None, get=None):
        self._post = post or FakeResponse(204)
        self._get = get or FakeResponse(200, {"workflow_runs": []})
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append({"url": url, "headers": headers, "json": json})
        if isinstance(self._post, Exception):
            raise self._post
        return self._post

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({"url": url, "headers": headers, "params": params})
        if isinstance(self._get, Exception):
            raise self._get
        return self._get


@pytest.fixture(autouse=True)
def clean_globals():
    wd._reset_for_tests()
    yield
    wd._reset_for_tests()


@pytest.fixture
def api(monkeypatch):
    """Patched PAT + HTTP client; returns the FakeRequests for assertions."""
    fake = FakeRequests()
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    return fake


def _with_post(monkeypatch, response):
    fake = FakeRequests(post=response)
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatch_posts_the_expected_payload(api):
    ok, msg, nonce = wd.dispatch_refresh(["roster", "elevations"])
    assert ok, msg
    assert len(api.posts) == 1
    call = api.posts[0]
    assert call["url"].endswith("/actions/workflows/refresh.yml/dispatches")
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"]["ref"] == wd.BRANCH
    inputs = call["json"]["inputs"]
    assert inputs["targets"] == "roster,elevations"
    assert inputs["nonce"] == nonce
    assert len(nonce) == 8 and int(nonce, 16) >= 0          # 8 hex chars
    assert all(isinstance(v, str) for v in inputs.values())  # GitHub rejects non-strings


def test_dispatch_defaults_to_all_when_given_nothing(api):
    ok, _msg, _nonce = wd.dispatch_refresh([])
    assert ok
    assert api.posts[0]["json"]["inputs"]["targets"] == "all"


def test_dispatch_accepts_a_csv_string(api):
    wd.dispatch_refresh("injuries, inactives")
    assert api.posts[0]["json"]["inputs"]["targets"] == "injuries,inactives"


@pytest.mark.parametrize("status,body,headers,phrase", [
    (401, "", {}, "invalid or has expired"),
    (403, "Resource not accessible by personal access token", {}, "Actions: Read and write"),
    (403, "Workflow does not have 'workflow_dispatch' trigger or is disabled", {}, "disabled"),
    (403, "", {"x-ratelimit-remaining": "0"}, "rate limit"),
    (404, "", {}, "isn't on master yet"),
    (422, "Unexpected inputs", {}, "drifted apart"),
    (500, "boom", {}, "returned 500"),
])
def test_dispatch_error_messages_name_the_actual_fix(monkeypatch, status, body, headers, phrase):
    _with_post(monkeypatch, FakeResponse(status, text=body, headers=headers))
    ok, msg, nonce = wd.dispatch_refresh(["roster"])
    assert not ok and nonce == ""
    assert phrase in msg


def test_dispatch_reports_a_network_error(monkeypatch):
    _with_post(monkeypatch, ConnectionError("no route"))
    ok, msg, _ = wd.dispatch_refresh(["roster"])
    assert not ok and "Network error" in msg


def test_dispatch_without_pat_or_gh_explains_the_secret(monkeypatch):
    monkeypatch.setattr(wd, "_get_pat", lambda: None)
    monkeypatch.setattr(wd, "running_locally", lambda: False)
    ok, msg, _ = wd.dispatch_refresh(["roster"])
    assert not ok
    assert "GITHUB_PAT" in msg and "Actions: Read and write" in msg


def test_local_without_pat_falls_back_to_the_gh_cli(monkeypatch):
    calls = {}

    class P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return P()

    monkeypatch.setattr(wd, "_get_pat", lambda: None)
    monkeypatch.setattr(wd, "running_locally", lambda: True)
    monkeypatch.setattr(wd, "_gh_available", lambda: True)
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    ok, msg, nonce = wd.dispatch_refresh(["roster"])
    assert ok and nonce
    assert calls["cmd"][:5] == ["gh", "workflow", "run", "refresh.yml", "--ref"]
    assert f"nonce={nonce}" in calls["cmd"]
    assert "gh CLI" in msg


def test_cooldown_blocks_the_second_dispatch(api):
    ok, _, _ = wd.dispatch_refresh(["roster"])
    assert ok
    ok2, msg, nonce = wd.dispatch_refresh(["roster"])
    assert not ok2 and nonce == ""
    assert "available again" in msg
    assert len(api.posts) == 1               # the second never reached GitHub
    assert wd.cooldown_remaining() > 0


def test_a_failed_dispatch_does_not_start_the_cooldown(monkeypatch):
    _with_post(monkeypatch, FakeResponse(403, text="Resource not accessible"))
    wd.dispatch_refresh(["roster"])
    assert wd.cooldown_remaining() == 0.0


# ---------------------------------------------------------------------------
# find_run / describe_run
# ---------------------------------------------------------------------------


def _runs(*rows):
    return FakeResponse(200, {"workflow_runs": list(rows)})


def test_find_run_matches_the_nonce_in_the_display_title(monkeypatch):
    wanted = {"id": 2, "display_title": "Refresh roster · abc12345", "status": "in_progress"}
    other = {"id": 1, "display_title": "Refresh all · zzzzzzzz", "status": "completed"}
    fake = FakeRequests(get=_runs(other, wanted))
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    assert wd.find_run("abc12345", since_epoch=0)["id"] == 2
    assert fake.gets[0]["params"]["event"] == "workflow_dispatch"


def test_find_run_falls_back_to_run_started_at(monkeypatch):
    row = {"id": 7, "display_title": "", "status": "queued",
           "created_at": "2026-09-22T13:00:00Z"}
    fake = FakeRequests(get=_runs(row))
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    since = wd._epoch("2026-09-22T12:59:00Z")
    assert wd.find_run("", since_epoch=since)["id"] == 7


def test_find_run_is_none_before_github_registers_it(monkeypatch):
    fake = FakeRequests(get=_runs())
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    assert wd.find_run("abc12345", since_epoch=0) is None


def test_find_run_ignores_older_runs_than_the_dispatch(monkeypatch):
    row = {"id": 3, "display_title": "", "status": "completed",
           "created_at": "2026-09-22T10:00:00Z"}
    fake = FakeRequests(get=_runs(row))
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: fake)
    assert wd.find_run("abc12345", since_epoch=wd._epoch("2026-09-22T13:00:00Z")) is None


@pytest.mark.parametrize("run,state,phrase", [
    (None, "pending", "hasn't registered"),
    ({"status": "queued"}, "queued", "Queued behind"),
    ({"status": "in_progress", "run_started_at": "2026-09-22T13:00:00Z"}, "running", "Running since"),
    ({"status": "completed", "conclusion": "success"}, "done", "Finished"),
    ({"status": "completed", "conclusion": "cancelled"}, "superseded", "Superseded"),
    ({"status": "completed", "conclusion": "failure"}, "failed", "failure"),
])
def test_describe_run_distinguishes_every_state(run, state, phrase):
    got_state, line = wd.describe_run(run)
    assert got_state == state
    assert phrase in line


def test_cancelled_never_reads_as_failed():
    """A third dispatch cancels the pending run in the concurrency group."""
    _state, line = wd.describe_run({"status": "completed", "conclusion": "cancelled"})
    assert "fail" not in line.lower()


# ---------------------------------------------------------------------------
# Contract with the workflow file and the runner
# ---------------------------------------------------------------------------


def test_ui_targets_match_the_runner_targets():
    assert set(wd.TARGETS) == set(run_afternoon.REFRESH_TARGETS)


def test_workflow_file_matches_what_the_dashboard_sends():
    """Guard against a 422 in production by failing here first."""
    spec = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    triggers = spec[True] if True in spec else spec["on"]   # PyYAML reads bare `on:` as True
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) >= {"targets", "date", "nonce", "requested_by"}
    described = inputs["targets"]["description"]
    for target in run_afternoon.REFRESH_TARGETS:
        assert target in described
    # The run name has to carry the nonce or find_run can only guess.
    assert "inputs.nonce" in spec["run-name"]


def test_workflow_shares_the_daily_pipeline_concurrency_group():
    """It must queue behind the daily run, never write data/ alongside it."""
    spec = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert spec["concurrency"]["group"] == "daily-pipeline"
    assert spec["concurrency"]["cancel-in-progress"] is False


def _committed_paths() -> set[str]:
    """The data dirs the commit step force-adds (reading the `for path in` list,
    not the comments around it)."""
    spec = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    step = next(s for s in spec["jobs"]["refresh"]["steps"] if s["name"].startswith("Commit"))
    body = step["run"].replace("\\\n", " ")
    line = next(ln for ln in body.splitlines() if ln.strip().startswith("for path in"))
    return {p for p in line.split("for path in", 1)[1].replace(";", " ").split()
            if p.startswith("data/")}


def test_workflow_commits_only_what_a_refresh_writes():
    paths = _committed_paths()
    assert paths == {"data/roster", "data/injuries", "data/inactives", "data/audit",
                     "data/odds", "data/reports", "data/raw", "data/logs"}
    # A CI pid in the repo would confuse the local runner's liveness check, and
    # no refresh target touches the weekly sheets, the schedule or OurLads.
    assert not {p for p in paths if "pipeline_status" in p}
    assert "data/weekly_projections" not in paths
    assert "data/depth_charts" not in paths
    assert "data/schedule" not in paths


def test_the_api_and_gh_paths_label_themselves_differently(monkeypatch):
    """A run has to say which transport sent it, or "is the PAT working?"
    can't be answered from the Actions list."""
    api = FakeRequests()
    monkeypatch.setattr(wd, "_get_pat", lambda: "tok")
    monkeypatch.setattr(wd, "_requests", lambda: api)
    wd.dispatch_refresh(["roster"])
    assert api.posts[0]["json"]["inputs"]["requested_by"] == "dashboard"

    wd._reset_for_tests()
    sent = {}

    class P:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(wd, "_get_pat", lambda: None)
    monkeypatch.setattr(wd, "running_locally", lambda: True)
    monkeypatch.setattr(wd, "_gh_available", lambda: True)
    monkeypatch.setattr(wd.subprocess, "run", lambda cmd, **kw: (sent.update(cmd=cmd), P())[1])
    wd.dispatch_refresh(["roster"])
    assert "requested_by=dashboard-local" in sent["cmd"]


def test_run_name_carries_the_transport_label():
    spec = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert "inputs.requested_by" in spec["run-name"]
