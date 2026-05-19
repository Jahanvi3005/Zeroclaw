import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.conftest import FAKE_USER, FAKE_ENCRYPTION_KEY


def _blocking_recv():
    """Return an AsyncMock for recv that blocks forever (until cancelled).

    This prevents the relay task from sending spurious status messages
    during tests that only care about the main WS handler logic.
    """
    async def _block():
        await asyncio.sleep(3600)
    mock = AsyncMock(side_effect=_block)
    return mock


@pytest.mark.asyncio
async def test_lifecycle_loop_sweeps_temp_and_lifeatlas_files(monkeypatch, tmp_path):
    import claw_proxy.app as app_module

    data_dir = tmp_path / "data"
    volume_dir = data_dir / "user-123"
    volume_dir.mkdir(parents=True)

    docker_sentinel = object()
    supabase_sentinel = object()
    temp_paths = ["workspace/temp/old.pdf"]
    lifeatlas_paths = ["workspace/lifeatlas/files/old.pdf"]
    finder_calls = []
    delete_calls = []
    export_calls = []

    def find_temp(volume_path, max_age_hours):
        finder_calls.append(("temp", volume_path, max_age_hours))
        return temp_paths

    def find_lifeatlas(volume_path, max_age_hours):
        finder_calls.append(("lifeatlas", volume_path, max_age_hours))
        return lifeatlas_paths

    def delete_files(docker_client, volume_path, paths):
        delete_calls.append((docker_client, volume_path, paths))

    async def delete_exports(supabase_client, *, user_id, max_age_hours):
        export_calls.append((supabase_client, user_id, max_age_hours))

    async def noop(*args, **kwargs):
        return None

    sleep_calls = 0

    async def sleep_once_then_cancel(delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            return None
        raise asyncio.CancelledError

    monkeypatch.setattr(
        app_module,
        "ZEROCLAW_CONFIG",
        {
            "lifecycle_check_interval_minutes": 0,
            "zeroclaw_data_dir": str(data_dir),
        },
    )
    monkeypatch.setattr(app_module, "get_supabase_client", lambda: supabase_sentinel)
    monkeypatch.setattr(app_module, "ensure_bucket_exists", noop)
    monkeypatch.setattr(app_module, "delete_expired_exports", delete_exports)
    monkeypatch.setattr(app_module, "find_expired_temp_uploads", find_temp)
    monkeypatch.setattr(
        app_module,
        "find_expired_lifeatlas_files",
        find_lifeatlas,
        raising=False,
    )
    monkeypatch.setattr(app_module, "delete_files_via_busybox", delete_files)
    monkeypatch.setattr(
        app_module,
        "_orchestrator",
        type("Orchestrator", (), {"docker": docker_sentinel})(),
    )
    monkeypatch.setattr(app_module, "_audit_store", None)
    monkeypatch.setattr(app_module, "_job_store", None)
    monkeypatch.setattr(app_module.asyncio, "sleep", sleep_once_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await app_module._lifecycle_loop()

    assert export_calls == [(supabase_sentinel, "user-123", 24)]
    assert finder_calls == [
        ("temp", str(volume_dir), 24),
        ("lifeatlas", str(volume_dir), 24),
    ]
    assert delete_calls == [
        (docker_sentinel, str(volume_dir), temp_paths),
        (docker_sentinel, str(volume_dir), lifeatlas_paths),
    ]


@pytest.fixture
def mock_orchestrator():
    orch = AsyncMock()
    orch.get = AsyncMock(return_value=MagicMock(
        container_id="c_abc",
        ws_url="ws://localhost:9000/ws/chat",
        http_url="http://localhost:9000",
        bearer_token="tok_secret",
        status="ready",
        current_session_id=None,
    ))
    orch.provision = AsyncMock()
    orch.update_activity = AsyncMock()
    orch.registry = AsyncMock()
    orch.registry.update_session_id = AsyncMock()
    orch.registry.get = AsyncMock(return_value={"current_session_id": None})
    orch.data_dir = "/data/zeroclaw"
    orch.docker = MagicMock()
    return orch


@pytest.fixture
def mock_auth():
    """Override get_current_user_ws to return fake user."""
    async def fake_auth(token: str):
        if token == "valid-jwt":
            return FAKE_USER.copy()
        return None
    return fake_auth


@pytest.fixture
def app(mock_orchestrator, mock_auth):
    from claw_proxy.ws.proxy import create_ws_app
    return create_ws_app(
        orchestrator=mock_orchestrator,
        auth_fn=mock_auth,
    )


class TestWSAuth:
    def test_missing_token_rejected(self, app):
        """No token: accepted, error message sent, then closed with 4001."""
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "AUTH_FAILED"
            assert "Missing token" in msg["message"]

    def test_invalid_token_rejected(self, app):
        """Bad token: accepted, error message sent, then closed with 4001."""
        client = TestClient(app)
        with client.websocket_connect("/ws?token=bad-jwt") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "AUTH_FAILED"
            assert "Invalid" in msg["message"]


class TestWSConnect:
    def test_connect_sends_connected_msg(self, app, mock_orchestrator):
        """Valid auth + ready container: receives 'connected' with session info."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "sess-1",
                "resumed": False,
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "connected"
                    # Session ID is a UUID generated by proxy, just verify it exists
                    assert "sessionId" in msg
                    assert msg["messages"] == []
                    assert msg["messageCount"] == 0

        mock_orchestrator.registry.update_session_id.assert_not_called()

    def test_connect_with_default_session_id(self, mock_orchestrator, mock_auth):
        """When default_session_id is provided, it's used instead of random UUID."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="fixed-sess-123",
        )
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "fixed-sess-123",
                "resumed": False,
                "message_count": 5,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = [{"role": "user", "content": "hi"}]
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "connected"
                    assert msg["sessionId"] == "fixed-sess-123"
                    assert msg["messageCount"] == 5
                    assert len(msg["messages"]) == 1

    def test_upstream_connect_failure(self, app, mock_orchestrator):
        """Upstream connect failure triggers restart attempt, then sends error and closes."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value=None)  # connect failed (both attempts)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with client.websocket_connect("/ws?token=valid-jwt") as ws:
                # Proxy first announces the restart attempt, then errors after retry fails.
                status = ws.receive_json()
                assert status["type"] == "status"
                msg = ws.receive_json()
                assert msg["type"] == "error"
                assert msg["code"] == "UPSTREAM_ERROR"

    def test_container_unavailable(self, mock_auth):
        """Orchestrator.get returning None and provision raising sends error."""
        from claw_proxy.ws.proxy import create_ws_app
        orch = AsyncMock()
        orch.get = AsyncMock(return_value=None)
        orch.provision = AsyncMock(side_effect=RuntimeError("Docker failed"))
        orch.update_activity = AsyncMock()
        app = create_ws_app(orchestrator=orch, auth_fn=mock_auth)

        client = TestClient(app)
        with client.websocket_connect("/ws?token=valid-jwt") as ws:
            # First message is status (Starting your assistant...)
            msg = ws.receive_json()
            if msg["type"] == "status":
                msg = ws.receive_json()
            assert msg["type"] == "error"
            assert msg["code"] == "CONTAINER_UNAVAILABLE"


class TestSessionResume:
    def test_resumes_stored_session(self, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        orch = AsyncMock()
        orch.get = AsyncMock(return_value=MagicMock(
            container_id="c_abc",
            ws_url="ws://localhost:9000/ws/chat",
            http_url="http://localhost:9000",
            bearer_token="tok_secret",
            status="ready",
            current_session_id="stored-sess-42",
        ))
        orch.update_activity = AsyncMock()
        orch.registry = AsyncMock()
        orch.registry.update_session_id = AsyncMock()
        app = create_ws_app(orchestrator=orch, auth_fn=mock_auth)

        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "stored-sess-42",
                "resumed": True,
                "message_count": 5,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = [{"role": "user", "content": "old msg"}]
                mock_sl.return_value = [
                    {"session_id": "stored-sess-42"},
                    {"session_id": "other-sess"},
                ]
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "connected"
                    assert msg["sessionId"] == "stored-sess-42"
                    assert len(msg["messages"]) == 1

        orch.registry.update_session_id.assert_not_called()

    def test_stale_session_clears_registry_and_starts_draft(self, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        orch = AsyncMock()
        orch.get = AsyncMock(return_value=MagicMock(
            container_id="c_abc",
            ws_url="ws://localhost:9000/ws/chat",
            http_url="http://localhost:9000",
            bearer_token="tok_secret",
            status="ready",
            current_session_id="stale-sess-99",
        ))
        orch.update_activity = AsyncMock()
        orch.registry = AsyncMock()
        orch.registry.update_session_id = AsyncMock()
        app = create_ws_app(orchestrator=orch, auth_fn=mock_auth)

        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "new-uuid",
                "resumed": False,
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = []
                mock_sl.return_value = [{"session_id": "other-sess-1"}]
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "connected"
                    assert msg["sessionId"] != "stale-sess-99"

        orch.registry.update_session_id.assert_called_once_with(FAKE_USER["id"], None)

    def test_no_stored_session_creates_new(self, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        orch = AsyncMock()
        orch.get = AsyncMock(return_value=MagicMock(
            container_id="c_abc",
            ws_url="ws://localhost:9000/ws/chat",
            http_url="http://localhost:9000",
            bearer_token="tok_secret",
            status="ready",
            current_session_id=None,
        ))
        orch.update_activity = AsyncMock()
        orch.registry = AsyncMock()
        orch.registry.update_session_id = AsyncMock()
        app = create_ws_app(orchestrator=orch, auth_fn=mock_auth)

        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "new-uuid",
                "resumed": False,
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()
                    assert msg["type"] == "connected"

        orch.registry.update_session_id.assert_not_called()


class TestWSRelay:
    def test_relay_processes_done_exports_only(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            get_supabase_client=lambda: object(),
        )
        client = TestClient(app)
        process_mock = AsyncMock(
            return_value={
                "type": "chat.done",
                "fullResponse": "https://signed.example/doc.pdf",
            }
        )

        with patch("claw_proxy.ws.proxy.process_download_tags", new=process_mock), patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "sess-1",
                "message_count": 0,
            })
            instance.connected = True
            recv_count = 0

            async def recv_side_effect():
                nonlocal recv_count
                recv_count += 1
                if recv_count == 1:
                    return {"type": "done", "full_response": "raw [DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"}
                await asyncio.sleep(3600)

            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()
                    received_done = ws.receive_json()

        assert received_done["fullResponse"] == "https://signed.example/doc.pdf"
        process_mock.assert_awaited_once()

    def test_connect_processes_transcript_exports(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            get_supabase_client=lambda: object(),
        )
        client = TestClient(app)
        transcript_mock = AsyncMock(
            return_value=[
                {
                    "role": "assistant",
                    "content": "Here: [doc.pdf](https://signed.example/doc.pdf)",
                }
            ]
        )

        with patch("claw_proxy.ws.proxy.process_transcript_download_tags", new=transcript_mock), patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "sess-1",
                "message_count": 1,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = [
                    {
                        "role": "assistant",
                        "content": "Here: [DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]",
                    }
                ]
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    msg = ws.receive_json()

        assert msg["type"] == "connected"
        assert msg["messages"] == [
            {
                "role": "assistant",
                "content": "Here: [doc.pdf](https://signed.example/doc.pdf)",
            }
        ]
        transcript_mock.assert_awaited_once()
        kwargs = transcript_mock.await_args.kwargs
        assert kwargs["user_id"] == FAKE_USER["id"]
        assert kwargs["container_id"] == "c_abc"
        assert kwargs["volume_path"] == f"/data/zeroclaw/{FAKE_USER['id']}"

    def test_relay_leaves_chunk_exports_unchanged(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app

        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            get_supabase_client=lambda: object(),
        )
        client = TestClient(app)
        process_mock = AsyncMock()

        with patch("claw_proxy.ws.proxy.process_download_tags", new=process_mock), patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "sess-1",
                "message_count": 0,
            })
            instance.connected = True
            recv_count = 0

            async def recv_side_effect():
                nonlocal recv_count
                recv_count += 1
                if recv_count == 1:
                    return {"type": "chunk", "content": "[DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"}
                await asyncio.sleep(3600)

            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()
                    received_chunk = ws.receive_json()

        assert received_chunk == {
            "type": "chat.chunk",
            "content": "[DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]",
        }
        process_mock.assert_not_awaited()

    def test_chat_message_forwarded_upstream(self, app, mock_orchestrator):
        """Frontend chat message is translated and sent upstream."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "s1",
                "message_count": 0,
            })
            instance.connected = True
            instance.send = AsyncMock()

            done_emitted = False

            async def recv_side_effect():
                nonlocal done_emitted
                if not done_emitted:
                    done_emitted = True
                    return {"type": "done", "full_response": ""}
                await asyncio.sleep(3600)

            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    ws.send_json({"type": "message", "content": "Hello!"})
                    time.sleep(0.05)

                # After disconnect, verify send was called
                instance.send.assert_called_once_with(
                    {"type": "message", "content": "Hello!"}
                )

    def test_invalid_json_returns_error(self, app, mock_orchestrator):
        """Invalid JSON from frontend gets an error response."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "s1",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    ws.send_text("not valid json{{{")
                    time.sleep(0.05)
                    msg = ws.receive_json()
                    assert msg["type"] == "error"
                    assert msg["code"] == "INVALID_MESSAGE"

    def test_unknown_message_type_returns_error(self, app, mock_orchestrator):
        """Unknown message type gets an error response."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "s1",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    ws.send_json({"type": "bogus", "data": "stuff"})
                    time.sleep(0.05)
                    msg = ws.receive_json()
                    assert msg["type"] == "error"
                    assert msg["code"] == "INVALID_MESSAGE"
                    assert "bogus" in msg["message"]

    def test_upstream_chunk_relayed(self, app, mock_orchestrator):
        """Upstream chunk messages are translated and forwarded to frontend."""
        client = TestClient(app)
        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            call_count = 0

            async def recv_side_effect():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return {"type": "chunk", "content": "Hello "}
                if call_count == 2:
                    return {"type": "done", "full_response": "Hello world"}
                # Block after delivering messages
                await asyncio.sleep(3600)

            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "s1",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    # Receive chunk
                    msg = ws.receive_json()
                    assert msg["type"] == "chat.chunk"
                    assert msg["content"] == "Hello "
                    # Receive done
                    msg = ws.receive_json()
                    assert msg["type"] == "chat.done"
                    assert msg["fullResponse"] == "Hello world"


