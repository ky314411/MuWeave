import json

import httpx
import pytest
from app.db import Database
from pydantic_ai import Agent


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def test_credentials_never_enter_database_or_public_config(tmp_path):
    from app.providers import ProviderStore

    db = Database(tmp_path / "state.db")
    store = ProviderStore(db, MemorySecrets())
    connection = store.save_connection(
        {"name": "自定义", "base_url": "https://example.org/v1", "api_key": "private-test-secret"}
    )
    assert connection["has_key"] is True
    assert "private-test-secret" not in json.dumps(connection)
    assert "private-test-secret" not in json.dumps(db.list("providers"))
    connection = store.save_connection({**connection, "name": "重命名"})
    assert store.secret(connection["id"]) == "private-test-secret"
    with pytest.raises(ValueError):
        store.save_connection({"name": "bad", "base_url": "file:///tmp", "api_key": "x"})


async def test_compatible_model_executes_real_framework_tool_roundtrip(tmp_path):
    from app.providers import ProviderStore

    def handler(request):
        body = json.loads(request.content)
        if body["messages"][-1]["role"] == "tool":
            assert "42" in body["messages"][-1]["content"]
            message = {"role": "assistant", "content": "工具结果是 42"}
            finish = "stop"
        else:
            assert body["model"] == "my-local-model"
            assert body["tools"][0]["function"]["name"] == "multiply"
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "multiply", "arguments": '{"a":6,"b":7}'},
                    }
                ],
            }
            finish = "tool_calls"
        return httpx.Response(
            200,
            json={
                "id": "chat-test",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = ProviderStore(Database(tmp_path / "state.db"), MemorySecrets(), client)
        connection = store.save_connection({"name": "本地", "base_url": "http://localhost:11434/v1"})
        model = store.save_model(
            {
                "provider_id": connection["id"],
                "name": "本地模型",
                "model_id": "my-local-model",
                "kind": "text",
                "protocol": "ollama",
                "capabilities": {"tools": True},
            }
        )
        agent = Agent(store.text_model(model["id"]))

        @agent.tool_plain
        def multiply(a: int, b: int) -> int:
            return a * b

        assert (await agent.run("计算六乘七")).output == "工具结果是 42"


def test_template_substitution_keeps_values_as_data():
    from app.providers import render_template, select

    prompt = '引号"和 {braces}，不要执行 ${secret}'
    payload = render_template(
        {"input": "${prompt}", "duration": "${duration}", "nested": ["prefix ${prompt}"]},
        {"prompt": prompt, "duration": 6},
    )
    assert payload["input"] == prompt
    assert payload["duration"] == 6
    assert (
        select({"output": [{"url": "https://example.org/video.mp4"}]}, "output.0.url")
        == "https://example.org/video.mp4"
    )
    with pytest.raises(ValueError):
        render_template({"x": "${secret}"}, {})


def test_tools_capability_is_enforced(tmp_path):
    from app.providers import ProviderStore

    store = ProviderStore(Database(tmp_path / "state.db"), MemorySecrets())
    connection = store.save_connection({"name": "chat", "base_url": "http://localhost:8001/v1"})
    model = store.save_model(
        {
            "provider_id": connection["id"],
            "name": "聊天模型",
            "model_id": "chat",
            "kind": "text",
            "protocol": "openai-compatible",
        }
    )
    with pytest.raises(ValueError, match="tools"):
        store.text_model(model["id"], require_tools=True)
