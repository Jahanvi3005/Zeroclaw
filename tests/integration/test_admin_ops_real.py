from __future__ import annotations

import os
import secrets
from asyncio import TimeoutError
from unittest.mock import AsyncMock

import pytest

from claw_proxy.admin.operations import AdminOperations
from claw_proxy.admin.schema_cache import SchemaCache
from claw_proxy.containers.orchestrator import ContainerOrchestrator


@pytest.mark.integration
async def test_workspace_write_and_config_read(
    real_docker_client,
    zeroclaw_image,
    real_data_dir,
    cleanup_container,
    reclaim_volume,
):
    registry = AsyncMock()
    registry.get.return_value = None
    registry.insert.return_value = None
    registry.update_urls = AsyncMock()
    registry.update_status = AsyncMock()

    orch = ContainerOrchestrator(
        registry=registry,
        docker_client=real_docker_client,
        image=zeroclaw_image,
        data_dir=str(real_data_dir),
        push_webhook_base_url="http://localhost:0",
        templates_dir="",
        network_mode="host",
    )
    user_id = f"{secrets.token_hex(4)}-aaaa-bbbb-cccc-deadbeef0002"
    container_name = f"zeroclaw-{user_id[:8]}"
    info = None
    try:
        info = await orch.provision(user_id)
    except TimeoutError as exc:
        cleanup_container(container_name)
        pytest.skip(f"zeroclaw image did not become healthy in time: {exc}")

    reclaim_volume(os.path.join(str(real_data_dir), user_id))

    try:
        registry.get.return_value = {
            "container_id": info.container_id,
            "user_id": user_id,
            "status": "ready",
            "http_url": info.http_url,
        }
        ops = AdminOperations(
            orchestrator=orch,
            docker_client=real_docker_client,
            registry=registry,
            schema_cache=SchemaCache(real_docker_client),
        )

        await ops.write_workspace_file(
            user_id,
            path="hello.txt",
            content="world",
            mode="overwrite",
        )
        listed = await ops.list_workspace_files(user_id, path="")
        assert any(item["path"] == "hello.txt" for item in listed)

        config = await ops.read_config(user_id)
        assert isinstance(config, dict)
    finally:
        await orch.stop(user_id)
        cleanup_container(info.container_id)
