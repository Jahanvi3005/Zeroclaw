from __future__ import annotations

import tomli_w


def _workspace_setup(tmp_path, admin_deps):
    user_dir = tmp_path / "u1" / "workspace"
    user_dir.mkdir(parents=True)
    (user_dir / "USER.md").write_text("# user", encoding="utf-8")
    (tmp_path / "u1" / ".zeroclaw").mkdir(parents=True)
    (tmp_path / "u1" / ".zeroclaw" / "config.toml").write_bytes(
        tomli_w.dumps({"llm": {"provider": "openrouter"}}).encode()
    )
    admin_deps["orchestrator"].data_dir = str(tmp_path)
    admin_deps["registry"].get.return_value = {
        "container_id": "c1",
        "user_id": "u1",
        "status": "ready",
        "http_url": "",
        "last_active_at": "t",
    }
    return tmp_path


def test_get_config(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.get("/containers/u1/config")
    assert response.status_code == 200
    assert response.json()["llm.provider"] == "openrouter"


def test_list_workspace(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.get("/containers/u1/workspace/files")
    assert response.status_code == 200
    assert any(item["path"] == "USER.md" for item in response.json()["items"])


def test_read_workspace_file(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.get("/containers/u1/workspace/files/USER.md")
    assert response.status_code == 200
    assert "# user" in response.json()["content"]


def test_write_workspace_file(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.put(
        "/containers/u1/workspace/files/notes.md",
        json={"content": "x", "mode": "create"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "written"


def test_patch_workspace_file(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.patch(
        "/containers/u1/workspace/files/USER.md",
        json={
            "operations": [
                {
                    "action": "replace_substring",
                    "find": "# user",
                    "replace": "# patched",
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "patched"
    assert (tmp_path / "u1" / "workspace" / "USER.md").read_text() == "# patched"


def test_delete_workspace_file(authed_client, admin_deps, tmp_path):
    _workspace_setup(tmp_path, admin_deps)
    response = authed_client.delete("/containers/u1/workspace/files/USER.md")
    assert response.status_code == 200
