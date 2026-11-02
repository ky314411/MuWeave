import hashlib
import json
import os
import re
import threading
from pathlib import Path

import yaml

from .db import Database, new_id, now

MAX_TEXT_BYTES = 2 * 1024 * 1024
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def digest(content: str | None) -> str | None:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".muweave-{new_id()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


class Workspace:
    def __init__(self, data_dir: Path, db: Database):
        self.data_dir, self.db = data_dir.resolve(), db
        self.lock = threading.RLock()
        (self.data_dir / "projects").mkdir(parents=True, exist_ok=True)
        self.recover()

    def root(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", project_id):
            raise ValueError("Invalid project id")
        self.db.get("projects", project_id)
        path = self.data_dir / "projects" / project_id
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked project root is forbidden")
        return path

    def path(self, project_id: str, relative: str) -> Path:
        root = self.root(project_id)
        parts = relative.replace("\\", "/").split("/")
        if not parts or any(
            not part
            or part in {".", ".."}
            or part.startswith(".")
            or part.endswith((".", " "))
            or re.search(r'[<>:"|?*\x00-\x1f]', part)
            or part.split(".")[0].upper() in RESERVED
            for part in parts
        ):
            raise ValueError("Unsafe workspace path")
        path = root
        for part in parts:
            path /= part
            if path.is_symlink() or path.is_junction():
                raise ValueError("Linked workspace paths are forbidden")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Path escapes workspace")
        return path

    def create_project(self, title: str, profile: str) -> dict:
        title = title.strip()
        if not title or len(title) > 120 or profile not in {"general", "rules"}:
            raise ValueError("Invalid title or profile")
        project = self.db.put(
            "projects", {"id": new_id(), "title": title, "profile": profile, "created_at": now()}
        )
        root = self.root(project["id"])
        for directory in ("bible", "outline", "episodes", "production", "assets", "exports"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        files = {
            "series.yaml": yaml.safe_dump(
                {
                    "title": title,
                    "profile": profile,
                    "episode_duration": 90,
                    "aspect_ratio": "9:16",
                    "status": "draft",
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            "bible/world.md": "# 世界观\n\n",
            "bible/characters.md": "# 人物\n\n",
            "outline/series.md": "# 总纲\n\n",
            "outline/episodes.md": "# 分集卡\n\n",
            "episodes/01.md": "# 第一集\n\n",
            "bible/foreshadowing.md": "# 伏笔台账\n\n",
        }
        if profile == "rules":
            files.update({"bible/rules.md": "# 规则台账\n\n", "bible/copies.md": "# 副本台账\n\n"})
        self.apply_changes(
            project["id"],
            "initial",
            [{"path": path, "content": text, "expected_hash": None} for path, text in files.items()],
        )
        return project

    def read(self, project_id: str, relative: str) -> dict:
        path = self.path(project_id, relative)
        if path.stat().st_size > MAX_TEXT_BYTES:
            raise ValueError("Text file exceeds 2 MiB")
        content = path.read_text(encoding="utf-8")
        return {"path": relative, "content": content, "hash": digest(content)}

    def files(self, project_id: str) -> list[str]:
        root = self.root(project_id)
        result = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and not (Path(directory) / name).is_junction()
                and not (Path(directory) / name).is_symlink()
                and name not in {"assets", "exports"}
            ]
            for name in names:
                if not name.startswith(".") and not (Path(directory) / name).is_symlink():
                    result.append((Path(directory) / name).relative_to(root).as_posix())
        return sorted(result)

    def apply_changes(self, project_id: str, operation_id: str, changes: list[dict]) -> dict:
        if not operation_id or len(operation_id) > 160 or not 1 <= len(changes) <= 40:
            raise ValueError("Invalid operation or modification group")
        request_hash = digest(json.dumps(changes, sort_keys=True, ensure_ascii=False))
        record_id = hashlib.sha256(f"{project_id}:{operation_id}".encode()).hexdigest()
        with self.lock:
            try:
                existing = self.db.get("changes", record_id)
            except KeyError:
                existing = None
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("operation id reused with different arguments")
                if existing["status"] != "committed":
                    raise ValueError("operation is not committed; inspect history")
                return existing
            snapshots, seen = [], set()
            for change in changes:
                relative = change["path"]
                if relative.split("/")[0] in {"assets", "exports"}:
                    raise ValueError("Media paths must use media tools")
                path = self.path(project_id, relative)
                canonical = str(path).casefold()
                if canonical in seen:
                    raise ValueError("Duplicate path in modification group")
                seen.add(canonical)
                before = self.read(project_id, relative)["content"] if path.exists() else None
                if digest(before) != change.get("expected_hash"):
                    raise ValueError(f"Version conflict: {relative}")
                after = change["content"]
                if after is not None and len(after.encode()) > MAX_TEXT_BYTES:
                    raise ValueError("Text file exceeds 2 MiB")
                snapshots.append({"path": relative, "before": before, "after": after})
            record = {
                "id": record_id,
                "project_id": project_id,
                "operation_id": operation_id,
                "request_hash": request_hash,
                "status": "applying",
                "changes": snapshots,
                "created_at": now(),
            }
            history = self.data_dir / ".studio-state" / project_id / "history" / f"{record_id}.json"
            atomic_write(history, json.dumps(record, ensure_ascii=False))
            self.db.put("changes", record)
            try:
                for change in snapshots:
                    path = self.path(project_id, change["path"])
                    current = self.read(project_id, change["path"])["content"] if path.exists() else None
                    if digest(current) != digest(change["before"]):
                        raise ValueError(f"Version conflict during apply: {change['path']}")
                    if change["after"] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, change["after"])
            except BaseException:
                self._rollback(record)
                raise
            record["status"] = "committed"
            self.db.put("changes", record)
            atomic_write(history, json.dumps(record, ensure_ascii=False))
            return record

    def _rollback(self, record: dict):
        conflict = False
        for change in reversed(record["changes"]):
            path = self.path(record["project_id"], change["path"])
            current = self.read(record["project_id"], change["path"])["content"] if path.exists() else None
            if digest(current) == digest(change["before"]):
                continue
            if digest(current) != digest(change["after"]):
                conflict = True
                continue
            if change["before"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, change["before"])
        record["status"] = "recovery_conflict" if conflict else "rolled_back"
        self.db.put("changes", record)

    def recover(self):
        for record in self.db.list("changes", status="applying"):
            self._rollback(record)

    def undo(self, project_id: str, change_id: str, operation_id: str) -> dict:
        record = self.db.get("changes", change_id)
        if record["project_id"] != project_id or record["status"] != "committed":
            raise ValueError("Invalid change group")
        return self.apply_changes(
            project_id,
            operation_id,
            [
                {
                    "path": change["path"],
                    "content": change["before"],
                    "expected_hash": digest(change["after"]),
                }
                for change in record["changes"]
            ],
        )
