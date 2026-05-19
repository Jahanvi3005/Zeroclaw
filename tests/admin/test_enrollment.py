from __future__ import annotations

import hashlib

import pytest

from claw_proxy.admin.storage import EncryptedJsonAdminStore


@pytest.fixture
def store(tmp_path, fernet):
    return EncryptedJsonAdminStore(tmp_path / "admin.json.enc", fernet)


@pytest.mark.asyncio
async def test_bootstrap_when_empty(store):
    from claw_proxy.admin.enrollment import bootstrap_if_empty

    result = await bootstrap_if_empty(store)

    assert result is not None
    assert result.enrollment_token
    assert result.static_token
    tokens = await store.list_static_tokens()
    assert any(token.label == "first-run" for token in tokens)


@pytest.mark.asyncio
async def test_bootstrap_idempotent(store):
    from claw_proxy.admin.enrollment import bootstrap_if_empty

    await bootstrap_if_empty(store)
    result = await bootstrap_if_empty(store)

    assert result is None


@pytest.mark.asyncio
async def test_invite_token_is_consumable(store):
    from claw_proxy.admin.enrollment import generate_invite_token

    raw = await generate_invite_token(store, ttl_minutes=15, issued_by="admin_a")
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    found = await store.find_enrollment_token(hashed)

    assert found is not None
    assert found.consumed is False
