from __future__ import annotations

import asyncio

import pytest

from claw_proxy.admin.jobs import EncryptedJsonJobStore, Job, JobTarget
from claw_proxy.crypto import TokenCrypto
from tests.conftest import FAKE_ENCRYPTION_KEY


@pytest.fixture
def store(tmp_path):
    return EncryptedJsonJobStore(tmp_path / "jobs", TokenCrypto(FAKE_ENCRYPTION_KEY))


def _new_job(targets=("u1", "u2")):
    return Job(
        id="j1",
        operation="restart",
        status="created",
        created_at="t",
        started_at=None,
        completed_at=None,
        params={},
        targets=[JobTarget(user_id=u, status="pending") for u in targets],
        summary={
            "total": len(targets),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "pending": len(targets),
        },
    )


@pytest.mark.asyncio
async def test_runs_each_target(store):
    from claw_proxy.admin.jobs import JobRunner

    job = _new_job()
    await store.put(job)
    calls: list[str] = []

    async def per_target(user_id, params):
        calls.append(user_id)
        return {"status": "success"}

    runner = JobRunner(store=store, inter_target_delay_seconds=0)
    await runner.run(job_id="j1", per_target=per_target)

    assert calls == ["u1", "u2"]
    final = await store.get("j1")
    assert final is not None
    assert final.status == "complete"
    assert final.summary["success"] == 2


@pytest.mark.asyncio
async def test_marks_skipped_status(store):
    from claw_proxy.admin.jobs import JobRunner

    job = _new_job(targets=("u1",))
    await store.put(job)

    async def per_target(user_id, params):
        return {"status": "skipped", "reason": "busy"}

    await JobRunner(store=store, inter_target_delay_seconds=0).run(
        "j1", per_target=per_target
    )
    final = await store.get("j1")
    assert final is not None
    assert final.summary["skipped"] == 1


@pytest.mark.asyncio
async def test_counts_operation_statuses_as_success(store):
    from claw_proxy.admin.jobs import JobRunner

    job = _new_job()
    await store.put(job)

    async def per_target(user_id, params):
        return {"status": "restarted"}

    await JobRunner(store=store, inter_target_delay_seconds=0).run(
        "j1", per_target=per_target
    )
    final = await store.get("j1")
    assert final is not None
    assert final.status == "complete"
    assert final.summary == {
        "total": 2,
        "success": 2,
        "failed": 0,
        "skipped": 0,
        "pending": 0,
    }
    assert [target.status for target in final.targets] == ["restarted", "restarted"]


@pytest.mark.asyncio
async def test_cancellation(store):
    from claw_proxy.admin.jobs import JobRunner

    job = _new_job(targets=("u1", "u2", "u3"))
    await store.put(job)
    runner = JobRunner(store=store, inter_target_delay_seconds=0)

    async def per_target(user_id, params):
        if user_id == "u1":
            runner.cancel("j1")
        await asyncio.sleep(0)
        return {"status": "success"}

    await runner.run("j1", per_target=per_target)
    final = await store.get("j1")
    assert final is not None
    assert final.status == "cancelled"
    assert final.summary["pending"] >= 1
