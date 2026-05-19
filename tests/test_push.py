from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeWebSocket:
    def __init__(self) -> None:
        self.send_json = AsyncMock()


def _make_client(
    token_map: dict[str, str],
    connections: dict[str, set],
    *,
    orchestrator=None,
    get_supabase_client=None,
):
    from claw_proxy.push import create_push_router

    app = FastAPI()
    app.include_router(
        create_push_router(
            token_map=token_map,
            get_connections=lambda: connections,
            orchestrator=orchestrator,
            get_supabase_client=get_supabase_client,
        ),
        prefix="/zeroclaw",
    )
    return TestClient(app)


class TestPushWebhook:
    def test_valid_token_returns_200(self):
        client = _make_client({"known-token": "user-1"}, {})

        response = client.post(
            "/zeroclaw/push",
            headers={"Authorization": "Bearer known-token"},
            json={"content": "hello"},
        )

        assert response.status_code == 200
        assert response.json() == {"delivered": 0}

    def test_invalid_token_returns_401(self):
        client = _make_client({"known-token": "user-1"}, {})

        response = client.post(
            "/zeroclaw/push",
            headers={"Authorization": "Bearer unknown-token"},
            json={"content": "hello"},
        )

        assert response.status_code == 401
        assert response.json() == {"error": "unknown token"}

    def test_missing_token_returns_401(self):
        client = _make_client({"known-token": "user-1"}, {})

        response = client.post("/zeroclaw/push", json={"content": "hello"})

        assert response.status_code == 401
        assert response.json() == {"error": "missing bearer token"}

    def test_routes_push_to_active_connection(self):
        fake_ws = FakeWebSocket()
        client = _make_client(
            {"known-token": "user-1"},
            {"user-1": {fake_ws}},
        )

        response = client.post(
            "/zeroclaw/push",
            headers={"Authorization": "Bearer known-token"},
            json={"content": "hello", "subject": "reminder"},
        )

        assert response.status_code == 200
        assert response.json() == {"delivered": 1}
        fake_ws.send_json.assert_awaited_once_with(
            {
                "type": "push.message",
                "content": "hello",
                "subject": "reminder",
            }
        )

    def test_offline_user_returns_delivered_zero(self):
        client = _make_client({"known-token": "user-1"}, {"user-1": set()})

        response = client.post(
            "/zeroclaw/push",
            headers={"Authorization": "Bearer known-token"},
            json={"content": "hello"},
        )

        assert response.status_code == 200
        assert response.json() == {"delivered": 0}

    def test_missing_content_returns_400(self):
        client = _make_client({"known-token": "user-1"}, {})

        response = client.post(
            "/zeroclaw/push",
            headers={"Authorization": "Bearer known-token"},
            json={},
        )

        assert response.status_code == 400
        assert response.json() == {"error": "missing content"}

    def test_push_handler_processes_export_tags(self):
        fake_ws = FakeWebSocket()
        orch = AsyncMock()
        orch.get = AsyncMock(return_value=type(
            "ContainerInfo",
            (),
            {"container_id": "container-123"},
        )())
        orch.data_dir = "/data/zeroclaw"
        orch.docker = object()
        process_mock = AsyncMock(
            return_value={
                "type": "push.message",
                "content": "https://signed.example/out.txt",
            }
        )

        client = _make_client(
            {"known-token": "user-1"},
            {"user-1": {fake_ws}},
            orchestrator=orch,
            get_supabase_client=lambda: object(),
        )

        from unittest.mock import patch

        with patch("claw_proxy.push.process_download_tags", new=process_mock):
            response = client.post(
                "/zeroclaw/push",
                headers={"Authorization": "Bearer known-token"},
                json={"content": "[SAVE:/zeroclaw-data/workspace/output.txt]"},
            )

        assert response.status_code == 200
        process_mock.assert_awaited_once()
        fake_ws.send_json.assert_awaited_once_with(
            {
                "type": "push.message",
                "content": "https://signed.example/out.txt",
            }
        )
