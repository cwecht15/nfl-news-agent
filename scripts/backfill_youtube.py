"""One-off backfill: grab all matching YouTube videos since 3/17/2026."""
import sys
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backfill")

from config_loader import get_teams, get_settings, get_youtube_keywords, get_data_dir
from collectors.youtube_collector import scan_channel, download_captions, _parse_subtitle_file

import re

settings = get_settings()
keywords = get_youtube_keywords()
teams = get_teams()

# Override settings for backfill
settings["collection"]["lookback_hours"] = 648  # 27 days (3/17 to 4/13)
settings["collection"]["youtube_max_per_channel"] = 30  # Check more videos

date_str = "backfill-2026-03-17-to-2026-04-13"
transcripts_dir = get_data_dir("transcripts", date_str)
temp_dir = get_data_dir("raw", date_str) / "youtube_temp"
temp_dir.mkdir(exist_ok=True)

all_videos = []
all_transcripts = []
delay = settings["collection"].get("request_delay", 2.0)

logger.info("=" * 60)
logger.info("YouTube Backfill: 2026-03-17 to 2026-04-13")
logger.info("Scanning %d teams, %d keywords", len(teams), len(keywords))
logger.info("=" * 60)

for team in teams:
    abbr = team["abbr"]
    videos = scan_channel(team, keywords, settings, date_str)

    for v in videos:
        all_videos.append({**v, "team": abbr, "team_name": team["name"]})

        # Download captions
        caption_path = download_captions(
            v["video_id"], v["url"], temp_dir, settings
        )

        transcript_text = None
        method = "captions"

        if caption_path:
            transcript_text = _parse_subtitle_file(caption_path)
        else:
            logger.info("  No captions for: %s (skipping Whisper for backfill speed)", v["title"])

        if transcript_text and len(transcript_text.strip()) > 50:
            slug = re.sub(r"[^\w\s-]", "", v["title"])[:60].strip().replace(" ", "_")
            txt_path = transcripts_dir / f"{abbr}_{slug}.txt"
            txt_path.write_text(transcript_text, encoding="utf-8")

            all_transcripts.append({
                "video_id": v["video_id"],
                "title": v["title"],
                "team": abbr,
                "team_name": team["name"],
                "url": v["url"],
                "upload_date": v.get("upload_date", ""),
                "method": method,
                "transcript_file": txt_path.name,
            })

        time.sleep(delay)
    time.sleep(delay)

# Save metadata
out_dir = get_data_dir("raw", date_str)
with open(out_dir / "all_videos.json", "w", encoding="utf-8") as f:
    json.dump(all_videos, f, indent=2, ensure_ascii=False)

with open(out_dir / "all_transcripts.json", "w", encoding="utf-8") as f:
    json.dump(all_transcripts, f, indent=2, ensure_ascii=False)

logger.info("=" * 60)
logger.info("Backfill complete!")
logger.info("Total videos found: %d", len(all_videos))
logger.info("Transcripts downloaded: %d", len(all_transcripts))
logger.info("Videos saved to: %s", out_dir / "all_videos.json")
logger.info("Transcripts saved to: %s", transcripts_dir)
logger.info("=" * 60)

# Print summary by team
print("\n--- Summary by Team ---")
from collections import Counter
team_counts = Counter(v["team"] for v in all_videos)
for abbr, count in sorted(team_counts.items()):
    team_name = next(t["name"] for t in teams if t["abbr"] == abbr)
    print(f"  {abbr} ({team_name}): {count} videos")
