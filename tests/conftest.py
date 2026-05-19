import os
import pytest

# Set test env vars before any proxy imports.
# Values aligned with test_router.py to avoid conflicts when running both.
os.environ.setdefault("SUPABASE_URL", "http://localhost:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")

FAKE_USER = {
    "id": "00000000-1111-2222-3333-444444444444",
    "email": "test@example.com",
}

FAKE_ENCRYPTION_KEY = "test-encryption-key-must-be-at-least-32-bytes!"


@pytest.fixture
def fake_user():
    return FAKE_USER.copy()


@pytest.fixture(autouse=True)
def _reset_shared_session_state():
    """Clear shared_session module state between tests so they don't leak."""
    from claw_proxy.ws.shared_session import reset_state
    reset_state()
    yield
    reset_state()
