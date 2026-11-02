from fastapi.testclient import TestClient
from test_providers import MemorySecrets


def test_local_project_editor_and_provider_api(tmp_path):
    from app.main import create_app

    with TestClient(create_app(tmp_path, secrets=MemorySecrets())) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        project = client.post("/api/projects", json={"title": "端到端项目", "profile": "general"}).json()
        pid = project["id"]
        document = client.get(f"/api/projects/{pid}/file", params={"path": "episodes/01.md"}).json()
        changes = [{"path": "episodes/01.md", "content": "新的剧本", "expected_hash": document["hash"]}]
        assert (
            client.post(
                f"/api/projects/{pid}/changes", json={"operation_id": "api-1", "changes": changes}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/projects/{pid}/changes", json={"operation_id": "api-2", "changes": changes}
            ).status_code
            == 409
        )
        assert client.get(f"/api/projects/{pid}/file", params={"path": "../README.md"}).status_code == 400
        connection = client.post(
            "/api/providers",
            json={"name": "自定义连接", "base_url": "http://localhost:11434/v1", "api_key": "test-private"},
        ).json()
        assert connection["has_key"]
        assert "test-private" not in client.get("/api/providers").text
        response = client.post(
            "/api/models",
            json={
                "provider_id": connection["id"],
                "name": "本机",
                "model_id": "local-model",
                "kind": "text",
                "protocol": "ollama",
                "capabilities": {"tools": True},
            },
        )
        assert response.status_code == 200
        assert client.get(f"/api/projects/{pid}/timeline").json()["hash"] is None


def test_cross_site_mutations_are_rejected(tmp_path):
    from app.main import create_app

    with TestClient(create_app(tmp_path, secrets=MemorySecrets())) as client:
        response = client.post(
            "/api/projects", json={"title": "不应创建"}, headers={"Origin": "https://untrusted.example"}
        )
        assert response.status_code == 403
