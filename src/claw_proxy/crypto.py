"""Fernet-based encryption for container bearer tokens.

Derives a 32-byte key from the passphrase via SHA-256 so the env var
can be any string, not a raw base64 Fernet key. For internal use only —
not a password hash.
"""

import base64
import hashlib

from cryptography.fernet import Fernet


class TokenCrypto:
    def __init__(self, passphrase: str) -> None:
        # Derive a 32-byte key from passphrase via SHA-256, then base64 for Fernet
        key = hashlib.sha256(passphrase.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()
