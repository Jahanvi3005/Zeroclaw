"""Read-only helpers for LifeAtlas events and selected races."""

from __future__ import annotations

from datetime import datetime


class EventsHelper:
    """Retrieve custom user events and selected races from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_user_events(
        self,
        user_id: str,
        upcoming_only: bool = True,
        limit: int = 20,
    ) -> dict:
        """Get the user's custom events."""
        query = (
            self.db.table("user_events")
            .select("event_name, event_date, event_type, location, notes")
            .eq("user_id", user_id)
            .order("event_date", desc=False)
            .limit(limit)
        )
        if upcoming_only:
            query = query.gte("event_date", datetime.now().date().isoformat())

        result = query.execute()
        if not getattr(result, "data", None):
            return {"message": "No events found", "events": []}

        events = [
            {
                "event_name": event.get("event_name"),
                "event_date": event.get("event_date"),
                "event_type": event.get("event_type"),
                "location": event.get("location"),
                "notes": event.get("notes"),
            }
            for event in result.data
        ]
        return {"events": events, "count": len(events)}

    def get_selected_races(self, user_id: str) -> dict:
        """Get races selected by the user from triathlon events."""
        result = (
            self.db.table("user_selected_events")
            .select(
                "event_id, selected_at, notes, "
                "triathlon_events(name, event_date, location, event_type, difficulty)"
            )
            .eq("user_id", user_id)
            .execute()
        )

        if not getattr(result, "data", None):
            return {"message": "No selected races", "races": []}

        races = []
        for row in result.data:
            triathlon_event = row.get("triathlon_events") or {}
            if isinstance(triathlon_event, list) and triathlon_event:
                triathlon_event = triathlon_event[0]

            races.append(
                {
                    "name": triathlon_event.get("name"),
                    "event_date": triathlon_event.get("event_date"),
                    "location": triathlon_event.get("location"),
                    "event_type": triathlon_event.get("event_type"),
                    "difficulty": triathlon_event.get("difficulty"),
                    "notes": row.get("notes"),
                }
            )

        today = datetime.now().date().isoformat()
        upcoming = [race for race in races if (race.get("event_date") or "") >= today]
        return {"races": races, "upcoming": upcoming, "count": len(races)}
