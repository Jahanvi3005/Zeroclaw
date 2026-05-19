"""End-to-end test: frontend WS -> proxy -> mock ZeroClaw."""
import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from tests.conftest import FAKE_USER


@pytest.fixture
def e2e_app():
    """Create proxy app with mocked orchestrator and upstream."""
    mock_orch = AsyncMock()
    mock_orch.get = AsyncMock(return_value=MagicMock(
        container_id="c_test",
        ws_url="ws://localhost:9000/ws/chat",
        http_url="http://localhost:9000",
        bearer_token="tok",
        status="ready",
        current_session_id=None,
    ))
    mock_orch.provision = AsyncMock()
    mock_orch.update_activity = AsyncMock()
    mock_orch.registry = AsyncMock()
    mock_orch.registry.update_session_id = AsyncMock()

    async def mock_auth(token):
        return FAKE_USER.copy() if token == "valid" else None

    from claw_proxy.ws.proxy import create_ws_app
    return create_ws_app(orchestrator=mock_orch, auth_fn=mock_auth)


class TestE2EChatFlow:
    def test_send_message_gets_streamed_response(self, e2e_app):
        """Full flow: connect -> send message -> receive chunks -> done."""
        client = TestClient(e2e_app)

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream, \
             patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:

            mock_tx.return_value = [{"role": "assistant", "content": "Hi!"}]

            # Simulate ZeroClaw responses
            upstream_messages = [
                {"type": "chunk", "content": "Sure"},
                {"type": "chunk", "content": ", I'll help"},
                {"type": "done", "full_response": "Sure, I'll help"},
            ]
            msg_iter = iter(upstream_messages)

            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "s1",
                "resumed": False,
                "message_count": 1,
            })
            instance.connected = True

            async def mock_recv():
                try:
                    return next(msg_iter)
                except StopIteration:
                    await asyncio.sleep(10)  # block until test ends
                    return None

            instance.recv = mock_recv
            instance.send = AsyncMock()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with client.websocket_connect("/ws?token=valid") as ws:
                # Should get connected message with transcript
                msg = ws.receive_json()
                assert msg["type"] == "connected"
                assert msg["messages"] == [{"role": "assistant", "content": "Hi!"}]

                # Send a message
                ws.send_json({"type": "message", "content": "hello"})
                instance.send.assert_called()

                # Receive streamed response
                chunk1 = ws.receive_json()
                assert chunk1 == {"type": "chat.chunk", "content": "Sure"}

                chunk2 = ws.receive_json()
                assert chunk2 == {"type": "chat.chunk", "content": ", I'll help"}

                done = ws.receive_json()
                assert done == {"type": "chat.done", "fullResponse": "Sure, I'll help"}
