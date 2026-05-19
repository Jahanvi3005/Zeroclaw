"""ZeroClaw chat backend — standalone deployment or LifeAtlas backend integration.

When integrating into the LifeAtlas backend, copy the httpx.RequestError
exception handler below into the main app — it's a transport-error backstop
that catches unhandled connection failures, DNS errors, and timeouts.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from claw_proxy.config import (
    ALLOWED_ORIGIN_REGEX,
    ALLOWED_ORIGINS,
    LOG_LEVEL,
    ZEROCLAW_CONFIG,
    get_supabase_client,
)
from claw_proxy.containers.workspace import (
    delete_files_via_busybox,
    find_expired_lifeatlas_files,
    find_expired_temp_uploads,
)
from claw_proxy.files.storage import delete_expired_exports, ensure_bucket_exists

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger(__name__)

_cleanup_task = None
_orchestrator = None
_registry = None
_admin_store = None
_audit_store = None
_job_store = None


@asynccontextmanager
async def lifespan(app):
    global _cleanup_task
    if ZEROCLAW_CONFIG:
        supabase_client = get_supabase_client()
        try:
            await ensure_bucket_exists(supabase_client)
        except Exception as exc:
            log.warning("Bucket initialization failed (will retry later): %s", exc)

        if _orchestrator is not None:
            try:
                await _orchestrator.ensure_workspace_directories()
            except Exception as exc:
                log.warning("Workspace directory check failed: %s", exc)

        if _admin_store is not None:
            try:
                await bootstrap_if_empty(_admin_store)
            except Exception as exc:
                log.warning("Admin bootstrap failed: %s", exc)

        _cleanup_task = asyncio.create_task(_lifecycle_loop())
    yield
    if _cleanup_task:
        _cleanup_task.cancel()


app = FastAPI(title="Claw proxy", lifespan=lifespan)


@app.exception_handler(httpx.RequestError)
async def httpx_transport_error_handler(request: Request, exc: httpx.RequestError):
    """Backstop for unhandled httpx transport errors (connection refused, DNS,
    timeout, etc.). Endpoints with specific error contracts (auth, chat, DB
    lookups) handle their own errors locally — this catches anything that
    slips through."""
    log.error(
        "Upstream transport error on %s %s: %s", request.method, request.url.path, exc
    )
    return JSONResponse(
        status_code=502, content={"detail": "Upstream service unavailable"}
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


# ZeroClaw WS proxy (conditional — only when TOKEN_ENCRYPTION_KEY is set)
if ZEROCLAW_CONFIG:
    if ZEROCLAW_CONFIG["network_mode"] == "shared" and not os.path.exists(
        "/.dockerenv"
    ):
        raise RuntimeError(
            "ZEROCLAW_NETWORK_MODE='shared' requires the proxy to run inside a "
            "Docker container on the shared Docker network"
        )

    from claw_proxy.admin.activity import DefaultActivityChecker
    from claw_proxy.admin.app import create_admin_app
    from claw_proxy.admin.audit import EncryptedJsonlAuditStore
    from claw_proxy.admin.auth import WebAuthnConfig
    from claw_proxy.admin.enrollment import bootstrap_if_empty
    from claw_proxy.admin.jobs import EncryptedJsonJobStore
    from claw_proxy.admin.schema_cache import SchemaCache
    from claw_proxy.admin.storage import EncryptedJsonAdminStore
    from claw_proxy.auth import get_current_user, verify_jwt
    from claw_proxy.containers.orchestrator import ContainerOrchestrator
    from claw_proxy.crypto import TokenCrypto
    from claw_proxy.db import ContainerRegistry
    from claw_proxy.files.upload import create_upload_router
    from claw_proxy.push import create_push_router
    from claw_proxy.tools.lifeatlas.router import create_lifeatlas_tools_router
    from claw_proxy.user_config import create_user_config_router
    from claw_proxy.ws.proxy import create_ws_app
    from claw_proxy.ws.shared_session import get_connections

    _registry = ContainerRegistry(ZEROCLAW_CONFIG["token_encryption_key"])

    _llm_env: dict[str, str] = {}
    if "llm_api_key" in ZEROCLAW_CONFIG:
        _llm_env["ZEROCLAW_LLM_API_KEY"] = ZEROCLAW_CONFIG["llm_api_key"]
        _llm_env["ZEROCLAW_LLM_PROVIDER"] = ZEROCLAW_CONFIG["llm_provider"]
        if ZEROCLAW_CONFIG.get("llm_model"):
            _llm_env["ZEROCLAW_LLM_MODEL"] = ZEROCLAW_CONFIG["llm_model"]

    _orchestrator = ContainerOrchestrator(
        registry=_registry,
        image=ZEROCLAW_CONFIG["zeroclaw_image"],
        data_dir=ZEROCLAW_CONFIG["zeroclaw_data_dir"],
        host_data_dir=ZEROCLAW_CONFIG.get(
            "zeroclaw_host_data_dir",
            ZEROCLAW_CONFIG["zeroclaw_data_dir"],
        ),
        llm_env=_llm_env,
        push_webhook_base_url=ZEROCLAW_CONFIG["push_webhook_base_url"],
        templates_dir=ZEROCLAW_CONFIG["templates_dir"],
        network_mode=ZEROCLAW_CONFIG["network_mode"],
        network_name=ZEROCLAW_CONFIG["network_name"],
    )

    async def _auth_fn(token: str) -> dict | None:
        try:
            return await verify_jwt(token)
        except Exception:
            return None

    _zc_app = create_ws_app(
        orchestrator=_orchestrator,
        auth_fn=_auth_fn,
        get_supabase_client=get_supabase_client,
    )
    _zc_app.include_router(
        create_push_router(
            token_map=_orchestrator.token_map,
            get_connections=get_connections,
            orchestrator=_orchestrator,
            get_supabase_client=get_supabase_client,
        )
    )
    _zc_app.include_router(
        create_upload_router(
            orchestrator=_orchestrator,
            auth_fn=get_current_user,
        )
    )
    _zc_app.include_router(
        create_lifeatlas_tools_router(
            token_map=_orchestrator.token_map,
            get_supabase_client=get_supabase_client,
            get_orchestrator=lambda: _orchestrator,
        )
    )
    app.mount("/zeroclaw", _zc_app)

    _admin_data_dir = ZEROCLAW_CONFIG["admin_data_dir"]
    os.makedirs(_admin_data_dir, exist_ok=True)
    os.makedirs(os.path.join(_admin_data_dir, "jobs"), exist_ok=True)

    _admin_crypto = TokenCrypto(ZEROCLAW_CONFIG["token_encryption_key"])
    _admin_store = EncryptedJsonAdminStore(
        os.path.join(_admin_data_dir, "admin.json.enc"),
        _admin_crypto,
    )
    _audit_store = EncryptedJsonlAuditStore(
        os.path.join(_admin_data_dir, "audit.jsonl.enc"),
        _admin_crypto,
    )
    _job_store = EncryptedJsonJobStore(
        os.path.join(_admin_data_dir, "jobs"),
        _admin_crypto,
    )
    _activity_checker = DefaultActivityChecker(
        registry=_registry,
        get_connections=get_connections,
        idle_threshold_seconds=60,
    )
    _schema_cache = SchemaCache(_orchestrator.docker)
    _webauthn_cfg = WebAuthnConfig(
        rp_id=os.environ.get("ADMIN_WEBAUTHN_RP_ID", "claw-admin.local"),
        rp_name=os.environ.get("ADMIN_WEBAUTHN_RP_NAME", "Claw admin"),
        origin=os.environ.get("ADMIN_WEBAUTHN_ORIGIN", "http://localhost:8000"),
    )

    _admin_app = create_admin_app(
        admin_store=_admin_store,
        audit_store=_audit_store,
        job_store=_job_store,
        webauthn_cfg=_webauthn_cfg,
        jwt_secret=os.environ.get("ADMIN_JWT_SECRET", "dev-admin-jwt-secret"),
        orchestrator=_orchestrator,
        docker_client=_orchestrator.docker,
        registry=_registry,
        activity_checker=_activity_checker,
        schema_cache=_schema_cache,
        supabase_client=get_supabase_client(),
    )
    app.mount("/claw-admin", _admin_app)

    _user_config_router = create_user_config_router(
        orchestrator=_orchestrator,
        docker_client=_orchestrator.docker,
        registry=_registry,
        schema_cache=_schema_cache,
        auth_fn=_auth_fn,
        allowlist_path=os.environ.get(
            "USER_CONFIG_ALLOWLIST_PATH", "data/user_config_allowlist.toml"
        ),
    )
    _zc_app.include_router(_user_config_router, prefix="/user/config")


async def _lifecycle_loop():
    """Background task: stop inactive containers, clean up deleted accounts."""
    assert ZEROCLAW_CONFIG is not None
    interval = ZEROCLAW_CONFIG["lifecycle_check_interval_minutes"] * 60
    supabase_client = get_supabase_client()
    export_max_age_hours = 24
    await asyncio.sleep(interval)  # first run after one interval
    while True:
        try:
            log.info("Lifecycle check running")
            try:
                await ensure_bucket_exists(supabase_client)
            except Exception as exc:
                log.warning(
                    "Bucket initialization failed during lifecycle pass: %s", exc
                )

            data_dir = ZEROCLAW_CONFIG["zeroclaw_data_dir"]
            host_data_dir = ZEROCLAW_CONFIG.get("zeroclaw_host_data_dir", data_dir)
            if os.path.isdir(data_dir) and _orchestrator is not None:
                for user_id in os.listdir(data_dir):
                    volume_path = os.path.join(data_dir, user_id)
                    host_volume_path = os.path.join(host_data_dir, user_id)
                    if not os.path.isdir(volume_path):
                        continue
                    await delete_expired_exports(
                        supabase_client,
                        user_id=user_id,
                        max_age_hours=export_max_age_hours,
                    )
                    expired_temp = await asyncio.to_thread(
                        find_expired_temp_uploads,
                        volume_path,
                        export_max_age_hours,
                    )
                    await asyncio.to_thread(
                        delete_files_via_busybox,
                        _orchestrator.docker,
                        host_volume_path,
                        expired_temp,
                    )
                    expired_lifeatlas = await asyncio.to_thread(
                        find_expired_lifeatlas_files,
                        volume_path,
                        export_max_age_hours,
                    )
                    await asyncio.to_thread(
                        delete_files_via_busybox,
                        _orchestrator.docker,
                        host_volume_path,
                        expired_lifeatlas,
                    )

            if _audit_store is not None:
                try:
                    removed = await _audit_store.prune_older_than(
                        days=int(os.environ.get("ADMIN_AUDIT_RETENTION_DAYS", "90"))
                    )
                    if removed:
                        log.info("Pruned %d audit entries", removed)
                except Exception as exc:
                    log.warning("Audit pruning failed: %s", exc)

            if _job_store is not None:
                try:
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(hours=24)
                    ).isoformat()
                    jobs = await _job_store.list_jobs()
                    for job in jobs:
                        if (
                            job.status in ("complete", "failed", "cancelled")
                            and (job.completed_at or "") < cutoff
                        ):
                            await _job_store.delete(job.id)
                except Exception as exc:
                    log.warning("Job cleanup failed: %s", exc)

            # TODO: query container_registry for inactive/deleted, call orchestrator.stop()
            # File cleanup now runs here too; inactive-container cleanup remains follow-up work.
        except Exception as e:
            log.error("Lifecycle check failed: %s", e)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
