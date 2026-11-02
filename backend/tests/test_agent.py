import json

import httpx
import pytest
from app.db import Database
from app.providers import ProviderStore
from app.workspace import Workspace
from test_providers import MemorySecrets


async def test_agent_writes_files_and_persists_full_tool_messages(tmp_path):
    from app.agent import AgentService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("工具写作", "general")["id"]

    def handler(request):
        body = json.loads(request.content)
        if any(message["role"] == "tool" for message in body["messages"]):
            message = {"role": "assistant", "content": "剧本已写入"}
            finish = "stop"
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "write-1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "path": "episodes/02.md",
                                    "content": "# 第二集\n\n林舟推开门。",
                                    "expected_hash": None,
                                }
                            ),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        return httpx.Response(
            200,
            json={
                "id": "chat",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = ProviderStore(db, MemorySecrets(), client)
        connection = providers.save_connection({"name": "test", "base_url": "http://model.test/v1"})
        model = providers.save_model(
            {
                "name": "test",
                "model_id": "test",
                "provider_id": connection["id"],
                "kind": "text",
                "protocol": "openai-compatible",
                "capabilities": {"tools": True},
            }
        )
        service = AgentService(db, ws, providers)
        run = service.create(pid, model["id"], "写第二集", "write")
        await service.execute(run["id"])
    saved = db.get("runs", run["id"])
    assert saved["status"] == "completed"
    assert "林舟" in ws.read(pid, "episodes/02.md")["content"]
    assert "tool-call" in json.dumps(saved["messages"])
    assert "tool-return" in json.dumps(saved["messages"])
    assert [event["type"] for event in db.events(run["id"])].count("tool_completed") == 1
    assert db.events(run["id"])[-1]["type"] == "completed"


def test_restart_marks_runs_interrupted_without_replaying(tmp_path):
    from app.agent import AgentService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("恢复", "general")["id"]
    db.put("runs", {"id": "unfinished", "project_id": pid, "status": "running", "messages": []})
    AgentService(db, ws, ProviderStore(db, MemorySecrets()))
    assert db.get("runs", "unfinished")["status"] == "interrupted"
    assert not (ws.root(pid) / "unexpected.md").exists()


def test_non_tool_model_cannot_start_agent(tmp_path):
    from app.agent import AgentService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("模型", "general")["id"]
    store = ProviderStore(db, MemorySecrets())
    connection = store.save_connection({"name": "chat", "base_url": "http://localhost:8001/v1"})
    model = store.save_model(
        {
            "name": "chat",
            "model_id": "chat",
            "provider_id": connection["id"],
            "kind": "text",
            "protocol": "openai-compatible",
        }
    )
    with pytest.raises(ValueError, match="tools"):
        AgentService(db, ws, store).create(pid, model["id"], "写作", "write")
