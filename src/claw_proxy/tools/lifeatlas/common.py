"""Shared helpers for LifeAtlas ZeroClaw tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


async def run_sync(fn: Callable, *args, **kwargs):
    """Run sync Supabase helper work without blocking the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def parse_int_param(
    raw: str | None,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def parse_bool_param(raw: str | None, *, default: bool, name: str) -> bool:
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def resolve_active_profile_id(supabase, user_id: str) -> str:
    active = (
        supabase.table("user_active_profiles")
        .select("active_profile_id")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if getattr(active, "data", None):
        return active.data["active_profile_id"]

    fallback = (
        supabase.table("user_profiles")
        .select("id")
        .eq("user_id", user_id)
        .eq("is_default", True)
        .maybe_single()
        .execute()
    )
    if getattr(fallback, "data", None):
        return fallback.data["id"]

    raise ValueError(f"No active profile found for user {user_id}")


class ToolLookupError(Exception):
    """Entity (file, event) not found or not owned by the requesting user."""


class ContainerNotProvisioned(Exception):
    """User has no active ZeroClaw container."""


class FetchTooLarge(Exception):
    """Storage object exceeds MAX_FETCH_SIZE."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"object is {size_bytes} bytes; limit is {MAX_FETCH_SIZE}")
        self.size_bytes = size_bytes


class StorageFetchError(Exception):
    """Bucket download failed (object missing, RLS, network)."""


MAX_FETCH_SIZE = 25 * 1024 * 1024  # 25 MB, matches MAX_FILE_SIZE in files/upload.py
