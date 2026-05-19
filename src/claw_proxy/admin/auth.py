"""Static-token auth helpers for admin access."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_urlsafe
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException
import webauthn
from webauthn import options_to_json, verify_authentication_response, verify_registration_response
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from claw_proxy.admin.storage import AdminStore


def hash_static_token(token: str) -> str:
    return bcrypt.hashpw(token.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def generate_static_token() -> str:
    return token_urlsafe(32)


async def verify_static_token(token: str, store: AdminStore) -> str | None:
    token_bytes = token.encode("utf-8")
    for static_token in await store.list_static_tokens():
        if bcrypt.checkpw(token_bytes, static_token.hash.encode("utf-8")):
            now_iso = datetime.now(timezone.utc).isoformat()
            await store.touch_static_token(static_token.label, now_iso)
            return static_token.label
    return None


@dataclass(frozen=True)
class SessionPayload:
    admin_id: str
    expires_at: int


class SessionInvalidError(Exception):
    pass


def issue_session_jwt(admin_id: str, *, secret: str, ttl_seconds: int = 1800) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {"sub": admin_id, "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_session_jwt(token: str, *, secret: str) -> SessionPayload:
    try:
        decoded: dict = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise SessionInvalidError(str(exc)) from exc
    return SessionPayload(admin_id=decoded["sub"], expires_at=decoded["exp"])


@dataclass(frozen=True)
class WebAuthnConfig:
    rp_id: str
    rp_name: str
    origin: str


@dataclass(frozen=True)
class AdminContext:
    admin_id: str
    channel: str


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


async def start_registration(
    *, admin_id: str, admin_name: str, cfg: WebAuthnConfig
) -> tuple[str, bytes]:
    options = webauthn.generate_registration_options(
        rp_id=cfg.rp_id,
        rp_name=cfg.rp_name,
        user_id=admin_id.encode("utf-8"),
        user_name=admin_name,
    )
    return options_to_json(options), options.challenge


async def verify_registration(
    *,
    admin_id: str,
    response_json: str,
    expected_challenge: bytes,
    store: AdminStore,
    cfg: WebAuthnConfig,
):
    from claw_proxy.admin.storage import WebAuthnCredential

    verification = verify_registration_response(
        credential=response_json,
        expected_challenge=expected_challenge,
        expected_rp_id=cfg.rp_id,
        expected_origin=cfg.origin,
    )
    cred = WebAuthnCredential(
        credential_id=_b64encode(verification.credential_id),
        public_key=_b64encode(verification.credential_public_key),
        sign_count=verification.sign_count,
        registered_at=datetime.now(timezone.utc).isoformat(),
    )
    await store.add_credential(admin_id, cred)
    return cred


async def start_login(*, store: AdminStore, cfg: WebAuthnConfig) -> tuple[str, bytes]:
    admins = await store.list_admins()
    allow_list: list[PublicKeyCredentialDescriptor] = []
    for admin in admins:
        for credential in admin.webauthn_credentials:
            allow_list.append(
                PublicKeyCredentialDescriptor(id=_b64decode(credential.credential_id))
            )
    options = webauthn.generate_authentication_options(
        rp_id=cfg.rp_id,
        allow_credentials=allow_list,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.dumps(options.__dict__, default=str), options.challenge


async def verify_login(
    *,
    response_json: str,
    expected_challenge: bytes,
    store: AdminStore,
    cfg: WebAuthnConfig,
):
    payload: dict[str, Any] = json.loads(response_json)
    raw_id = payload.get("rawId") or payload.get("id")
    if not isinstance(raw_id, str):
        raise SessionInvalidError("missing credential id")

    credential_id = raw_id
    admin = await store.find_admin_by_credential(credential_id)
    if admin is None:
        raise SessionInvalidError("unknown credential")

    credential = next(
        cred for cred in admin.webauthn_credentials if cred.credential_id == credential_id
    )
    verification = verify_authentication_response(
        credential=response_json,
        expected_challenge=expected_challenge,
        expected_rp_id=cfg.rp_id,
        expected_origin=cfg.origin,
        credential_public_key=_b64decode(credential.public_key),
        credential_current_sign_count=credential.sign_count,
    )
    await store.update_credential_sign_count(
        admin.id, credential.credential_id, verification.new_sign_count
    )
    return admin


async def require_admin(
    *, authorization: str | None, store: AdminStore, jwt_secret: str
) -> AdminContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = verify_session_jwt(token, secret=jwt_secret)
        return AdminContext(admin_id=payload.admin_id, channel="passkey")
    except SessionInvalidError:
        pass

    label = await verify_static_token(token, store)
    if label is not None:
        return AdminContext(admin_id=label, channel="static_token")

    raise HTTPException(status_code=401, detail="invalid token")
