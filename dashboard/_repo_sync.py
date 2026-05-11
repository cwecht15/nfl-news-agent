"""Push small visitor-edited state files to origin/master via GitHub Contents API.

Exists because Streamlit Cloud's container has a writable but ephemeral
filesystem. When a visitor flags a finding or dismisses a transaction
through the UI we write to a JSON file inside the container, but that
file is discarded on every redeploy (a redeploy fires on any master
push, including unrelated code commits). The daily 10:00 UTC cron
commits these files back, but in-flight changes between cron runs are
at risk until they're persisted.

This module provides a manual escape hatch: pages render a "Save to
repo" button that calls `push_file_to_repo()`, which uses the GitHub
Contents API + a fine-grained PAT (stored in Streamlit secrets as
`GITHUB_PAT`) to commit the current file contents to master with a
`[skip ci]` marker. After this push the changes are durable across
redeploys.

`push_flag_store_to_repo` and `push_overrides_to_repo` are thin
wrappers around `push_file_to_repo` for the two files that have UIs
hooked up so far.

The PAT scope only needs:
  - Repository: cwecht15/nfl-news-agent
  - Permissions: Contents: Read and write
Generate at: github.com → Settings → Developer settings →
  Personal access tokens → Fine-grained tokens.
"""

from __future__ import annotations

import base64
from pathlib import Path

import streamlit as st

# Module-level config — adjust if the repo or branch ever changes.
REPO_OWNER = "cwecht15"
REPO_NAME = "nfl-news-agent"
BRANCH = "master"
FLAG_FILE_PATH = "data/flagged_findings.json"
OVERRIDES_FILE_PATH = "data/projections/transaction_overrides.json"

_SECRET_KEY = "GITHUB_PAT"
_GITHUB_API = "https://api.github.com"


def _get_pat() -> str | None:
    """Return the configured PAT or None when the secret isn't set."""
    try:
        return st.secrets.get(_SECRET_KEY)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return None
    except Exception:
        return None


def has_pat_configured() -> bool:
    return bool(_get_pat())


def push_file_to_repo(
    repo_path: str,
    commit_message: str,
    success_label: str = "file",
) -> tuple[bool, str]:
    """Commit `<repo_root>/<repo_path>` to origin/master via GitHub Contents API.

    `repo_path` is the path inside the repo (also used as the local path
    relative to the project root). `commit_message` is the message
    written to the GitHub commit. `success_label` is the noun used in
    the success message rendered to the user (e.g. "flags", "dismissals").

    Returns (ok, human_message).
    """
    token = _get_pat()
    if not token:
        return False, (
            f"Streamlit secret `{_SECRET_KEY}` is not configured. Add a "
            "fine-grained PAT with Contents:write on this repo and try again."
        )

    project_root = Path(__file__).parent.parent
    local_path = project_root / repo_path
    if not local_path.exists():
        return False, f"Local file missing: {repo_path}"

    try:
        content_bytes = local_path.read_bytes()
    except Exception as e:
        return False, f"Could not read local file: {e}"

    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    # Lazy import — `requests` is already in requirements but keeping
    # the import inside the function means a missing dep wouldn't
    # break the page on import time.
    import requests

    url = f"{_GITHUB_API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{repo_path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # GET current SHA — required by the PUT for non-create updates.
    sha: str | None = None
    try:
        r = requests.get(url, headers=headers, params={"ref": BRANCH}, timeout=15)
    except Exception as e:
        return False, f"Network error fetching current file SHA: {e}"
    if r.status_code == 200:
        try:
            sha = r.json().get("sha")
        except Exception:
            sha = None
    elif r.status_code == 404:
        # File not yet on remote — first-time commit, no SHA needed.
        sha = None
    elif r.status_code == 401:
        return False, "PAT rejected (401). Verify the token is valid + has Contents:write."
    elif r.status_code == 403:
        return False, "PAT lacks permission (403). Contents:write on this repo is required."
    else:
        return False, (
            f"GitHub API returned {r.status_code} fetching SHA: "
            f"{r.text[:200]}"
        )

    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        return False, f"Network error during push: {e}"

    if r.status_code in (200, 201):
        try:
            new_sha = r.json().get("commit", {}).get("sha", "")[:7]
        except Exception:
            new_sha = ""
        return True, (
            f"Pushed to origin/master ({new_sha or 'ok'}). Streamlit Cloud "
            f"will redeploy in ~1 minute; {success_label} are now durable."
        )

    if r.status_code == 409:
        return False, (
            "Conflict (409) — master moved while we were pushing. The cron "
            "may have just committed. Click Save again to retry."
        )
    return False, (
        f"GitHub API returned {r.status_code}: {r.text[:200]}"
    )


def push_flag_store_to_repo(
    commit_message: str | None = None,
    flagger_name: str = "",
) -> tuple[bool, str]:
    """Commit `data/flagged_findings.json` to origin/master."""
    suffix = f" — by {flagger_name}" if flagger_name else ""
    msg = commit_message or f"Sync flag store from cloud dashboard{suffix} [skip ci]"
    return push_file_to_repo(FLAG_FILE_PATH, msg, success_label="flags")


def push_overrides_to_repo(
    commit_message: str | None = None,
) -> tuple[bool, str]:
    """Commit `data/projections/transaction_overrides.json` to origin/master."""
    msg = commit_message or "Sync transaction dismissals from cloud dashboard [skip ci]"
    return push_file_to_repo(OVERRIDES_FILE_PATH, msg, success_label="dismissals")
