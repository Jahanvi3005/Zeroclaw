"""SaveHelper - agent-callable archive-to-LifeAtlas-library tool."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import posixpath
import stat as stat_module
import tarfile
from typing import Any

from claw_proxy.containers.workspace import (
    _run_busybox_script,
    container_path_to_relative_path,
    normalize_workspace_relative_path,
    relative_path_to_container_path,
)
from claw_proxy.files.storage import (
    HEALTH_DATA_ALLOWED_MIME,
    HEALTH_DATA_BUCKET,
    HealthDataDatabaseError,
    HealthDataStorageUploadError,
    UnsupportedHealthDataMime,
    get_signed_url,
    upload_to_health_data,
)
from claw_proxy.tools.lifeatlas.common import (
    MAX_FETCH_SIZE,
    resolve_active_profile_id,
)

log = logging.getLogger(__name__)


class WorkspaceReadError(Exception):
    """Workspace file could not be validated or read."""


class WorkspacePathInvalid(Exception):
    """Workspace path is missing, resolves outside workspace, or is not a file."""


class WorkspaceFileTooLarge(Exception):
    """Workspace file exceeds the fetch limit."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"file is {size_bytes} bytes; limit is {MAX_FETCH_SIZE}")
        self.size_bytes = size_bytes


_PATH_INVALID_EXIT_STATUSES = {20, 21, 22}
_FILE_TOO_LARGE_EXIT_STATUSES = {23}
_DOCKER_CP_TAR_OVERHEAD_BYTES = 1024 * 1024


def _failure(error_code: str, detail: str) -> dict[str, Any]:
    return {"success": False, "error_code": error_code, "detail": detail}