class TestMultiTabFanout:
    def test_two_tabs_same_session_share_upstream(self, mock_orchestrator, mock_auth):
        """Both tabs land in the same SharedSession; UpstreamConnection
        is constructed only once."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="shared-sess",
        )
        client = TestClient(app)

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "shared-sess",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = []
                mock_sl.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected — should reuse upstream
                        # UpstreamConnection should have been instantiated exactly once
                        assert MockUpstream.call_count == 1

    def test_chat_chunk_fans_out_to_all_tabs(self, mock_orchestrator, mock_auth):
        """A chunk from upstream reaches both connected tabs."""
        from claw_proxy.ws.proxy import create_ws_app
        import claw_proxy.ws.shared_session as shared_session
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="shared-sess",
        )
        client = TestClient(app)

        chunks = [
            {"type": "chunk", "content": "Hi"},
            {"type": "done", "full_response": "Hi"},
        ]
        chunk_iter = iter(chunks)

        async def recv_side_effect():
            # Block until both tabs have joined the SharedSession so the
            # broadcast reaches both. Prevents the test race where the
            # relay drains upstream before tab B connects.
            while True:
                sess = shared_session.get_session(
                    FAKE_USER["id"], "shared-sess"
                )
                if sess is not None and len(sess.connections) >= 2:
                    break
                await asyncio.sleep(0.01)
            try:
                return next(chunk_iter)
            except StopIteration:
                await asyncio.sleep(3600)

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "shared-sess",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected
                        msg_a = ws_a.receive_json()
                        msg_b = ws_b.receive_json()
                        assert msg_a["type"] == "chat.chunk"
                        assert msg_b["type"] == "chat.chunk"
                        assert msg_a["content"] == "Hi"
                        assert msg_b["content"] == "Hi"

    def test_user_message_fans_out_to_other_tabs_only(self, mock_orchestrator, mock_auth):
        """A locally typed user message is echoed to sibling tabs, not back to sender."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="shared-sess",
        )
        client = TestClient(app)

        import anyio

        sent = asyncio.Event()
        allow_done = asyncio.Event()
        done_emitted = False

        async def fake_send(msg):
            assert msg == {"type": "message", "content": "hello"}
            sent.set()

        async def fake_recv():
            nonlocal done_emitted
            await sent.wait()
            await allow_done.wait()
            if done_emitted:
                await asyncio.sleep(3600)
            done_emitted = True
            return {"type": "done", "full_response": "reply"}

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "shared-sess",
                "message_count": 0,
            })
            instance.connected = True
            instance.send = AsyncMock(side_effect=fake_send)
            instance.recv = AsyncMock(side_effect=fake_recv)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected

                        ws_a.send_json({"type": "message", "content": "hello"})
                        time.sleep(0.05)
                        echoed_raw = ws_b._send_rx.receive_nowait()
                        echoed = json.loads(echoed_raw["text"])
                        assert echoed == {"type": "message", "content": "hello"}

                        try:
                            stray = ws_a._send_rx.receive_nowait()
                            assert False, f"sender received unexpected message: {stray}"
                        except anyio.WouldBlock:
                            pass

                        allow_done.set()
                        done_a = ws_a.receive_json()
                        done_b = ws_b.receive_json()
                        assert done_a == {"type": "chat.done", "fullResponse": "reply"}
                        assert done_b == {"type": "chat.done", "fullResponse": "reply"}

    def test_first_tab_disconnect_keeps_upstream(self, mock_orchestrator, mock_auth):
        """When tab A disconnects but tab B remains, upstream stays open."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="shared-sess",
        )
        client = TestClient(app)

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "shared-sess",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                    ws_b.receive_json()
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                        ws_a.receive_json()
                    # ws_a closed; ws_b still open
                    time.sleep(0.05)
                    instance.disconnect.assert_not_called()
                # both closed now
                time.sleep(0.05)
                instance.disconnect.assert_called_once()

    def test_concurrent_sends_serialize_via_send_lock(
        self, mock_orchestrator, mock_auth
    ):
        """When tab A is mid-stream, tab B's send waits until A's chat.done."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="shared-sess",
        )
        client = TestClient(app)

        # The mock upstream emits one done per `send` call. We track send order.
        send_order: list[str] = []
        pending_done = asyncio.Event()
        pending_done.set()  # ready to emit done immediately

        async def fake_send(msg):
            send_order.append(msg["content"])
            # Hold the line so concurrent senders queue up
            await asyncio.sleep(0.05)

        emit_count = 0
        async def fake_recv():
            nonlocal emit_count
            # Emit a done after each send
            while len(send_order) <= emit_count:
                await asyncio.sleep(0.01)
            emit_count += 1
            return {"type": "done", "full_response": ""}

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "shared-sess",
                "message_count": 0,
            })
            instance.connected = True
            instance.send = AsyncMock(side_effect=fake_send)
            instance.recv = AsyncMock(side_effect=fake_recv)
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected
                        ws_a.send_json({"type": "message", "content": "first"})
                        ws_b.send_json({"type": "message", "content": "second"})
                        # Drain the chat.done events from both tabs
                        time.sleep(0.5)

        # The send_lock guarantees A's send happens entirely before B's send
        assert send_order == ["first", "second"]


