from datetime import date
import importlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def _make_client(
    token_map: dict[str, str],
    *,
    get_supabase_client=None,
    get_orchestrator=None,
) -> TestClient:
    from claw_proxy.tools.lifeatlas.router import create_lifeatlas_tools_router

    app = FastAPI()
    app.include_router(
        create_lifeatlas_tools_router(
            token_map=token_map,
            get_supabase_client=get_supabase_client or (lambda: object()),
            get_orchestrator=get_orchestrator or (lambda: object()),
        ),
        prefix="/zeroclaw",
    )
    return TestClient(app)


def test_missing_token_returns_401():
    client = _make_client({"known-token": "user-1"})

    response = client.get("/zeroclaw/tools/lifeatlas/get_recent_sleep")

    assert response.status_code == 401
    assert response.json() == {"error": "missing token"}


def test_route_factory_exposes_tools_under_zeroclaw_prefix():
    client = _make_client({"known-token": "user-1"})

    response = client.get("/zeroclaw/tools/lifeatlas/get_recent_sleep")

    assert response.status_code == 401
    assert response.json() == {"error": "missing token"}


def test_app_mounts_lifeatlas_tools_router_when_zeroclaw_enabled(
    monkeypatch, request, tmp_path
):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service")

    import claw_proxy.config as proxy_config
    import claw_proxy.containers.orchestrator as orchestrator_module
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    zeroclaw_config = {
        "token_encryption_key": "test-key",
        "zeroclaw_image": "zeroclaw:test",
        "zeroclaw_data_dir": "/tmp/zeroclaw-test",
        "admin_data_dir": str(tmp_path / "admin"),
        "network_mode": "host",
        "network_name": "lifeatlas-net",
        "push_webhook_base_url": "http://127.0.0.1:8000",
        "lifecycle_check_interval_minutes": 60,
        "inactive_container_days": 30,
        "templates_dir": "",
    }

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.token_map = {"known-token": "user-1"}
            self.docker = object()

        async def ensure_workspace_directories(self):
            return None

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        return {"ok": True, "user_id": user_id, "tool_name": tool_name}

    monkeypatch.setattr(proxy_config, "ZEROCLAW_CONFIG", zeroclaw_config)
    monkeypatch.setattr(proxy_config, "get_supabase_client", lambda: object())
    monkeypatch.setattr(
        orchestrator_module,
        "ContainerOrchestrator",
        FakeOrchestrator,
    )
    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    sys.modules.pop("claw_proxy.app", None)
    request.addfinalizer(lambda: sys.modules.pop("claw_proxy.app", None))
    app_module = importlib.import_module("claw_proxy.app")

    client = TestClient(app_module.app)
    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "user_id": "user-1",
        "tool_name": "get_recent_sleep",
    }


def test_unknown_token_returns_401():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "unknown-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"error": "unknown token"}


def test_unknown_tool_returns_404():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/not_a_tool",
        params={"token": "known-token"},
    )

    assert response.status_code == 404
    assert response.json() == {"error": "unknown tool"}


def test_unknown_tool_ignores_invalid_irrelevant_days_param():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/not_a_tool",
        params={"token": "known-token", "days": "not-an-int"},
    )

    assert response.status_code == 404
    assert response.json() == {"error": "unknown tool"}


def test_bad_days_parameter_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token", "days": "0"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "days must be between 1 and 90"}


def test_success_result_json_encodes_dates(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        return {"start_date": date(2026, 4, 28)}

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"start_date": "2026-04-28"}


