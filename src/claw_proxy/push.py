"""Push webhook endpoint for ZeroClaw container callbacks."""

import json
import logging
from typing import Callable

from claw_proxy.files.downloads import process_download_tags
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def create_push_router(
    token_map: dict[str, str],
    get_connections: Callable[[], dict[str, set]],
    orchestrator=None,
    get_supabase_client: Callable | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/push")
    async def push_handler(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "missing bearer token"})
        token = auth[7:].strip()

        user_id = token_map.get(token)
        if not user_id:
            return JSONResponse(status_code=401, content={"error": "unknown token"})

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "invalid json"})

        content = body.get("content", "") if isinstance(body, dict) else ""
        if not content:
            return JSONResponse(status_code=400, content={"error": "missing content"})

        msg = {"type": "push.message", "content": content}
        supabase_client = get_supabase_client() if get_supabase_client else None
        if supabase_client is not None and orchestrator is not None:
            try:
                info = await orchestrator.get(user_id)
                if info is not None:
                    host_data_dir = getattr(
                        orchestrator,
                        "host_data_dir",
                        orchestrator.data_dir,
                    )
                    msg = await process_download_tags(
                        msg,
                        user_id=user_id,
                        container_id=info.container_id,
                        volume_path=f"{host_data_dir}/{user_id}",
                        docker_client=orchestrator.docker,
                        supabase_client=supabase_client,
                    )
            except Exception as exc:
                log.warning("Push export processing failed for %s: %s", user_id[:8], exc)
        if isinstance(body, dict) and body.get("subject"):
            msg["subject"] = body["subject"]

        connections = get_connections()
        ws_set = connections.get(user_id, set())
        delivered = 0

        for ws in list(ws_set):
            try:
                await ws.send_json(msg)
                delivered += 1
            except Exception:
                pass

        log.info("Push delivered to %d connections for user %s", delivered, user_id[:8])
        return JSONResponse(status_code=200, content={"delivered": delivered})

    return router
