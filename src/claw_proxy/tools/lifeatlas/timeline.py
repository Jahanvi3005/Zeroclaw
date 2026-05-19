"""Read-only helpers for LifeAtlas timeline entries and injuries."""

from __future__ import annotations

from typing import Any


class TimelineHelper:
    """Retrieve profile-scoped timeline entries and injuries from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_entry_types(self, user_id: str, profile_id: str | None) -> dict:
        """Get sorted unique timeline entry types for a profile."""
        if not profile_id:
            return {"message": "No active profile selected", "entry_types": []}

        result = (
            self.db.table("timeline_entries")
            .select("entry_type")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .execute()
        )

        if not result.data:
            return {"entry_types": [], "count": 0}

        entry_types = sorted(
            {row["entry_type"] for row in result.data if row.get("entry_type")}
        )
        return {"entry_types": entry_types, "count": len(entry_types)}

    def get_timeline_entries(
        self,
        user_id: str,
        profile_id: str | None,
        limit: int = 50,
        entry_type: str | list[str] | None = None,
    ) -> dict:
        """Get timeline entries for a profile, optionally filtered by entry type."""
        if not profile_id:
            return {"message": "No active profile selected"}

        query = (
            self.db.table("timeline_entries")
            .select(
                "id, entry_type, description, start_date, end_date, location, "
                "body_part, pain_level, start_year, end_year"
            )
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .order("start_date", desc=True)
            .limit(limit)
        )

        resolved = self._resolve_entry_type_filter(user_id, profile_id, entry_type)
        if resolved:
            if len(resolved) == 1:
                query = query.eq("entry_type", resolved[0])
            else:
                query = query.in_("entry_type", resolved)

        result = query.execute()
        if not result.data:
            return {"message": "No timeline entries found", "entries": []}

        entries = [self._format_timeline_entry(row) for row in result.data]
        return {"entries": entries, "count": len(entries)}

    def get_injuries(
        self,
        user_id: str,
        profile_id: str | None,
        active_only: bool = True,
    ) -> dict:
        """Get injuries for a profile."""
        if not profile_id:
            return {"message": "No active profile selected"}

        query = (
            self.db.table("injuries")
            .select("body_part, pain_level, description, start_date, end_date")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .order("start_date", desc=True)
        )
        if active_only:
            query = query.is_("end_date", "null")

        result = query.execute()
        if not result.data:
            return {"message": "No injuries found", "injuries": []}

        injuries = [
            {
                "body_part": row.get("body_part"),
                "pain_level": row.get("pain_level"),
                "description": row.get("description"),
                "start_date": row.get("start_date"),
                "end_date": row.get("end_date"),
            }
            for row in result.data
        ]
        return {"injuries": injuries, "count": len(injuries)}

    def _resolve_entry_type_filter(
        self,
        user_id: str,
        profile_id: str | None,
        entry_type: str | list[str] | None,
    ) -> list[str] | None:
        if not profile_id or entry_type is None:
            return None

        types_result = self.get_entry_types(user_id, profile_id)
        allowed = types_result.get("entry_types") or []
        if not allowed:
            return None

        requested = [entry_type] if isinstance(entry_type, str) else entry_type
        resolved = []
        for requested_type in requested:
            if not requested_type:
                continue
            for allowed_type in allowed:
                if allowed_type.lower() == requested_type.lower():
                    resolved.append(allowed_type)
                    break

        return resolved or None

    @staticmethod
    def _format_timeline_entry(row: dict[str, Any]) -> dict:
        return {
            "entry_type": row.get("entry_type"),
            "description": row.get("description"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
            "location": row.get("location"),
            "body_part": row.get("body_part"),
            "pain_level": row.get("pain_level"),
        }
