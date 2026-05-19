from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from claw_proxy.admin.activity import ActivityState


def _state(*, idle: bool) -> ActivityState:
    return ActivityState(
        is_idle=idle,
        last_activity_at=None,
        signals=[] if idle else ["WS"],
    )


@pytest.fixture
def ops():
    from claw_proxy.admin.operations import AdminOperations

    return AdminOperations(
        orchestrator=AsyncMock(),
        docker_client=MagicMock(),
        registry=AsyncMock(),
        activity_checker=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_stop_calls_orchestrator(ops):
    ops.activity_checker.get_state.return_value = _state(idle=True)

    result = await ops.stop_container("u1", careful=False)

    ops.orchestrator.stop.assert_awaited_once_with("u1")
    assert result["status"] == "stopped"


@pytest.mark.asyncio
async def test_careful_skips_when_busy(ops):
    ops.activity_checker.get_state.return_value = _state(idle=False)

    result = await ops.stop_container("u1", careful=True, careful_timeout_seconds=1)

    ops.orchestrator.stop.assert_not_awaited()
    assert result["status"] == "skipped"
    assert result["reason"] == "not idle within timeout"


@pytest.mark.asyncio
async def test_careful_proceeds_after_idle(ops):
    ops.activity_checker.get_state.side_effect = [
        _state(idle=False),
        _state(idle=True),
    ]

    result = await ops.restart_container(
        "u1",
        careful=True,
        careful_timeout_seconds=10,
        _poll_interval_seconds=0,
    )

    ops.orchestrator.restart.assert_awaited_once_with("u1")
    assert result["status"] == "restarted"


@pytest.mark.asyncio
async def test_start_calls_orchestrator(ops):
    result = await ops.start_container("u1")

    ops.orchestrator.start.assert_awaited_once_with("u1")
    assert result["status"] == "started"
