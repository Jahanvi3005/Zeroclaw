from __future__ import annotations

import secrets
from asyncio import TimeoutError
from unittest.mock import AsyncMock

import pytest

from claw_proxy.containers.orchestrator import ContainerOrchestrator


@pytest.mark.integration
async def test_provision_then_stop(
    real_docker_client, zeroclaw_image, real_data_dir, cleanup_container
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
    user_id = f"{secrets.token_hex(4)}-aaaa-bbbb-cccc-deadbeef0001"
    container_name = f"zeroclaw-{user_id[:8]}"
    info = None
    try:
        info = await orch.provision(user_id)
    except TimeoutError as exc:
        cleanup_container(container_name)
        pytest.skip(f"zeroclaw image did not become healthy in time: {exc}")
    try:
        assert info.container_id
        assert info.http_url.startswith("http://localhost:")
    finally:
        await orch.stop(user_id)
        cleanup_container(info.container_id)
