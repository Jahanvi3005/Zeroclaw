from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ops():
    from claw_proxy.admin.operations import AdminOperations

    return AdminOperations(
        orchestrator=AsyncMock(),
        docker_client=MagicMock(),
        registry=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_list_containers_basic(ops):
    ops.registry.list_all.return_value = [
        {
            "user_id": "u1",
            "container_id": "c1",
            "status": "ready",
            "last_active_at": "t1",
            "ws_url": "ws://x",
            "http_url": "http://x",
            "current_session_id": None,
        },
        {
            "user_id": "u2",
            "container_id": "c2",
            "status": "stopped",
            "last_active_at": "t2",
            "ws_url": "ws://y",
            "http_url": "http://y",
            "current_session_id": None,
        },
    ]

    items = await ops.list_containers()

    assert len(items) == 2
    assert {item["user_id"] for item in items} == {"u1", "u2"}


@pytest.mark.asyncio
async def test_list_filters_by_status(ops):
    ops.registry.list_all.return_value = [
        {
            "user_id": "u1",
            "container_id": "c1",
            "status": "ready",
            "last_active_at": "t1",
            "ws_url": "",
            "http_url": "",
            "current_session_id": None,
        },
        {
            "user_id": "u2",
            "container_id": "c2",
            "status": "stopped",
            "last_active_at": "t2",
            "ws_url": "",
            "http_url": "",
            "current_session_id": None,
        },
    ]

    items = await ops.list_containers(status="stopped")

    assert [item["user_id"] for item in items] == ["u2"]


@pytest.mark.asyncio
async def test_get_container_status(ops):
    ops.registry.get.return_value = {
        "user_id": "u1",
        "container_id": "c1",
        "status": "ready",
        "last_active_at": "t",
        "ws_url": "",
        "http_url": "",
        "current_session_id": None,
    }

    info = await ops.get_container("u1")

    assert info["user_id"] == "u1"
