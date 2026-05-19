"""First-run admin bootstrap and invite-token issuance."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from claw_proxy.admin.auth import generate_static_token, hash_static_token
from claw_proxy.admin.storage import AdminStore, EnrollmentToken, StaticToken

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BootstrapResult:
    enrollment_token: str
    static_token: str


async def bootstrap_if_empty(store: AdminStore) -> BootstrapResult | None:
    admins = await store.list_admins()
    static_tokens = await store.list_static_tokens()
    if admins or static_tokens:
        return None

    enrollment_token = secrets.token_urlsafe(24)
    await store.add_enrollment_token(
        EnrollmentToken(
            token_hash=hashlib.sha256(enrollment_token.encode()).hexdigest(),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            consumed=False,
            issued_by=None,
        )
    )

    static_token = generate_static_token()
    await store.add_static_token(
        StaticToken(
            hash=hash_static_token(static_token),
            label="first-run",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_used_at=None,
        )
    )

    log.warning(
        "FIRST-RUN ADMIN ENROLLMENT\n"
        "Enrollment token (15 min): %s\n"
        "Static CLI token: %s",
        enrollment_token,
        static_token,
    )
    return BootstrapResult(
        enrollment_token=enrollment_token,
        static_token=static_token,
    )


async def generate_invite_token(
    store: AdminStore, *, ttl_minutes: int = 15, issued_by: str | None
) -> str:
    raw = secrets.token_urlsafe(24)
    await store.add_enrollment_token(
        EnrollmentToken(
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(),
            consumed=False,
            issued_by=issued_by,
        )
    )
    return raw
