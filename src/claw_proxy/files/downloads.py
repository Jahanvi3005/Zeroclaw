"""Shared [DOWNLOAD:] export processing."""

import asyncio
import io
import logging
import mimetypes
import re
import tarfile

from claw_proxy.files.storage import get_signed_url, upload_file
from claw_proxy.containers.workspace import container_path_to_relative_path

log = logging.getLogger(__name__)

DOWNLOAD_TAG_RE = re.compile(r"\[DOWNLOAD:([^\]]+)\]")
EXPORT_TIMEOUT_SECONDS = 15


def _fields_for_message(msg: dict) -> list[str]:
    if msg.get("type") == "chat.done" and isinstance(msg.get("fullResponse"), str):
        return ["fullResponse"]
    if msg.get("type") == "push.message" and isinstance(msg.get("content"), str):
        return ["content"]
    return []


def _guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename, strict=False)[0] or "application/octet-stream"


def _read_file_via_docker_cp_sync(docker_client, container_id: str, container_path: str) -> bytes:
    container = docker_client.containers.get(container_id)
    stream, _ = container.get_archive(container_path)
    archive = b"".join(stream)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            return handle.read()
    raise FileNotFoundError(container_path)


def _read_file_with_fallback_sync(
    docker_client,
    container_id: str,
    container_path: str,
    volume_path: str,
) -> bytes:
    from claw_proxy.containers.workspace import read_file_via_busybox, relative_path_to_container_path

    relative_path = container_path_to_relative_path(container_path)
    normalized_container_path = relative_path_to_container_path(relative_path)
    try:
        return _read_file_via_docker_cp_sync(docker_client, container_id, normalized_container_path)
    except Exception:
        return read_file_via_busybox(docker_client, volume_path, relative_path)


async def _materialize_signed_url(
    *,
    container_path: str,
    user_id: str,
    container_id: str,
    volume_path: str,
    docker_client,
    supabase_client,
) -> str:
    relative_path = container_path_to_relative_path(container_path)
    file_bytes = await asyncio.to_thread(
        _read_file_with_fallback_sync,
        docker_client,
        container_id,
        container_path,
        volume_path,
    )
    filename = relative_path.rsplit("/", 1)[-1]
    storage_path = await upload_file(
        supabase_client,
        user_id=user_id,
        file_bytes=file_bytes,
        filename=filename,
        content_type=_guess_content_type(filename),
        permanent=False,
    )
    return await get_signed_url(supabase_client, storage_path)


async def process_download_tags(
    msg: dict,
    *,
    user_id: str,
    container_id: str,
    volume_path: str,
    docker_client,
    supabase_client,
) -> dict:
    fields = _fields_for_message(msg)
    if not fields:
        return msg

    result = dict(msg)
    for field in fields:
        text = result[field]
        for match in reversed(list(DOWNLOAD_TAG_RE.finditer(text))):
            container_path = match.group(1)
            try:
                signed = await asyncio.wait_for(
                    _materialize_signed_url(
                        container_path=container_path,
                        user_id=user_id,
                        container_id=container_id,
                        volume_path=volume_path,
                        docker_client=docker_client,
                        supabase_client=supabase_client,
                    ),
                    timeout=EXPORT_TIMEOUT_SECONDS,
                )
                filename = container_path.rsplit("/", 1)[-1]
                markdown_link = f"[{filename}]({signed})"
                text = text[:match.start()] + markdown_link + text[match.end():]
            except Exception as exc:
                log.warning(
                    "Export processing failed for %s path=%r: %s",
                    user_id[:8],
                    container_path,
                    exc,
                )
                continue
        result[field] = text
    return result


async def process_transcript_download_tags(
    messages: list[dict],
    *,
    user_id: str,
    container_id: str,
    volume_path: str,
    docker_client,
    supabase_client,
) -> list[dict]:
    """Rewrite export tags in persisted transcript message content."""
    processed: list[dict] = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            processed.append(message)
            continue

        rewritten = await process_download_tags(
            {"type": "push.message", "content": message["content"]},
            user_id=user_id,
            container_id=container_id,
            volume_path=volume_path,
            docker_client=docker_client,
            supabase_client=supabase_client,
        )
        if rewritten.get("content") == message["content"]:
            processed.append(message)
            continue

        updated = dict(message)
        updated["content"] = rewritten["content"]
        processed.append(updated)
    return processed
