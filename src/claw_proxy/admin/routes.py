"""Admin REST routes."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from claw_proxy.admin.audit import AuditContext, emit_audit
from claw_proxy.admin.auth import (
    AdminContext,
    generate_static_token,
    hash_static_token,
    issue_session_jwt,
    require_admin,
    start_login,
)
from claw_proxy.admin.enrollment import generate_invite_token
from claw_proxy.admin.jobs import Job, JobTarget
from claw_proxy.admin.storage import StaticToken
from claw_proxy.admin import update_stubs
from claw_proxy.admin.resolver import AmbiguousLookupError, NotFoundError, resolve_user

log = logging.getLogger(__name__)


class ConfigUpdate(BaseModel):
    path: str
    value: object


class ConfigPatchBody(BaseModel):
    updates: list[ConfigUpdate]
    careful: bool = False
    careful_timeout_seconds: int = 300


class BatchConfigSetBody(BaseModel):
    targets: list[str]
    updates: list[ConfigUpdate]
    careful: bool = False
    careful_timeout_seconds: int = 300


def create_router(
    *,
    admin_store,
    audit_store,
    job_store,
    webauthn_cfg,
    jwt_secret,
    operations,
    resolver_deps,
    activity_checker,
    schema_cache,
    job_runner,
) -> APIRouter:
    router = APIRouter()
    challenges: dict[str, bytes] = {}

    async def _admin_dep(authorization: str | None = Header(default=None)):
        return await require_admin(
            authorization=authorization,
            store=admin_store,
            jwt_secret=jwt_secret,
        )

    def _audit_ctx(request: Request, admin: AdminContext) -> AuditContext:
        client_ip = request.client.host if request.client else None
        return AuditContext(admin=admin, client_ip=client_ip)

    @router.post("/auth/login")
    async def auth_login():
        options_json, challenge = await start_login(store=admin_store, cfg=webauthn_cfg)
        challenges["login"] = challenge
        return {"options": options_json}

    @router.post("/auth/register")
    async def auth_register(request: Request):
        body = await request.json()
        enrollment_token = body.get("enrollment_token", "")
        token_hash = hashlib.sha256(enrollment_token.encode()).hexdigest()
        entry = await admin_store.find_enrollment_token(token_hash)
        if entry is None or entry.consumed:
            raise HTTPException(status_code=403, detail="enrollment token invalid")
        if entry.expires_at < datetime.now(timezone.utc).isoformat():
            raise HTTPException(status_code=403, detail="enrollment token expired")
        return {"status": "pending"}

    @router.post("/auth/logout")
    async def auth_logout(admin: AdminContext = Depends(_admin_dep)):
        return {"status": "ok"}

    @router.get("/users")
    async def users_resolve(
        lookup: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        try:
            result = await resolve_user(lookup, **resolver_deps)
        except AmbiguousLookupError as exc:
            raise HTTPException(
                status_code=409,
                detail={"candidates": [candidate.__dict__ for candidate in exc.candidates]},
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return result.__dict__

    @router.get("/containers")
    async def containers_list(
        status: str | None = None,
        registry_health: str | None = None,
        email: str | None = None,
        email_contains: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        user_id: str | None = None,
        container_id: str | None = None,
        container_name: str | None = None,
        inactive_for_days: int | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "last_active_at",
        order: str = "desc",
        admin: AdminContext = Depends(_admin_dep),
    ):
        items = await operations.list_containers(
            status=status,
            registry_health=registry_health,
            email=email,
            email_contains=email_contains,
            first_name=first_name,
            last_name=last_name,
            user_id=user_id,
            container_id=container_id,
            container_name=container_name,
            inactive_for_days=inactive_for_days,
            limit=limit,
            offset=offset,
            sort=sort,
            order=order,
        )
        return {"items": items, "limit": limit, "offset": offset}

    @router.get("/containers/{user_id}")
    async def container_get(
        user_id: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        info = await operations.get_container(user_id)
        if not info:
            raise HTTPException(status_code=404, detail="no container for user")
        return info

    @router.post("/containers/{user_id}/start")
    async def container_start(
        user_id: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        result = await operations.start_container(user_id)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="container.start",
            target={"type": "user", "user_id": user_id},
            params={},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.post("/containers/{user_id}/stop")
    async def container_stop(
        user_id: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        careful = bool(body.get("careful", False))
        careful_timeout_seconds = int(body.get("careful_timeout_seconds", 300))
        result = await operations.stop_container(
            user_id,
            careful=careful,
            careful_timeout_seconds=careful_timeout_seconds,
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="container.stop",
            target={"type": "user", "user_id": user_id},
            params={"careful": careful, "careful_timeout_seconds": careful_timeout_seconds},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.post("/containers/{user_id}/restart")
    async def container_restart(
        user_id: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        careful = bool(body.get("careful", False))
        careful_timeout_seconds = int(body.get("careful_timeout_seconds", 300))
        result = await operations.restart_container(
            user_id,
            careful=careful,
            careful_timeout_seconds=careful_timeout_seconds,
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="container.restart",
            target={"type": "user", "user_id": user_id},
            params={"careful": careful, "careful_timeout_seconds": careful_timeout_seconds},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.get("/containers/{user_id}/config")
    async def config_get(
        user_id: str,
        section: str | None = None,
        admin: AdminContext = Depends(_admin_dep),
    ):
        return await operations.read_config(user_id, section_prefix=section)

    @router.get("/containers/{user_id}/config/schema")
    async def config_schema(
        user_id: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        info = await operations.get_container(user_id)
        if not info:
            raise HTTPException(status_code=404, detail="no container for user")
        return await schema_cache.get_schema(info["container_id"])

    @router.patch("/containers/{user_id}/config")
    async def config_set(
        user_id: str,
        body: ConfigPatchBody,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        result = await operations.write_config(
            user_id,
            updates=[update.model_dump() for update in body.updates],
            careful=body.careful,
            careful_timeout_seconds=body.careful_timeout_seconds,
        )
        info = await operations.get_container(user_id)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="config.set",
            target={"type": "user", "user_id": user_id},
            params={"updates": [update.model_dump() for update in body.updates]},
            result="success",
            duration_ms=0,
            container_id=info["container_id"] if info else None,
            schema_cache=schema_cache,
        )
        return {"results": result}

    @router.get("/containers/{user_id}/workspace/files")
    async def ws_list(
        user_id: str,
        path: str = "",
        recursive: bool = False,
        admin: AdminContext = Depends(_admin_dep),
    ):
        return {
            "items": await operations.list_workspace_files(
                user_id, path=path, recursive=recursive
            )
        }

    @router.get("/containers/{user_id}/workspace/files/{file_path:path}")
    async def ws_read(
        user_id: str,
        file_path: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        try:
            return await operations.read_workspace_file(user_id, path=file_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @router.put("/containers/{user_id}/workspace/files/{file_path:path}")
    async def ws_write(
        user_id: str,
        file_path: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        content = body.get("content", "")
        mode = body.get("mode", "overwrite")
        encoding = body.get("encoding", "text")
        result = await operations.write_workspace_file(
            user_id,
            path=file_path,
            content=content,
            mode=mode,
            encoding=encoding,
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="workspace.write",
            target={"type": "user", "user_id": user_id},
            params={"path": file_path, "content": content, "mode": mode},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.patch("/containers/{user_id}/workspace/files/{file_path:path}")
    async def ws_patch(
        user_id: str,
        file_path: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        patch_operations = body.get("operations", [])
        result = await operations.patch_workspace_file(
            user_id,
            path=file_path,
            operations=patch_operations,
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="workspace.patch",
            target={"type": "user", "user_id": user_id},
            params={"path": file_path, "operations": patch_operations},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.delete("/containers/{user_id}/workspace/files/{file_path:path}")
    async def ws_delete(
        user_id: str,
        file_path: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        result = await operations.delete_workspace_file(user_id, path=file_path)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="workspace.delete",
            target={"type": "user", "user_id": user_id},
            params={"path": file_path},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.get("/orphans")
    async def orphans_list(admin: AdminContext = Depends(_admin_dep)):
        return {"items": await operations.list_orphans()}

    @router.delete("/orphans/{container_id}")
    async def orphan_remove(
        container_id: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        result = await operations.remove_orphan(container_id)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="orphan.remove",
            target={"type": "container", "container_id": container_id},
            params={},
            result=result["status"],
            duration_ms=0,
        )
        return result

    @router.post("/skills/retrofit")
    async def skills_retrofit(
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        result = await operations.retrofit_skills()
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="skills.retrofit",
            target={"type": "system"},
            params={},
            result="success",
            duration_ms=0,
        )
        return result

    @router.get("/admins")
    async def admins_list(admin: AdminContext = Depends(_admin_dep)):
        items = [
            {
                "id": item.id,
                "name": item.name,
                "credentials": len(item.webauthn_credentials),
                "created_at": item.created_at,
            }
            for item in await admin_store.list_admins()
        ]
        return {"items": items}

    @router.post("/admins/invite")
    async def admins_invite(
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        token = await generate_invite_token(
            admin_store,
            ttl_minutes=15,
            issued_by=admin.admin_id,
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="admin.invite",
            target={"type": "system"},
            params={},
            result="success",
            duration_ms=0,
        )
        return {"enrollment_token": token, "expires_in_minutes": 15}

    @router.delete("/admins/{admin_id}")
    async def admins_revoke(
        admin_id: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        await admin_store.remove_admin(admin_id)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="admin.revoke",
            target={"type": "admin", "admin_id": admin_id},
            params={},
            result="success",
            duration_ms=0,
        )
        return {"status": "removed"}

    @router.get("/admins/static-tokens")
    async def admins_tokens_list(admin: AdminContext = Depends(_admin_dep)):
        items = [
            {
                "label": token.label,
                "created_at": token.created_at,
                "last_used_at": token.last_used_at,
            }
            for token in await admin_store.list_static_tokens()
        ]
        return {"items": items}

    @router.post("/admins/static-tokens")
    async def admins_tokens_create(
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        label = body.get("label", "")
        token = generate_static_token()
        await admin_store.add_static_token(
            StaticToken(
                hash=hash_static_token(token),
                label=label,
                created_at=datetime.now(timezone.utc).isoformat(),
                last_used_at=None,
            )
        )
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="admin.token_create",
            target={"type": "system"},
            params={"label": label},
            result="success",
            duration_ms=0,
        )
        return {"token": token, "label": label}

    @router.delete("/admins/static-tokens/{label}")
    async def admins_tokens_delete(
        label: str,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        await admin_store.delete_static_token(label)
        await emit_audit(
            store=audit_store,
            ctx=_audit_ctx(request, admin),
            action="admin.token_revoke",
            target={"type": "system"},
            params={"label": label},
            result="success",
            duration_ms=0,
        )
        return {"status": "revoked"}

    @router.get("/audit")
    async def audit_query(
        since: str | None = None,
        until: str | None = None,
        admin_id: str | None = None,
        action: str | None = None,
        target_user_id: str | None = None,
        job_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        _admin: AdminContext = Depends(_admin_dep),
    ):
        rows = await audit_store.query(
            since=since,
            until=until,
            admin_id=admin_id,
            action=action,
            target_user_id=target_user_id,
            job_id=job_id,
            limit=limit,
            offset=offset,
        )
        return {"items": [row.__dict__ for row in rows], "limit": limit, "offset": offset}

    @router.post("/batch/restart", status_code=202)
    async def batch_restart(
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        body = await request.json()
        targets = body.get("targets", [])
        careful = bool(body.get("careful", False))
        careful_timeout_seconds = int(body.get("careful_timeout_seconds", 300))
        inter_target_delay_seconds = float(body.get("inter_target_delay_seconds", 1.0))
        job_id = f"j_{__import__('secrets').token_hex(6)}"
        now = datetime.now(timezone.utc).isoformat()
        job = Job(
            id=job_id,
            operation="restart",
            status="created",
            created_at=now,
            started_at=None,
            completed_at=None,
            params={
                "targets": targets,
                "careful": careful,
                "careful_timeout_seconds": careful_timeout_seconds,
                "inter_target_delay_seconds": inter_target_delay_seconds,
            },
            targets=[JobTarget(user_id=user_id, status="pending") for user_id in targets],
            summary={
                "total": len(targets),
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "pending": len(targets),
            },
        )
        await job_store.put(job)

        async def per_target(user_id: str, params: dict):
            return await operations.restart_container(
                user_id,
                careful=params["careful"],
                careful_timeout_seconds=params["careful_timeout_seconds"],
            )

        _ = job_runner  # keep explicit reference for readability
        import asyncio as _asyncio

        _asyncio.create_task(job_runner.run(job_id, per_target=per_target))
        return {"job_id": job_id, "status_url": f"/claw-admin/jobs/{job_id}"}

    @router.get("/jobs")
    async def jobs_list(
        status: str | None = None,
        operation: str | None = None,
        admin: AdminContext = Depends(_admin_dep),
    ):
        rows = await job_store.list_jobs(status=status, operation=operation)
        return {
            "items": [
                {
                    "id": job.id,
                    "operation": job.operation,
                    "status": job.status,
                    "created_at": job.created_at,
                    "summary": job.summary,
                }
                for job in rows
            ]
        }

    @router.post("/batch/config.set", status_code=202)
    async def batch_config_set(
        body: BatchConfigSetBody,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        import asyncio as _asyncio
        import secrets as _secrets

        job_id = f"j_{_secrets.token_hex(6)}"
        now = datetime.now(timezone.utc).isoformat()
        params = body.model_dump()
        job = Job(
            id=job_id,
            operation="config.set",
            status="created",
            created_at=now,
            started_at=None,
            completed_at=None,
            params=params,
            targets=[JobTarget(user_id=user_id, status="pending") for user_id in body.targets],
            summary={
                "total": len(body.targets),
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "pending": len(body.targets),
            },
        )
        await job_store.put(job)

        async def per_target(user_id: str, params: dict):
            results = await operations.write_config(
                user_id,
                updates=params["updates"],
                careful=params["careful"],
                careful_timeout_seconds=params["careful_timeout_seconds"],
            )
            ok = all(r.get("applied") for r in results)
            return {"status": "success" if ok else "failed", "results": results}

        _asyncio.create_task(job_runner.run(job_id, per_target=per_target))
        return {"job_id": job_id, "status_url": f"/claw-admin/jobs/{job_id}"}

    @router.get("/jobs/{job_id}")
    async def jobs_get(
        job_id: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        job = await job_store.get(job_id)
        if not job:
            raise HTTPException(status_code=404)
        return {**job.__dict__, "targets": [t.__dict__ for t in job.targets]}

    @router.delete("/jobs/{job_id}")
    async def jobs_cancel(
        job_id: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        job_runner.cancel(job_id)
        return {"status": "cancellation_requested"}

    @router.post("/containers/{user_id}/update", status_code=501)
    async def container_update(
        user_id: str,
        body: update_stubs.UpdateBody,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        return JSONResponse(status_code=501, content=update_stubs.UPDATE_NOT_IMPLEMENTED)

    @router.get("/containers/{user_id}/update/status", status_code=501)
    async def container_update_status(
        user_id: str,
        admin: AdminContext = Depends(_admin_dep),
    ):
        return JSONResponse(status_code=501, content=update_stubs.UPDATE_NOT_IMPLEMENTED)

    @router.post("/batch/update", status_code=501)
    async def batch_update(
        body: update_stubs.BatchUpdateBody,
        request: Request,
        admin: AdminContext = Depends(_admin_dep),
    ):
        return JSONResponse(status_code=501, content=update_stubs.UPDATE_NOT_IMPLEMENTED)

    return router