def _exception_status(exc: BaseException) -> int | None:
    for attr in ("exit_status", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


class _BoundedIterableReader:
    def __init__(self, chunks, limit: int):
        self._chunks = iter(chunks)
        self._limit = limit
        self._seen = 0
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            self._seen += len(chunk)
            if self._seen > self._limit:
                raise WorkspaceFileTooLarge(self._seen)
            self._buffer.extend(chunk)
        if size < 0:
            size = len(self._buffer)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


def _read_tar_member_bounded(handle, member_size: int, max_size: int) -> bytes:
    if member_size > max_size:
        raise WorkspaceFileTooLarge(member_size)

    data = bytearray()
    while True:
        chunk = handle.read(min(64 * 1024, max_size + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_size:
            raise WorkspaceFileTooLarge(len(data))
    return bytes(data)


def _read_file_via_docker_cp_bounded_sync(
    docker_client,
    container_id: str,
    container_path: str,
    max_size: int,
) -> bytes:
    """Read a docker cp tar stream without materializing the full archive."""
    container = docker_client.containers.get(container_id)
    stream, stat = container.get_archive(container_path)
    if isinstance(stat, dict):
        size = stat.get("size")
        if isinstance(size, int) and size > max_size:
            raise WorkspaceFileTooLarge(size)
        mode = stat.get("mode")
        if isinstance(mode, int) and not stat_module.S_ISREG(mode):
            raise WorkspaceReadError("docker archive target is not a regular file")

    archive_limit = max_size + _DOCKER_CP_TAR_OVERHEAD_BYTES
    reader = _BoundedIterableReader(stream, archive_limit)
    with tarfile.open(fileobj=reader, mode="r|*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            return _read_tar_member_bounded(handle, member.size, max_size)
    raise FileNotFoundError(container_path)


def _workspace_file_size_via_busybox(
    docker_client,
    volume_path: str,
    relative_path: str,
) -> int:
    """Resolve and validate a workspace file, returning its byte size."""
    normalized = normalize_workspace_relative_path(relative_path)
    try:
        output = _run_busybox_script(
            docker_client,
            volume_path=volume_path,
            mode="ro",
            script="""
root=/vol/workspace
resolved=$(readlink -f -- "$1") || { echo "target does not exist" >&2; exit 20; }
case "$resolved" in
  "$root"/*) ;;
  *) echo "target must stay under workspace" >&2; exit 22 ;;
esac
[ -f "$resolved" ] || { echo "target is not a regular file" >&2; exit 21; }
wc -c < "$resolved"
""",
            args=[f"/vol/{normalized}"],
        )
    except Exception as exc:
        if _exception_status(exc) in _PATH_INVALID_EXIT_STATUSES:
            raise WorkspacePathInvalid("workspace path is invalid") from exc
        raise WorkspaceReadError("could not inspect workspace file") from exc

    try:
        return int(output.decode("utf-8").strip())
    except (AttributeError, UnicodeDecodeError, ValueError) as exc:
        raise WorkspaceReadError("could not inspect workspace file") from exc


def _read_file_via_checked_busybox(
    docker_client,
    volume_path: str,
    relative_path: str,
    max_size: int,
) -> bytes:
    """Revalidate the resolved target and read it through busybox."""
    normalized = normalize_workspace_relative_path(relative_path)
    try:
        output = _run_busybox_script(
            docker_client,
            volume_path=volume_path,
            mode="ro",
            script="""
root=/vol/workspace
resolved=$(readlink -f -- "$1") || { echo "target does not exist" >&2; exit 20; }
case "$resolved" in
  "$root"/*) ;;
  *) echo "target must stay under workspace" >&2; exit 22 ;;
esac
[ -f "$resolved" ] || { echo "target is not a regular file" >&2; exit 21; }
size=$(wc -c < "$resolved")
[ "$size" -le "$3" ] || { echo "target exceeds size limit" >&2; exit 23; }
head -c "$2" "$resolved"
""",
            args=[f"/vol/{normalized}", str(max_size + 1), str(max_size)],
        )
    except Exception as exc:
        status = _exception_status(exc)
        if status in _PATH_INVALID_EXIT_STATUSES:
            raise WorkspacePathInvalid("workspace path is invalid") from exc
        if status in _FILE_TOO_LARGE_EXIT_STATUSES:
            raise WorkspaceFileTooLarge(max_size + 1) from exc
        raise WorkspaceReadError("could not read workspace file") from exc
    if len(output) > max_size:
        raise WorkspaceFileTooLarge(len(output))
    return output


async def _read_workspace_file(
    orchestrator,
    container,
    user_id: str,
    relative_path: str,
) -> bytes:
    """Read a workspace file via docker cp first, busybox fallback."""
    normalized = normalize_workspace_relative_path(relative_path)
    container_path = relative_path_to_container_path(normalized)
    volume_path = f"{orchestrator.data_dir}/{user_id}"
    size_bytes = await asyncio.to_thread(
        _workspace_file_size_via_busybox,
        orchestrator.docker,
        volume_path,
        normalized,
    )
    if size_bytes > MAX_FETCH_SIZE:
        raise WorkspaceFileTooLarge(size_bytes)

    try:
        file_bytes = await asyncio.to_thread(
            _read_file_via_docker_cp_bounded_sync,
            orchestrator.docker,
            container.container_id,
            container_path,
            MAX_FETCH_SIZE,
        )
    except WorkspaceFileTooLarge:
        raise
    except Exception:
        file_bytes = await asyncio.to_thread(
            _read_file_via_checked_busybox,
            orchestrator.docker,
            volume_path,
            normalized,
            MAX_FETCH_SIZE,
        )
    if len(file_bytes) > MAX_FETCH_SIZE:
        raise WorkspaceFileTooLarge(len(file_bytes))
    return file_bytes


def _file_too_large_failure(size_bytes: int) -> dict[str, Any]:
    return _failure(
        "file_too_large",
        f"File is {size_bytes} bytes; limit is {MAX_FETCH_SIZE}",
    )


class SaveHelper:
    def __init__(self, supabase, orchestrator):
        self.db = supabase
        self.orch = orchestrator

    async def save_to_library(
        self,
        user_id: str,
        workspace_path: str | None,
    ) -> dict[str, Any]:
        if not workspace_path:
            return _failure("path_invalid", "workspace_path is required")

        try:
            if workspace_path.startswith("/"):
                relative = container_path_to_relative_path(workspace_path)
            else:
                candidate = (
                    workspace_path
                    if workspace_path.startswith("workspace/")
                    or workspace_path == "workspace"
                    else f"workspace/{workspace_path}"
                )
                relative = normalize_workspace_relative_path(candidate)
        except ValueError as exc:
            return _failure("path_invalid", str(exc))

        container = await self.orch.get(user_id)
        if not container:
            return _failure("container_unavailable", "Container not provisioned")

        try:
            file_bytes = await _read_workspace_file(
                self.orch,
                container,
                user_id,
                relative,
            )
        except WorkspaceFileTooLarge as exc:
            return _file_too_large_failure(exc.size_bytes)
        except WorkspacePathInvalid:
            return _failure(
                "path_invalid",
                "Workspace path is missing or unavailable",
            )
        except Exception as exc:
            log.warning(
                "save read failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            return _failure("container_unavailable", "Could not read workspace file")

        if len(file_bytes) > MAX_FETCH_SIZE:
            return _file_too_large_failure(len(file_bytes))

        filename = posixpath.basename(relative)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type not in HEALTH_DATA_ALLOWED_MIME:
            return _failure(
                "unsupported_mime",
                f"Only PDF, CSV, PNG, and JPEG can be saved. Got {content_type}.",
            )

        try:
            profile_id = await asyncio.to_thread(
                resolve_active_profile_id,
                self.db,
                user_id,
            )
        except ValueError:
            return _failure(
                "no_active_profile",
                "No active LifeAtlas profile is selected",
            )
        except Exception as exc:
            log.warning(
                "save profile resolution failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            return _failure(
                "db_unavailable",
                "Could not resolve active LifeAtlas profile",
            )

        try:
            path, file_id = await upload_to_health_data(
                self.db,
                user_id=user_id,
                profile_id=profile_id,
                file_bytes=file_bytes,
                filename=filename,
                content_type=content_type,
            )
        except UnsupportedHealthDataMime as exc:
            return _failure("unsupported_mime", str(exc))
        except HealthDataDatabaseError as exc:
            log.warning(
                "save metadata insert failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            return _failure(
                "db_unavailable",
                "Could not save file metadata to LifeAtlas",
            )
        except HealthDataStorageUploadError as exc:
            log.warning(
                "save upload failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            return _failure(
                "storage_unavailable",
                "Could not save file to LifeAtlas storage",
            )
        except Exception as exc:
            log.warning(
                "save upload failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            return _failure(
                "storage_unavailable",
                "Could not save file to LifeAtlas storage",
            )

        try:
            signed = await get_signed_url(self.db, path, bucket=HEALTH_DATA_BUCKET)
        except Exception as exc:
            log.warning(
                "save signed-url failed user=%s: %s",
                user_id[:8],
                exc,
                exc_info=True,
            )
            signed = ""

        log.info(
            "save success user=%s profile=%s content_type=%s size=%d",
            user_id[:8],
            profile_id[:8],
            content_type,
            len(file_bytes),
        )
        return {
            "success": True,
            "file_id": file_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(file_bytes),
            "signed_url": signed,
            "signed_url_expires_in": 3600,
            "markdown_link": f"[{filename}]({signed})" if signed else f"`{filename}`",
        }
