"""Streams Supabase storage objects into container workspace volume."""

import asyncio
import logging
import re

from claw_proxy.containers.workspace import (
    relative_path_to_container_path,
    write_file_via_busybox,
)
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    MAX_FETCH_SIZE,
    StorageFetchError,
)

log = logging.getLogger(__name__)

_LIFEATLAS_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")
_DEST_BASENAME_PREFIX_RE = re.compile(r"[a-zA-Z0-9\-_]+")
_ALLOWED_DEST_SUBDIRS = {"files", "photos"}


def _sanitize_basename(name: str) -> str:
    safe_name = _LIFEATLAS_FILENAME_RE.sub("_", name).strip("._")
    return safe_name or "file"


def _validate_dest_subdir(dest_subdir: str) -> str:
    if dest_subdir not in _ALLOWED_DEST_SUBDIRS:
        raise ValueError("dest_subdir must be 'files' or 'photos'")
    return dest_subdir


def _validate_dest_basename_prefix(prefix: str) -> str:
    if not prefix or not _DEST_BASENAME_PREFIX_RE.fullmatch(prefix):
        raise ValueError("dest_basename_prefix must be a safe path component")
    return prefix


def _download_sync(supabase_client, bucket: str, storage_path: str) -> bytes:
    try:
        return supabase_client.storage.from_(bucket).download(storage_path)
    except Exception as exc:
        raise StorageFetchError(str(exc)) from exc


async def fetch_to_workspace(
    *,
    orchestrator,
    supabase,
    user_id,
    bucket,
    storage_path,
    dest_subdir,
    dest_basename_prefix,
    original_filename,
) -> dict:
    container = await orchestrator.get(user_id)
    if not container:
        raise ContainerNotProvisioned()

    safe_dest_subdir = _validate_dest_subdir(dest_subdir)
    safe_prefix = _validate_dest_basename_prefix(dest_basename_prefix)
    safe_name = _sanitize_basename(original_filename)
    relative_path = f"workspace/lifeatlas/{safe_dest_subdir}/{safe_prefix}_{safe_name}"
    container_path = relative_path_to_container_path(relative_path)
    agent_relative = relative_path[len("workspace/") :]

    raw = await asyncio.to_thread(_download_sync, supabase, bucket, storage_path)
    # Supabase storage downloads are materialized by the client. Upstream LifeAtlas
    # upload limits are expected to enforce size before storage; this guard keeps
    # oversized objects out of the workspace and away from agent access.
    if len(raw) > MAX_FETCH_SIZE:
        raise FetchTooLarge(len(raw))

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

    log.info(
        "Fetched LifeAtlas object from bucket %s to %s for user %s (%d bytes)",
        bucket,
        relative_path,
        user_id[:8],
        len(raw),
    )
    return {
        "workspace_path": agent_relative,
        "container_path": container_path,
        "filename": safe_name,
        "size_bytes": len(raw),
    }
