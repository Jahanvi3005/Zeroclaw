import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestUpstreamConnect:
    @pytest.mark.asyncio
    async def test_connect_builds_url(self):
        from claw_proxy.ws.upstream import UpstreamConnection

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = MagicMock(return_value=iter([]))
        mock_ws.close = AsyncMock()

        with patch("claw_proxy.ws.upstream.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            conn = UpstreamConnection(
                ws_url="ws://localhost:9000/ws/chat",
                bearer_token="tok_abc",
            )
            url = conn._build_url("session-123")
            assert "session_id=session-123" in url
            assert "token=tok_abc" in url

    @pytest.mark.asyncio
    async def test_connect_uses_relaxed_upstream_keepalive(self):
        from claw_proxy.ws.upstream import UpstreamConnection

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "session_start", "session_id": "session-123"})
        )

        with patch("claw_proxy.ws.upstream.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws

            conn = UpstreamConnection(
                ws_url="ws://localhost:9000/ws/chat",
                bearer_token="tok_abc",
            )
            await conn.connect("session-123")

            mock_connect.assert_awaited_once_with(
                "ws://localhost:9000/ws/chat?session_id=session-123&token=tok_abc",
                ping_interval=300,
                ping_timeout=60,
            )

    @pytest.mark.asyncio
    async def test_connect_with_name(self):
        from claw_proxy.ws.upstream import UpstreamConnection

        conn = UpstreamConnection(
            ws_url="ws://localhost:9000/ws/chat",
            bearer_token="tok_abc",
        )
        url = conn._build_url("session-123", name="Work chat")
        assert "name=Work+chat" in url or "name=Work%20chat" in url


class TestUpstreamSend:
    @pytest.mark.asyncio
    async def test_send_json(self):
        from claw_proxy.ws.upstream import UpstreamConnection

        conn = UpstreamConnection(
            ws_url="ws://localhost:9000/ws/chat",
            bearer_token="tok",
        )
        mock_ws = AsyncMock()
        conn._ws = mock_ws

        await conn.send({"type": "message", "content": "hello"})
        mock_ws.send.assert_called_once_with(json.dumps({"type": "message", "content": "hello"}))
