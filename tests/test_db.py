import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import FAKE_USER, FAKE_ENCRYPTION_KEY


@pytest.fixture
def mock_http():
    """Mock httpx responses for Supabase REST."""
    with patch("claw_proxy.db.http_client") as mock:
        yield mock


@pytest.fixture
def registry():
    from claw_proxy.db import ContainerRegistry
    return ContainerRegistry(FAKE_ENCRYPTION_KEY)


class TestGetContainer:
    @pytest.mark.asyncio
    async def test_found(self, registry, mock_http):
        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=lambda: [{
                "user_id": FAKE_USER["id"],
                "container_id": "abc123",
                "ws_url": "ws://localhost:9000/ws/chat",
                "http_url": "http://localhost:9000",
                "bearer_token_enc": registry.crypto.encrypt("tok_secret"),
                "status": "ready",
                "current_session_id": "sess-123",
            }],
        ))
        info = await registry.get(FAKE_USER["id"])
        assert info is not None
        assert info["container_id"] == "abc123"
        assert info["bearer_token"] == "tok_secret"  # decrypted
        assert info["current_session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_not_found(self, registry, mock_http):
        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=200, json=lambda: [],
        ))
        assert await registry.get(FAKE_USER["id"]) is None

    @pytest.mark.asyncio
    async def test_db_error(self, registry, mock_http):
        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=500, text="internal error",
        ))
        with pytest.raises(Exception, match="Database"):
            await registry.get(FAKE_USER["id"])


class TestInsertContainer:
    @pytest.mark.asyncio
    async def test_insert(self, registry, mock_http):
        mock_http.post = AsyncMock(return_value=AsyncMock(status_code=201))
        await registry.insert(
            user_id=FAKE_USER["id"],
            container_id="abc123",
            ws_url="ws://localhost:9000/ws/chat",
            http_url="http://localhost:9000",
            bearer_token="tok_secret",
        )
        # Verify the token was encrypted before sending
        call_kwargs = mock_http.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["bearer_token_enc"] != "tok_secret"


class TestUpdateActivity:
    @pytest.mark.asyncio
    async def test_update(self, registry, mock_http):
        mock_http.patch = AsyncMock(return_value=AsyncMock(status_code=200))
        await registry.update_activity(FAKE_USER["id"])
        mock_http.patch.assert_called_once()


class TestGetProfile:
    @pytest.mark.asyncio
    async def test_found(self, mock_http):
        from claw_proxy.db import get_profile

        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=lambda: [{
                "first_name": "Alice",
                "last_name": "Smith",
                "date_of_birth": "1990-05-15",
            }],
        ))

        profile = await get_profile(FAKE_USER["id"])

        assert profile["first_name"] == "Alice"
        assert profile["last_name"] == "Smith"
        assert profile["date_of_birth"] == "1990-05-15"

    @pytest.mark.asyncio
    async def test_not_found(self, mock_http):
        from claw_proxy.db import get_profile

        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=200,
            json=lambda: [],
        ))

        profile = await get_profile(FAKE_USER["id"])

        assert profile == {}

    @pytest.mark.asyncio
    async def test_error_returns_empty(self, mock_http):
        from claw_proxy.db import get_profile

        mock_http.get = AsyncMock(return_value=AsyncMock(
            status_code=500,
            text="error",
        ))

        profile = await get_profile(FAKE_USER["id"])

        assert profile == {}


class TestUpdateSessionId:
    @pytest.mark.asyncio
    async def test_update(self, registry, mock_http):
        mock_http.patch = AsyncMock(return_value=AsyncMock(status_code=200))

        await registry.update_session_id(FAKE_USER["id"], "new-sess-123")

        mock_http.patch.assert_called_once()
        call_kwargs = mock_http.patch.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["current_session_id"] == "new-sess-123"

    @pytest.mark.asyncio
    async def test_clear(self, registry, mock_http):
        mock_http.patch = AsyncMock(return_value=AsyncMock(status_code=200))

        await registry.update_session_id(FAKE_USER["id"], None)

        mock_http.patch.assert_called_once()
        call_kwargs = mock_http.patch.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["current_session_id"] is None
