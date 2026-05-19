from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import claw_proxy.admin.activity as activity_module
from claw_proxy.admin.activity import ActivityState, DefaultActivityChecker, _parse_iso_timestamp


class FakeRegistry:
    def __init__(self, row: dict | None) -> None:
        self.row = row

    async def get(self, user_id: str) -> dict | None:
        return self.row


def _connections_for(*user_ids: str):
    active = set(user_ids)

    def get_connections() -> dict[str, set[object]]:
        return {user_id: {object()} for user_id in active}

    return get_connections


@pytest.mark.asyncio
async def test_idle_when_no_ws_and_old_activity():
    now = datetime.now(timezone.utc)
    registry = FakeRegistry(
        {
            "last_active_at": (now - timedelta(seconds=61)).isoformat(),
        }
    )
    checker = DefaultActivityChecker(
        registry=registry,
        get_connections=_connections_for(),
        idle_threshold_seconds=60,
    )

    state = await checker.get_state("user_1")

    expected_last_activity = now - timedelta(seconds=61)
    assert state == ActivityState(
        is_idle=True,
        last_activity_at=expected_last_activity,
        signals=[],
    )


@pytest.mark.asyncio
async def test_not_idle_when_ws_open_and_signal_mentions_ws():
    registry = FakeRegistry(None)
    checker = DefaultActivityChecker(
        registry=registry,
        get_connections=_connections_for("user_1"),
        idle_threshold_seconds=60,
    )

    state = await checker.get_state("user_1")

    assert state.is_idle is False
    assert state.last_activity_at is None
    assert "1 active WS tab" in state.signals
    assert "no last_active_at" in state.signals


@pytest.mark.asyncio
async def test_not_idle_when_recent_activity():
    now = datetime.now(timezone.utc)
    registry = FakeRegistry(
        {
            "last_active_at": (now - timedelta(seconds=15)).isoformat(),
        }
    )
    checker = DefaultActivityChecker(
        registry=registry,
        get_connections=_connections_for(),
        idle_threshold_seconds=60,
    )

    state = await checker.get_state("user_1")

    assert state.is_idle is False
    assert state.last_activity_at is not None
    assert state.last_activity_at.tzinfo is timezone.utc
    assert state.signals == ["last activity 15 seconds ago"]


@pytest.mark.asyncio
async def test_not_idle_when_activity_is_exactly_at_threshold(monkeypatch):
    fixed_now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(activity_module, "datetime", FakeDateTime)

    registry = FakeRegistry(
        {
            "last_active_at": (fixed_now - timedelta(seconds=60)).isoformat(),
        }
    )
    checker = DefaultActivityChecker(
        registry=registry,
        get_connections=_connections_for(),
        idle_threshold_seconds=60,
    )

    state = await checker.get_state("user_1")

    assert state.is_idle is False
    assert state.last_activity_at == fixed_now - timedelta(seconds=60)
    assert state.signals == ["last activity 60 seconds ago"]


def test_parse_iso_timestamp_accepts_trailing_z():
    parsed = _parse_iso_timestamp("2026-04-22T10:00:00Z")

    assert parsed == datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc)
