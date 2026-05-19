from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker.errors
import httpx
import pytest
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI


load_dotenv(override=True)


@dataclasses.dataclass(frozen=True)
class LocalSupabaseUser:
    id: str
    email: str
    access_token: str


@dataclasses.dataclass(frozen=True)
class E2EStack:
    app: FastAPI
    admin_token: str


@dataclasses.dataclass(frozen=True)
class LiveServer:
    http_base_url: str
    ws_base_url: str


def _local_supabase_env() -> tuple[str, str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not anon_key or not service_key:
        pytest.skip("SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY are required")
    if not (url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:")):
        pytest.skip(f"local Supabase required for this integration test, got {url!r}")
    return url, anon_key, service_key


def _service_headers(service_key: str) -> dict[str, str]:
    return {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }


async def _delete_registry_row(user_id: str) -> None:
    url, _, service_key = _local_supabase_env()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.delete(
            f"{url}/rest/v1/container_registry?user_id=eq.{user_id}",
            headers=_service_headers(service_key),
        )


async def _fetch_registry_row(user_id: str) -> dict[str, Any] | None:
    url, _, service_key = _local_supabase_env()
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{url}/rest/v1/container_registry"
            f"?user_id=eq.{user_id}"
            "&select=user_id,container_id,ws_url,http_url,status,current_session_id",
            headers=_service_headers(service_key),
        )
    assert response.status_code == 200, response.text
    rows = response.json()
    return rows[0] if rows else None


async def _wait_for_registry_row(user_id: str, *, timeout: float = 15) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        row = await _fetch_registry_row(user_id)
        if row is not None:
            return row
        await asyncio.sleep(0.25)
    raise AssertionError(f"container_registry row was not created for {user_id}")


def _chown_volume(real_docker_client, volume_path: str, uid: int, gid: int) -> None:
    real_docker_client.containers.run(
        "busybox",
        command=f"chown -R {uid}:{gid} /vol",
        user="root",
        volumes={volume_path: {"bind": "/vol", "mode": "rw"}},
        remove=True,
    )


def _llm_env_from_os() -> dict[str, str]:
    env: dict[str, str] = {}
    api_key = os.environ.get("ZEROCLAW_LLM_API_KEY")
    if api_key:
        env["ZEROCLAW_LLM_API_KEY"] = api_key
        env["ZEROCLAW_LLM_PROVIDER"] = os.environ.get("ZEROCLAW_LLM_PROVIDER", "openrouter")
        if os.environ.get("ZEROCLAW_LLM_MODEL"):
            env["ZEROCLAW_LLM_MODEL"] = os.environ["ZEROCLAW_LLM_MODEL"]
    return env


@pytest.fixture
def zeroclaw_e2e_image(real_docker_client):
    image = os.environ.get("ZEROCLAW_TEST_IMAGE") or os.environ.get(
        "ZEROCLAW_IMAGE", "zeroclaw:latest"
    )
    try:
        real_docker_client.images.get(image)
    except docker.errors.ImageNotFound:
        pytest.skip(f"image {image} not present locally")
    return image


@pytest.fixture
async def local_supabase_user() -> LocalSupabaseUser:
    url, anon_key, service_key = _local_supabase_env()
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            settings = await client.get(f"{url}/auth/v1/settings")
        except httpx.RequestError as exc:
            pytest.skip(f"local Supabase auth is not reachable: {exc}")
        if settings.status_code != 200:
            pytest.skip(f"local Supabase auth settings returned {settings.status_code}")

        email = f"claw-e2e-{secrets.token_hex(8)}@example.test"
        password = f"Test-{secrets.token_urlsafe(24)}-1a"
        created = await client.post(
            f"{url}/auth/v1/admin/users",
            headers=_service_headers(service_key),
            json={"email": email, "password": password, "email_confirm": True},
        )
        assert created.status_code in (200, 201), created.text
        user_id = created.json()["id"]

        token = await client.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {anon_key}",
                "Content-Type": "application/json",
            },
            json={"email": email, "password": password},
        )
        assert token.status_code == 200, token.text
        access_token = token.json()["access_token"]

    try:
        yield LocalSupabaseUser(id=user_id, email=email, access_token=access_token)
    finally:
        await _delete_registry_row(user_id)
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"{url}/auth/v1/admin/users/{user_id}",
                headers=_service_headers(service_key),
            )


