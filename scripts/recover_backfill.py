"""Recover all_videos.json + all_transcripts.json for a backfill whose
Streamlit run was interrupted before the post-loop JSON dump.

When the dashboard backfill completes per-team work in `as_completed` but
the browser session dies (laptop sleep, tab close, server restart), the
.vtt captions and parsed .txt transcripts are already on disk — only the
final two JSON manifest files are missing. Without them, the
"Load previous backfill" expander can't see the work, and `bf_results`
in session state is gone.

This script reads the surviving .vtt + .txt files, fetches per-video
metadata via yt-dlp (cheap — no caption/audio re-download), and writes
the manifest JSONs in the same shape the dashboard expects.

Usage:
    python scripts/recover_backfill.py backfill-2026-05-01-to-2026-05-06
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from config_loader import get_data_dir, get_teams


def _slug(title: str) -> str:
    """Replicate the backfill's slug logic for matching filenames."""
    return re.sub(r"[^\w\s-]", "", title)[:60].strip().replace(" ", "_")


def main(date_str: str) -> int:
    raw_dir = get_data_dir("raw", date_str)
    temp_dir = raw_dir / "youtube_temp"
    transcripts_dir = get_data_dir("transcripts", date_str)

    if not temp_dir.exists():
        print(f"ERROR: {temp_dir} not found", file=sys.stderr)
        return 1
    if not transcripts_dir.exists():
        print(f"ERROR: {transcripts_dir} not found", file=sys.stderr)
        return 1

    vtt_files = sorted(temp_dir.glob("*.vtt"))
    if not vtt_files:
        print(f"ERROR: no .vtt files in {temp_dir}", file=sys.stderr)
        return 1

    txt_files = sorted(transcripts_dir.glob("*.txt"))
    print(f"Found {len(vtt_files)} .vtt files and {len(txt_files)} .txt files")

    teams = get_teams()
    team_name_by_abbr = {t["abbr"]: t["name"] for t in teams}

    txt_by_slug: dict[str, list[Path]] = {}
    for p in txt_files:
        stem = p.stem
        if "_" not in stem:
            continue
        abbr, slug = stem.split("_", 1)
        txt_by_slug.setdefault(slug, []).append(p)

    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }

    all_videos = []
    all_transcripts = []
    matched = 0
    unmatched_videos = 0
    no_txt = 0

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for i, vtt in enumerate(vtt_files, 1):
            video_id = vtt.stem.split(".")[0]
            url = f"https://www.youtube.com/watch?v={video_id}"
            print(f"[{i}/{len(vtt_files)}] {video_id} ...", end=" ", flush=True)
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                print(f"metadata fetch failed: {e}")
                unmatched_videos += 1
                continue

            title = info.get("title", "")
            upload_date = info.get("upload_date", "")
            duration = info.get("duration") or 0
            timestamp = info.get("timestamp")
            published_at = ""
            if timestamp:
                from datetime import datetime, timezone
                published_at = datetime.fromtimestamp(
                    timestamp, tz=timezone.utc
                ).isoformat()

            slug = _slug(title)
            txt_candidates = txt_by_slug.get(slug, [])

            if not txt_candidates:
                print(f"no .txt match for slug='{slug}' (title={title!r})")
                all_videos.append({
                    "video_id": video_id,
                    "title": title,
                    "url": url,
                    "upload_date": upload_date,
                    "published_at": published_at,
                    "duration": duration,
                    "team": "",
                    "team_name": "",
                })
                no_txt += 1
                continue

            txt_path = txt_candidates[0]
            abbr = txt_path.stem.split("_", 1)[0]
            team_name = team_name_by_abbr.get(abbr, abbr)

            all_videos.append({
                "video_id": video_id,
                "title": title,
                "url": url,
                "upload_date": upload_date,
                "published_at": published_at,
                "duration": duration,
                "team": abbr,
                "team_name": team_name,
            })
            all_transcripts.append({
                "video_id": video_id,
                "title": title,
                "team": abbr,
                "team_name": team_name,
                "url": url,
                "upload_date": upload_date,
                "method": "captions",
            })
            matched += 1
            print(f"{abbr}  {title[:60]}")

    videos_path = raw_dir / "all_videos.json"
    transcripts_path = raw_dir / "all_transcripts.json"
    with open(videos_path, "w", encoding="utf-8") as f:
        json.dump(all_videos, f, indent=2, ensure_ascii=False)
    with open(transcripts_path, "w", encoding="utf-8") as f:
        json.dump(all_transcripts, f, indent=2, ensure_ascii=False)

    print()
    print("=" * 60)
    print(f"Wrote {videos_path}  ({len(all_videos)} videos)")
    print(f"Wrote {transcripts_path}  ({len(all_transcripts)} transcripts)")
    print(f"  matched:    {matched}")
    print(f"  no .txt:    {no_txt}  (caption present but transcript not parsed)")
    print(f"  fetch fail: {unmatched_videos}")
    print("=" * 60)
    print("Reload the dashboard Transcripts tab → 'Load previous backfill'.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(
            "Usage: python scripts/recover_backfill.py "
            "backfill-YYYY-MM-DD-to-YYYY-MM-DD",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
