import json

import httpx
import pytest
from app.db import Database
from app.providers import ProviderStore
from app.workspace import Workspace
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from test_providers import MemorySecrets


async def test_review_has_read_tools_but_no_write_tools(tmp_path, monkeypatch):
    from app.agent import AgentService

    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("独立审稿", "general")["id"]
    character = ws.read(pid, "bible/characters.md")
    ws.apply_changes(
        pid,
        "character",
        [
            {
                "path": "bible/characters.md",
                "content": "林舟的真实身份是守门人",
                "expected_hash": character["hash"],
            }
        ],
    )

    def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert "read_file" in names and "write_file" not in names
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if results:
            assert "守门人" in str(results[-1].content)
            return ModelResponse(parts=[TextPart("已核对人物原文")])
        return ModelResponse(
            parts=[ToolCallPart("read_file", {"path": "bible/characters.md"}, "review-read")]
        )

    providers = ProviderStore(db, MemorySecrets())
    db.put("models", {"id": "test-model", "capabilities": {}})
    monkeypatch.setattr(providers, "text_model", lambda *args, **kwargs: FunctionModel(model))
    service = AgentService(db, ws, providers)
    record = service.create(pid, "test-model", "读取人物台账并审稿", "review")
    await service.execute(record["id"])
    assert db.get("runs", record["id"])["status"] == "completed"
    assert db.get("runs", record["id"])["output"] == "已核对人物原文"


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
