from __future__ import annotations

from unittest.mock import AsyncMock, call


def test_skills_retrofit_calls_orchestrator_for_token_map(authed_client, admin_deps):
    orchestrator = admin_deps["orchestrator"]
    admin_deps["registry"].list_all.return_value = []
    orchestrator.token_map = {"tok-1": "u1", "tok-2": "u2"}
    orchestrator._ensure_skills_current = AsyncMock(side_effect=[True, False])

    response = authed_client.post("/skills/retrofit")

    assert response.status_code == 200
    assert response.json() == {
        "checked": 2,
        "updated": 1,
        "skipped": 1,
        "failed": 0,
    }
    orchestrator._ensure_skills_current.assert_has_awaits(
        [call("u1", "tok-1"), call("u2", "tok-2")]
    )


def test_skills_retrofit_hydrates_targets_from_registry(authed_client, admin_deps):
    orchestrator = admin_deps["orchestrator"]
    orchestrator.token_map = {}
    orchestrator._ensure_skills_current = AsyncMock(side_effect=[False, False])
    admin_deps["registry"].list_all.return_value = [
        {"user_id": "u1", "bearer_token": "tok-1"},
        {"user_id": "u2", "bearer_token": "tok-2"},
    ]

    response = authed_client.post("/skills/retrofit")

    assert response.status_code == 200
    assert response.json() == {
        "checked": 2,
        "updated": 0,
        "skipped": 2,
        "failed": 0,
    }
    orchestrator._ensure_skills_current.assert_has_awaits(
        [call("u1", "tok-1"), call("u2", "tok-2")]
    )
    assert orchestrator.token_map == {"tok-1": "u1", "tok-2": "u2"}
