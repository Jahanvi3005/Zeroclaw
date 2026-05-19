from __future__ import annotations


def test_resolve_users(authed_client, admin_deps):
    admin_deps["registry"].get.return_value = {"container_id": "c1"}
    admin_deps["supabase_client"].table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        {
            "id": "00000000-1111-2222-3333-444444444444",
            "email": "a@b",
            "first_name": "A",
            "last_name": "B",
        },
    ]

    response = authed_client.get(
        "/users",
        params={"lookup": "00000000-1111-2222-3333-444444444444"},
    )
    assert response.status_code == 200
    assert response.json()["user_id"] == "00000000-1111-2222-3333-444444444444"


def test_list_containers(authed_client, admin_deps):
    admin_deps["registry"].list_all.return_value = [
        {
            "user_id": "u1",
            "container_id": "c1",
            "status": "ready",
            "last_active_at": "t",
            "ws_url": "",
            "http_url": "",
            "current_session_id": None,
        },
    ]

    response = authed_client.get("/containers")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_get_container(authed_client, admin_deps):
    admin_deps["registry"].get.return_value = {
        "user_id": "u1",
        "container_id": "c1",
        "status": "ready",
        "last_active_at": "t",
        "ws_url": "",
        "http_url": "",
        "current_session_id": None,
    }

    response = authed_client.get("/containers/u1")
    assert response.status_code == 200
    assert response.json()["user_id"] == "u1"
