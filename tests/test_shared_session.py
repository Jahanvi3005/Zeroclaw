import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from claw_proxy.ws.shared_session import (
    SharedSession,
    get_user_lock,
    get_session,
    list_user_sessions,
    register_session,
    unregister_session,
    add_tab,
    remove_tab,
    broadcast_to_session,
    broadcast_to_user,
    get_connections,
    reset_state,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_state()
    yield
    reset_state()


def _make_session(user_id="u1", session_id="s1"):
    return SharedSession(
        user_id=user_id,
        session_id=session_id,
        upstream=MagicMock(),
    )


class TestUserLock:
    @pytest.mark.asyncio
    async def test_returns_same_lock_for_same_user(self):
        a = get_user_lock("u1")
        b = get_user_lock("u1")
        assert a is b

    @pytest.mark.asyncio
    async def test_returns_different_lock_for_different_user(self):
        assert get_user_lock("u1") is not get_user_lock("u2")


class TestRegistry:
    def test_register_then_get(self):
        s = _make_session()
        register_session(s)
        assert get_session("u1", "s1") is s

    def test_get_returns_none_for_unknown(self):
        assert get_session("u1", "missing") is None

    def test_unregister_removes(self):
        s = _make_session()
        register_session(s)
        unregister_session(s)
        assert get_session("u1", "s1") is None

    def test_list_user_sessions(self):
        register_session(_make_session("u1", "a"))
        register_session(_make_session("u1", "b"))
        register_session(_make_session("u2", "c"))
        sessions = list_user_sessions("u1")
        assert {s.session_id for s in sessions} == {"a", "b"}


class TestTabs:
    def test_add_tab(self):
        s = _make_session()
        ws = MagicMock()
        add_tab(s, ws)
        assert ws in s.connections

    def test_remove_tab_returns_true_when_empty(self):
        s = _make_session()
        ws = MagicMock()
        add_tab(s, ws)
        assert remove_tab(s, ws) is True

    def test_remove_tab_returns_false_when_others_remain(self):
        s = _make_session()
        ws1, ws2 = MagicMock(), MagicMock()
        add_tab(s, ws1)
        add_tab(s, ws2)
        assert remove_tab(s, ws1) is False
        assert ws2 in s.connections


class TestBroadcasts:
    @pytest.mark.asyncio
    async def test_broadcast_to_session_sends_to_all_tabs(self):
        s = _make_session()
        ws1, ws2 = AsyncMock(), AsyncMock()
        add_tab(s, ws1)
        add_tab(s, ws2)
        await broadcast_to_session(s, {"type": "x"})
        ws1.send_json.assert_awaited_once_with({"type": "x"})
        ws2.send_json.assert_awaited_once_with({"type": "x"})

    @pytest.mark.asyncio
    async def test_broadcast_to_session_drops_dead_ws(self):
        s = _make_session()
        good = AsyncMock()
        dead = AsyncMock()
        dead.send_json.side_effect = RuntimeError("closed")
        add_tab(s, good)
        add_tab(s, dead)
        await broadcast_to_session(s, {"type": "x"})
        assert dead not in s.connections
        assert good in s.connections

    @pytest.mark.asyncio
    async def test_broadcast_to_user_covers_all_sessions(self):
        s1 = _make_session("u1", "a")
        s2 = _make_session("u1", "b")
        s3 = _make_session("u2", "c")
        ws_a = AsyncMock()
        ws_b = AsyncMock()
        ws_c = AsyncMock()
        add_tab(s1, ws_a)
        add_tab(s2, ws_b)
        add_tab(s3, ws_c)
        register_session(s1)
        register_session(s2)
        register_session(s3)

        await broadcast_to_user("u1", {"type": "x"})

        ws_a.send_json.assert_awaited_once_with({"type": "x"})
        ws_b.send_json.assert_awaited_once_with({"type": "x"})
        ws_c.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_to_user_dedupes_ws_in_multiple_sessions(self):
        # Same WS shouldn't appear in two SharedSessions in practice, but
        # the helper should still send only once if it does.
        s1 = _make_session("u1", "a")
        s2 = _make_session("u1", "b")
        register_session(s1)
        register_session(s2)
        ws = AsyncMock()
        add_tab(s1, ws)
        add_tab(s2, ws)

        await broadcast_to_user("u1", {"type": "x"})
        assert ws.send_json.await_count == 1


class TestGetConnections:
    def test_returns_user_id_to_ws_mapping(self):
        s1 = _make_session("u1", "a")
        s2 = _make_session("u1", "b")
        s3 = _make_session("u2", "c")
        ws_a, ws_b, ws_c = MagicMock(), MagicMock(), MagicMock()
        add_tab(s1, ws_a)
        add_tab(s2, ws_b)
        add_tab(s3, ws_c)
        register_session(s1)
        register_session(s2)
        register_session(s3)

        conns = get_connections()
        assert conns["u1"] == {ws_a, ws_b}
        assert conns["u2"] == {ws_c}
