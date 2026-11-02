import asyncio
import json

import yaml
from pydantic import BaseModel, Field

from .db import Database, new_id, now
from .media import probe, refresh_review, run_process
from .workspace import Workspace, atomic_write, digest


class Clip(BaseModel):
    asset_id: str
    in_point: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    out_point: float = Field(gt=0, allow_inf_nan=False)
    volume: float = Field(default=1.0, ge=0, le=3)


class Subtitle(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=500)


class Timeline(BaseModel):
    clips: list[Clip] = Field(default_factory=list, max_length=100)
    subtitles: list[Subtitle] = Field(default_factory=list, max_length=2000)
    subtitles_reviewed: bool = False
    width: int = Field(default=720, ge=160, le=2160, multiple_of=2)
    height: int = Field(default=1280, ge=160, le=3840, multiple_of=2)
    crf: int = Field(default=18, ge=12, le=28)


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    return f"{milliseconds // 3600000:02}:{milliseconds // 60000 % 60:02}:{milliseconds // 1000 % 60:02},{milliseconds % 1000:03}"


def make_srt(subtitles: list[dict]) -> str:
    return (
        "\n\n".join(
            f"{index}\n{timestamp(item['start'])} --> {timestamp(item['end'])}\n{item['text']}"
            for index, item in enumerate(subtitles, 1)
        )
        + "\n"
    )


