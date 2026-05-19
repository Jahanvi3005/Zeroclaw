"""Encrypted JSON file helpers for admin storage."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from claw_proxy.crypto import TokenCrypto


class EncryptedJsonFile:
    def __init__(self, path: str | Path, fernet: TokenCrypto) -> None:
        self.path = Path(path)
        self._fernet = fernet

    def read(self, default=None):
        if not self.path.exists():
            return default
        ciphertext = self.path.read_text(encoding="utf-8")
        plaintext = self._fernet.decrypt(ciphertext)
        return json.loads(plaintext)

    def write(self, data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(data, separators=(",", ":"))
        ciphertext = self._fernet.encrypt(plaintext)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            ) as tmp_file:
                tmp_file.write(ciphertext)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_path = Path(tmp_file.name)
            tmp_path.replace(self.path)
            dir_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp_path is not None and tmp_path.exists() and tmp_path != self.path:
                tmp_path.unlink(missing_ok=True)


@dataclass
class WebAuthnCredential:
    credential_id: str
    public_key: str
    sign_count: int
    registered_at: str


@dataclass
class Admin:
    id: str
    name: str
    webauthn_credentials: list[WebAuthnCredential]
    created_at: str


@dataclass
class StaticToken:
    hash: str
    label: str
    created_at: str
    last_used_at: str | None


@dataclass
class EnrollmentToken:
    token_hash: str
    expires_at: str
    consumed: bool
    issued_by: str | None


class AdminStore(Protocol):
    async def list_admins(self) -> list[Admin]: ...

    async def add_admin(self, admin: Admin) -> None: ...

    async def remove_admin(self, admin_id: str) -> None: ...

    async def add_credential(self, admin_id: str, cred: WebAuthnCredential) -> None: ...

    async def update_credential_sign_count(
        self, admin_id: str, credential_id: str, sign_count: int
    ) -> None: ...

    async def find_admin_by_credential(self, credential_id: str) -> Admin | None: ...

    async def list_static_tokens(self) -> list[StaticToken]: ...

    async def add_static_token(self, token: StaticToken) -> None: ...

    async def delete_static_token(self, label: str) -> None: ...

    async def touch_static_token(self, label: str, when: str) -> None: ...

    async def add_enrollment_token(self, token: EnrollmentToken) -> None: ...

    async def find_enrollment_token(self, token_hash: str) -> EnrollmentToken | None: ...

    async def consume_enrollment_token(self, token_hash: str) -> None: ...


def _default_schema() -> dict:
    return {
        "schema_version": 1,
        "admins": [],
        "static_tokens": [],
        "enrollment_tokens": [],
    }


def _admin_from_dict(data: dict) -> Admin:
    return Admin(
        id=data["id"],
        name=data["name"],
        webauthn_credentials=[
            WebAuthnCredential(**credential)
            for credential in data.get("webauthn_credentials", [])
        ],
        created_at=data["created_at"],
    )


def _normalize_schema(data: dict | None) -> dict:
    schema = _default_schema()
    if data:
        schema.update({key: data.get(key, schema[key]) for key in schema})
        schema["schema_version"] = data.get("schema_version", schema["schema_version"])
    return schema


class EncryptedJsonAdminStore:
    def __init__(self, path: str | Path, fernet: TokenCrypto) -> None:
        self._file = EncryptedJsonFile(path, fernet)
        self._lock = asyncio.Lock()

    async def _read(self) -> dict:
        return _normalize_schema(self._file.read(default=_default_schema()))

    def _write(self, data: dict) -> None:
        self._file.write(data)

    async def list_admins(self) -> list[Admin]:
        async with self._lock:
            data = await self._read()
            return [_admin_from_dict(admin) for admin in data["admins"]]

    async def add_admin(self, admin: Admin) -> None:
        async with self._lock:
            data = await self._read()
            data["admins"].append(asdict(admin))
            self._write(data)

    async def remove_admin(self, admin_id: str) -> None:
        async with self._lock:
            data = await self._read()
            data["admins"] = [admin for admin in data["admins"] if admin["id"] != admin_id]
            self._write(data)

    async def add_credential(self, admin_id: str, cred: WebAuthnCredential) -> None:
        async with self._lock:
            data = await self._read()
            for admin in data["admins"]:
                if admin["id"] == admin_id:
                    admin.setdefault("webauthn_credentials", []).append(asdict(cred))
                    break
            self._write(data)

    async def update_credential_sign_count(
        self, admin_id: str, credential_id: str, sign_count: int
    ) -> None:
        async with self._lock:
            data = await self._read()
            for admin in data["admins"]:
                if admin["id"] != admin_id:
                    continue
                for credential in admin.get("webauthn_credentials", []):
                    if credential["credential_id"] == credential_id:
                        credential["sign_count"] = sign_count
                        break
            self._write(data)

    async def find_admin_by_credential(self, credential_id: str) -> Admin | None:
        async with self._lock:
            data = await self._read()
            for admin in data["admins"]:
                for credential in admin.get("webauthn_credentials", []):
                    if credential["credential_id"] == credential_id:
                        return _admin_from_dict(admin)
            return None

    async def list_static_tokens(self) -> list[StaticToken]:
        async with self._lock:
            data = await self._read()
            return [StaticToken(**token) for token in data["static_tokens"]]

    async def add_static_token(self, token: StaticToken) -> None:
        async with self._lock:
            data = await self._read()
            data["static_tokens"].append(asdict(token))
            self._write(data)

    async def delete_static_token(self, label: str) -> None:
        async with self._lock:
            data = await self._read()
            data["static_tokens"] = [
                token for token in data["static_tokens"] if token["label"] != label
            ]
            self._write(data)

    async def touch_static_token(self, label: str, when: str) -> None:
        async with self._lock:
            data = await self._read()
            for token in data["static_tokens"]:
                if token["label"] == label:
                    token["last_used_at"] = when
                    break
            self._write(data)

    async def add_enrollment_token(self, token: EnrollmentToken) -> None:
        async with self._lock:
            data = await self._read()
            data["enrollment_tokens"].append(asdict(token))
            self._write(data)

    async def find_enrollment_token(self, token_hash: str) -> EnrollmentToken | None:
        async with self._lock:
            data = await self._read()
            for token in data["enrollment_tokens"]:
                if token["token_hash"] == token_hash:
                    return EnrollmentToken(**token)
            return None

    async def consume_enrollment_token(self, token_hash: str) -> None:
        async with self._lock:
            data = await self._read()
            for token in data["enrollment_tokens"]:
                if token["token_hash"] == token_hash:
                    token["consumed"] = True
                    break
            self._write(data)
