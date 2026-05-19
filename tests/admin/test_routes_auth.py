from __future__ import annotations


def test_login_starts_challenge(client):
    response = client.post("/auth/login")
    assert response.status_code == 200
    assert "options" in response.json() or "publicKey" in response.json()


def test_logout_returns_ok(authed_client):
    response = authed_client.post("/auth/logout")
    assert response.status_code == 200


def test_register_requires_enrollment_token(client):
    response = client.post(
        "/auth/register",
        json={"enrollment_token": "bogus", "name": "A", "credential": {}},
    )
    assert response.status_code in (400, 401, 403)
