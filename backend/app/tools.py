import json
from dataclasses import dataclass

import yaml
from pydantic_ai import Agent, ModelRetry, RunContext

from .db import Database, now
from .workspace import Workspace


@dataclass
class ToolDeps:
    project_id: str
    run_id: str
    workspace: Workspace
    db: Database


def validate_project(ws: Workspace, project_id: str) -> dict:
    issues = []
    for path in ws.files(project_id):
        if path.endswith((".yaml", ".yml")):
            try:
                yaml.safe_load(ws.read(project_id, path)["content"])
            except yaml.YAMLError:
                issues.append({"path": path, "severity": "error", "message": "YAML 格式错误"})
    series = yaml.safe_load(ws.read(project_id, "series.yaml")["content"])
    if not isinstance(series, dict) or not series.get("title"):
        issues.append({"path": "series.yaml", "severity": "error", "message": "缺少项目标题"})
    return {"issues": issues, "mechanical_only": True, "note": "连续性和艺术质量仍须结合台账与人工/模型审稿"}


def register_tools(agent: Agent, readonly: bool = False):
    def tool(function=None, **kwargs):
        def register(fn):
            name = kwargs.get("name", fn.__name__)
            if readonly and name not in {"list_files", "read_file", "search_files", "validate_project"}:
                return fn
            return agent.tool(fn, **kwargs)

        return register(function) if function is not None else register

    def perform(ctx, callback):
        deps = ctx.deps
        call_id = f"{deps.run_id}:{ctx.tool_call_id}"
        try:
            record = deps.db.get("tool_calls", call_id)
        except KeyError:
            record = None
        if record and record["status"] == "completed":
            return record["result"]
        record = {
            "id": call_id,
            "run_id": deps.run_id,
            "name": ctx.tool_name,
            "status": "executing",
            "started_at": now(),
        }
        deps.db.put("tool_calls", record)
        deps.db.event(deps.run_id, "tool_started", name=ctx.tool_name, call_id=ctx.tool_call_id)
        try:
            result = callback(deps, call_id)
        except (ValueError, KeyError, FileNotFoundError, UnicodeError) as error:
            record.update(status="failed", error=str(error))
            deps.db.put("tool_calls", record)
            raise ModelRetry(str(error)) from error
        record.update(status="completed", result=result)
        deps.db.put("tool_calls", record)
        deps.db.event(deps.run_id, "tool_completed", name=ctx.tool_name, call_id=ctx.tool_call_id)
        return result

    @tool
    def list_files(ctx: RunContext[ToolDeps]) -> list[str]:
        """List text project files; inspect only what the current task needs."""
        return perform(ctx, lambda deps, _: deps.workspace.files(deps.project_id))

    @tool
    def read_file(ctx: RunContext[ToolDeps], path: str) -> dict:
        """Read UTF-8 text and its SHA-256 hash. Read before editing existing files."""
        return perform(ctx, lambda deps, _: deps.workspace.read(deps.project_id, path))

    @tool
    def search_files(ctx: RunContext[ToolDeps], query: str) -> list[dict]:
        """Search literal text in project files, returning up to 40 matching lines."""

        def search(deps, _):
            if not query or len(query) > 300:
                raise ValueError("Search query must contain 1–300 characters")
            matches = []
            for path in deps.workspace.files(deps.project_id):
                for number, line in enumerate(
                    deps.workspace.read(deps.project_id, path)["content"].splitlines(), 1
                ):
                    if query.casefold() in line.casefold():
                        matches.append({"path": path, "line": number, "text": line[:600]})
                        if len(matches) >= 40:
                            return matches
            return matches

        return perform(ctx, search)

    @tool
    def write_file(ctx: RunContext[ToolDeps], path: str, content: str, expected_hash: str | None) -> dict:
        """Write text. expected_hash is the last read hash, or null for a new file. Never lock content."""
        return perform(
            ctx,
            lambda deps, call_id: deps.workspace.apply_changes(
                deps.project_id, call_id, [{"path": path, "content": content, "expected_hash": expected_hash}]
            ),
        )

    @tool
    def edit_file(
        ctx: RunContext[ToolDeps], path: str, old_text: str, new_text: str, expected_hash: str
    ) -> dict:
        """Replace one exact unique occurrence; fails on stale versions or ambiguous matches."""

        def edit(deps, call_id):
            document = deps.workspace.read(deps.project_id, path)
            if document["hash"] != expected_hash or not old_text or document["content"].count(old_text) != 1:
                raise ValueError("Version conflict or nonunique exact text")
            return deps.workspace.apply_changes(
                deps.project_id,
                call_id,
                [
                    {
                        "path": path,
                        "content": document["content"].replace(old_text, new_text, 1),
                        "expected_hash": expected_hash,
                    }
                ],
            )

        return perform(ctx, edit)

    @tool
    def apply_changes(ctx: RunContext[ToolDeps], changes: list[dict]) -> dict:
        """Apply a related modification group; each change has path, content and expected_hash."""
        return perform(
            ctx, lambda deps, call_id: deps.workspace.apply_changes(deps.project_id, call_id, changes)
        )

    @tool
    def delete_file(ctx: RunContext[ToolDeps], path: str, expected_hash: str) -> dict:
        """Soft delete text with recoverable history. Return the change id needed for restore."""
        return perform(
            ctx,
            lambda deps, call_id: deps.workspace.apply_changes(
                deps.project_id, call_id, [{"path": path, "content": None, "expected_hash": expected_hash}]
            ),
        )

    @tool
    def restore_file(ctx: RunContext[ToolDeps], change_id: str) -> dict:
        """Undo a committed modification group after verifying current file versions."""
        return perform(ctx, lambda deps, call_id: deps.workspace.undo(deps.project_id, change_id, call_id))

    @tool
    def move_file(ctx: RunContext[ToolDeps], source: str, destination: str, expected_hash: str) -> dict:
        """Move text through a recoverable modification group; destination must not exist."""

        def move(deps, call_id):
            document = deps.workspace.read(deps.project_id, source)
            return deps.workspace.apply_changes(
                deps.project_id,
                call_id,
                [
                    {"path": source, "content": None, "expected_hash": expected_hash},
                    {"path": destination, "content": document["content"], "expected_hash": None},
                ],
            )

        return perform(ctx, move)

    @tool(name="validate_project")
    def validate(ctx: RunContext[ToolDeps]) -> dict:
        """Run mechanical project checks; this does not establish artistic quality."""
        return perform(ctx, lambda deps, _: validate_project(deps.workspace, deps.project_id))

    @tool
    def export_script_book(ctx: RunContext[ToolDeps]) -> dict:
        """Collect current episode text into a Markdown script book in production/script-book.md."""

        def export(deps, call_id):
            ws, pid = deps.workspace, deps.project_id
            content = "\n\n---\n\n".join(
                ws.read(pid, path)["content"]
                for path in ws.files(pid)
                if path.startswith("episodes/") and path.endswith(".md")
            )
            path = "production/script-book.md"
            existing = ws.read(pid, path)["hash"] if ws.path(pid, path).exists() else None
            return ws.apply_changes(
                pid, call_id, [{"path": path, "content": content, "expected_hash": existing}]
            )

        return perform(ctx, export)


def build_context(ws: Workspace, pid: str, current_file: str | None = None) -> str:
    paths = [
        "series.yaml",
        "bible/world.md",
        "bible/characters.md",
        "outline/series.md",
        "outline/episodes.md",
    ]
    if current_file and current_file not in paths:
        paths.append(current_file)
    context, remaining = [], 24000
    for path in paths:
        try:
            content = ws.read(pid, path)["content"]
        except FileNotFoundError:
            continue
        limit = min(remaining, 12000 if path == current_file else 4000)
        excerpt = content[:limit]
        context.append({"path": path, "excerpt": excerpt, "truncated": len(content) > len(excerpt)})
        remaining -= len(excerpt)
    return json.dumps(context, ensure_ascii=False)
