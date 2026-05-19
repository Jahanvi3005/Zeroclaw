"""REST endpoint for uploading files to a user's ZeroClaw workspace."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from claw_proxy.containers.workspace import (
    relative_path_to_container_path,
    sanitize_upload_basename,
    write_file_via_busybox,
)

log = logging.getLogger(__name__)

MAX_FILE_SIZE = 25 * 1024 * 1024
ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/markdown",
    "text/csv",
}


def _annotation_tag(content_type: str, container_path: str) -> str:
    if content_type.startswith("image/"):
        return f"[IMAGE:{container_path}]"
    if content_type == "application/pdf":
        return f"[PDF:{container_path}]"
    return f"[FILE:{container_path}]"


def create_upload_router(orchestrator, auth_fn) -> APIRouter:
    router = APIRouter()

    @router.post("/workspace/files")
    async def upload_file(file: UploadFile, user: dict = Depends(auth_fn)):
        content_type = file.content_type or "application/octet-stream"
        if content_type not in ALLOWED_MIME_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}")

        try:
            safe_name = sanitize_upload_basename(file.filename or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        raw = await file.read()
        if len(raw) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large")

        user_id = user["id"]
        container_info = await orchestrator.get(user_id)
        if not container_info:
            raise HTTPException(status_code=404, detail="No container provisioned")

        relative_path = f"workspace/temp/{int(time.time() * 1000)}_{safe_name}"
        container_path = relative_path_to_container_path(relative_path)

        try:
            local_volume_path = f"{orchestrator.data_dir}/{user_id}"
            host_volume_path = (
                f"{getattr(orchestrator, 'host_data_dir', orchestrator.data_dir)}/{user_id}"
            )
            await asyncio.to_thread(
                write_file_via_busybox,
                orchestrator.docker,
                host_volume_path,
                relative_path,
                raw,
                local_volume_path=local_volume_path,
            )
        except Exception as exc:
            log.error("File write failed for user %s: %s", user_id[:8], exc)
            raise HTTPException(status_code=502, detail="Failed to write file") from exc

        return {
            "filename": safe_name,
            "path": container_path,
            "annotation": _annotation_tag(content_type, container_path),
        }

    return router
