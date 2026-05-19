"""End-to-end integration tests for LifeAtlas file-access tools.

Requires Docker, a local Supabase test project, and the ZeroClaw test image.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
import time
from asyncio import TimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from claw_proxy.containers.orchestrator import ContainerOrchestrator
from claw_proxy.containers.workspace import (
    delete_files_via_busybox,
    find_expired_lifeatlas_files,
    write_file_via_busybox,
)
from claw_proxy.tools.lifeatlas.router import create_lifeatlas_tools_router
from tests.integration.test_full_e2e_real import (
    LocalSupabaseUser,
    _local_supabase_env,
    local_supabase_user,
)


pytestmark = pytest.mark.integration


@dataclasses.dataclass
class SeededUser:
    user_id: str
    profile_id: str
    tool_token: str
    volume_path: str
    http: httpx.AsyncClient
    supabase: Any
    orchestrator: ContainerOrchestrator
    storage_paths: list[tuple[str, str]]
    health_file_ids: list[str]
    log_event_ids: list[str]


def _run_or_skip(label: str, fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        pytest.skip(f"LifeAtlas test schema/storage unavailable while {label}: {exc}")


def _create_supabase_client():
    url, _, service_key = _local_supabase_env()
    try:
        from supabase import create_client
    except ImportError as exc:
        pytest.skip(f"supabase client package is required: {exc}")
    return create_client(url, service_key)


def _ensure_bucket(supabase, name: str) -> None:
    buckets = supabase.storage.list_buckets()
    if any((bucket.get("name") if isinstance(bucket, dict) else bucket.name) == name for bucket in buckets):
        return
    supabase.storage.create_bucket(name, options={"public": False})


def _seed_profile(supabase, user_id: str) -> str:
    profile_id = user_id
    supabase.table("user_profiles").upsert(
        {
            "id": profile_id,
            "user_id": user_id,
            "type": "ME",
            "name": "Me",
            "is_default": True,
        }
    ).execute()
    supabase.table("user_active_profiles").upsert(
        {
            "user_id": user_id,
            "active_profile_id": profile_id,
        }
    ).execute()
    return profile_id


def _cleanup_seeded_rows(seeded: SeededUser) -> None:
    supabase = seeded.supabase
    for event_id in seeded.log_event_ids:
        try:
            supabase.table("log_events").delete().eq("id", event_id).execute()
        except Exception:
            pass
    for file_id in seeded.health_file_ids:
        try:
            supabase.table("health_data_files").delete().eq("id", file_id).execute()
        except Exception:
            pass
    try:
        supabase.table("user_active_profiles").delete().eq("user_id", seeded.user_id).execute()
    except Exception:
        pass
    try:
        supabase.table("user_profiles").delete().eq("id", seeded.profile_id).execute()
    except Exception:
        pass


def _cleanup_seeded_storage(seeded: SeededUser) -> None:
    by_bucket: dict[str, list[str]] = {}
    for bucket, path in seeded.storage_paths:
        by_bucket.setdefault(bucket, []).append(path)
    for bucket, paths in by_bucket.items():
        try:
            seeded.supabase.storage.from_(bucket).remove(paths)
        except Exception:
            pass


@pytest.fixture
def lifeatlas_supabase():
    supabase = _create_supabase_client()
    _run_or_skip("checking health_data bucket", _ensure_bucket, supabase, "health_data")
    _run_or_skip("checking event_photos bucket", _ensure_bucket, supabase, "event_photos")
    return supabase


@pytest.fixture
async def seeded_user(
    real_docker_client,
    zeroclaw_image,
    real_data_dir,
    cleanup_container,
    lifeatlas_supabase,
    local_supabase_user: LocalSupabaseUser,
):
    registry = AsyncMock()
    registry.get.return_value = None
    registry.insert.return_value = None
    registry.update_urls = AsyncMock()
    registry.update_status = AsyncMock()

    orchestrator = ContainerOrchestrator(
        registry=registry,
        docker_client=real_docker_client,
        image=zeroclaw_image,
        data_dir=str(real_data_dir),
        push_webhook_base_url="http://localhost:0",
        templates_dir=str(Path.cwd() / "templates"),
        network_mode="host",
    )
    user_id = local_supabase_user.id
    container_name = f"zeroclaw-{user_id[:8]}"
    try:
        info = await orchestrator.provision(user_id)
    except TimeoutError as exc:
        cleanup_container(container_name)
        pytest.skip(f"zeroclaw image did not become healthy in time: {exc}")

    profile_id = _run_or_skip("seeding active profile", _seed_profile, lifeatlas_supabase, user_id)
    registry.get.return_value = {
        "container_id": info.container_id,
        "user_id": user_id,
        "ws_url": info.ws_url,
        "http_url": info.http_url,
        "bearer_token": info.bearer_token,
        "status": "ready",
        "current_session_id": None,
    }

    app = FastAPI()
    app.include_router(
        create_lifeatlas_tools_router(
            token_map=orchestrator.token_map,
            get_supabase_client=lambda: lifeatlas_supabase,
            get_orchestrator=lambda: orchestrator,
        ),
        prefix="/zeroclaw",
    )
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=60,
    )
    seeded = SeededUser(
        user_id=user_id,
        profile_id=profile_id,
        tool_token=info.bearer_token,
        volume_path=os.path.join(str(real_data_dir), user_id),
        http=http,
        supabase=lifeatlas_supabase,
        orchestrator=orchestrator,
        storage_paths=[],
        health_file_ids=[],
        log_event_ids=[],
    )
    try:
        yield seeded
    finally:
        await http.aclose()
        _cleanup_seeded_storage(seeded)
        _cleanup_seeded_rows(seeded)
        try:
            await orchestrator.stop(user_id)
        except Exception:
            pass
        cleanup_container(info.container_id)
        cleanup_container(container_name)


async def _seed_health_file(
    seeded: SeededUser,
    *,
    filename: str = "e2e-labs.pdf",
    content: bytes = b"%PDF-1.4\n% lifeatlas e2e\n",
    content_type: str = "application/pdf",
) -> dict[str, Any]:
    file_id = str(uuid4())
    storage_path = f"{seeded.profile_id}/{int(time.time() * 1000)}_{secrets.token_hex(4)}_{filename}"

    def _seed() -> dict[str, Any]:
        seeded.supabase.storage.from_("health_data").upload(
            storage_path,
            content,
            {"content-type": content_type, "upsert": "false"},
        )
        row = {
            "id": file_id,
            "user_id": seeded.user_id,
            "profile_id": seeded.profile_id,
            "filename": filename,
            "file_path": storage_path,
            "file_size": len(content),
            "content_type": content_type,
        }
        inserted = seeded.supabase.table("health_data_files").insert(row).execute()
        assert inserted.data
        return row

    seeded.storage_paths.append(("health_data", storage_path))
    seeded.health_file_ids.append(file_id)
    return await asyncio.to_thread(_run_or_skip, "seeding health data file", _seed)


async def _seed_log_event_photo(seeded: SeededUser) -> dict[str, Any]:
    event_id = str(uuid4())
    photo_path = f"{seeded.user_id}/log_events/{secrets.token_hex(6)}.jpg"
    image = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"

    def _seed() -> dict[str, Any]:
        seeded.supabase.storage.from_("event_photos").upload(
            photo_path,
            image,
            {"content-type": "image/jpeg", "upsert": "false"},
        )
        row = {
            "id": event_id,
            "profile_id": seeded.profile_id,
            "user_id": seeded.user_id,
            "created_by_user_id": seeded.user_id,
            "event_type": "training",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "description": "E2E event with a photo",
            "photo_path": photo_path,
        }
        inserted = seeded.supabase.table("log_events").insert(row).execute()
        assert inserted.data
        return row

    seeded.storage_paths.append(("event_photos", photo_path))
    seeded.log_event_ids.append(event_id)
    return await asyncio.to_thread(_run_or_skip, "seeding log event photo", _seed)


async def _write_workspace_file(
    seeded: SeededUser,
    real_docker_client,
    relative_path: str,
    content: bytes | str,
) -> None:
    await asyncio.to_thread(
        write_file_via_busybox,
        real_docker_client,
        seeded.volume_path,
        f"workspace/{relative_path}",
        content,
    )


async def test_list_health_data_files_end_to_end(seeded_user: SeededUser):
    row = await _seed_health_file(seeded_user)

    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/list_health_data_files",
        params={"token": seeded_user.tool_token},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] >= 1
    listed = {item["file_id"]: item for item in body["files"]}
    assert listed[row["id"]]["filename"] == row["filename"]
    assert listed[row["id"]]["has_extracted_text"] is False


async def test_get_health_data_file_content_streams_to_workspace(
    seeded_user: SeededUser,
):
    row = await _seed_health_file(seeded_user)

    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": seeded_user.tool_token, "file_id": row["id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_id"] == row["id"]
    assert body["workspace_path"].startswith("lifeatlas/files/")
    assert body["container_path"].startswith("/zeroclaw-data/workspace/lifeatlas/files/")
    assert os.path.exists(Path(seeded_user.volume_path) / "workspace" / body["workspace_path"])


async def test_get_log_event_photo_returns_image_tag(seeded_user: SeededUser):
    row = await _seed_log_event_photo(seeded_user)

    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/get_log_event_photo",
        params={"token": seeded_user.tool_token, "event_id": row["id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["event_id"] == row["id"]
    assert body["image_tag"] == f"[IMAGE:{body['container_path']}]"
    assert body["workspace_path"].startswith("lifeatlas/photos/")
    assert os.path.exists(Path(seeded_user.volume_path) / "workspace" / body["workspace_path"])


async def test_save_to_library_inserts_health_data_files_row(
    seeded_user: SeededUser,
    real_docker_client,
):
    await _write_workspace_file(
        seeded_user,
        real_docker_client,
        "outputs/e2e-save.pdf",
        b"%PDF-1.4\n% saved by e2e\n",
    )

    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/save_to_library",
        params={"token": seeded_user.tool_token, "workspace_path": "outputs/e2e-save.pdf"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    seeded_user.health_file_ids.append(body["file_id"])

    def _lookup_saved() -> dict[str, Any]:
        row = (
            seeded_user.supabase.table("health_data_files")
            .select("id, profile_id, user_id, filename, file_path, content_type")
            .eq("id", body["file_id"])
            .maybe_single()
            .execute()
            .data
        )
        assert row
        return row

    saved = await asyncio.to_thread(_lookup_saved)
    seeded_user.storage_paths.append(("health_data", saved["file_path"]))
    assert saved["profile_id"] == seeded_user.profile_id
    assert saved["user_id"] == seeded_user.user_id
    assert saved["filename"] == "e2e-save.pdf"

    downloaded = await asyncio.to_thread(
        seeded_user.supabase.storage.from_("health_data").download,
        saved["file_path"],
    )
    assert downloaded.startswith(b"%PDF-1.4")


async def test_save_to_library_rejects_markdown(
    seeded_user: SeededUser,
    real_docker_client,
):
    await _write_workspace_file(
        seeded_user,
        real_docker_client,
        "outputs/not-health-data.md",
        "# not a supported health-data file\n",
    )

    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/save_to_library",
        params={"token": seeded_user.tool_token, "workspace_path": "outputs/not-health-data.md"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "unsupported_mime"


async def test_lifecycle_sweep_removes_old_lifeatlas_files(
    seeded_user: SeededUser,
    real_docker_client,
):
    row = await _seed_health_file(seeded_user)
    response = await seeded_user.http.get(
        "/zeroclaw/tools/lifeatlas/get_health_data_file_content",
        params={"token": seeded_user.tool_token, "file_id": row["id"]},
    )
    assert response.status_code == 200, response.text
    workspace_path = response.json()["workspace_path"]
    relative_path = f"workspace/{workspace_path}"
    host_path = Path(seeded_user.volume_path) / relative_path
    assert host_path.exists()

    await asyncio.to_thread(
        real_docker_client.containers.run,
        "busybox",
        command=[
            "sh",
            "-ceu",
            'touch -t 197001010000 "$1"',
            "sh",
            f"/vol/{relative_path}",
        ],
        user="root",
        volumes={seeded_user.volume_path: {"bind": "/vol", "mode": "rw"}},
        remove=True,
    )

    expired = await asyncio.to_thread(
        find_expired_lifeatlas_files,
        seeded_user.volume_path,
        24,
    )
    assert relative_path in expired
    deleted = await asyncio.to_thread(
        delete_files_via_busybox,
        real_docker_client,
        seeded_user.volume_path,
        expired,
    )
    assert deleted >= 1
    assert not host_path.exists()
