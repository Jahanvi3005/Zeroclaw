import pytest

from claw_proxy.admin.jobs import EncryptedJsonJobStore, Job, JobTarget
from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def store(tmp_path):
    return EncryptedJsonJobStore(
        tmp_path / "jobs",
        TokenCrypto(FAKE_ENCRYPTION_KEY),
    )


def _job(
    job_id: str,
    *,
    status: str,
    operation: str,
    created_at: str,
    targets: list[JobTarget] | None = None,
) -> Job:
    return Job(
        id=job_id,
        operation=operation,
        status=status,
        created_at=created_at,
        started_at=None,
        completed_at=None,
        params={},
        targets=targets or [],
        summary={
            "total": len(targets or []),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "pending": len(targets or []),
        },
    )


@pytest.mark.asyncio
async def test_create_then_get_job(store):
    job = _job(
        "j1",
        status="created",
        operation="restart",
        created_at="2026-04-22T10:00:00Z",
        targets=[
            JobTarget(
                user_id="u1",
                status="pending",
                result={"status": "queued", "attempt": 1},
            )
        ],
    )

    await store.put(job)

    loaded = await store.get("j1")
    assert loaded is not None
    assert loaded.id == "j1"
    assert loaded.targets[0].user_id == "u1"
    assert loaded.targets[0].result == {"status": "queued", "attempt": 1}


@pytest.mark.asyncio
async def test_list_jobs_filters_by_status_and_operation(store):
    await store.put(
        _job(
            "j1",
            status="complete",
            operation="restart",
            created_at="2026-04-22T10:00:00Z",
        )
    )
    await store.put(
        _job(
            "j2",
            status="running",
            operation="config.set",
            created_at="2026-04-22T11:00:00Z",
        )
    )
    await store.put(
        _job(
            "j3",
            status="running",
            operation="restart",
            created_at="2026-04-22T12:00:00Z",
        )
    )

    running = await store.list_jobs(status="running")
    assert [job.id for job in running] == ["j3", "j2"]

    restart_jobs = await store.list_jobs(operation="restart")
    assert [job.id for job in restart_jobs] == ["j3", "j1"]


@pytest.mark.asyncio
async def test_delete_job(store):
    await store.put(
        _job(
            "j1",
            status="cancelled",
            operation="restart",
            created_at="2026-04-22T10:00:00Z",
        )
    )

    assert await store.get("j1") is not None

    await store.delete("j1")

    assert await store.get("j1") is None


@pytest.mark.asyncio
async def test_list_ids(store):
    await store.put(_job("j1", status="created", operation="restart", created_at="t1"))
    await store.put(_job("j2", status="created", operation="restart", created_at="t2"))

    assert await store.list_ids() == ["j1", "j2"]


@pytest.mark.asyncio
async def test_rejects_unsafe_job_id(store):
    with pytest.raises(ValueError):
        await store.put(
            _job(
                "../escape",
                status="created",
                operation="restart",
                created_at="2026-04-22T10:00:00Z",
            )
        )
