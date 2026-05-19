import pytest
from tests.conftest import FAKE_ENCRYPTION_KEY


def test_encrypt_decrypt_roundtrip():
    from claw_proxy.crypto import TokenCrypto
    c = TokenCrypto(FAKE_ENCRYPTION_KEY)
    token = "zc_test_bearer_token_abc123"
    encrypted = c.encrypt(token)
    assert encrypted != token
    assert c.decrypt(encrypted) == token


def test_decrypt_wrong_key():
    from claw_proxy.crypto import TokenCrypto
    c1 = TokenCrypto(FAKE_ENCRYPTION_KEY)
    c2 = TokenCrypto("different-key-also-at-least-32-bytes!")
    encrypted = c1.encrypt("secret")
    with pytest.raises(Exception):
        c2.decrypt(encrypted)


def test_different_encryptions_differ():
    """Fernet includes a timestamp, so same plaintext encrypts differently."""
    from claw_proxy.crypto import TokenCrypto
    c = TokenCrypto(FAKE_ENCRYPTION_KEY)
    e1 = c.encrypt("same")
    e2 = c.encrypt("same")
    assert e1 != e2  # different due to timestamp/IV
