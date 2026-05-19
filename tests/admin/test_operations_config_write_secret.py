from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import tomli_w


def _exec(stdout=b"", exit_code=0):
    result = MagicMock()
    result.exit_code = exit_code
    result.output = stdout
    return result


def _build(tmp_path, *, status: str):
    from claw_proxy.admin.operations import AdminOperations

    user_dir = tmp_path / "u1"
    (user_dir / ".zeroclaw").mkdir(parents=True)
    (user_dir / ".zeroclaw" / "config.toml").write_bytes(
        tomli_w.dumps(
            {
                "llm": {
                    "provider": "openrouter",
                    "api-key": "old-secret",
                }
            }
        ).encode()
    )

    schema = AsyncMock()
    schema.is_secret_path.side_effect = lambda cid, path: path.endswith(".api-key")

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    container.exec_run.return_value = _exec(b"ok")

    orch = AsyncMock()
    orch.data_dir = str(tmp_path)

    reg = AsyncMock()
    reg.get.return_value = {
        "container_id": "c1",
        "user_id": "u1",
        "status": status,
        "http_url": "http://localhost:1",
    }

    return AdminOperations(
        orchestrator=orch,
        docker_client=docker_client,
        registry=reg,
        schema_cache=schema,
    )


@pytest.mark.asyncio
async def test_secret_write_no_rollback_on_unhealthy(tmp_path, monkeypatch):
    ops = _build(tmp_path, status="ready")

    async def fake_health(http_url, timeout=10):
        return False

    monkeypatch.setattr("claw_proxy.admin.operations._health_ok", fake_health)

    result = await ops.write_config(
        "u1",
        updates=[{"path": "llm.api-key", "value": "new"}],
    )

    assert result[0]["applied"] is True
    assert result[0]["rollback_available"] is False
    assert result[0]["post_write_health"] == "unhealthy"


@pytest.mark.asyncio
async def test_secret_write_rejected_on_stopped(tmp_path):
    ops = _build(tmp_path, status="stopped")

    result = await ops.write_config(
        "u1",
        updates=[{"path": "llm.api-key", "value": "new"}],
    )

    assert result[0]["applied"] is False
    assert "secret writes" in result[0]["error"].lower()
