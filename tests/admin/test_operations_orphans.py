from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ops():
    from claw_proxy.admin.operations import AdminOperations

    docker_client = MagicMock()
    fake_container_a = MagicMock(
        id="abc123",
        name="zeroclaw-aabbccdd",
        attrs={"State": {"Status": "running"}},
    )
    fake_container_a.name = "zeroclaw-aabbccdd"
    fake_container_b = MagicMock(
        id="def456",
        name="zeroclaw-eeff0011",
        attrs={"State": {"Status": "exited"}},
    )
    fake_container_b.name = "zeroclaw-eeff0011"
    docker_client.containers.list.return_value = [fake_container_a, fake_container_b]
    docker_client.containers.get.return_value = fake_container_b

    registry = AsyncMock()
    registry.list_all.return_value = [{"container_id": "abc123", "user_id": "u1"}]

    orch = MagicMock()
    orch.docker = docker_client
    return AdminOperations(orchestrator=orch, docker_client=docker_client, registry=registry)


@pytest.mark.asyncio
async def test_list_orphans(ops):
    items = await ops.list_orphans()
    ids = [item["container_id"] for item in items]

    assert "def456" in ids
    assert "abc123" not in ids


@pytest.mark.asyncio
async def test_remove_orphan(ops):
    await ops.remove_orphan("def456")
    container = ops.docker.containers.list.return_value[1]
    container.stop.assert_called()
    container.remove.assert_called()
