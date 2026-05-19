"""Supabase JWT verification via GoTrue."""

import logging

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from claw_proxy.config import SUPABASE_ANON_KEY, SUPABASE_URL, http_client

log = logging.getLogger(__name__)

security = HTTPBearer()


async def verify_jwt(token: str) -> dict:
    """Verify a Supabase JWT by calling GoTrue remotely.

    Returns the full user dict (has "id", "email", etc.).
    """
    try:
        resp = await http_client.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": SUPABASE_ANON_KEY,
            },
        )
    except Exception as e:
        log.error("GoTrue unreachable: %s", e)
        raise HTTPException(status_code=502, detail="Auth service unavailable")

    if resp.status_code >= 500:
        log.error("GoTrue error: %s %s", resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail="Auth service unavailable")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid token")

    user = resp.json()
    if not isinstance(user, dict) or "id" not in user:
        log.error("GoTrue returned unexpected response shape: %s", user)
        raise HTTPException(status_code=502, detail="Auth service unavailable")

    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """FastAPI dependency that extracts and verifies the bearer token."""
    return await verify_jwt(credentials.credentials)
