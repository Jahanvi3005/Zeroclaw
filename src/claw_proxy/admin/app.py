"""Admin FastAPI app factory."""

from __future__ import annotations

from fastapi import FastAPI

from claw_proxy.admin.jobs import JobRunner
from claw_proxy.admin.operations import AdminOperations
from claw_proxy.admin.routes import create_router


def create_admin_app(
    *,
    admin_store,
    audit_store,
    job_store,
    webauthn_cfg,
    jwt_secret,
    orchestrator,
    docker_client,
    registry,
    activity_checker,
    schema_cache,
    supabase_client,
) -> FastAPI:
    app = FastAPI(title="Claw admin")
    operations = AdminOperations(
        orchestrator=orchestrator,
        docker_client=docker_client,
        registry=registry,
        activity_checker=activity_checker,
        schema_cache=schema_cache,
    )
    router = create_router(
        admin_store=admin_store,
        audit_store=audit_store,
        job_store=job_store,
        webauthn_cfg=webauthn_cfg,
        jwt_secret=jwt_secret,
        operations=operations,
        resolver_deps={
            "registry": registry,
            "docker_client": docker_client,
            "supabase_client": supabase_client,
        },
        activity_checker=activity_checker,
        schema_cache=schema_cache,
        job_runner=JobRunner(store=job_store),
    )
    app.include_router(router)
    return app
