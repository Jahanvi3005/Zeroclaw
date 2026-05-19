"""Read-only helpers for LifeAtlas log_events."""

from __future__ import annotations

import asyncio
import mimetypes
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from claw_proxy.tools.lifeatlas.common import ToolLookupError


class LogEventsHelper:
    """Retrieve logged events (medicine, food, training, etc.) and attached photos."""

    def __init__(self, supabase, fetch_to_workspace: Callable[..., Awaitable[dict]]):
        self.db = supabase
        self.fetch_to_workspace = fetch_to_workspace

    def list_log_events(
        self,
        user_id: str,
        profile_id: str,
        event_type: str | None = None,
        days: int = 30,
        limit: int = 50,
    ) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query = (
            self.db.table("log_events")
            .select("id, event_type, occurred_at, location, description, photo_path")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .gte("occurred_at", cutoff)
            .order("occurred_at", desc=True)
            .limit(limit)
        )
        if event_type:
            query = query.eq("event_type", event_type)
        rows = query.execute().data or []

        events = [
            {
                "event_id": row["id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "location": row.get("location"),
                "description": row.get("description"),
                "has_photo": bool(row.get("photo_path")),
            }
            for row in rows
        ]
        return {"events": events, "count": len(events)}

    def _lookup_event_photo_row(
        self,
        user_id: str,
        profile_id: str,
        event_id: str,
    ) -> dict[str, Any] | None:
        row_resp = (
            self.db.table("log_events")
            .select("id, profile_id, photo_path")
            .eq("id", event_id)
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .maybe_single()
            .execute()
        )
        return getattr(row_resp, "data", None)

    async def get_log_event_photo(
        self,
        user_id: str,
        profile_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        row = await asyncio.to_thread(
            self._lookup_event_photo_row,
            user_id,
            profile_id,
            event_id,
        )
        if not row:
            raise ToolLookupError(f"event {event_id} not found")

        if not row.get("photo_path"):
            return {"message": "No photo attached to this event", "event_id": event_id}

        original_name = posixpath.basename(row["photo_path"])
        fetched = await self.fetch_to_workspace(
            user_id=user_id,
            bucket="event_photos",
            storage_path=row["photo_path"],
            dest_subdir="photos",
            dest_basename_prefix=event_id,
            original_filename=original_name,
        )
        content_type = mimetypes.guess_type(fetched["filename"])[0] or "image/jpeg"
        return {
            "event_id": event_id,
            "filename": fetched["filename"],
            "content_type": content_type,
            "workspace_path": fetched["workspace_path"],
            "container_path": fetched["container_path"],
            "image_tag": f"[IMAGE:{fetched['container_path']}]",
        }
