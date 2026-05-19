from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import bcrypt
import pytest

from claw_proxy.admin.storage import StaticToken


def test_hash_static_token_roundtrip():
    from claw_proxy.admin.auth import hash_static_token

    token = "secret-token"
    hashed = hash_static_token(token)

    assert hashed != token
    assert bcrypt.checkpw(token.encode("utf-8"), hashed.encode("utf-8"))


@pytest.mark.asyncio
async def test_verify_static_token_returns_label_and_touches(monkeypatch):
    import claw_proxy.admin.auth as auth

    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return SimpleNamespace(
                isoformat=lambda: "2026-04-22T12:34:56+00:00",
            )

    monkeypatch.setattr(auth, "datetime", FakeDateTime)

    token = "secret-token"
    store = SimpleNamespace(
        list_static_tokens=AsyncMock(
            return_value=[
                StaticToken(
                    hash=auth.hash_static_token(token),
                    label="cli",
                    created_at="2026-04-22T00:00:00Z",
                    last_used_at=None,
                )
            ]
        ),
        touch_static_token=AsyncMock(),
    )

    label = await auth.verify_static_token(token, store)

    assert label == "cli"
    store.touch_static_token.assert_awaited_once_with(
        "cli",
        "2026-04-22T12:34:56+00:00",
    )


@pytest.mark.asyncio
async def test_verify_static_token_returns_none_for_bad_token():
    from claw_proxy.admin.auth import hash_static_token, verify_static_token

    store = SimpleNamespace(
        list_static_tokens=AsyncMock(
            return_value=[
                StaticToken(
                    hash=hash_static_token("other-token"),
                    label="cli",
                    created_at="2026-04-22T00:00:00Z",
                    last_used_at=None,
                )
            ]
        ),
        touch_static_token=AsyncMock(),
    )

    assert await verify_static_token("wrong-token", store) is None
    store.touch_static_token.assert_not_awaited()
