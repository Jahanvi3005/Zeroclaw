import pytest

from claw_proxy.admin.storage import (
    Admin,
    EncryptedJsonAdminStore,
    EnrollmentToken,
    StaticToken,
    WebAuthnCredential,
)
from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def store(tmp_path):
    return EncryptedJsonAdminStore(
        tmp_path / "admin.json.enc",
        TokenCrypto(FAKE_ENCRYPTION_KEY),
    )


@pytest.mark.asyncio
async def test_empty_then_add_admin(store):
    assert await store.list_admins() == []

    admin = Admin(
        id="admin_a",
        name="Alice",
        webauthn_credentials=[],
        created_at="2026-04-22T00:00:00Z",
    )
    await store.add_admin(admin)

    listed = await store.list_admins()
    assert len(listed) == 1
    assert listed[0].id == "admin_a"


@pytest.mark.asyncio
async def test_add_credential_to_admin(store):
    await store.add_admin(
        Admin(
            id="a1",
            name="A",
            webauthn_credentials=[],
            created_at="t",
        )
    )

    cred = WebAuthnCredential(
        credential_id="cid",
        public_key="pk",
        sign_count=0,
        registered_at="t",
    )
    await store.add_credential("a1", cred)

    admin = (await store.list_admins())[0]
    assert admin.webauthn_credentials[0].credential_id == "cid"


@pytest.mark.asyncio
async def test_static_token_lifecycle(store):
    await store.add_static_token(
        StaticToken(
            hash="$2b$...",
            label="cli",
            created_at="t",
            last_used_at=None,
        )
    )

    tokens = await store.list_static_tokens()
    assert tokens[0].label == "cli"

    await store.delete_static_token("cli")
    assert await store.list_static_tokens() == []


@pytest.mark.asyncio
async def test_enrollment_token_consume(store):
    token = EnrollmentToken(
        token_hash="h",
        expires_at="2099-01-01T00:00:00Z",
        consumed=False,
        issued_by=None,
    )
    await store.add_enrollment_token(token)

    found = await store.find_enrollment_token("h")
    assert found is not None
    assert found.consumed is False

    await store.consume_enrollment_token("h")
    found_again = await store.find_enrollment_token("h")
    assert found_again is not None
    assert found_again.consumed is True
