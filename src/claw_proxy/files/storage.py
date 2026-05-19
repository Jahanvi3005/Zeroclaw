"""Supabase Storage helpers wrapped for async callers."""

import asyncio
import logging
import re
import time
from urllib.parse import urlsplit, urlunsplit

from claw_proxy.config import SUPABASE_PUBLIC_URL

log = logging.getLogger(__name__)

BUCKET_NAME = "workspace_files"
HEALTH_DATA_BUCKET = "health_data"
HEALTH_DATA_ALLOWED_MIME = frozenset(
    [
        "application/pdf",
        "text/csv",
        "image/png",
        "image/jpeg",
        "image/jpg",
    ]
)
_HEALTH_DATA_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")


class UnsupportedHealthDataMime(ValueError):
    """Content type not in the website-supported allowlist."""


class HealthDataStorageUploadError(RuntimeError):
    """Health data bucket upload failed."""


class HealthDataDatabaseError(RuntimeError):
    """health_data_files insert failed after upload rollback."""


def _sanitize_health_data_filename(name: str) -> str:
    cleaned = _HEALTH_DATA_FILENAME_RE.sub("_", name).strip("._")
    return cleaned or "file"


def _bucket_name(bucket) -> str | None:
    if isinstance(bucket, dict):
        return bucket.get("name")
    return getattr(bucket, "name", None)


def _ensure_bucket_exists_sync(supabase_client) -> None:
    buckets = supabase_client.storage.list_buckets()
    if any(_bucket_name(bucket) == BUCKET_NAME for bucket in buckets):
        return
    supabase_client.storage.create_bucket(BUCKET_NAME, options={"public": False})


def _upload_file_sync(
    supabase_client,
    *,
    user_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    permanent: bool,
) -> str:
    _ensure_bucket_exists_sync(supabase_client)
    if permanent:
        path = f"{user_id}/permanent/{filename}"
    else:
        path = f"{user_id}/exports/{int(time.time() * 1000)}_{filename}"

    bucket = supabase_client.storage.from_(BUCKET_NAME)
    file_options = {"content-type": content_type}
    if permanent:
        file_options["upsert"] = "true"
    bucket.upload(path, file_bytes, file_options)
    return path


def _upload_to_health_data_sync(
    supabase_admin,
    *,
    user_id: str,
    profile_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> tuple[str, str]:
    """Mirrors supabase/functions/upload-file/index.ts in lifeatlas-core.

    See docs/superpowers/specs/2026-05-04-lifeatlas-file-access-design.md.
    Drift surface: keep this in sync with the edge function.
    """
    if content_type not in HEALTH_DATA_ALLOWED_MIME:
        raise UnsupportedHealthDataMime(content_type)

    timestamp = int(time.time() * 1000)
    safe_name = _sanitize_health_data_filename(filename)
    path = f"{profile_id}/{timestamp}_{safe_name}"

    try:
        bucket = supabase_admin.storage.from_(HEALTH_DATA_BUCKET)
        bucket.upload(
            path,
            file_bytes,
            {
                "content-type": content_type,
                "upsert": "false",
            },
        )
    except Exception as exc:
        raise HealthDataStorageUploadError("health data storage upload failed") from exc

    try:
        inserted = (
            supabase_admin.table("health_data_files")
            .insert(
                {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "filename": filename,
                    "file_path": path,
                    "file_size": len(file_bytes),
                    "content_type": content_type,
                }
            )
            .execute()
        )
        if not getattr(inserted, "data", None):
            raise RuntimeError("health_data_files insert returned no row")
        file_id = inserted.data[0]["id"]
    except Exception as exc:
        try:
            bucket.remove([path])
        except Exception:
            log.warning("health data upload rollback failed", exc_info=True)
        raise HealthDataDatabaseError("health_data_files insert failed") from exc
    return path, file_id


def _get_signed_url_sync(supabase_client, storage_path: str, expires_in: int, bucket: str) -> str:
    sb_bucket = supabase_client.storage.from_(bucket)
    result = sb_bucket.create_signed_url(storage_path, expires_in)
    signed_url = result.get("signedURL") or result.get("signedUrl")
    if not signed_url:
        raise KeyError(f"Missing signed URL in storage response for {storage_path}")
    return _rewrite_signed_url_for_public_base(signed_url, SUPABASE_PUBLIC_URL)


def _rewrite_signed_url_for_public_base(signed_url: str, public_base_url: str) -> str:
    """Use the browser-facing Supabase origin while preserving path/query."""
    public = urlsplit(public_base_url)
    if not public.scheme or not public.netloc:
        return signed_url

    signed = urlsplit(signed_url)
    if not signed.scheme and not signed.netloc:
        path = signed.path if signed.path.startswith("/") else f"/{signed.path}"
        signed = signed._replace(path=path)

    public_path = public.path.rstrip("/")
    signed_path = signed.path
    if public_path and not signed_path.startswith(f"{public_path}/"):
        signed_path = f"{public_path}{signed_path}"

    return urlunsplit(
        (
            public.scheme,
            public.netloc,
            signed_path,
            signed.query,
            signed.fragment,
        )
    )


def _delete_expired_exports_sync(supabase_client, user_id: str, max_age_hours: int) -> int:
    bucket = supabase_client.storage.from_(BUCKET_NAME)
    files = bucket.list(f"{user_id}/exports")
    cutoff_ms = int((time.time() - max_age_hours * 3600) * 1000)
    to_delete = []
    for item in files or []:
        name = item.get("name", "") if isinstance(item, dict) else getattr(item, "name", "")
        prefix, _, _ = (name or "").partition("_")
        if not prefix.isdigit():
            continue
        if int(prefix) < cutoff_ms:
            to_delete.append(f"{user_id}/exports/{name}")
    if to_delete:
        bucket.remove(to_delete)
    return len(to_delete)


async def ensure_bucket_exists(supabase_client) -> None:
    await asyncio.to_thread(_ensure_bucket_exists_sync, supabase_client)


async def upload_file(
    supabase_client,
    *,
    user_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    permanent: bool = False,
) -> str:
    return await asyncio.to_thread(
        _upload_file_sync,
        supabase_client,
        user_id=user_id,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
        permanent=permanent,
    )


async def upload_to_health_data(
    supabase_admin,
    *,
    user_id: str,
    profile_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> tuple[str, str]:
    return await asyncio.to_thread(
        _upload_to_health_data_sync,
        supabase_admin,
        user_id=user_id,
        profile_id=profile_id,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
    )


async def get_signed_url(
    supabase_client,
    storage_path: str,
    expires_in: int = 3600,
    bucket: str = BUCKET_NAME,
) -> str:
    return await asyncio.to_thread(
        _get_signed_url_sync,
        supabase_client,
        storage_path,
        expires_in,
        bucket,
    )


async def delete_expired_exports(
    supabase_client,
    user_id: str,
    max_age_hours: int = 24,
) -> int:
    return await asyncio.to_thread(
        _delete_expired_exports_sync,
        supabase_client,
        user_id,
        max_age_hours,
    )
