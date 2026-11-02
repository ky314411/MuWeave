import asyncio
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from keyring.errors import KeyringError
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent import RECIPES, AgentService
from .db import Database, new_id, now
from .jobs import JobService
from .media import MAX_MEDIA_BYTES, dependencies, import_asset, refresh_review
from .providers import MEDIA_PRESETS, ProviderStore
from .timeline import TimelineService
from .tools import validate_project
from .workspace import Workspace


class ProjectInput(BaseModel):
    title: str
    profile: Literal["general", "rules"] = "general"


class ChangesInput(BaseModel):
    operation_id: str
    changes: list[dict] = Field(min_length=1, max_length=40)


class RunInput(BaseModel):
    model_id: str
    prompt: str
    recipe: str = "write"
    current_file: str | None = None
    parent_id: str | None = None


class TimelineInput(BaseModel):
    timeline: dict
    expected_hash: str | None


class ReviewInput(BaseModel):
    review: Literal["accepted", "rejected"]


def create_app(data_dir: Path | None = None, secrets=None) -> FastAPI:
    data_dir = Path(data_dir or os.environ.get("MUWEAVE_DATA_DIR", str(Path.home() / ".muweave"))).resolve()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=60,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            follow_redirects=False,
        ) as client:
            db = Database(data_dir / "app-state" / "muweave.sqlite3")
            ws = Workspace(data_dir, db)
            providers = ProviderStore(db, secrets, client)
            agent, jobs, timeline = (
                AgentService(db, ws, providers),
                JobService(db, ws, providers, client),
                TimelineService(db, ws),
            )
            app.state.db, app.state.ws, app.state.providers = db, ws, providers
            app.state.agent, app.state.jobs, app.state.timeline = agent, jobs, timeline
            for job in db.list("jobs"):
                if job["status"] in {"queued", "submitted", "running", "downloading", "checking"}:
                    jobs.start(job["id"])
            yield
            await agent.close()
            await jobs.close()
            await timeline.close()

    app = FastAPI(title="MuWeave", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/"), "http://localhost:5173", "http://127.0.0.1:5173"}
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and origin not in allowed:
            return JSONResponse({"detail": "Cross-site local API access is not allowed"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > MAX_MEDIA_BYTES + 65536:
            return JSONResponse({"detail": "Request exceeds upload limit"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        message = str(error)
        return JSONResponse({"detail": message}, status_code=409 if "conflict" in message.lower() else 400)

    @app.exception_handler(KeyError)
    @app.exception_handler(FileNotFoundError)
    async def missing(request, error):
        return JSONResponse({"detail": "Requested record or file was not found"}, status_code=404)

    @app.exception_handler(KeyringError)
    async def credential_error(request, error):
        return JSONResponse(
            {"detail": "System credential store unavailable; configure a supported keyring backend"},
            status_code=503,
        )

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "ffprobe": bool(shutil.which("ffprobe")),
        }

    @app.get("/api/projects")
    def projects():
        return app.state.db.list("projects")

    @app.post("/api/projects")
    def create_project(body: ProjectInput):
        return app.state.ws.create_project(body.title, body.profile)

    @app.get("/api/projects/{pid}/files")
    def files(pid: str):
        return app.state.ws.files(pid)

    @app.get("/api/projects/{pid}/file")
    def read_file(pid: str, path: str):
        return app.state.ws.read(pid, path)

    @app.post("/api/projects/{pid}/changes")
    def changes(pid: str, body: ChangesInput):
        return app.state.ws.apply_changes(pid, body.operation_id, body.changes)

    @app.get("/api/projects/{pid}/history")
    def history(pid: str):
        app.state.ws.root(pid)
        return app.state.db.list("changes", project_id=pid)

    @app.post("/api/projects/{pid}/history/{change_id}/undo")
    def undo(pid: str, change_id: str):
        return app.state.ws.undo(pid, change_id, new_id())

    @app.post("/api/projects/{pid}/validate")
    def validate(pid: str):
        return validate_project(app.state.ws, pid)

    @app.get("/api/providers")
    def providers():
        return app.state.db.list("providers")

    @app.post("/api/providers")
    def save_provider(body: dict):
        return app.state.providers.save_connection(body)

    @app.get("/api/models")
    def models():
        return app.state.db.list("models")

    @app.post("/api/models")
    def save_model(body: dict):
        return app.state.providers.save_model(body)

    @app.get("/api/presets")
    def presets():
        return {"media": MEDIA_PRESETS, "recipes": RECIPES}

    @app.post("/api/models/{model_id}/test")
    async def test_model(model_id: str):
        providers = app.state.providers
        config = app.state.db.get("models", model_id)
        if config["kind"] != "text":
            raise ValueError("Media connection tests use an explicit generation task")
        called = False
        agent = Agent(providers.text_model(model_id))
        if config["capabilities"].get("tools"):

            @agent.tool_plain
            def muweave_connection_probe(value: str) -> str:
                nonlocal called
                called = True
                return "probe-success:" + value

        try:
            result = await agent.run(
                "必须调用 muweave_connection_probe 工具，value 为 MuWeave，然后描述工具结果。"
                if config["capabilities"].get("tools")
                else "请回复 MuWeave 连接成功",
                usage_limits=UsageLimits(request_limit=3),
                timeout=45,
            )
            passed = called or not config["capabilities"].get("tools")
            record = {
                "id": model_id,
                "passed": passed,
                "tools_verified": called,
                "output": providers.redact(result.output),
                "tested_at": now(),
            }
        except Exception as error:
            record = {
                "id": model_id,
                "passed": False,
                "error": providers.redact(f"{type(error).__name__}: {error}")[:1500],
                "tested_at": now(),
            }
        return app.state.db.put("model_tests", record)

    @app.get("/api/projects/{pid}/runs")
    def runs(pid: str):
        app.state.ws.root(pid)
        return app.state.db.list("runs", project_id=pid)

    @app.post("/api/projects/{pid}/runs")
    async def create_run(pid: str, body: RunInput):
        run = app.state.agent.create(pid, **body.model_dump())
        app.state.agent.start(run["id"])
        return run

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        app.state.db.get("runs", run_id)
        task = app.state.agent.tasks.get(run_id)
        if task:
            task.cancel()
        return {"cancel_requested": bool(task)}

    @app.get("/api/events/{stream}")
    async def events(stream: str, request: Request, after: int = 0):
        cursor = max(after, int(request.headers.get("last-event-id", "0")))

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(app.state.db.events, stream, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/projects/{pid}/jobs")
    def jobs(pid: str):
        app.state.ws.root(pid)
        return app.state.db.list("jobs", project_id=pid)

    @app.post("/api/projects/{pid}/jobs")
    async def create_job(pid: str, body: dict):
        job = app.state.jobs.create(pid, body)
        app.state.jobs.start(job["id"])
        return job

    @app.post("/api/jobs/{job_id}/attach")
    async def attach(job_id: str, body: dict):
        app.state.jobs.attach(job_id, body.get("remote_id", ""))
        return app.state.db.get("jobs", job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        app.state.jobs.cancel(job_id)
        return {"local_cancel_requested": True, "remote_cancelled": False}

    @app.post("/api/jobs/{job_id}/retry-download")
    async def retry_download(job_id: str):
        app.state.jobs.resume_download(job_id)
        return {"resumed": True}

    @app.get("/api/projects/{pid}/assets")
    def assets(pid: str):
        app.state.ws.root(pid)
        return [refresh_review(app.state.ws, asset) for asset in app.state.db.list("assets", project_id=pid)]

    @app.post("/api/projects/{pid}/assets")
    async def upload(pid: str, kind: Literal["image", "video"], file: UploadFile = File()):
        app.state.ws.root(pid)
        chunks, size = [], 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MEDIA_BYTES:
                raise HTTPException(413, "Upload exceeds 256 MiB")
            chunks.append(chunk)
        return await import_asset(app.state.ws, pid, b"".join(chunks), kind, file.filename or "upload")

    @app.post("/api/assets/{asset_id}/review")
    def review_asset(asset_id: str, body: ReviewInput):
        asset = app.state.db.get("assets", asset_id)
        if body.review == "accepted":
            asset["accepted_dependencies"] = dependencies(
                app.state.ws, asset["project_id"], list(asset.get("dependencies", {}))
            )
        asset.update(review=body.review, reviewed_at=now())
        return app.state.db.put("assets", asset)

    @app.get("/api/projects/{pid}/media/{asset_id}")
    def media(pid: str, asset_id: str):
        asset = app.state.db.get("assets", asset_id)
        if asset["project_id"] != pid:
            raise HTTPException(404)
        return FileResponse(app.state.ws.path(pid, asset["path"]))

    @app.get("/api/projects/{pid}/timeline")
    def timeline(pid: str):
        return app.state.timeline.load(pid)

    @app.post("/api/projects/{pid}/timeline")
    def save_timeline(pid: str, body: TimelineInput):
        return app.state.timeline.save(pid, body.timeline, body.expected_hash)

    @app.post("/api/projects/{pid}/renders")
    async def render(pid: str):
        record = app.state.timeline.create_render(pid)
        app.state.timeline.start(record["id"])
        return record

    @app.get("/api/projects/{pid}/renders")
    def renders(pid: str):
        app.state.ws.root(pid)
        return app.state.db.list("renders", project_id=pid)

    @app.get("/api/projects/{pid}/exports/{render_id}/{kind}")
    def export(pid: str, render_id: str, kind: Literal["video", "subtitle", "manifest"]):
        record = app.state.db.get("renders", render_id)
        if record["project_id"] != pid or record["status"] != "ready":
            raise HTTPException(404)
        path = app.state.ws.path(pid, record[f"{kind}_path"])
        return FileResponse(path, filename=path.name)

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    # backend/app/main.py -> repository is parents[2].
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="frontend-assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/") or not (dist / "index.html").is_file():
            raise HTTPException(404, "Build the frontend with npm run build, or use the Vite dev server")
        return FileResponse(dist / "index.html")

    return app
