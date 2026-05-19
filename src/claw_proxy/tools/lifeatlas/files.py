"""Read-only helpers for LifeAtlas health_data_files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

from claw_proxy.tools.lifeatlas.common import ToolLookupError

DEFAULT_TEXT_PREVIEW_CHARS = int(
    os.environ.get("LIFEATLAS_TEXT_PREVIEW_CHARS", "4000")
)


class FilesHelper:
    """Retrieve documents from the user's LifeAtlas library."""

    def __init__(self, supabase, fetch_to_workspace: Callable[..., Awaitable[dict]]):
        self.db = supabase
        self.fetch_to_workspace = fetch_to_workspace

    def list_health_data_files(
        self,
        user_id: str,
        profile_id: str,
        timeline_entry_id: str | None = None,
    ) -> dict[str, Any]:
        query = (
            self.db.table("health_data_files")
            .select(
                "id, filename, content_type, file_size, uploaded_at, timeline_entry_id"
            )
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
        )
        if timeline_entry_id:
            query = query.eq("timeline_entry_id", timeline_entry_id)
        rows = (
            query.is_("deleted_at", "null")
            .order("uploaded_at", desc=True)
            .execute()
            .data
            or []
        )

        if not rows:
            return {"files": [], "count": 0}

        ids = [row["id"] for row in rows]
        extracted = (
            self.db.table("extracted_content_for_bot")
            .select("file_id")
            .in_("file_id", ids)
            .execute()
            .data
            or []
        )
        extracted_ids = {row["file_id"] for row in extracted}

        files = [
            {
                "file_id": row["id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "file_size": row["file_size"],
                "uploaded_at": row["uploaded_at"],
                "timeline_entry_id": row.get("timeline_entry_id"),
                "has_extracted_text": row["id"] in extracted_ids,
            }
            for row in rows
        ]
        return {"files": files, "count": len(files)}

    async def get_health_data_file_content(
        self,
        user_id: str,
        profile_id: str,
        file_id: str,
        full: bool = False,
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._lookup_file_row,
            user_id,
            profile_id,
            file_id,
        )
        if not row:
            raise ToolLookupError(f"file {file_id} not found")

        cached = await asyncio.to_thread(self._lookup_cached_content, file_id)
        if cached is not None and cached.get("content") is not None:
            cached_text = cached["content"]
            return self._format_cached(row, cached_text, full)

        fetched = await self.fetch_to_workspace(
            user_id=user_id,
            bucket="health_data",
            storage_path=row["file_path"],
            dest_subdir="files",
            dest_basename_prefix=file_id,
            original_filename=row["filename"],
        )
        return {
            "file_id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "workspace_path": fetched["workspace_path"],
            "container_path": fetched["container_path"],
        }

    def _lookup_file_row(
        self,
        user_id: str,
        profile_id: str,
        file_id: str,
    ) -> dict[str, Any] | None:
        response = (
            self.db.table("health_data_files")
            .select("id, profile_id, filename, file_path, content_type")
            .eq("id", file_id)
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        return getattr(response, "data", None)

    def _lookup_cached_content(self, file_id: str) -> dict[str, Any] | None:
        response = (
            self.db.table("extracted_content_for_bot")
            .select("content")
            .eq("file_id", file_id)
            .maybe_single()
            .execute()
        )
        return getattr(response, "data", None)

    @staticmethod
    def _format_cached(row: dict, text: str, full: bool) -> dict:
        total_chars = len(text)
        result = {
            "file_id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "text_total_chars": total_chars,
        }
        if full or total_chars <= DEFAULT_TEXT_PREVIEW_CHARS:
            return {
                **result,
                "text": text,
                "text_truncated": False,
            }

        return {
            **result,
            "text": text[:DEFAULT_TEXT_PREVIEW_CHARS],
            "text_truncated": True,
            "truncation_note": (
                "Text preview truncated; use full=true to retrieve the complete text."
            ),
        }


__all__ = ["FilesHelper", "ToolLookupError"]
