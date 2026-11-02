import asyncio
import importlib.util
import json
import sys
from pathlib import Path

from .db import Database, new_id, now
from .media import run_process
from .timeline import TimelineService
from .workspace import Workspace


class TranscriptionService:
    def __init__(self, db: Database, ws: Workspace, timeline: TimelineService):
        self.db, self.ws, self.timeline = db, ws, timeline
        self.tasks: dict[str, asyncio.Task] = {}
        self.limit = asyncio.Semaphore(1)
        for record in db.list("transcriptions"):
            if record["status"] in {"queued", "running"}:
                record["status"] = "interrupted"
                db.put("transcriptions", record)

    def create(self, pid: str, model_path: str, language: str = "zh", device: str = "cpu") -> dict:
        if not Path(model_path).is_dir() or not (Path(model_path) / "model.bin").is_file():
            raise ValueError("Select a local converted faster-whisper model directory containing model.bin")
        if importlib.util.find_spec("faster_whisper") is None:
            raise ValueError("Install local transcription dependencies first: uv sync --extra transcription")
        if device not in {"cpu", "cuda"} or len(language) > 8:
            raise ValueError("Invalid transcription device or language")
        current = self.timeline.load(pid)
        self.timeline.validate(pid, current["timeline"], require_subtitles=False)
        record = {
            "id": new_id(),
            "project_id": pid,
            "status": "queued",
            "model_path": str(Path(model_path).resolve()),
            "language": language,
            "device": device,
            "timeline_hash": current["hash"],
            "timeline": current["timeline"],
            "created_at": now(),
        }
        return self.db.put("transcriptions", record)

    def start(self, record_id: str):
        task = asyncio.create_task(self.execute(record_id))
        self.tasks[record_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(record_id, None))

    async def execute(self, record_id: str):
        record = self.db.get("transcriptions", record_id)
        async with self.limit:
            try:
                record["status"] = "running"
                self.db.put("transcriptions", record)
                clips = record["timeline"]["clips"]
                directory = (
                    self.ws.data_dir / ".studio-state" / record["project_id"] / "transcriptions" / record_id
                )
                directory.mkdir(parents=True, exist_ok=True)
                audio = directory / "selected-audio.wav"
                args, filters, concat = ["ffmpeg", "-v", "error", "-nostdin", "-y"], [], []
                for index, clip in enumerate(clips):
                    asset = self.db.get("assets", clip["asset_id"])
                    args.extend(
                        [
                            "-ss",
                            str(clip["in_point"]),
                            "-t",
                            str(clip["out_point"] - clip["in_point"]),
                            "-i",
                            str(self.ws.path(record["project_id"], asset["path"])),
                        ]
                    )
                    filters.append(
                        f"[{index}:a]asetpts=PTS-STARTPTS,aresample=16000,aformat=channel_layouts=mono[a{index}]"
                    )
                    concat.append(f"[a{index}]")
                filters.append("".join(concat) + f"concat=n={len(clips)}:v=0:a=1[aout]")
                await run_process(
                    *args, "-filter_complex", ";".join(filters), "-map", "[aout]", str(audio), timeout=600
                )
                worker = Path(__file__).with_name("transcribe_worker.py")
                result = json.loads(
                    await run_process(
                        sys.executable,
                        str(worker),
                        json.dumps(
                            {
                                "model_path": record["model_path"],
                                "audio_path": str(audio),
                                "language": record["language"],
                                "device": record["device"],
                            }
                        ),
                        timeout=3600,
                    )
                )
                record.update(status="ready", **result)
            except asyncio.CancelledError:
                record.update(status="interrupted", error="本地转写中断")
            except Exception as error:
                record.update(status="failed", error=str(error)[:2000])
            finally:
                self.db.put("transcriptions", record)

    def apply(self, record_id: str) -> dict:
        record = self.db.get("transcriptions", record_id)
        current = self.timeline.load(record["project_id"])
        if record["status"] != "ready":
            raise ValueError("Transcript is not ready")
        if current["hash"] != record["timeline_hash"]:
            raise ValueError("Timeline conflict: selected clips changed during transcription")
        timeline = current["timeline"]
        timeline.update(subtitles=record["segments"], subtitles_reviewed=False)
        return self.timeline.save(record["project_id"], timeline, current["hash"])

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
