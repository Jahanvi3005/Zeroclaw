from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from claw_proxy.admin.storage import Admin, WebAuthnCredential


@pytest.fixture
def cfg():
    from claw_proxy.admin.auth import WebAuthnConfig

    return WebAuthnConfig(
        rp_id="example.com",
        rp_name="Example",
        origin="https://example.com",
    )


@pytest.fixture
def store(tmp_path, fernet):
    from claw_proxy.admin.storage import EncryptedJsonAdminStore

    return EncryptedJsonAdminStore(tmp_path / "admin.json.enc", fernet)


@pytest.mark.asyncio
async def test_start_registration_returns_options_and_challenge(cfg):
    from claw_proxy.admin import auth

    with patch.object(auth, "webauthn") as wa, patch.object(
        auth, "options_to_json", return_value='{"options":1}'
    ):
        wa.generate_registration_options.return_value = MagicMock(challenge=b"chal")

        options, challenge = await auth.start_registration(
            admin_id="a1",
            admin_name="Alice",
            cfg=cfg,
        )

    assert options == '{"options":1}'
    assert challenge == b"chal"


@pytest.mark.asyncio
async def test_verify_registration_persists_credential(store, cfg):
    from claw_proxy.admin import auth

    await store.add_admin(
        Admin(
            id="a1",
            name="Alice",
            webauthn_credentials=[],
            created_at="t",
        )
    )

    fake_verification = MagicMock(
        credential_id=b"cid",
        credential_public_key=b"pk",
        sign_count=0,
    )

    with patch.object(auth, "verify_registration_response", return_value=fake_verification):
        cred = await auth.verify_registration(
            admin_id="a1",
            response_json='{"r":1}',
            expected_challenge=b"chal",
            store=store,
            cfg=cfg,
        )

    assert cred.credential_id
    admins = await store.list_admins()
    assert len(admins[0].webauthn_credentials) == 1


@pytest.mark.asyncio
async def test_verify_login_returns_admin(store, cfg):
    from claw_proxy.admin import auth

    cred = WebAuthnCredential(
        credential_id="Y2lk",
        public_key="cGs=",
        sign_count=0,
        registered_at="t",
    )
    await store.add_admin(
        Admin(
            id="a1",
            name="Alice",
            webauthn_credentials=[cred],
            created_at="t",
        )
    )
    fake_verify = MagicMock(new_sign_count=1, credential_id=b"cid")

    with patch.object(auth, "verify_authentication_response", return_value=fake_verify):
        admin = await auth.verify_login(
            response_json='{"rawId":"Y2lk"}',
            expected_challenge=b"chal",
            store=store,
            cfg=cfg,
        )

    assert admin.id == "a1"
