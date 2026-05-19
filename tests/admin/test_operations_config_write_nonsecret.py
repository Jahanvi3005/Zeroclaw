from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import tomli_w


def _exec(stdout=b"", exit_code=0):
    result = MagicMock()
    result.exit_code = exit_code
    result.output = stdout
    return result


@pytest.fixture
def ops(tmp_path):
    from claw_proxy.admin.operations import AdminOperations

    user_dir = tmp_path / "u1"
    (user_dir / ".zeroclaw").mkdir(parents=True)
    (user_dir / ".zeroclaw" / "config.toml").write_bytes(
        tomli_w.dumps(
            {
                "llm": {
                    "provider": "openrouter",
                    "model": "gpt-4o",
                }
            }
        ).encode()
    )

    schema_cache = AsyncMock()
    schema_cache.is_secret_path.return_value = False

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    container.exec_run.return_value = _exec(b"ok")

    orchestrator = AsyncMock()
    orchestrator.data_dir = str(tmp_path)

    registry = AsyncMock()
    registry.get.return_value = {
        "container_id": "c1",
        "user_id": "u1",
        "status": "ready",
        "http_url": "http://localhost:1234",
    }

    return AdminOperations(
        orchestrator=orchestrator,
        docker_client=docker_client,
        registry=registry,
        schema_cache=schema_cache,
    )


@pytest.mark.asyncio
async def test_write_calls_zeroclaw_config_set(ops, monkeypatch):
    async def fake_health(http_url, timeout=10):
        return True

    monkeypatch.setattr("claw_proxy.admin.operations._health_ok", fake_health)

    result = await ops.write_config(
        "u1",
        updates=[{"path": "llm.model", "value": "claude-4-7"}],
    )

    assert result[0]["applied"] is True
    container = ops.docker.containers.get.return_value
    container.exec_run.assert_called()


@pytest.mark.asyncio
async def test_write_rolls_back_on_unhealthy(ops, monkeypatch):
    async def fake_health(http_url, timeout=10):
        return False

    monkeypatch.setattr("claw_proxy.admin.operations._health_ok", fake_health)

    result = await ops.write_config(
        "u1",
        updates=[{"path": "llm.model", "value": "broken"}],
    )

    assert result[0]["applied"] is False
    assert result[0]["rollback_available"] is True
    assert "health" in (result[0]["error"] or "").lower()
    container = ops.docker.containers.get.return_value
    assert container.exec_run.call_count >= 2