@pytest.fixture
async def e2e_stack(
    tmp_path,
    real_docker_client,
    real_data_dir,
    cleanup_container,
    local_supabase_user: LocalSupabaseUser,
    zeroclaw_e2e_image,
) -> E2EStack:
    token_encryption_key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not token_encryption_key:
        pytest.skip("TOKEN_ENCRYPTION_KEY is required for the real container registry")

    from claw_proxy.admin.activity import DefaultActivityChecker
    from claw_proxy.admin.app import create_admin_app
    from claw_proxy.admin.audit import EncryptedJsonlAuditStore
    from claw_proxy.admin.auth import WebAuthnConfig, hash_static_token
    from claw_proxy.admin.jobs import EncryptedJsonJobStore
    from claw_proxy.admin.schema_cache import SchemaCache
    from claw_proxy.admin.storage import EncryptedJsonAdminStore, StaticToken
    from claw_proxy.containers.orchestrator import ContainerOrchestrator
    from claw_proxy.crypto import TokenCrypto
    from claw_proxy.db import ContainerRegistry
    from claw_proxy.files.upload import create_upload_router
    from claw_proxy.ws.proxy import create_ws_app
    from claw_proxy.ws.shared_session import get_connections

    registry = ContainerRegistry(token_encryption_key)
    orchestrator = ContainerOrchestrator(
        registry=registry,
        docker_client=real_docker_client,
        image=zeroclaw_e2e_image,
        data_dir=str(real_data_dir),
        llm_env=_llm_env_from_os(),
        push_webhook_base_url="http://localhost:0",
        templates_dir=str(Path.cwd() / "templates"),
        network_mode="host",
    )

    from claw_proxy.auth import get_current_user, verify_jwt

    zeroclaw_app = create_ws_app(
        orchestrator=orchestrator,
        auth_fn=verify_jwt,
    )
    zeroclaw_app.include_router(
        create_upload_router(orchestrator=orchestrator, auth_fn=get_current_user)
    )

    crypto = TokenCrypto(token_encryption_key)
    admin_store = EncryptedJsonAdminStore(tmp_path / "admin.json.enc", crypto)
    audit_store = EncryptedJsonlAuditStore(tmp_path / "audit.jsonl.enc", crypto)
    job_store = EncryptedJsonJobStore(tmp_path / "jobs", crypto)
    admin_token = f"static-{secrets.token_urlsafe(24)}"
    await admin_store.add_static_token(
        StaticToken(
            hash=hash_static_token(admin_token),
            label="integration-e2e",
            created_at=datetime.now(timezone.utc).isoformat(),
            last_used_at=None,
        )
    )
    admin_app = create_admin_app(
        admin_store=admin_store,
        audit_store=audit_store,
        job_store=job_store,
        webauthn_cfg=WebAuthnConfig(
            rp_id="localhost",
            rp_name="Claw integration",
            origin="http://localhost",
        ),
        jwt_secret="integration-admin-jwt-secret",
        orchestrator=orchestrator,
        docker_client=real_docker_client,
        registry=registry,
        activity_checker=DefaultActivityChecker(
            registry=registry,
            get_connections=get_connections,
            idle_threshold_seconds=0,
        ),
        schema_cache=SchemaCache(real_docker_client),
        supabase_client=object(),
    )

    app = FastAPI()
    app.mount("/zeroclaw", zeroclaw_app)
    app.mount("/claw-admin", admin_app)

    try:
        yield E2EStack(app=app, admin_token=admin_token)
    finally:
        row = await _fetch_registry_row(local_supabase_user.id)
        if row and row.get("container_id"):
            cleanup_container(row["container_id"])
        cleanup_container(f"zeroclaw-{local_supabase_user.id[:8]}")
        await _delete_registry_row(local_supabase_user.id)


