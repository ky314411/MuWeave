import pytest
from app.db import Database
from app.media import import_asset
from app.workspace import Workspace


async def setup_timeline(tmp_path, video_bytes):
    from app.timeline import TimelineService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("成片", "general")["id"]
    asset = await import_asset(ws, pid, video_bytes, "video", "shot.mp4")
    return db, ws, pid, asset, TimelineService(db, ws)


async def test_unreviewed_and_out_of_bounds_clips_are_rejected(tmp_path, video_bytes):
    db, _, pid, asset, service = await setup_timeline(tmp_path, video_bytes)
    timeline = {
        "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": 0.8}],
        "subtitles": [],
        "subtitles_reviewed": True,
    }
    with pytest.raises(ValueError, match="accepted"):
        service.validate(pid, timeline)
    asset["review"] = "accepted"
    db.put("assets", asset)
    timeline["clips"][0]["out_point"] = 9
    with pytest.raises(ValueError, match="range"):
        service.validate(pid, timeline)


async def test_ffmpeg_export_contains_audio_subtitles_and_manifest(tmp_path, video_bytes):
    from app.media import probe

    db, ws, pid, asset, service = await setup_timeline(tmp_path, video_bytes)
    asset["review"] = "accepted"
    db.put("assets", asset)
    timeline = {
        "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": 0.8}],
        "subtitles": [{"start": 0, "end": 0.7, "text": "第一句对白"}],
        "subtitles_reviewed": True,
        "width": 160,
        "height": 288,
    }
    service.save(pid, timeline, None)
    task = service.create_render(pid)
    await service.render(task["id"])
    result = db.get("renders", task["id"])
    assert result["status"] == "ready", result
    info = await probe(ws.path(pid, result["video_path"]))
    assert any(stream["codec_type"] == "audio" for stream in info["streams"])
    assert 0.7 <= float(info["format"]["duration"]) <= 1.0
    assert "第一句对白" in ws.path(pid, result["subtitle_path"]).read_text(encoding="utf-8")
    assert ws.path(pid, result["manifest_path"]).is_file()


async def test_source_edit_invalidates_review_and_export(tmp_path, video_bytes):
    db, ws, pid, asset, service = await setup_timeline(tmp_path, video_bytes)
    source = ws.read(pid, "episodes/01.md")
    asset.update(review="accepted", dependencies={"episodes/01.md": source["hash"]})
    db.put("assets", asset)
    ws.apply_changes(
        pid,
        "new-version",
        [{"path": "episodes/01.md", "content": "角色改名", "expected_hash": source["hash"]}],
    )
    with pytest.raises(ValueError, match="accepted"):
        service.validate(
            pid,
            {
                "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": 0.8}],
                "subtitles": [],
                "subtitles_reviewed": True,
            },
        )