class TestSessionSwitch:
    def test_switch_moves_caller_only(self, mock_orchestrator, mock_auth):
        """Tab A switching to a new session does not affect tab B."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-main",
        )
        client = TestClient(app)

        constructed = []

        def factory(*args, **kwargs):
            instance = AsyncMock()
            sid_holder = {"sid": None}

            async def connect(session_id, name=None):
                sid_holder["sid"] = session_id
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            instance.session_id = property(lambda self: sid_holder["sid"])
            constructed.append(instance)
            return instance

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = []
                mock_sl.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                    ws_b.receive_json()  # connected on sess-main
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                        ws_a.receive_json()  # connected on sess-main (joins same SharedSession)
                        # Tab A switches to a different session
                        ws_a.send_json({"type": "session.switch", "sessionId": "other-sess"})
                        # Tab A receives status + session.switched
                        msg = ws_a.receive_json()
                        if msg["type"] == "status":
                            msg = ws_a.receive_json()
                        assert msg["type"] == "session.switched"
                        assert msg["sessionId"] == "other-sess"

                        # Tab B should not have received any unsolicited message.
                        # _send_rx is the anyio stream the client reads from (server→client);
                        # receive_nowait() raises WouldBlock if nothing is buffered.
                        import anyio
                        try:
                            stray = ws_b._send_rx.receive_nowait()
                            assert False, f"Tab B received unexpected message: {stray}"
                        except anyio.WouldBlock:
                            pass

                        # A second upstream should have been constructed
                        assert len(constructed) == 2
                        # Draft switches do not update the resumable session.
                        mock_orchestrator.registry.update_session_id.assert_not_called()

    def test_switch_tears_down_old_session_when_caller_was_last(
        self, mock_orchestrator, mock_auth
    ):
        """Tab A is alone on X, switches to Y; X's upstream is disconnected."""
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-x",
        )
        client = TestClient(app)

        constructed: list[AsyncMock] = []

        def factory(*args, **kwargs):
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "any",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            constructed.append(instance)
            return instance

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = []
                mock_sl.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    ws.send_json({"type": "session.switch", "sessionId": "sess-y"})
                    msg = ws.receive_json()
                    if msg["type"] == "status":
                        msg = ws.receive_json()
                    assert msg["type"] == "session.switched"
                    # The first upstream (for sess-x) was disconnected
                    constructed[0].disconnect.assert_called_once()


