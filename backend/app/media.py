import asyncio
import hashlib
import json
from pathlib import Path

from .db import new_id, now
from .workspace import Workspace, digest

MAX_MEDIA_BYTES = 256 * 1024 * 1024


async def run_process(*args: str, timeout: int = 120, cwd: Path | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise ValueError(f"{Path(args[0]).name} failed: {stderr.decode(errors='replace')[-1200:]}")
    return stdout


async def probe(path: Path) -> dict:
    result = json.loads(
        await run_process(
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path), timeout=30
        )
    )
    if not any(stream.get("codec_type") == "video" for stream in result.get("streams", [])):
        raise ValueError("Media has no visual stream")
    return result


def dependencies(ws: Workspace, project_id: str, paths: list[str]) -> dict:
    return {path: ws.read(project_id, path)["hash"] for path in paths}


def refresh_review(ws: Workspace, asset: dict) -> dict:
    stale = False
    for path, expected in asset.get("dependencies", {}).items():
        try:
            actual = ws.read(asset["project_id"], path)["hash"]
        except FileNotFoundError:
            actual = None
        stale = stale or expected != actual
    for reference_id, expected in asset.get("reference_hashes", {}).items():
        reference = ws.db.get("assets", reference_id)
        stale = (
            stale or reference["sha256"] != expected or refresh_review(ws, reference)["review"] != "accepted"
        )
    if stale and asset["review"] != "needs_review":
        asset["review"] = "needs_review"
        ws.db.put("assets", asset)
    return asset


async def import_asset(
    ws: Workspace, project_id: str, content: bytes, kind: str, filename: str, **metadata
) -> dict:
    if not content or len(content) > MAX_MEDIA_BYTES:
        raise ValueError("Media must contain 1–256 MiB")
    extension = Path(filename).suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp"} if kind == "image" else {".mp4", ".mov", ".webm"}
    if extension not in allowed:
        extension = ".png" if kind == "image" else ".mp4"
    asset_id = new_id()
    relative = f"assets/{asset_id}{extension}"
    path = ws.path(project_id, relative)
    await asyncio.to_thread(path.write_bytes, content)
    try:
        info = await probe(path)
        if kind == "video" and not any(stream.get("codec_type") == "audio" for stream in info["streams"]):
            raise ValueError("Video must contain an audio track for the native-audio workflow")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    asset = {
        "id": asset_id,
        "project_id": project_id,
        "path": relative,
        "kind": kind,
        "label": Path(filename).name[:120],
        "sha256": hashlib.sha256(content).hexdigest(),
        "review": "unreviewed",
        "probe": info,
        "created_at": now(),
        **metadata,
    }
    ws.db.put("assets", asset)
    return asset


def storyboard_hash(ws: Workspace, pid: str, content: dict) -> str:
    return digest(json.dumps(content, sort_keys=True, ensure_ascii=False))
