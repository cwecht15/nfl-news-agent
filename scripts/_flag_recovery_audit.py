"""One-shot audit: how many flags are recoverable from git history."""

import io
import json
import subprocess
import sys
from pathlib import Path


def load_flags_from_str(text):
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    return data.get("flags", [])


def at_commit(ref):
    r = subprocess.run(
        ["git", "show", f"{ref}:data/flagged_findings.json"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        return []
    return load_flags_from_str(r.stdout)


def main():
    repo_root = Path(__file__).resolve().parent.parent
    current = load_flags_from_str(
        io.open(repo_root / "data/flagged_findings.json", encoding="utf-8").read()
    )
    cur_ids = {f.get("id") for f in current if f.get("id")}
    print(f"current file:     {len(current)} flags")

    # Walk every commit that ever touched the file.
    r = subprocess.run(
        ["git", "log", "--format=%H %s", "--", "data/flagged_findings.json"],
        capture_output=True, text=True, encoding="utf-8",
    )
    commits = [line.split(" ", 1) for line in r.stdout.splitlines() if line.strip()]
    print(f"commits touching the file: {len(commits)}")

    all_hist_ids = set()
    union_by_id = {}  # id -> most-recent historical record
    for sha, subject in commits:
        flags = at_commit(sha)
        ids = {f.get("id") for f in flags if f.get("id")}
        missing_here = ids - cur_ids
        print(f"  {sha[:7]} | {len(flags):3d} flags | missing-from-current: {len(missing_here):3d} | {subject}")
        for f in flags:
            fid = f.get("id")
            if not fid:
                continue
            all_hist_ids.add(fid)
            # Keep the LATEST historical version (git log is reverse-chrono,
            # so first time we see this id is its newest committed form).
            if fid not in union_by_id:
                union_by_id[fid] = f

    recoverable = all_hist_ids - cur_ids
    print()
    print(f"unique flag IDs ever committed: {len(all_hist_ids)}")
    print(f"recoverable (in history, not in current): {len(recoverable)}")

    if recoverable:
        sample = list(recoverable)[:5]
        print("sample recoverable flags:")
        for fid in sample:
            f = union_by_id[fid]
            print(f"  - id={fid} date={f.get('report_date')} section={f.get('section')} content={(f.get('content') or '')[:80]!r}")


if __name__ == "__main__":
    main()
