import json

import httpx
from app.agent import AgentService
from app.db import Database
from app.providers import ProviderStore
from app.workspace import Workspace
from test_providers import MemorySecrets


async def test_streamed_tool_arguments_complete_before_execution(tmp_path):
    db = Database(tmp_path / "state.db")
    ws = Workspace(tmp_path, db)
    pid = ws.create_project("流式工具", "general")["id"]

    def handler(request):
        body = json.loads(request.content)
        assert body.get("stream") is True
        if any(item["role"] == "tool" for item in body["messages"]):
            deltas = [({"role": "assistant", "content": "完成"}, None), ({}, "stop")]
        else:
            args = json.dumps({"path": "episodes/02.md", "content": "完整剧本", "expected_hash": None})
            deltas = [
                (
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "stream-write",
                                "type": "function",
                                "function": {"name": "write_file", "arguments": args[:15]},
                            }
                        ],
                    },
                    None,
                ),
                ({"tool_calls": [{"index": 0, "function": {"arguments": args[15:]}}]}, None),
                ({}, "tool_calls"),
            ]
        chunks = [
            {
                "id": "stream-1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            for delta, finish in deltas
        ]
        chunks.append(
            {
                "id": "stream-1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        providers = ProviderStore(db, MemorySecrets(), client)
        connection = providers.save_connection({"name": "stream", "base_url": "http://stream.test/v1"})
        model = providers.save_model(
            {
                "name": "stream",
                "model_id": "test",
                "provider_id": connection["id"],
                "kind": "text",
                "protocol": "openai-compatible",
                "capabilities": {"tools": True, "streaming": True},
            }
        )
        service = AgentService(db, ws, providers)
        record = service.create(pid, model["id"], "写入文件", "write")
        await service.execute(record["id"])
    assert db.get("runs", record["id"])["status"] == "completed"
    assert ws.read(pid, "episodes/02.md")["content"] == "完整剧本"
    assert any(event["type"] == "text_delta" for event in db.events(record["id"]))