class TimelineService:
    def __init__(self, db: Database, workspace: Workspace):
        self.db, self.workspace = db, workspace
        self.limit = asyncio.Semaphore(1)
        self.tasks: dict[str, asyncio.Task] = {}
        for record in db.list("renders"):
            if record["status"] in {"queued", "running"}:
                record["status"] = "interrupted"
                db.put("renders", record)

    def load(self, pid: str) -> dict:
        try:
            document = self.workspace.read(pid, "production/timeline.yaml")
            return {
                "timeline": Timeline.model_validate(yaml.safe_load(document["content"])).model_dump(),
                "hash": document["hash"],
            }
        except FileNotFoundError:
            return {"timeline": Timeline().model_dump(), "hash": None}

    def save(self, pid: str, data: dict, expected_hash: str | None) -> dict:
        timeline = Timeline.model_validate(data).model_dump()
        previous = self.load(pid)
        if previous["hash"] and previous["timeline"]["clips"] != timeline["clips"]:
            timeline["subtitles_reviewed"] = False
        self.workspace.apply_changes(
            pid,
            new_id(),
            [
                {
                    "path": "production/timeline.yaml",
                    "content": yaml.safe_dump(timeline, allow_unicode=True, sort_keys=False),
                    "expected_hash": expected_hash,
                }
            ],
        )
        if timeline["subtitles_reviewed"]:
            self.db.put(
                "timeline_reviews",
                {"id": pid, "fingerprint": self.review_fingerprint(timeline), "confirmed_at": now()},
            )
        return self.load(pid)

    @staticmethod
    def review_fingerprint(timeline: dict) -> str:
        return digest(
            json.dumps(
                {"clips": timeline["clips"], "subtitles": timeline["subtitles"]},
                sort_keys=True,
                ensure_ascii=False,
            )
        )

    def validate(self, pid: str, data: dict, require_subtitles: bool = True) -> dict:
        timeline = Timeline.model_validate(data).model_dump()
        if not timeline["clips"]:
            raise ValueError("Timeline has no clips")
        total = 0
        for clip in timeline["clips"]:
            asset = refresh_review(self.workspace, self.db.get("assets", clip["asset_id"]))
            if asset["project_id"] != pid or asset["kind"] != "video" or asset["review"] != "accepted":
                raise ValueError("Only accepted current project video assets can be exported")
            path = self.workspace.path(pid, asset["path"])
            if not path.is_file():
                raise ValueError("Missing asset file")
            duration = float(asset["probe"]["format"].get("duration", 0))
            if clip["out_point"] <= clip["in_point"] or clip["out_point"] > duration + 0.02:
                raise ValueError("Clip range exceeds actual asset duration")
            if not any(stream.get("codec_type") == "audio" for stream in asset["probe"]["streams"]):
                raise ValueError("Video has no audio")
            total += clip["out_point"] - clip["in_point"]
        if not require_subtitles:
            return {"timeline": timeline, "duration": total}
        if not timeline["subtitles_reviewed"]:
            raise ValueError("Review subtitles against the actual selected audio before export")
        try:
            confirmation = self.db.get("timeline_reviews", pid)
        except KeyError:
            confirmation = {}
        if confirmation.get("fingerprint") != self.review_fingerprint(timeline):
            raise ValueError("Human review does not match current clips and subtitles")
        previous_end = 0
        for subtitle in timeline["subtitles"]:
            if (
                subtitle["end"] <= subtitle["start"]
                or subtitle["end"] > total + 0.02
                or subtitle["start"] < previous_end
            ):
                raise ValueError("Subtitle range overlaps or exceeds timeline")
            previous_end = subtitle["end"]
        return {"timeline": timeline, "duration": total}

    def create_render(self, pid: str) -> dict:
        current = self.load(pid)
        validated = self.validate(pid, current["timeline"])
        record = {
            "id": new_id(),
            "project_id": pid,
            "status": "queued",
            "timeline_hash": current["hash"],
            **validated,
            "created_at": now(),
        }
        self.db.put("renders", record)
        return record

    def start(self, render_id: str):
        task = asyncio.create_task(self.render(render_id))
        self.tasks[render_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(render_id, None))

    async def render(self, render_id: str):
        record = self.db.get("renders", render_id)
        async with self.limit:
            try:
                self.validate(record["project_id"], record["timeline"])
                record["status"] = "running"
                self.db.put("renders", record)
                pid, timeline = record["project_id"], record["timeline"]
                directory = self.workspace.path(pid, f"exports/{render_id}")
                directory.mkdir(parents=True, exist_ok=True)
                subtitle_path = directory / "subtitles.srt"
                atomic_write(subtitle_path, make_srt(timeline["subtitles"]))
                args = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
                filters, concat, manifest = [], [], []
                for index, clip in enumerate(timeline["clips"]):
                    asset = self.db.get("assets", clip["asset_id"])
                    path = self.workspace.path(pid, asset["path"])
                    args.extend(
                        [
                            "-ss",
                            str(clip["in_point"]),
                            "-t",
                            str(clip["out_point"] - clip["in_point"]),
                            "-i",
                            str(path),
                        ]
                    )
                    filters.append(
                        f"[{index}:v]setpts=PTS-STARTPTS,scale={timeline['width']}:{timeline['height']}:force_original_aspect_ratio=decrease,pad={timeline['width']}:{timeline['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v{index}]"
                    )
                    filters.append(
                        f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,volume={clip['volume']}[a{index}]"
                    )
                    concat.append(f"[v{index}][a{index}]")
                    manifest.append({"clip": clip, "asset": asset})
                filters.append("".join(concat) + f"concat=n={len(concat)}:v=1:a=1[vout][aout]")
                video_label = "[vout]"
                if timeline["subtitles"]:
                    filters.append("[vout]subtitles=filename=subtitles.srt[vsub]")
                    video_label = "[vsub]"
                output = directory / "video.mp4"
                args.extend(
                    [
                        "-filter_complex",
                        ";".join(filters),
                        "-map",
                        video_label,
                        "-map",
                        "[aout]",
                        "-c:v",
                        "libx264",
                        "-crf",
                        str(timeline["crf"]),
                        "-preset",
                        "medium",
                        "-threads",
                        "2",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-movflags",
                        "+faststart",
                        str(output),
                    ]
                )
                await run_process(*args, timeout=3600, cwd=directory)
                info = await probe(output)
                if abs(float(info["format"]["duration"]) - record["duration"]) > 0.5:
                    raise ValueError("Rendered duration differs from the timeline")
                self.validate(pid, timeline)
                if self.load(pid)["hash"] != record["timeline_hash"]:
                    raise ValueError("Timeline changed during render; review and export again")
                atomic_write(
                    directory / "manifest.json",
                    json.dumps(
                        {
                            "timeline": timeline,
                            "timeline_hash": record["timeline_hash"],
                            "assets": manifest,
                            "probe": info,
                            "created_at": now(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                record.update(
                    status="ready",
                    video_path=f"exports/{render_id}/video.mp4",
                    subtitle_path=f"exports/{render_id}/subtitles.srt",
                    manifest_path=f"exports/{render_id}/manifest.json",
                    probe=info,
                )
            except asyncio.CancelledError:
                record.update(status="interrupted", error="本地渲染中断，可重新导出")
            except Exception as error:
                record.update(status="failed", error=str(error)[:2000])
            finally:
                self.db.put("renders", record)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
