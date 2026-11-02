import asyncio

import httpx
import pytest
import yaml
from test_jobs import PNG, setup
from test_timeline import setup_timeline
from test_workspace import workspace


async def test_https_provider_rejects_download_downgrade(tmp_path):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"url": "http://secure.test/file.png"})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db, _, providers, pid, model, service = setup(tmp_path, client)
        connection = db.get("providers", model["provider_id"])
        providers.save_connection(
            {**connection, "base_url": "https://secure.test/v1", "api_key": "download-secret"}
        )
        model["media"].update(url_path="url")
        db.put("models", model)
        job = service.create(pid, {"model_id": model["id"], "prompt": "图片"})
        await service.execute(job["id"])
        assert db.get("jobs", job["id"])["status"] == "failed"
        assert "downgrade" in db.get("jobs", job["id"]).get("error", "")


async def test_failed_download_resumes_without_another_paid_submit(tmp_path):
    attempts, posts = 0, 0

    def handler(request):
        nonlocal attempts, posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"url": "http://media.test/file.png"})
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("download timeout", request=request)
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db, _, _, pid, model, service = setup(tmp_path, client)
        model["media"].update(url_path="url")
        db.put("models", model)
        job = service.create(pid, {"model_id": model["id"], "prompt": "下载"})
        await service.execute(job["id"])
        assert db.get("jobs", job["id"])["status"] == "failed"
        service.resume_download(job["id"])
        await service.tasks[job["id"]]
        assert db.get("jobs", job["id"])["status"] == "ready"
        assert posts == 1


async def test_shutdown_preserves_remote_tasks(tmp_path):
    queried = asyncio.Event()

    async def handler(request):
        queried.set()
        await asyncio.sleep(60)
        return httpx.Response(200, json={"status": "running"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db, _, _, pid, model, service = setup(tmp_path, client)
        model["media"].update(query_path="/tasks/${task_id}")
        db.put("models", model)
        job = service.create(pid, {"model_id": model["id"], "prompt": "远端任务"})
        job.update(status="running", remote_id="remote-id")
        db.put("jobs", job)
        service.start(job["id"])
        await queried.wait()
        await service.close()
        assert db.get("jobs", job["id"])["status"] == "running"


async def test_user_cancellation_while_queued_survives_restart(tmp_path):
    async with httpx.AsyncClient() as client:
        db, _, _, pid, model, service = setup(tmp_path, client)
        await service.limit.acquire()
        await service.limit.acquire()
        await service.limit.acquire()
        await service.limit.acquire()
        job = service.create(pid, {"model_id": model["id"], "prompt": "取消"})
        service.start(job["id"])
        await asyncio.sleep(0)
        service.cancel(job["id"])
        await service.close()
        assert db.get("jobs", job["id"])["status"] == "cancelled"


async def test_yaml_cannot_self_authorize_human_review(tmp_path, video_bytes):
    db, ws, pid, asset, service = await setup_timeline(tmp_path, video_bytes)
    asset["review"] = "accepted"
    db.put("assets", asset)
    service.save(
        pid,
        {
            "clips": [{"asset_id": asset["id"], "in_point": 0, "out_point": 0.8}],
            "subtitles": [],
            "subtitles_reviewed": True,
        },
        None,
    )
    document = ws.read(pid, "production/timeline.yaml")
    data = yaml.safe_load(document["content"])
    data["clips"][0]["in_point"] = 0.2
    ws.apply_changes(
        pid,
        "agent-direct-edit",
        [
            {
                "path": "production/timeline.yaml",
                "content": yaml.safe_dump(data),
                "expected_hash": document["hash"],
            }
        ],
    )
    with pytest.raises(ValueError, match="review"):
        service.create_render(pid)


@pytest.mark.parametrize("path", ["assets\\overwrite.md", "ASSETS/overwrite.md", "Exports/overwrite.md"])
def test_media_paths_are_protected_for_windows_spelling(tmp_path, path):
    ws = workspace(tmp_path)
    pid = ws.create_project("保护素材", "general")["id"]
    with pytest.raises(ValueError, match="Media"):
        ws.apply_changes(pid, "invalid", [{"path": path, "content": "bad", "expected_hash": None}])
