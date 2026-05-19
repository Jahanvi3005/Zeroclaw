from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
import pytest

from claw_proxy.admin.audit import EncryptedJsonlAuditStore
from claw_proxy.admin.auth import WebAuthnConfig, hash_static_token
from claw_proxy.admin.jobs import EncryptedJsonJobStore
from claw_proxy.admin.storage import Admin, EncryptedJsonAdminStore, StaticToken
from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def fernet():
    return TokenCrypto(FAKE_ENCRYPTION_KEY)


JWT_SECRET = "test-jwt-secret"


@pytest.fixture
def admin_store(tmp_path, fernet):
    return EncryptedJsonAdminStore(tmp_path / "admin.json.enc", fernet)


@pytest.fixture
def audit_store(tmp_path, fernet):
    return EncryptedJsonlAuditStore(tmp_path / "audit.jsonl.enc", fernet)


@pytest.fixture
def job_store(tmp_path, fernet):
    return EncryptedJsonJobStore(tmp_path / "jobs", fernet)


@pytest.fixture
def webauthn_cfg():
    return WebAuthnConfig(rp_id="localhost", rp_name="Claw", origin="http://localhost")


@pytest.fixture
def admin_deps(admin_store, audit_store, job_store, webauthn_cfg):
    orchestrator = AsyncMock()
    docker_client = MagicMock()
    registry = AsyncMock()
    activity_checker = AsyncMock()
    schema_cache = AsyncMock()
    schema_cache.is_secret_path.return_value = False
    supabase = MagicMock()
    return {
        "admin_store": admin_store,
        "audit_store": audit_store,
        "job_store": job_store,
        "webauthn_cfg": webauthn_cfg,
        "jwt_secret": JWT_SECRET,
        "orchestrator": orchestrator,
        "docker_client": docker_client,
        "registry": registry,
        "activity_checker": activity_checker,
        "schema_cache": schema_cache,
        "supabase_client": supabase,
    }


@pytest.fixture
def app(admin_deps):
    from claw_proxy.admin.app import create_admin_app

    return create_admin_app(**admin_deps)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def static_token(admin_store):
    token = "test-static-token"
    import asyncio

    asyncio.run(
        admin_store.add_static_token(
            StaticToken(
                hash=hash_static_token(token),
                label="cli",
                created_at="t",
                last_used_at=None,
            )
        )
    )
    return token


@pytest.fixture
def authed_client(client, static_token):
    client.headers.update({"Authorization": f"Bearer {static_token}"})
    return client
