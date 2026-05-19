from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock
import pytest
import tomli_w

from claw_proxy.user_config import create_user_config_router


@pytest.fixture
def app(tmp_path, fernet):
    user_dir = tmp_path / "u1" / ".zeroclaw"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_bytes(
        tomli_w.dumps(
            {
                "llm": {"provider": "openrouter", "api-key": "secret", "model": "x"},
                "ui": {"theme": "dark"},
            }
        ).encode()
    )

    allowlist_path = tmp_path / "allow.toml"
    allowlist_path.write_text(
        'allowed_paths = ["llm.provider", "llm.api-key", "llm.model"]\n',
        encoding="utf-8",
    )

    schema_cache = AsyncMock()
    schema_cache.is_secret_path.side_effect = lambda cid, p: p.endswith(".api-key")

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    container.exec_run.return_value = MagicMock(exit_code=0, output=b"ok")

    orch = AsyncMock()
    orch.data_dir = str(tmp_path)

    reg = AsyncMock()
    reg.get.return_value = {
        "container_id": "c1",
        "user_id": "u1",
        "status": "ready",
        "http_url": "",
    }

    async def auth_fn(token):
        return {"id": "u1", "email": "u@x"}

    app = FastAPI()
    app.include_router(
        create_user_config_router(
            orchestrator=orch,
            docker_client=docker_client,
            registry=reg,
            schema_cache=schema_cache,
            auth_fn=auth_fn,
            allowlist_path=str(allowlist_path),
        ),
        prefix="/user/config",
    )
    return app


@pytest.fixture
def client(app):
    test_client = TestClient(app)
    test_client.headers.update({"Authorization": "Bearer fakejwt"})
    return test_client


def test_get_only_returns_allowed_with_secrets_masked(client):
    response = client.get("/user/config")
    assert response.status_code == 200
    cfg = response.json()
    assert set(cfg.keys()) == {"llm.provider", "llm.api-key", "llm.model"}
    assert cfg["llm.api-key"] == "****"


def test_put_rejects_disallowed_path(client, monkeypatch):
    async def fake_health(http_url, timeout=10):
        return True

    monkeypatch.setattr("claw_proxy.admin.operations._health_ok", fake_health)
    response = client.put(
        "/user/config",
        json={"updates": [{"path": "ui.theme", "value": "light"}]},
    )
    assert response.status_code == 200
    body = response.json()
    bad = next(item for item in body["results"] if item["path"] == "ui.theme")
    assert bad["applied"] is False
    assert "not in allowlist" in bad["error"]


def test_put_allowed_path(client, monkeypatch):
    async def fake_health(http_url, timeout=10):
        return True

    monkeypatch.setattr("claw_proxy.admin.operations._health_ok", fake_health)
    response = client.put(
        "/user/config",
        json={"updates": [{"path": "llm.model", "value": "claude-4-7"}]},
    )
    assert response.status_code == 200