@pytest.fixture
async def live_server(e2e_stack: E2EStack) -> LiveServer:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        e2e_stack.app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while not server.started:
            if task.done():
                task.result()
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("Uvicorn test server did not start")
            await asyncio.sleep(0.05)
        yield LiveServer(
            http_base_url=f"http://127.0.0.1:{port}",
            ws_base_url=f"ws://127.0.0.1:{port}",
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def _recv_json(ws, *, timeout: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def _connect_until_ready(ws_url: str, access_token: str) -> tuple[Any, str]:
    ws = await websockets.connect(
        f"{ws_url}/zeroclaw/ws?token={access_token}",
        ping_interval=None,
        open_timeout=10,
    )
    try:
        while True:
            msg = await _recv_json(ws, timeout=90)
            if msg.get("type") == "connected":
                return ws, msg["sessionId"]
            if msg.get("type") == "error":
                raise AssertionError(f"WS connect failed: {msg}")
    except Exception:
        await ws.close()
        raise


async def _live_chat_once(ws_url: str, access_token: str, prompt: str) -> str:
    ws, _session_id = await _connect_until_ready(ws_url, access_token)
    chunks: list[str] = []
    try:
        await ws.send(json.dumps({"type": "message", "content": prompt}))
        while True:
            msg = await _recv_json(ws, timeout=180)
            msg_type = msg.get("type")
            if msg_type == "chat.chunk":
                chunks.append(msg.get("content", ""))
            elif msg_type == "chat.done":
                response = msg.get("fullResponse") or "".join(chunks)
                assert response.strip(), "live chat completed with an empty response"
                return response
            elif msg_type == "error":
                raise AssertionError(f"live chat failed: {msg}")
    finally:
        await ws.close()


@pytest.mark.integration
async def test_full_supabase_auth_container_admin_restart_journey(
    live_server: LiveServer,
    e2e_stack: E2EStack,
    local_supabase_user: LocalSupabaseUser,
    real_docker_client,
    real_data_dir,
):
    access_token = local_supabase_user.access_token
    user_id = local_supabase_user.id
    admin_headers = {"Authorization": f"Bearer {e2e_stack.admin_token}"}

    first_response = await _live_chat_once(
        live_server.ws_base_url,
        access_token,
        "Reply with a short sentence containing E2E_OK.",
    )
    assert first_response

    row = await _wait_for_registry_row(user_id)
    async with httpx.AsyncClient(timeout=10) as direct_http:
        health = await direct_http.get(f"{row['http_url']}/health")
    assert health.status_code == 200

    async with httpx.AsyncClient(base_url=live_server.http_base_url, timeout=60) as http:
        upload = await http.post(
            "/zeroclaw/workspace/files",
            headers={"Authorization": f"Bearer {access_token}"},
            files={"file": ("e2e-note.txt", b"hello from e2e", "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        upload_body = upload.json()
        assert upload_body["filename"] == "e2e-note.txt"
        assert upload_body["annotation"].startswith("[FILE:/zeroclaw-data/workspace/temp/")

        config_response = await http.get(
            f"/claw-admin/containers/{user_id}/config",
            headers=admin_headers,
        )
        assert config_response.status_code == 200, config_response.text
        assert isinstance(config_response.json(), dict)

        config_write = await http.patch(
            f"/claw-admin/containers/{user_id}/config",
            headers=admin_headers,
            json={
                "updates": [
                    {"path": "autonomy.max-actions-per-hour", "value": 199},
                ],
            },
        )
        assert config_write.status_code == 200, config_write.text
        config_results = config_write.json()["results"]
        assert config_results[0]["applied"] is True, config_results

        volume_path = os.path.join(str(real_data_dir), user_id)
        _chown_volume(real_docker_client, volume_path, os.getuid(), os.getgid())

        workspace_write = await http.put(
            f"/claw-admin/containers/{user_id}/workspace/files/e2e-admin-note.txt",
            headers=admin_headers,
            json={"content": "admin wrote this\n", "mode": "overwrite"},
        )
        assert workspace_write.status_code == 200, workspace_write.text
        assert workspace_write.json()["status"] == "written"

        workspace_read = await http.get(
            f"/claw-admin/containers/{user_id}/workspace/files/e2e-admin-note.txt",
            headers=admin_headers,
        )
        assert workspace_read.status_code == 200, workspace_read.text
        assert workspace_read.json()["content"] == "admin wrote this\n"

        workspace_list = await http.get(
            f"/claw-admin/containers/{user_id}/workspace/files",
            headers=admin_headers,
            params={"recursive": "true"},
        )
        assert workspace_list.status_code == 200, workspace_list.text
        listed_paths = {item["path"] for item in workspace_list.json()["items"]}
        assert "e2e-admin-note.txt" in listed_paths
        assert any(path.startswith("temp/") and path.endswith("e2e-note.txt") for path in listed_paths)

        _chown_volume(real_docker_client, volume_path, 65534, 65534)

        stop = await http.post(
            f"/claw-admin/containers/{user_id}/stop",
            headers=admin_headers,
            json={},
        )
        assert stop.status_code == 200, stop.text
        assert stop.json()["status"] == "stopped"

        restart = await http.post(
            f"/claw-admin/containers/{user_id}/restart",
            headers=admin_headers,
            json={},
        )
        assert restart.status_code == 200, restart.text
        assert restart.json()["status"] == "restarted"

    restarted_row = await _wait_for_registry_row(user_id)
    async with httpx.AsyncClient(timeout=10) as direct_http:
        restarted_health = await direct_http.get(f"{restarted_row['http_url']}/health")
    assert restarted_health.status_code == 200

    second_response = await _live_chat_once(
        live_server.ws_base_url,
        access_token,
        "Reply with a short sentence containing E2E_RESTART_OK.",
    )
    assert second_response

    async with httpx.AsyncClient(base_url=live_server.http_base_url, timeout=60) as http:
        final_stop = await http.post(
            f"/claw-admin/containers/{user_id}/stop",
            headers=admin_headers,
            json={},
        )
    assert final_stop.status_code == 200, final_stop.text
    assert final_stop.json()["status"] == "stopped"
