import asyncio
import base64
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field

from .db import Database, new_id, now
from .media import MAX_MEDIA_BYTES, dependencies, import_asset, refresh_review
from .providers import ProviderStore, render_template, select, validate_url
from .workspace import Workspace


class GenerateRequest(BaseModel):
    model_id: str
    prompt: str = Field(min_length=1, max_length=16000)
    shot_id: str = ""
    reference_ids: list[str] = Field(default_factory=list, max_length=8)
    source_paths: list[str] = Field(default_factory=list, max_length=20)
    parameters: dict = Field(default_factory=dict)


class JobService:
    def __init__(
        self, db: Database, workspace: Workspace, providers: ProviderStore, client: httpx.AsyncClient
    ):
        self.db, self.workspace, self.providers, self.client = db, workspace, providers, client
        self.tasks: dict[str, asyncio.Task] = {}
        self.limit = asyncio.Semaphore(4)
        self.provider_limits: dict[str, asyncio.Semaphore] = {}
        for job in db.list("jobs"):
            if job["status"] == "submitting":
                job.update(status="uncertain", error="应用在提交期间中断，请先核对供应商任务")
                db.put("jobs", job)

    def create(self, project_id: str, data: dict) -> dict:
        request = GenerateRequest.model_validate(data)
        self.workspace.root(project_id)
        model = self.db.get("models", request.model_id)
        if model["kind"] not in {"image", "video"}:
            raise ValueError("Select an image or video model")
        if request.reference_ids and not model["capabilities"].get("reference_images"):
            raise ValueError("Model lacks reference_images capability")
        if model["kind"] == "video" and not model["capabilities"].get("native_audio"):
            raise ValueError("Model lacks native_audio capability")
        if not model["media"].get("submit_path"):
            raise ValueError("Configure media submit endpoint")
        reference_hashes = {}
        for reference_id in request.reference_ids:
            asset = refresh_review(self.workspace, self.db.get("assets", reference_id))
            if asset["project_id"] != project_id or asset["kind"] != "image" or asset["review"] != "accepted":
                raise ValueError("Select accepted project reference images")
            reference_hashes[reference_id] = asset["sha256"]
        job = {
            "id": new_id(),
            "project_id": project_id,
            **request.model_dump(),
            "model_snapshot": model,
            "status": "queued",
            "remote_id": None,
            "dependencies": dependencies(self.workspace, project_id, request.source_paths),
            "reference_hashes": reference_hashes,
            "created_at": now(),
        }
        self.db.put("jobs", job)
        self.db.event(project_id, "job_queued", job_id=job["id"])
        return job

    def start(self, job_id: str):
        if job_id in self.tasks:
            return
        task = asyncio.create_task(self.execute(job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def set_state(self, job: dict, status: str, **fields):
        job.update(status=status, updated_at=now(), **fields)
        self.db.put("jobs", job)
        self.db.event(job["project_id"], "job_updated", job_id=job["id"], status=status)

    def endpoint(self, connection: dict, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise ValueError("Endpoint must be a relative path beginning with /")
        return validate_url(connection["base_url"].rstrip("/") + path)

    async def execute(self, job_id: str):
        job = self.db.get("jobs", job_id)
        if job["status"] in {"ready", "uncertain", "failed", "cancelled"}:
            return
        model = job["model_snapshot"]
        connection = self.db.get("providers", model["provider_id"])
        limit = self.provider_limits.setdefault(connection["id"], asyncio.Semaphore(1))
        async with self.limit, limit:
            try:
                await self._execute(job, model, connection)
            except asyncio.CancelledError:
                status = "uncertain" if job["status"] == "submitting" else "cancelled"
                self.set_state(
                    job, status, remote_cancelled=False, error="本地执行已停止；远端任务可能仍在运行"
                )
            except Exception as error:
                status = "uncertain" if job["status"] == "submitting" else "failed"
                self.set_state(
                    job, status, error=self.providers.redact(f"{type(error).__name__}: {error}")[:1600]
                )

    async def _execute(self, job: dict, model: dict, connection: dict):
        config = model["media"]
        headers = self.providers.headers(connection["id"])
        if job["status"] == "queued":
            references = []
            for reference_id in job["reference_ids"]:
                asset = self.db.get("assets", reference_id)
                path = self.workspace.path(job["project_id"], asset["path"])
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError("Reference image exceeds 16 MiB")
                mime = (
                    "image/jpeg" if path.suffix in {".jpg", ".jpeg"} else "image/" + path.suffix.lstrip(".")
                )
                references.append(
                    f"data:{mime};base64,"
                    + base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode()
                )
            variables = {
                **model["settings"],
                **job["parameters"],
                "model": model["model_id"],
                "prompt": job["prompt"],
                "references": references,
                "request_id": job["id"],
                "content": [
                    {"type": "text", "text": job["prompt"]},
                    *[{"type": "image_url", "image_url": {"url": reference}} for reference in references],
                ],
            }
            body = render_template(config.get("body", {}), variables)
            endpoint = self.endpoint(connection, config["submit_path"])
            self.set_state(job, "submitting")
            response = await self.client.post(endpoint, json=body, headers=headers, timeout=120)
            if 400 <= response.status_code < 500:
                self.set_state(job, "failed", error=f"Provider rejected request: HTTP {response.status_code}")
                return
            response.raise_for_status()
            result = response.json()
            remote_id = select(result, config.get("task_id_path", "")) if config.get("task_id_path") else None
            if remote_id:
                self.set_state(job, "submitted", remote_id=str(remote_id))
            else:
                job["result"] = result
                self.set_state(job, "downloading")
        if job.get("remote_id"):
            self.set_state(job, "running")
            query = render_template(
                config.get("query_path", ""), {"task_id": quote(job["remote_id"], safe="")}
            )
            if not query:
                raise ValueError("Configure query_path for asynchronous tasks")
            for _ in range(3600):
                response = await self.client.get(
                    self.endpoint(connection, query), headers=headers, timeout=45
                )
                response.raise_for_status()
                result = response.json()
                status = select(result, config.get("status_path", "status"))
                if status in config.get("failure_states", ["failed", "cancelled"]):
                    raise ValueError(f"Remote generation ended: {status}")
                if status in config.get("success_states", ["succeeded", "completed"]):
                    job["result"] = result
                    self.set_state(job, "downloading")
                    break
                await asyncio.sleep(3)
            else:
                raise ValueError("Polling window exceeded; resume using the saved remote task id")
        result = job.get("result", {})
        encoded = select(result, config["base64_path"]) if config.get("base64_path") else None
        if encoded:
            if len(encoded) > MAX_MEDIA_BYTES * 4 // 3 + 4:
                raise ValueError("Media response exceeds limit")
            content = base64.b64decode(encoded, validate=True)
        else:
            url = select(result, config.get("url_path", "url"))
            if not isinstance(url, str):
                raise ValueError("No media URL/base64 at configured response path")
            validate_url(url)
            # Credentials belong to the provider endpoint, never to arbitrary result URLs.
            download_headers = (
                headers if urlsplit(url).netloc == urlsplit(connection["base_url"]).netloc else {}
            )
            chunks, total = [], 0
            async with self.client.stream("GET", url, headers=download_headers, timeout=120) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise ValueError("Media download exceeds 256 MiB")
                    chunks.append(chunk)
            content = b"".join(chunks)
        self.set_state(job, "checking")
        extension = ".png" if model["kind"] == "image" else ".mp4"
        asset = await import_asset(
            self.workspace,
            job["project_id"],
            content,
            model["kind"],
            job.get("shot_id") or job["id"] + extension,
            job_id=job["id"],
            shot_id=job.get("shot_id", ""),
            prompt=job["prompt"],
            model_id=job["model_id"],
            dependencies=job["dependencies"],
            reference_hashes=job["reference_hashes"],
        )
        self.set_state(job, "ready", asset_id=asset["id"])

    def attach(self, job_id: str, remote_id: str):
        job = self.db.get("jobs", job_id)
        if job["status"] not in {"uncertain", "failed", "cancelled"} or not remote_id.strip():
            raise ValueError("Task cannot attach a remote id")
        self.set_state(job, "submitted", remote_id=remote_id.strip(), error=None)
        self.start(job_id)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
