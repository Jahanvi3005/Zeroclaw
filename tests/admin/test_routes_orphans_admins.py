from __future__ import annotations

from unittest.mock import MagicMock


def test_list_orphans(authed_client, admin_deps):
    container = MagicMock(id="orph1", attrs={"State": {"Status": "exited"}})
    container.name = "zeroclaw-aabbccdd"
    admin_deps["docker_client"].containers.list.return_value = [container]
    admin_deps["registry"].list_all.return_value = []

    response = authed_client.get("/orphans")
    assert response.status_code == 200
    assert any(item["container_id"] == "orph1" for item in response.json()["items"])


def test_list_admins(authed_client):
    response = authed_client.get("/admins")
    assert response.status_code == 200
    assert "items" in response.json()


def test_invite_returns_token(authed_client):
    response = authed_client.post("/admins/invite")
    assert response.status_code == 200
    assert response.json()["enrollment_token"]


def test_static_token_create_then_revoke(authed_client):
    response = authed_client.post("/admins/static-tokens", json={"label": "ops"})
    assert response.status_code == 200
    assert response.json()["token"]

    response2 = authed_client.delete("/admins/static-tokens/ops")
    assert response2.status_code == 200


def test_audit_query_empty(authed_client):
    response = authed_client.get("/audit")
    assert response.status_code == 200
    assert response.json()["items"] == []
