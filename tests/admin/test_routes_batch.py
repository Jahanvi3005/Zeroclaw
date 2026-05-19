from __future__ import annotations

from claw_proxy.admin.activity import ActivityState
from claw_proxy.admin.jobs import Job, JobTarget


def test_batch_restart_kicks_off_job(authed_client, admin_deps):
    admin_deps["activity_checker"].get_state.return_value = ActivityState(
        is_idle=True,
        last_activity_at=None,
        signals=[],
    )
    response = authed_client.post("/batch/restart", json={"targets": ["u1", "u2"]})
    assert response.status_code == 202
    assert response.json()["job_id"]


def test_jobs_list(authed_client):
    response = authed_client.get("/jobs")
    assert response.status_code == 200
    assert "items" in response.json()


def test_batch_config_set_kicks_off_job(authed_client, admin_deps):
    admin_deps["registry"].get.return_value = None

    response = authed_client.post(
        "/batch/config.set",
        json={
            "targets": ["u1", "u2"],
            "updates": [{"path": "gateway.base_url", "value": "http://example"}],
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["status_url"].endswith(body["job_id"])


def test_jobs_get_returns_job(authed_client, admin_deps):
    import asyncio

    job = Job(
        id="j_abc",
        operation="restart",
        status="created",
        created_at="2026-04-23T00:00:00+00:00",
        started_at=None,
        completed_at=None,
        params={"targets": ["u1"]},
        targets=[JobTarget(user_id="u1", status="pending")],
        summary={"total": 1, "success": 0, "failed": 0, "skipped": 0, "pending": 1},
    )
    asyncio.run(admin_deps["job_store"].put(job))

    response = authed_client.get("/jobs/j_abc")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "j_abc"
    assert body["operation"] == "restart"
    assert body["targets"][0]["user_id"] == "u1"


def test_jobs_get_missing_returns_404(authed_client):
    response = authed_client.get("/jobs/j_nope")
    assert response.status_code == 404


def test_jobs_cancel_requests_cancellation(authed_client):
    response = authed_client.delete("/jobs/j_whatever")
    assert response.status_code == 200
    assert response.json()["status"] == "cancellation_requested"