class TestSessionCreate:
    def test_create_moves_caller_only(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-main",
        )
        client = TestClient(app)

        sids: list[str] = []

        def factory(*args, **kwargs):
            instance = AsyncMock()

            async def connect(session_id, name=None):
                sids.append(session_id)
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            return instance

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()
                    ws.send_json({"type": "session.create", "name": "Brainstorm"})
                    msg = ws.receive_json()
                    if msg["type"] == "status":
                        msg = ws.receive_json()
                    assert msg["type"] == "session.created"
                    assert msg["name"] == "Brainstorm"
                    assert "sessionId" in msg
                    assert msg["sessionId"] != "sess-main"
                    # Draft creation does not update the resumable session.
                    mock_orchestrator.registry.update_session_id.assert_not_called()

    def test_create_named_draft_is_not_listed_until_confirmed(
        self, mock_orchestrator, mock_auth
    ):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-main",
        )
        client = TestClient(app)

        def factory(*args, **kwargs):
            instance = AsyncMock()

            async def connect(session_id, name=None):
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            return instance

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl:
                mock_tx.return_value = []
                mock_sl.return_value = [{"session_id": "sess-main", "name": "Main"}]
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()
                    ws.send_json({"type": "session.create", "name": "Brainstorm"})
                    msg = ws.receive_json()
                    if msg["type"] == "status":
                        msg = ws.receive_json()
                    assert msg["type"] == "session.created"
                    new_session_id = msg["sessionId"]

                    ws.send_json({"type": "session.list"})
                    listed = ws.receive_json()
                    assert listed["type"] == "session.list.result"
                    by_id = {s["sessionId"]: s for s in listed["sessions"]}
                    assert new_session_id not in by_id
                    assert by_id["sess-main"]["active"] == "none"

    def test_first_message_confirms_draft_and_persists_name(
        self, mock_orchestrator, mock_auth
    ):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-main",
        )
        client = TestClient(app)

        def factory(*args, **kwargs):
            instance = AsyncMock()
            send_started = asyncio.Event()
            done_emitted = False

            async def connect(session_id, name=None):
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            async def send_side_effect(msg):
                send_started.set()

            async def recv_side_effect():
                nonlocal done_emitted
                await send_started.wait()
                if done_emitted:
                    await asyncio.sleep(3600)
                done_emitted = True
                return {"type": "done", "full_response": "ok"}

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = AsyncMock(side_effect=recv_side_effect)
            instance.send = AsyncMock(side_effect=send_side_effect)
            instance.disconnect = AsyncMock()
            return instance

        list_resp = [
            {"session_id": "sess-main", "name": "Main"},
        ]
        rename_resp = MagicMock()
        rename_resp.status_code = 200

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, patch(
                "claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock
            ) as mock_sl, patch("claw_proxy.config.http_client") as mock_http:
                mock_tx.return_value = []
                mock_sl.return_value = list_resp
                mock_http.put = AsyncMock(return_value=rename_resp)
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()
                    ws.send_json({"type": "session.create", "name": "Brainstorm"})
                    created = ws.receive_json()
                    if created["type"] == "status":
                        created = ws.receive_json()
                    assert created["type"] == "session.created"
                    new_session_id = created["sessionId"]

                    mock_sl.side_effect = [
                        [
                            {"session_id": "sess-main", "name": "Main"},
                            {
                                "session_id": new_session_id,
                                "name": None,
                                "message_count": 2,
                            },
                        ],
                        [
                            {"session_id": "sess-main", "name": "Main"},
                            {
                                "session_id": new_session_id,
                                "name": "Brainstorm",
                                "message_count": 2,
                            },
                        ],
                    ]

                    ws.send_json({"type": "message", "content": "hello"})
                    done = ws.receive_json()
                    assert done["type"] == "chat.done"

                    ws.send_json({"type": "session.list"})
                    listed = ws.receive_json()
                    assert listed["type"] == "session.list.result"
                    by_id = {s["sessionId"]: s for s in listed["sessions"]}
                    assert by_id[new_session_id]["name"] == "Brainstorm"
                    assert by_id[new_session_id]["active"] == "current"
                    mock_orchestrator.registry.update_session_id.assert_called_once_with(
                        FAKE_USER["id"], new_session_id
                    )
                    mock_http.put.assert_awaited_once_with(
                        f"http://localhost:9000/api/sessions/{new_session_id}",
                        headers={
                            "Authorization": "Bearer tok_secret",
                            "Content-Type": "application/json",
                        },
                        json={"name": "Brainstorm"},
                        timeout=10,
                    )


