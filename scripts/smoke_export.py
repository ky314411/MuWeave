"""Render synthetic media to verify duration and artifacts; this is not a drama quality evaluation."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import Database  # noqa: E402
from app.media import import_asset, run_process  # noqa: E402
from app.timeline import TimelineService  # noqa: E402
from app.workspace import Workspace  # noqa: E402


async def main(duration: float, data_dir: Path | None = None):
    if not 1 <= duration <= 180:
        raise ValueError("Duration must be 1–180 seconds")
    data = data_dir or ROOT / "app-state" / "smoke"
    data.mkdir(parents=True, exist_ok=True)
    db = Database(data / "app-state" / "muweave.sqlite3")
    ws = Workspace(data, db)
    project = ws.create_project("合成视频技术验收", "general")
    source = data / "synthetic-source.mp4"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x263f31:s=320x576:r=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        str(duration),
        "-c:v",
        "libx264",
        "-threads",
        "2",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(source),
    )
    asset = await import_asset(ws, project["id"], source.read_bytes(), "video", "synthetic.mp4")
    asset["review"] = "accepted"
    db.put("assets", asset)
    timeline = TimelineService(db, ws)
    timeline.save(
        project["id"],
        {
            "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": duration}],
            "subtitles": [
                {
                    "start": 0,
                    "end": min(4, duration),
                    "text": "Synthetic technical test - not generated drama",
                }
            ],
            "subtitles_reviewed": True,
            "width": 320,
            "height": 576,
        },
        None,
    )
    task = timeline.create_render(project["id"])
    await timeline.render(task["id"])
    result = db.get("renders", task["id"])
    if result["status"] != "ready":
        raise RuntimeError(result.get("error", result["status"]))
    print(
        json.dumps(
            {
                "status": result["status"],
                "actual_duration": result["probe"]["format"]["duration"],
                "video": str(ws.path(project["id"], result["video_path"])),
                "subtitle": str(ws.path(project["id"], result["subtitle_path"])),
                "manifest": str(ws.path(project["id"], result["manifest_path"])),
                "quality_evaluated": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--data-dir", type=Path, help="Use a writable container directory, e.g. /data/smoke")
    args = parser.parse_args()
    asyncio.run(main(args.duration, args.data_dir))
