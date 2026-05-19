from __future__ import annotations

from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient
import pytest

from claw_proxy.admin.auth import AdminContext, hash_static_token, issue_session_jwt, require_admin
from claw_proxy.admin.storage import Admin, EncryptedJsonAdminStore, StaticToken


JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes"


@pytest.fixture
def store(tmp_path, fernet):
    return EncryptedJsonAdminStore(tmp_path / "admin.json.enc", fernet)


@pytest.fixture
def app(store):
    app = FastAPI()

    async def _dep(authorization: str | None = Header(default=None)):
        return await require_admin(
            authorization=authorization,
            store=store,
            jwt_secret=JWT_SECRET,
        )

    @app.get("/protected")
    async def protected(ctx: AdminContext = Depends(_dep)):
        return {"channel": ctx.channel, "id": ctx.admin_id}

    return app


def test_no_header_rejected(app):
    client = TestClient(app)
    response = client.get("/protected")
    assert response.status_code == 401


def test_static_token_accepted(app, store):
    import asyncio

    asyncio.run(
        store.add_static_token(
            StaticToken(
                hash=hash_static_token("tok-123"),
                label="cli",
                created_at="t",
                last_used_at=None,
            )
        )
    )
    client = TestClient(app)
    response = client.get("/protected", headers={"Authorization": "Bearer tok-123"})
    assert response.status_code == 200
    assert response.json() == {"channel": "static_token", "id": "cli"}


def test_jwt_accepted(app):
    client = TestClient(app)
    token = issue_session_jwt("admin_a", secret=JWT_SECRET, ttl_seconds=60)
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"channel": "passkey", "id": "admin_a"}