class TestSessionRename:
    def test_rename_broadcasts_to_all_user_tabs(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-main",
        )
        client = TestClient(app)

        with patch("claw_proxy.ws.proxy.UpstreamConnection") as MockUpstream:
            instance = AsyncMock()
            instance.connect = AsyncMock(return_value={
                "type": "session_start",
                "session_id": "sess-main",
                "message_count": 0,
            })
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            MockUpstream.return_value = instance

            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                with patch("claw_proxy.config.http_client") as mock_http:
                    mock_http.put = AsyncMock(return_value=mock_resp)
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                        ws_a.receive_json()
                        with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                            ws_b.receive_json()
                            ws_a.send_json({
                                "type": "session.rename",
                                "sessionId": "sess-main",
                                "name": "New Name",
                            })
                            time.sleep(0.05)
                            msg_a = ws_a.receive_json()
                            msg_b = ws_b.receive_json()
                            assert msg_a["type"] == "session.renamed"
                            assert msg_b["type"] == "session.renamed"
                            assert msg_a["sessionId"] == "sess-main"
                            assert msg_a["name"] == "New Name"


def _drain_ws(ws, sleep_secs=0.15):
    """Sleep then drain all buffered server->client messages without blocking.

    Uses anyio's receive_nowait() so the call returns immediately when the
    buffer is empty instead of blocking until the next server push.
    """
    import anyio
    time.sleep(sleep_secs)
    msgs = []
    while True:
        try:
            raw = ws._send_rx.receive_nowait()
            if raw.get("type") == "websocket.close":
                break
            text = raw.get("text") or (raw.get("bytes") or b"").decode()
            if text:
                msgs.append(json.loads(text))
        except anyio.WouldBlock:
            break
    return msgs


