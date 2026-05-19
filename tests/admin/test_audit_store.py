import json
from datetime import datetime, timedelta, timezone

import pytest
from json import JSONDecodeError

from claw_proxy.admin.audit import AuditEntry, EncryptedJsonlAuditStore
from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def store(tmp_path):
    return EncryptedJsonlAuditStore(
        tmp_path / "audit.jsonl.enc",
        TokenCrypto(FAKE_ENCRYPTION_KEY),
    )


def _entry(
    entry_id: str,
    *,
    timestamp: datetime,
    admin_id: str,
    action: str,
    target: dict | None = None,
    job_id: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        id=entry_id,
        timestamp=timestamp,
        admin_id=admin_id,
        auth_channel="passkey",
        action=action,
        target=target,
        params={"note": entry_id},
        result="success",
        error=None,
        duration_ms=12,
        client_ip="127.0.0.1",
        job_id=job_id,
    )


@pytest.mark.asyncio
async def test_append_and_query(store):
    now = datetime.now(timezone.utc)
    first = _entry(
        "a1",
        timestamp=now - timedelta(minutes=10),
        admin_id="admin_a",
        action="container.restart",
        target={"type": "container", "container_id": "c1"},
    )
    second = _entry(
        "a2",
        timestamp=now - timedelta(minutes=5),
        admin_id="admin_b",
        action="config.set",
        target={"type": "user", "user_id": "user_2"},
        job_id="job_2",
    )

    await store.append(first)
    await store.append(second)

    rows = await store.query()
    assert [row.id for row in rows] == ["a2", "a1"]
    assert rows[0].job_id == "job_2"
    assert rows[1].target == {"type": "container", "container_id": "c1"}


@pytest.mark.asyncio
async def test_query_filters_by_action_and_admin(store):
    now = datetime.now(timezone.utc)
    await store.append(
        _entry(
            "a1",
            timestamp=now - timedelta(minutes=20),
            admin_id="admin_a",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a2",
            timestamp=now - timedelta(minutes=10),
            admin_id="admin_b",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a3",
            timestamp=now - timedelta(minutes=5),
            admin_id="admin_a",
            action="config.set",
            target={"type": "user", "user_id": "user_3"},
        )
    )

    rows = await store.query(admin_id="admin_a", action="container.restart")
    assert [row.id for row in rows] == ["a1"]

    rows = await store.query(target_user_id="user_3")
    assert [row.id for row in rows] == ["a3"]

    rows = await store.query(job_id="job_missing")
    assert rows == []


@pytest.mark.asyncio
async def test_query_filters_by_since_until_and_pagination(store):
    now = datetime.now(timezone.utc)
    await store.append(
        _entry(
            "a1",
            timestamp=now - timedelta(hours=3),
            admin_id="admin_a",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a2",
            timestamp=now - timedelta(hours=2),
            admin_id="admin_a",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a3",
            timestamp=now - timedelta(hours=1),
            admin_id="admin_a",
            action="container.restart",
        )
    )

    rows = await store.query(
        since=(now - timedelta(hours=2, minutes=30)).isoformat(),
        until=(now - timedelta(minutes=30)).isoformat(),
    )
    assert [row.id for row in rows] == ["a3", "a2"]

    rows = await store.query(limit=1, offset=1)
    assert [row.id for row in rows] == ["a2"]


@pytest.mark.asyncio
async def test_query_includes_since_and_until_boundaries(store):
    exact = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)

    await store.append(
        _entry(
            "a1",
            timestamp=exact,
            admin_id="admin_a",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a2",
            timestamp=exact - timedelta(minutes=1),
            admin_id="admin_a",
            action="container.restart",
        )
    )
    await store.append(
        _entry(
            "a3",
            timestamp=exact + timedelta(minutes=1),
            admin_id="admin_a",
            action="container.restart",
        )
    )

    rows = await store.query(
        since="2026-04-22T12:00:00Z",
        until="2026-04-22T12:00:00Z",
    )
    assert [row.id for row in rows] == ["a1"]


@pytest.mark.asyncio
async def test_prune_older_than_and_encrypts_each_line_individually(store, tmp_path):
    now = datetime.now(timezone.utc)
    old = _entry(
        "a1",
        timestamp=now - timedelta(days=10),
        admin_id="admin_a",
        action="container.restart",
    )
    recent = _entry(
        "a2",
        timestamp=now - timedelta(days=1),
        admin_id="admin_b",
        action="config.set",
    )

    await store.append(old)
    await store.append(recent)

    disk_before = (tmp_path / "audit.jsonl.enc").read_text(encoding="utf-8")
    lines_before = disk_before.splitlines()
    assert len(lines_before) == 2
    assert "admin_a" not in disk_before
    assert "container.restart" not in disk_before
    with pytest.raises(JSONDecodeError):
        json.loads(lines_before[0])

    removed = await store.prune_older_than(days=7)
    assert removed == 1
    assert [row.id for row in await store.query()] == ["a2"]

    disk_after = (tmp_path / "audit.jsonl.enc").read_text(encoding="utf-8")
    assert len(disk_after.splitlines()) == 1
    assert "admin_a" not in disk_after
    assert "container.restart" not in disk_after
    with pytest.raises(JSONDecodeError):
        json.loads(disk_after.splitlines()[0])