def test_helper_key_error_returns_502_not_unknown_tool(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise KeyError("missing_join")

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token"},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "tool unavailable"}


def test_helper_value_error_returns_502_not_server_exception(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise ValueError("some helper failure")

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token"},
    )

    assert response.status_code == 502
    assert response.json() == {"error": "tool unavailable"}


def test_profile_resolution_value_error_returns_409_no_active_profile(
    monkeypatch,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise ValueError("No active profile found for user user-1")

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_timeline_entries",
        params={"token": "known-token"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "no_active_profile"
    assert body["detail"] == "No active profile is configured for this user."
    assert "user-1" not in response.text


def test_get_last_workout_ignores_invalid_irrelevant_days_param(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    class FakeStravaHelper:
        def __init__(self, supabase):
            self.supabase = supabase

        def get_recent_activity(self, user_id):
            return {"activity": "run", "user_id": user_id}

    monkeypatch.setattr(lifeatlas_router, "StravaHelper", FakeStravaHelper)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_last_workout",
        params={"token": "known-token", "days": "not-an-int"},
    )

    assert response.status_code == 200
    assert response.json() == {"activity": "run", "user_id": "user-1"}


def test_dispatch_resolves_token_user_and_returns_tool_body(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        calls.append(
            {
                "supabase": supabase,
                "orchestrator": orchestrator,
                "user_id": user_id,
                "tool_name": tool_name,
                "params": params,
            }
        )
        return {"ok": True, "user_id": user_id}

    supabase = object()
    orchestrator = object()
    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client(
        {"known-token": "user-1"},
        get_supabase_client=lambda: supabase,
        get_orchestrator=lambda: orchestrator,
    )

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_recent_sleep",
        params={"token": "known-token", "days": "3"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "user_id": "user-1"}
    assert calls == [
        {
            "supabase": supabase,
            "orchestrator": orchestrator,
            "user_id": "user-1",
            "tool_name": "get_recent_sleep",
            "params": {
                "days": "3",
                "limit": None,
                "weeks_ago": None,
                "active_only": None,
                "upcoming_only": None,
                "entry_type": None,
                "file_id": None,
                "event_id": None,
                "timeline_entry_id": None,
                "full": None,
                "workspace_path": None,
                "event_type": None,
            },
        }
    ]


def test_list_health_data_files_dispatches_with_resolved_profile(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_health_data_files(self, user_id, profile_id, timeline_entry_id):
            calls.append(
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "timeline_entry_id": timeline_entry_id,
                }
            )
            return {"files": [], "count": 0}

    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_health_data_files",
        params={"token": "known-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"files": [], "count": 0}
    assert calls == [
        {
            "user_id": "user-1",
            "profile_id": "profile-1",
            "timeline_entry_id": None,
        }
    ]


def test_list_health_data_files_passes_timeline_entry_filter(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    timeline_entry_id = "8aa8dcd1-0871-44ad-a80d-81078b28627b"
    calls = []

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_health_data_files(self, user_id, profile_id, timeline_entry_id):
            calls.append(timeline_entry_id)
            return {"files": [{"file_id": "file-1"}], "count": 1}

    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_health_data_files",
        params={"token": "known-token", "timeline_entry_id": timeline_entry_id},
    )

    assert response.status_code == 200
    assert response.json() == {"files": [{"file_id": "file-1"}], "count": 1}
    assert calls == [timeline_entry_id]


def test_list_health_data_files_canonicalizes_timeline_entry_id(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    timeline_entry_id = "8aa8dcd1-0871-44ad-a80d-81078b28627b"
    calls = []

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_health_data_files(self, user_id, profile_id, timeline_entry_id):
            calls.append(timeline_entry_id)
            return {"files": [], "count": 0}

    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_health_data_files",
        params={
            "token": "known-token",
            "timeline_entry_id": f"{{{timeline_entry_id}}}",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"files": [], "count": 0}
    assert calls == [timeline_entry_id]


def test_list_health_data_files_invalid_timeline_entry_id_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_health_data_files",
        params={"token": "known-token", "timeline_entry_id": "not-a-uuid"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "timeline_entry_id must be a UUID"}


def test_new_file_query_params_are_forwarded_to_call_tool(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        calls.append(params)
        return {"ok": True}

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={
            "token": "known-token",
            "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
            "timeline_entry_id": "8aa8dcd1-0871-44ad-a80d-81078b28627b",
            "full": "true",
            "event_id": "event-1",
            "workspace_path": "workspace/lifeatlas/files/doc.txt",
            "event_type": "race",
        },
    )

    assert response.status_code == 200
    assert calls[0] == {
        "days": None,
        "limit": None,
        "weeks_ago": None,
        "active_only": None,
        "upcoming_only": None,
        "entry_type": None,
        "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
        "event_id": "event-1",
        "timeline_entry_id": "8aa8dcd1-0871-44ad-a80d-81078b28627b",
        "full": "true",
        "workspace_path": "workspace/lifeatlas/files/doc.txt",
        "event_type": "race",
    }


def test_save_to_library_returns_success_from_helper(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    class FakeSaveHelper:
        def __init__(self, supabase, orchestrator):
            self.supabase = supabase
            self.orchestrator = orchestrator

        async def save_to_library(self, user_id, workspace_path):
            calls.append(
                {
                    "user_id": user_id,
                    "workspace_path": workspace_path,
                }
            )
            return {"success": True, "file_id": "file-1"}

    monkeypatch.setattr(lifeatlas_router, "SaveHelper", FakeSaveHelper, raising=False)
    client = _make_client({"T": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/save_to_library",
        params={"token": "T", "workspace_path": "output/r.pdf"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [{"user_id": "user-1", "workspace_path": "output/r.pdf"}]


def test_save_to_library_structured_path_failure_returns_200(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    class FakeSaveHelper:
        def __init__(self, supabase, orchestrator):
            self.supabase = supabase
            self.orchestrator = orchestrator

        async def save_to_library(self, user_id, workspace_path):
            return {
                "success": False,
                "error_code": "path_invalid",
                "error": "path invalid",
            }

    monkeypatch.setattr(lifeatlas_router, "SaveHelper", FakeSaveHelper, raising=False)
    client = _make_client({"T": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/save_to_library",
        params={"token": "T", "workspace_path": "../secret.pdf"},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["error_code"] == "path_invalid"


def test_get_health_data_file_content_dispatches_and_parses_full(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    file_id = "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d"
    calls = []

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_health_data_file_content(self, user_id, profile_id, file_id, full):
            calls.append(
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "file_id": file_id,
                    "full": full,
                }
            )
            return {"file_id": file_id, "text": "complete"}

    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": "known-token", "file_id": file_id, "full": "true"},
    )

    assert response.status_code == 200
    assert response.json() == {"file_id": file_id, "text": "complete"}
    assert calls == [
        {
            "user_id": "user-1",
            "profile_id": "profile-1",
            "file_id": file_id,
            "full": True,
        }
    ]


def test_get_health_data_file_content_canonicalizes_file_id(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    file_id = "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d"
    calls = []

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_health_data_file_content(self, user_id, profile_id, file_id, full):
            calls.append(file_id)
            return {"file_id": file_id}

    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": "known-token", "file_id": f"urn:uuid:{file_id}"},
    )

    assert response.status_code == 200
    assert response.json() == {"file_id": file_id}
    assert calls == [file_id]


def test_get_health_data_file_content_fetch_partial_binds_supabase_and_orchestrator(
    monkeypatch,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    file_id = "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d"
    supabase = object()
    orchestrator = object()
    calls = []

    async def fake_fetch_to_workspace(
        *,
        orchestrator,
        supabase,
        user_id,
        bucket,
        storage_path,
        dest_subdir,
        dest_basename_prefix,
        original_filename,
    ):
        calls.append(
            {
                "orchestrator": orchestrator,
                "supabase": supabase,
                "user_id": user_id,
                "bucket": bucket,
                "storage_path": storage_path,
                "dest_subdir": dest_subdir,
                "dest_basename_prefix": dest_basename_prefix,
                "original_filename": original_filename,
            }
        )
        return {
            "workspace_path": "lifeatlas/files/doc.pdf",
            "container_path": "/workspace/lifeatlas/files/doc.pdf",
        }

    class FakeFilesHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_health_data_file_content(self, user_id, profile_id, file_id, full):
            return await self.files_fetch(
                user_id=user_id,
                bucket="health_data",
                storage_path="profiles/p1/doc.pdf",
                dest_subdir="files",
                dest_basename_prefix=file_id,
                original_filename="doc.pdf",
            )

    monkeypatch.setattr(
        lifeatlas_router,
        "_fetch_to_workspace",
        fake_fetch_to_workspace,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "FilesHelper",
        FakeFilesHelper,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client(
        {"known-token": "user-1"},
        get_supabase_client=lambda: supabase,
        get_orchestrator=lambda: orchestrator,
    )

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": "known-token", "file_id": file_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "workspace_path": "lifeatlas/files/doc.pdf",
        "container_path": "/workspace/lifeatlas/files/doc.pdf",
    }
    assert calls == [
        {
            "orchestrator": orchestrator,
            "supabase": supabase,
            "user_id": "user-1",
            "bucket": "health_data",
            "storage_path": "profiles/p1/doc.pdf",
            "dest_subdir": "files",
            "dest_basename_prefix": file_id,
            "original_filename": "doc.pdf",
        }
    ]


def test_get_health_data_file_content_missing_file_id_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": "known-token"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "file_id is required"}


def test_get_health_data_file_content_invalid_file_id_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": "known-token", "file_id": "not-a-uuid"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "file_id must be a UUID"}


def test_list_log_events_dispatches_with_resolved_profile_and_params(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_log_events(self, user_id, profile_id, event_type, days, limit):
            calls.append(
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "event_type": event_type,
                    "days": days,
                    "limit": limit,
                }
            )
            return {
                "events": [{"event_id": "event-1", "event_type": event_type}],
                "count": 1,
            }

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_log_events",
        params={
            "token": "known-token",
            "event_type": "medicine",
            "days": "7",
            "limit": "10",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "events": [{"event_id": "event-1", "event_type": "medicine"}],
        "count": 1,
    }
    assert calls == [
        {
            "user_id": "user-1",
            "profile_id": "profile-1",
            "event_type": "medicine",
            "days": 7,
            "limit": 10,
        }
    ]


@pytest.mark.parametrize(
    ("raw_days", "expected_days"),
    [
        (None, 30),
        ("1", 1),
        ("365", 365),
    ],
)
def test_list_log_events_parses_days_default_min_and_max(
    monkeypatch,
    raw_days,
    expected_days,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_log_events(self, user_id, profile_id, event_type, days, limit):
            calls.append(days)
            return {"events": [], "count": 0}

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})
    params = {"token": "known-token", "limit": "5"}
    if raw_days is not None:
        params["days"] = raw_days

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_log_events",
        params=params,
    )

    assert response.status_code == 200
    assert response.json() == {"events": [], "count": 0}
    assert calls == [expected_days]


@pytest.mark.parametrize(
    ("raw_limit", "expected_limit"),
    [
        (None, 50),
        ("1", 1),
        ("200", 200),
    ],
)
def test_list_log_events_parses_limit_default_min_and_max(
    monkeypatch,
    raw_limit,
    expected_limit,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    calls = []

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        def list_log_events(self, user_id, profile_id, event_type, days, limit):
            calls.append(limit)
            return {"events": [], "count": 0}

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})
    params = {"token": "known-token", "days": "5"}
    if raw_limit is not None:
        params["limit"] = raw_limit

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_log_events",
        params=params,
    )

    assert response.status_code == 200
    assert response.json() == {"events": [], "count": 0}
    assert calls == [expected_limit]


@pytest.mark.parametrize(
    ("param_name", "raw_value", "expected_error"),
    [
        ("days", "not-an-int", "days must be an integer"),
        ("days", "0", "days must be between 1 and 365"),
        ("days", "366", "days must be between 1 and 365"),
        ("limit", "not-an-int", "limit must be an integer"),
        ("limit", "0", "limit must be between 1 and 200"),
        ("limit", "201", "limit must be between 1 and 200"),
    ],
)
def test_list_log_events_bad_days_or_limit_returns_400(
    monkeypatch,
    param_name,
    raw_value,
    expected_error,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/list_log_events",
        params={"token": "known-token", param_name: raw_value},
    )

    assert response.status_code == 400
    assert response.json() == {"error": expected_error}


def test_get_log_event_photo_dispatches_with_resolved_profile_and_event_id(
    monkeypatch,
):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    event_id = "524f58bb-1722-40eb-90ca-64b4dc104655"
    calls = []

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_log_event_photo(self, user_id, profile_id, event_id):
            calls.append(
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "event_id": event_id,
                }
            )
            return {
                "event_id": event_id,
                "image_tag": "[IMAGE:/workspace/lifeatlas/photos/photo.jpg]",
            }

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token", "event_id": event_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "event_id": event_id,
        "image_tag": "[IMAGE:/workspace/lifeatlas/photos/photo.jpg]",
    }
    assert calls == [
        {
            "user_id": "user-1",
            "profile_id": "profile-1",
            "event_id": event_id,
        }
    ]


def test_get_log_event_photo_no_photo_response_passes_through_200(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    event_id = "524f58bb-1722-40eb-90ca-64b4dc104655"

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_log_event_photo(self, user_id, profile_id, event_id):
            return {"message": "No photo attached to this event", "event_id": event_id}

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token", "event_id": event_id},
    )

    assert response.status_code == 200
    assert response.json() == {
        "message": "No photo attached to this event",
        "event_id": event_id,
    }


def test_get_log_event_photo_missing_event_id_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "event_id is required"}


def test_get_log_event_photo_invalid_event_id_returns_400():
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token", "event_id": "not-a-uuid"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "event_id must be a UUID"}


def test_get_log_event_photo_lookup_error_returns_404_not_found(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router
    from claw_proxy.tools.lifeatlas.common import ToolLookupError

    event_id = "524f58bb-1722-40eb-90ca-64b4dc104655"

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_log_event_photo(self, user_id, profile_id, event_id):
            raise ToolLookupError("event missing")

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token", "event_id": event_id},
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "detail": "event missing"}


def test_get_log_event_photo_canonicalizes_event_id(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router

    event_id = "524f58bb-1722-40eb-90ca-64b4dc104655"
    calls = []

    class FakeLogEventsHelper:
        def __init__(self, supabase, files_fetch):
            self.supabase = supabase
            self.files_fetch = files_fetch

        async def get_log_event_photo(self, user_id, profile_id, event_id):
            calls.append(event_id)
            return {"event_id": event_id}

    monkeypatch.setattr(
        lifeatlas_router,
        "LogEventsHelper",
        FakeLogEventsHelper,
        raising=False,
    )
    monkeypatch.setattr(
        lifeatlas_router,
        "_profile_id_for",
        lambda supabase, user_id: "profile-1",
    )
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": "known-token", "event_id": f"{{{event_id}}}"},
    )

    assert response.status_code == 200
    assert response.json() == {"event_id": event_id}
    assert calls == [event_id]


def test_tool_lookup_error_returns_404_not_found(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router
    from claw_proxy.tools.lifeatlas.common import ToolLookupError

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise ToolLookupError("file missing")

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={
            "token": "known-token",
            "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "detail": "file missing"}


def test_container_not_provisioned_returns_409(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router
    from claw_proxy.tools.lifeatlas.common import ContainerNotProvisioned

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise ContainerNotProvisioned()

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={
            "token": "known-token",
            "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "container_not_provisioned",
        "detail": "Container not yet ready; try again shortly.",
    }


def test_fetch_too_large_returns_413(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router
    from claw_proxy.tools.lifeatlas.common import FetchTooLarge

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise FetchTooLarge(30_000_000)

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={
            "token": "known-token",
            "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "error": "file_too_large",
        "detail": "File is 30000000 bytes; the fetch limit is 25 MB.",
    }


def test_storage_fetch_error_returns_502_storage_unavailable(monkeypatch):
    from claw_proxy.tools.lifeatlas import router as lifeatlas_router
    from claw_proxy.tools.lifeatlas.common import StorageFetchError

    async def fake_call_tool(supabase, orchestrator, user_id, tool_name, params):
        raise StorageFetchError("storage down")

    monkeypatch.setattr(lifeatlas_router, "_call_tool", fake_call_tool)
    client = _make_client({"known-token": "user-1"})

    response = client.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={
            "token": "known-token",
            "file_id": "ae6edceb-3ec6-4475-8a5e-2ef78ecb8f1d",
        },
    )

    assert response.status_code == 502
    assert response.json() == {"error": "storage_unavailable"}