class TestSessionDelete:
    def _patch_upstream_factory(self):
        """Returns (patcher, instances_list). Each construct creates a new mock."""
        instances = []

        def factory(*args, **kwargs):
            instance = AsyncMock()

            async def connect(session_id, name=None):
                instance._sid = session_id
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            instances.append(instance)
            return instance

        return patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory), instances

    def test_delete_current_caller_gets_new_session(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-x",
        )
        client = TestClient(app)
        mock_orchestrator.get.return_value.current_session_id = "sess-x"
        # Registry default == current: triggers the D == X branch
        mock_orchestrator.registry.get = AsyncMock(return_value={"current_session_id": "sess-x"})

        patcher, _instances = self._patch_upstream_factory()
        with patcher, patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, \
             patch("claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock) as mock_sl:
            mock_tx.return_value = []
            mock_sl.return_value = [{"session_id": "sess-x"}]

            mock_resp = MagicMock()
            mock_resp.status_code = 204
            with patch("claw_proxy.config.http_client") as mock_http:
                mock_http.delete = AsyncMock(return_value=mock_resp)
                with client.websocket_connect("/ws?token=valid-jwt") as ws:
                    ws.receive_json()  # connected
                    ws.send_json({"type": "session.delete", "sessionId": "sess-x"})
                    msgs = _drain_ws(ws, sleep_secs=0.2)
                    seen_types = [m["type"] for m in msgs]
                    assert "session.deleted" in seen_types, f"seen: {seen_types}"
                    assert "session.switched" in seen_types, f"seen: {seen_types}"
                    deleted = next(m for m in msgs if m["type"] == "session.deleted")
                    switched = next(m for m in msgs if m["type"] == "session.switched")
                    assert deleted["sessionId"] == "sess-x"
                    assert switched["sessionId"] != "sess-x"
                    # No confirmed session exists until the new draft is persisted.
                    mock_orchestrator.registry.update_session_id.assert_any_call(
                        FAKE_USER["id"], None
                    )

    def test_delete_other_non_default_moves_other_tab_to_default(
        self, mock_orchestrator, mock_auth
    ):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-default",
        )
        client = TestClient(app)
        mock_orchestrator.get.return_value.current_session_id = "sess-default"
        mock_orchestrator.registry.get = AsyncMock(return_value={"current_session_id": "sess-default"})

        patcher, _instances = self._patch_upstream_factory()
        with patcher, patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, \
             patch("claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock) as mock_sl:
            mock_tx.return_value = []
            mock_sl.return_value = [
                {"session_id": "sess-default"},
                {"session_id": "sess-other"},
            ]
            mock_resp = MagicMock()
            mock_resp.status_code = 204
            with patch("claw_proxy.config.http_client") as mock_http:
                mock_http.delete = AsyncMock(return_value=mock_resp)
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected on sess-default
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected on sess-default
                        ws_b.send_json({"type": "session.switch", "sessionId": "sess-other"})
                        m = ws_b.receive_json()
                        if m["type"] == "status":
                            ws_b.receive_json()  # session.switched
                        ws_a.send_json({"type": "session.delete", "sessionId": "sess-other"})
                        msgs_b = _drain_ws(ws_b, sleep_secs=0.2)
                        types_b = [m["type"] for m in msgs_b]
                        assert "session.deleted" in types_b, f"ws_b msgs: {types_b}"
                        assert "session.switched" in types_b, f"ws_b msgs: {types_b}"
                        switched_b = next(m for m in msgs_b if m["type"] == "session.switched")
                        assert switched_b["sessionId"] == "sess-default"
                        assert switched_b.get("reason") == "deleted"

    def test_delete_other_when_other_is_default_clears_default_when_caller_is_on_draft(
        self, mock_orchestrator, mock_auth
    ):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-default",
        )
        client = TestClient(app)
        mock_orchestrator.get.return_value.current_session_id = "sess-default"
        mock_orchestrator.registry.get = AsyncMock(return_value={"current_session_id": "sess-default"})

        patcher, _instances = self._patch_upstream_factory()
        with patcher, patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx, \
             patch("claw_proxy.ws.proxy.fetch_session_list", new_callable=AsyncMock) as mock_sl:
            mock_tx.return_value = []
            mock_sl.return_value = [{"session_id": "sess-default"}]
            mock_resp = MagicMock()
            mock_resp.status_code = 204
            with patch("claw_proxy.config.http_client") as mock_http:
                mock_http.delete = AsyncMock(return_value=mock_resp)
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected on sess-default
                    ws_a.send_json({"type": "session.switch", "sessionId": "sess-other"})
                    m = ws_a.receive_json()
                    if m["type"] == "status":
                        ws_a.receive_json()  # session.switched
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected on sess-default
                        ws_a.send_json({"type": "session.delete", "sessionId": "sess-default"})
                        time.sleep(0.2)
                        mock_orchestrator.registry.update_session_id.assert_any_call(
                            FAKE_USER["id"], None
                        )
                        msgs_b = _drain_ws(ws_b, sleep_secs=0.0)
                        types_b = [m["type"] for m in msgs_b]
                        assert "session.deleted" in types_b, f"ws_b msgs: {types_b}"
                        switched_b = next(m for m in msgs_b if m["type"] == "session.switched")
                        assert switched_b["sessionId"] == "sess-other"

    def test_force_moved_tab_can_switch_again_without_duplicate_current(
        self, mock_orchestrator, mock_auth
    ):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-default",
        )
        client = TestClient(app)
        mock_orchestrator.get.return_value.current_session_id = "sess-default"
        mock_orchestrator.registry.get = AsyncMock(return_value={"current_session_id": "sess-default"})

        patcher, _instances = self._patch_upstream_factory()
        with patcher, patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
            mock_tx.return_value = []
            list_resp = MagicMock()
            list_resp.status_code = 200
            list_resp.json.return_value = [
                {"session_id": "sess-default", "name": "default"},
                {"session_id": "sess-other", "name": "other"},
                {"session_id": "sess-third", "name": "third"},
            ]
            delete_resp = MagicMock()
            delete_resp.status_code = 204
            with patch("claw_proxy.config.http_client") as mock_http:
                mock_http.get = AsyncMock(return_value=list_resp)
                mock_http.delete = AsyncMock(return_value=delete_resp)
                with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                    ws_a.receive_json()  # connected on sess-default
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                        ws_b.receive_json()  # connected on sess-default

                        ws_b.send_json({"type": "session.switch", "sessionId": "sess-other"})
                        m = ws_b.receive_json()
                        if m["type"] == "status":
                            ws_b.receive_json()  # session.switched

                        ws_a.send_json({"type": "session.delete", "sessionId": "sess-other"})
                        msgs_b = _drain_ws(ws_b, sleep_secs=0.2)
                        types_b = [m["type"] for m in msgs_b]
                        assert "session.switched" in types_b, f"ws_b msgs: {types_b}"

                        ws_b.send_json({"type": "session.switch", "sessionId": "sess-third"})
                        m = ws_b.receive_json()
                        if m["type"] == "status":
                            m = ws_b.receive_json()
                        assert m["type"] == "session.switched"
                        assert m["sessionId"] == "sess-third"

                        ws_b.send_json({"type": "session.list"})
                        listed = ws_b.receive_json()
                        assert listed["type"] == "session.list.result"
                        by_id = {s["sessionId"]: s for s in listed["sessions"]}
                        assert by_id["sess-third"]["active"] == "current"
                        assert by_id["sess-default"]["active"] == "other"


