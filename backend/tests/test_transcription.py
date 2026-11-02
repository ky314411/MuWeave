import pytest
from test_timeline import setup_timeline


async def test_transcript_applies_actual_timestamps_and_requires_human_review(tmp_path, video_bytes):
    from app.transcription import TranscriptionService

    db, ws, pid, asset, timeline = await setup_timeline(tmp_path, video_bytes)
    asset["review"] = "accepted"
    db.put("assets", asset)
    saved = timeline.save(
        pid,
        {
            "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": 0.8}],
            "subtitles": [],
            "subtitles_reviewed": True,
        },
        None,
    )
    service = TranscriptionService(db, ws, timeline)
    db.put(
        "transcriptions",
        {
            "id": "transcript-1",
            "project_id": pid,
            "status": "ready",
            "timeline_hash": saved["hash"],
            "segments": [{"start": 0.12, "end": 0.68, "text": "实际音轨对白"}],
        },
    )
    service.apply("transcript-1")
    result = timeline.load(pid)["timeline"]
    assert result["subtitles"] == [{"start": 0.12, "end": 0.68, "text": "实际音轨对白"}]
    assert result["subtitles_reviewed"] is False
    with pytest.raises(ValueError, match="[Rr]eview"):
        timeline.create_render(pid)


async def test_transcript_cannot_overwrite_changed_timeline(tmp_path, video_bytes):
    from app.transcription import TranscriptionService

    db, ws, pid, _, timeline = await setup_timeline(tmp_path, video_bytes)
    db.put(
        "transcriptions",
        {"id": "stale", "project_id": pid, "status": "ready", "timeline_hash": "old-hash", "segments": []},
    )
    with pytest.raises(ValueError, match="conflict"):
        TranscriptionService(db, ws, timeline).apply("stale")
