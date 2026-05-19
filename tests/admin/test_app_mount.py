from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_admin_app_mounted_when_token_encryption_key_set(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_URL", "http://supabase.test")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-key-must-be-32-bytes-or-longer")
    monkeypatch.setenv("ZEROCLAW_DATA_DIR", str(tmp_path / "zeroclaw"))
    monkeypatch.setenv("ADMIN_DATA_DIR", str(tmp_path / "admin"))
    monkeypatch.setenv("ZEROCLAW_TEMPLATES_DIR", str(tmp_path / "templates"))
    allowlist_path = tmp_path / "allowlist.toml"
    allowlist_path.write_text("allowed_paths = []\n", encoding="utf-8")
    monkeypatch.setenv("USER_CONFIG_ALLOWLIST_PATH", str(allowlist_path))

    import claw_proxy.config as config_module
    import claw_proxy.files.storage as storage_module
    import claw_proxy.app as app_module

    importlib.reload(config_module)
    monkeypatch.setattr(config_module, "get_supabase_client", lambda: object())

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(storage_module, "ensure_bucket_exists", _noop)
    importlib.reload(app_module)

    with TestClient(app_module.app) as client:
        response = client.get("/claw-admin/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Claw admin"
