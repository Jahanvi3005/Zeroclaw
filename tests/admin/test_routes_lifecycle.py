from __future__ import annotations

from claw_proxy.admin.activity import ActivityState


def test_start(authed_client, admin_deps):
    response = authed_client.post("/containers/u1/start")
    assert response.status_code == 200
    admin_deps["orchestrator"].start.assert_awaited_with("u1")


def test_stop_with_careful_skipped(authed_client, admin_deps):
    admin_deps["activity_checker"].get_state.return_value = ActivityState(
        is_idle=False,
        last_activity_at=None,
        signals=["ws"],
    )
    response = authed_client.post(
        "/containers/u1/stop",
        json={"careful": True, "careful_timeout_seconds": 1},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "skipped"


def test_restart(authed_client, admin_deps):
    admin_deps["activity_checker"].get_state.return_value = ActivityState(
        is_idle=True,
        last_activity_at=None,
        signals=[],
    )
    response = authed_client.post("/containers/u1/restart", json={})
    assert response.status_code == 200
    admin_deps["orchestrator"].restart.assert_awaited_with("u1")
