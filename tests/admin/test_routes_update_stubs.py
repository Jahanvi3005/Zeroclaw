from __future__ import annotations


def test_update_post_returns_501(authed_client):
    response = authed_client.post("/containers/u1/update", json={"mode": "in_place"})
    assert response.status_code == 501
    body = response.json()
    assert body["error"] == "update_not_implemented"
    assert "in_place" in body["planned_modes"]


def test_update_status_returns_501(authed_client):
    assert authed_client.get("/containers/u1/update/status").status_code == 501


def test_batch_update_returns_501(authed_client):
    assert authed_client.post("/batch/update", json={"targets": ["u1"]}).status_code == 501
