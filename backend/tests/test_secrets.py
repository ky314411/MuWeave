import os

import pytest
from app.secrets import EncryptedFileSecrets
from keyring.errors import KeyringError


def test_encrypted_credentials_survive_restart_and_deletion(tmp_path):
    path, key = tmp_path / "data" / "credentials.enc", tmp_path / "keys" / "master.key"
    store = EncryptedFileSecrets(path, key)
    store.set("provider", "private-api-key")
    assert b"private-api-key" not in path.read_bytes()
    assert EncryptedFileSecrets(path, key).get("provider") == "private-api-key"
    store.set("other", "second-key")
    store.set("provider", "")
    assert store.get("provider") is None
    assert store.get("other") == "second-key"
    if os.name != "nt":
        assert key.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_mode & 0o777 == 0o600


def test_missing_or_wrong_key_does_not_overwrite_credentials(tmp_path):
    path, key = tmp_path / "credentials.enc", tmp_path / "master.key"
    EncryptedFileSecrets(path, key).set("provider", "private-api-key")
    before = path.read_bytes()
    key.unlink()
    with pytest.raises(KeyringError):
        EncryptedFileSecrets(path, key).get("provider")
    EncryptedFileSecrets(tmp_path / "other.enc", key).set("other", "other-key")
    with pytest.raises(KeyringError):
        EncryptedFileSecrets(path, key).set("provider", "replacement")
    assert path.read_bytes() == before


def test_app_uses_explicit_container_credentials(tmp_path, monkeypatch):
    from app.main import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MUWEAVE_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    for index in range(2):
        with TestClient(create_app(tmp_path / "data")) as client:
            if index == 0:
                response = client.post(
                    "/api/providers",
                    json={
                        "name": "Container",
                        "base_url": "https://example.com/v1",
                        "api_key": "private-key",
                    },
                )
                assert response.status_code == 200
            result = client.get("/api/providers")
            assert result.json()[0]["has_key"]
            assert "private-key" not in result.text
