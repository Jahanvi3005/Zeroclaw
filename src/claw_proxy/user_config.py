"""User-facing config endpoint at /user/config."""

from __future__ import annotations

from typing import Any
import tomllib

from fastapi import APIRouter, Depends, Header, HTTPException

from claw_proxy.admin.operations import AdminOperations


def _load_allowlist(path: str) -> set[str]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return set(data.get("allowed_paths", []))


def create_user_config_router(
    *,
    orchestrator,
    docker_client,
    registry,
    schema_cache,
    auth_fn,
    allowlist_path: str,
) -> APIRouter:
    router = APIRouter()
    operations = AdminOperations(
        orchestrator=orchestrator,
        docker_client=docker_client,
        registry=registry,
        schema_cache=schema_cache,
    )
    allowlist = _load_allowlist(allowlist_path)

    async def _user_dep(authorization: str | None = Header(default=None)):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401)
        token = authorization.split(" ", 1)[1]
        user = await auth_fn(token)
        if not user:
            raise HTTPException(status_code=401)
        return user

    @router.get("")
    async def user_config_get(user=Depends(_user_dep)):
        row = await registry.get(user["id"])
        if not row:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "container_not_provisioned",
                    "hint": "Enable the agentic AI assistant first.",
                },
            )
        cfg = await operations.read_config(user["id"])
        return {key: value for key, value in cfg.items() if key in allowlist}

    @router.get("/schema")
    async def user_config_schema(user=Depends(_user_dep)):
        row = await registry.get(user["id"])
        if not row:
            raise HTTPException(status_code=409, detail={"error": "container_not_provisioned"})
        full = await schema_cache.get_schema(row["container_id"])
        props = full.get("properties", {})
        return {"properties": {key: value for key, value in props.items() if key in allowlist}}

    @router.put("")
    async def user_config_put(user=Depends(_user_dep), body: dict[str, Any] | None = None):
        row = await registry.get(user["id"])
        if not row:
            raise HTTPException(status_code=409, detail={"error": "container_not_provisioned"})

        body = body or {}
        updates = body.get("updates", [])
        results = []
        allowed_updates = []
        for update in updates:
            if update["path"] not in allowlist:
                results.append(
                    {
                        "path": update["path"],
                        "applied": False,
                        "rollback_available": False,
                        "error": "not in allowlist",
                    }
                )
            else:
                allowed_updates.append(update)
        if allowed_updates:
            written = await operations.write_config(user["id"], updates=allowed_updates)
            results.extend(written)
        return {"results": results}

    return router
