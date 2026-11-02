import base64

import httpx
from app.db import Database
from app.providers import ProviderStore
from app.workspace import Workspace
from test_providers import MemorySecrets

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


def setup(tmp_path, client):
    from app.jobs import JobService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("素材", "general")["id"]
    providers = ProviderStore(db, MemorySecrets(), client)
    connection = providers.save_connection({"name": "custom", "base_url": "http://media.test/v1"})
    model = providers.save_model(
        {
            "name": "image",
            "provider_id": connection["id"],
            "model_id": "image",
            "kind": "image",
            "protocol": "http-template",
            "media": {
                "submit_path": "/images/generations",
                "body": {"prompt": "${prompt}"},
                "base64_path": "data.0.b64_json",
            },
        }
    )
    return db, ws, providers, pid, model, JobService(db, ws, providers, client)


async def test_image_is_ready_only_after_download_and_inspection(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        )
    ) as client:
        db, ws, _, pid, model, service = setup(tmp_path, client)
        job = service.create(pid, {"model_id": model["id"], "prompt": "人物参考"})
        await service.execute(job["id"])
        saved = db.get("jobs", job["id"])
        assert saved["status"] == "ready", saved
        asset = db.get("assets", saved["asset_id"])
        assert asset["review"] == "unreviewed"
        assert (ws.root(pid) / asset["path"]).read_bytes() == PNG
        assert asset["probe"]["streams"][0]["width"] == 1


async def test_ambiguous_submit_is_not_replayed_after_restart(tmp_path):
    def handler(request):
        raise httpx.ReadTimeout("unknown submit outcome", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db, ws, providers, pid, model, service = setup(tmp_path, client)
        job = service.create(pid, {"model_id": model["id"], "prompt": "镜头"})
        await service.execute(job["id"])
        assert db.get("jobs", job["id"])["status"] == "uncertain"
        from app.jobs import JobService

        restarted = JobService(db, ws, providers, client)
        await restarted.execute(job["id"])
        assert db.get("jobs", job["id"])["status"] == "uncertain"


async def test_restart_queries_existing_task_instead_of_submitting(tmp_path):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path.endswith("/tasks/remote-1")
        return httpx.Response(
            200, json={"status": "succeeded", "data": [{"b64_json": base64.b64encode(PNG).decode()}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        db, _, _, pid, model, service = setup(tmp_path, client)
        model["media"].update(
            query_path="/tasks/${task_id}", status_path="status", success_states=["succeeded"]
        )
        db.put("models", model)
        job = service.create(pid, {"model_id": model["id"], "prompt": "续接"})
        job.update(status="running", remote_id="remote-1")
        db.put("jobs", job)
        await service.execute(job["id"])
        assert db.get("jobs", job["id"])["status"] == "ready"


async def test_model_capability_missing_blocks_paid_submit(tmp_path):
    import pytest

    async with httpx.AsyncClient() as client:
        _, _, _, pid, model, service = setup(tmp_path, client)
        with pytest.raises(ValueError, match="reference"):
            service.create(
                pid, {"model_id": model["id"], "prompt": "参考图", "reference_ids": ["not-present"]}
            )