class TestSessionListActiveField:
    def test_list_includes_active_field(self, mock_orchestrator, mock_auth):
        from claw_proxy.ws.proxy import create_ws_app
        app = create_ws_app(
            orchestrator=mock_orchestrator,
            auth_fn=mock_auth,
            default_session_id="sess-mine",
        )
        client = TestClient(app)

        sids: list[str] = []

        def factory(*args, **kwargs):
            instance = AsyncMock()

            async def connect(session_id, name=None):
                sids.append(session_id)
                return {
                    "type": "session_start",
                    "session_id": session_id,
                    "message_count": 0,
                }

            instance.connect = AsyncMock(side_effect=connect)
            instance.connected = True
            instance.recv = _blocking_recv()
            instance.disconnect = AsyncMock()
            return instance

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [
            {"session_id": "sess-mine", "name": "mine"},
            {"session_id": "sess-other-tab", "name": "other"},
            {"session_id": "sess-empty", "name": "empty"},
        ]

        with patch("claw_proxy.ws.proxy.UpstreamConnection", side_effect=factory):
            with patch("claw_proxy.ws.proxy.fetch_transcript", new_callable=AsyncMock) as mock_tx:
                mock_tx.return_value = []
                with patch("claw_proxy.config.http_client") as mock_http:
                    mock_http.get = AsyncMock(return_value=list_resp)
                    # Tab A on sess-mine
                    with client.websocket_connect("/ws?token=valid-jwt") as ws_a:
                        ws_a.receive_json()
                        # Tab B opens, switches to sess-other-tab
                        with client.websocket_connect("/ws?token=valid-jwt") as ws_b:
                            ws_b.receive_json()
                            ws_b.send_json({
                                "type": "session.switch",
                                "sessionId": "sess-other-tab",
                            })
                            m = ws_b.receive_json()
                            if m["type"] == "status":
                                ws_b.receive_json()
                            # Tab A asks for session.list
                            ws_a.send_json({"type": "session.list"})
                            time.sleep(0.05)
                            msg = ws_a.receive_json()
                            assert msg["type"] == "session.list.result"
                            by_id = {s["sessionId"]: s for s in msg["sessions"]}
                            assert by_id["sess-mine"]["active"] == "current"
                            assert by_id["sess-other-tab"]["active"] == "other"
                            assert by_id["sess-empty"]["active"] == "none"
