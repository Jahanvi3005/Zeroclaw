from __future__ import annotations

import pytest

from claw_proxy.admin.auth import (
    SessionInvalidError,
    issue_session_jwt,
    verify_session_jwt,
)

JWT_SECRET = "test-jwt-secret-with-at-least-32-bytes"
OTHER_JWT_SECRET = "other-jwt-secret-with-at-least-32-bytes"


def test_roundtrip():
    token = issue_session_jwt("admin_a", secret=JWT_SECRET, ttl_seconds=60)

    payload = verify_session_jwt(token, secret=JWT_SECRET)

    assert payload.admin_id == "admin_a"


def test_expired_rejected():
    token = issue_session_jwt("admin_b", secret=JWT_SECRET, ttl_seconds=-1)

    with pytest.raises(SessionInvalidError):
        verify_session_jwt(token, secret=JWT_SECRET)


def test_wrong_secret_rejected():
    token = issue_session_jwt("admin_c", secret=JWT_SECRET, ttl_seconds=60)

    with pytest.raises(SessionInvalidError):
        verify_session_jwt(token, secret=OTHER_JWT_SECRET)
