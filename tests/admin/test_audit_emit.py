from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def store(tmp_path):
    from claw_proxy.admin.audit import EncryptedJsonlAuditStore

    return EncryptedJsonlAuditStore(
        tmp_path / "audit.jsonl.enc",
        TokenCrypto(FAKE_ENCRYPTION_KEY),
    )


@pytest.mark.asyncio
async def test_emit_writes_entry(store):
    from claw_proxy.admin.audit import AuditContext, emit_audit
    from claw_proxy.admin.auth import AdminContext

    schema_cache = AsyncMock()
    schema_cache.is_secret_path.return_value = False
    ctx = AuditContext(
        admin=AdminContext(admin_id="a1", channel="static_token"),
        client_ip="127.0.0.1",
    )

    await emit_audit(
        store=store,
        ctx=ctx,
        action="container.restart",
        target={"type": "user", "user_id": "u1"},
        params={"careful": True},
        result="success",
        duration_ms=42,
        container_id=None,
        schema_cache=schema_cache,
    )

    rows = await store.query()
    assert len(rows) == 1
    assert rows[0].action == "container.restart"
    assert rows[0].admin_id == "a1"
