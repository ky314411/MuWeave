from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .db import Database, new_id
from .secrets import SystemSecrets


class Connection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    headers: dict[str, str] = Field(default_factory=dict)
    api_key: SecretStr | None = Field(default=None, exclude=True)
    has_key: bool = False


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=new_id, pattern=r"^[a-f0-9]{32}$")
    provider_id: str
    name: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=250)
    kind: Literal["text", "image", "video"]
    protocol: Literal["openai-compatible", "ollama", "anthropic", "google", "http-template"]
    capabilities: dict[str, bool] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    media: dict[str, Any] = Field(default_factory=dict)


def validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Only HTTP(S) URLs without embedded credentials are accepted")
    return value


def select(data: Any, path: str, default=None):
    try:
        for key in path.split(".") if path else []:
            data = data[int(key)] if isinstance(data, list) else data[key]
        return data
    except (KeyError, IndexError, TypeError, ValueError):
        return default


def render_template(template: Any, variables: dict) -> Any:
    if isinstance(template, dict):
        return {key: render_template(value, variables) for key, value in template.items()}
    if isinstance(template, list):
        return [render_template(value, variables) for value in template]
    if not isinstance(template, str):
        return template

    def lookup(match):
        key = match.group(1)
        if key not in variables:
            raise ValueError(f"Unknown template variable: {key}")
        return variables[key]

    exact = re.fullmatch(r"\$\{([a-z_]+)\}", template)
    if exact:
        return lookup(exact)
    return re.sub(r"\$\{([a-z_]+)\}", lambda match: str(lookup(match)), template)


class ProviderStore:
    def __init__(self, db: Database, secrets=None, http_client: httpx.AsyncClient | None = None):
        self.db = db
        self.secrets = secrets if secrets is not None else SystemSecrets()
        self.client = http_client

    def save_connection(self, data: dict) -> dict:
        connection = Connection.model_validate(data)
        validate_url(connection.base_url)
        if urlsplit(connection.base_url).query:
            raise ValueError("Connection base URL must not contain a query; store credentials in keyring")
        for name in [connection.auth_header, *connection.headers]:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("Invalid header name")
        if any(
            any(word in name.lower() for word in ("authorization", "key", "token", "secret", "cookie"))
            for name in connection.headers
        ):
            raise ValueError("Store authentication values in the credential field")
        if any("\r" in value or "\n" in value for value in connection.headers.values()):
            raise ValueError("Invalid header value")
        if connection.api_key is not None:
            self.secrets.set(connection.id, connection.api_key.get_secret_value())
            connection.has_key = bool(connection.api_key.get_secret_value())
        else:
            try:
                connection.has_key = self.db.get("providers", connection.id)["has_key"]
            except KeyError:
                connection.has_key = False
        return self.db.put("providers", connection.model_dump())

    def secret(self, provider_id: str) -> str | None:
        connection = self.db.get("providers", provider_id)
        return self.secrets.get(provider_id) if connection.get("has_key") else None

    def headers(self, provider_id: str) -> dict:
        connection = self.db.get("providers", provider_id)
        headers = dict(connection["headers"])
        secret = self.secret(provider_id)
        if secret:
            headers[connection["auth_header"]] = connection["auth_prefix"] + secret
        return headers

    def save_model(self, data: dict) -> dict:
        model = ModelConfig.model_validate(data)
        self.db.get("providers", model.provider_id)
        if model.kind != "text" and model.protocol != "http-template":
            raise ValueError("Media models require http-template protocol")
        if model.kind == "text" and model.protocol == "http-template":
            raise ValueError("Nonstandard text protocols require a model adapter")
        return self.db.put("models", model.model_dump())

    def text_model(self, model_id: str, require_tools: bool = False) -> Model:
        config = ModelConfig.model_validate(self.db.get("models", model_id))
        connection = self.db.get("providers", config.provider_id)
        if config.kind != "text":
            raise ValueError("Select a text model")
        if require_tools and not config.capabilities.get("tools"):
            raise ValueError("Selected model has no declared tools capability")
        secret = self.secret(config.provider_id)
        if config.protocol in {"openai-compatible", "ollama"}:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                base_url=connection["base_url"],
                api_key=secret or "local",
                http_client=self.client,
                default_headers=self.headers(config.provider_id),
                max_retries=0,
            )
            return OpenAIChatModel(
                config.model_id, provider=OpenAIProvider(openai_client=client), settings=config.settings
            )
        if config.protocol == "anthropic":
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            client = AsyncAnthropic(
                base_url=connection["base_url"],
                api_key=secret or "local",
                http_client=self.client,
                default_headers=connection["headers"],
                max_retries=0,
            )
            return AnthropicModel(
                config.model_id, provider=AnthropicProvider(anthropic_client=client), settings=config.settings
            )
        if config.protocol == "google":
            from google import genai
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            client = genai.Client(
                api_key=secret or "local",
                http_options={"base_url": connection["base_url"], "headers": connection["headers"]},
            )
            return GoogleModel(
                config.model_id, provider=GoogleProvider(client=client), settings=config.settings
            )
        raise ValueError("Unsupported text adapter")

    def redact(self, text: str) -> str:
        for connection in self.db.list("providers"):
            secret = self.secret(connection["id"])
            if secret:
                text = text.replace(secret, "[redacted]")
        return text


MEDIA_PRESETS = {
    "compatible-image": {
        "submit_path": "/images/generations",
        "body": {"model": "${model}", "prompt": "${prompt}", "n": 1},
        "url_path": "data.0.url",
        "base64_path": "data.0.b64_json",
    },
    "ark-video": {
        "submit_path": "/contents/generations/tasks",
        "body": {
            "model": "${model}",
            "content": "${content}",
            "generate_audio": True,
        },
        "task_id_path": "id",
        "query_path": "/contents/generations/tasks/${task_id}",
        "status_path": "status",
        "success_states": ["succeeded"],
        "failure_states": ["failed", "cancelled"],
        "url_path": "content.video_url",
    },
    "custom": {
        "submit_path": "/generate",
        "body": {"model": "${model}", "prompt": "${prompt}"},
        "task_id_path": "id",
        "query_path": "/tasks/${task_id}",
        "status_path": "status",
        "success_states": ["succeeded", "completed"],
        "failure_states": ["failed", "cancelled"],
        "url_path": "url",
    },
}
