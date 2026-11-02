from pathlib import Path

import pytest


def workspace(tmp_path):
    from app.db import Database
    from app.workspace import Workspace

    return Workspace(tmp_path, Database(tmp_path / "app-state" / "muweave.sqlite3"))


def test_create_edit_conflict_delete_restore_and_idempotency(tmp_path):
    ws = workspace(tmp_path)
    project = ws.create_project("测试短剧", "general")
    pid = project["id"]
    first = ws.apply_changes(
        pid, "create-1", [{"path": "episodes/02.md", "content": "初稿", "expected_hash": None}]
    )
    original = ws.read(pid, "episodes/02.md")
    assert original["content"] == "初稿"
    assert (
        ws.apply_changes(
            pid, "create-1", [{"path": "episodes/02.md", "content": "初稿", "expected_hash": None}]
        )
        == first
    )
    with pytest.raises(ValueError, match="operation"):
        ws.apply_changes(
            pid, "create-1", [{"path": "episodes/02.md", "content": "不同请求", "expected_hash": None}]
        )
    ws.apply_changes(
        pid, "edit-1", [{"path": "episodes/02.md", "content": "第二稿", "expected_hash": original["hash"]}]
    )
    with pytest.raises(ValueError, match="conflict"):
        ws.apply_changes(
            pid, "edit-2", [{"path": "episodes/02.md", "content": "覆盖", "expected_hash": original["hash"]}]
        )
    current = ws.read(pid, "episodes/02.md")
    deletion = ws.apply_changes(
        pid, "delete-1", [{"path": "episodes/02.md", "content": None, "expected_hash": current["hash"]}]
    )
    assert not (ws.root(pid) / "episodes/02.md").exists()
    ws.undo(pid, deletion["id"], "restore-1")
    assert ws.read(pid, "episodes/02.md")["content"] == "第二稿"


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "C:/escape.md",
        "episodes/../../escape.md",
        ".git/config",
        "CON.md",
        "x.md:stream",
        "x./y.md",
    ],
)
def test_rejects_unsafe_paths(tmp_path, path):
    ws = workspace(tmp_path)
    pid = ws.create_project("边界", "general")["id"]
    with pytest.raises(ValueError):
        ws.read(pid, path)


def test_multi_file_conflict_changes_nothing(tmp_path):
    ws = workspace(tmp_path)
    pid = ws.create_project("修改组", "general")["id"]
    ws.apply_changes(pid, "seed", [{"path": "a.md", "content": "A", "expected_hash": None}])
    with pytest.raises(ValueError, match="conflict"):
        ws.apply_changes(
            pid,
            "group",
            [
                {"path": "b.md", "content": "B", "expected_hash": None},
                {"path": "a.md", "content": "C", "expected_hash": None},
            ],
        )
    assert not (ws.root(pid) / "b.md").exists()
    assert ws.read(pid, "a.md")["content"] == "A"


def test_symlink_cannot_escape(tmp_path):
    ws = workspace(tmp_path)
    pid = ws.create_project("链接", "general")["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (ws.root(pid) / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(ValueError):
        ws.apply_changes(pid, "escape", [{"path": "linked/a.md", "content": "x", "expected_hash": None}])
    assert not (outside / "a.md").exists()


def test_project_state_survives_reopen(tmp_path):
    ws = workspace(tmp_path)
    pid = ws.create_project("持久化", "rules")["id"]
    reopened = workspace(tmp_path)
    assert reopened.db.get("projects", pid)["title"] == "持久化"
    assert "规则" in reopened.read(pid, "bible/rules.md")["content"]


def test_failed_group_compensates_applied_files(tmp_path, monkeypatch):
    ws = workspace(tmp_path)
    pid = ws.create_project("恢复", "general")["id"]
    original = Path.replace

    def fail_second(source, target):
        if Path(target).name == "b.md":
            raise OSError("disk failure")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(OSError):
        ws.apply_changes(
            pid,
            "failing",
            [
                {"path": "a.md", "content": "A", "expected_hash": None},
                {"path": "b.md", "content": "B", "expected_hash": None},
            ],
        )
    assert not (ws.root(pid) / "a.md").exists()
