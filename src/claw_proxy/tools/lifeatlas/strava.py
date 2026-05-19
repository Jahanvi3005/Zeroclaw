"""Read-only helpers for Strava activity data."""

from __future__ import annotations

from datetime import datetime, timedelta


class StravaHelper:
    """Retrieve and summarize Strava activity rows from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_weekly_summary(self, user_id: str, weeks_back: int = 0) -> dict:
        """Get summarized activity stats for a seven-day period."""
        end_date = datetime.now() - timedelta(weeks=weeks_back)
        start_date = end_date - timedelta(days=7)

        result = (
            self.db.table("strava_activities")
            .select(
                "type, distance, moving_time, total_elevation_gain, average_heartrate"
            )
            .eq("user_id", user_id)
            .gte("start_date", start_date.isoformat())
            .lte("start_date", end_date.isoformat())
            .execute()
        )

        if not result.data:
            return {"message": "No activities this week"}

        summary = {}
        for activity in result.data:
            activity_type = activity["type"]
            if activity_type not in summary:
                summary[activity_type] = {
                    "count": 0,
                    "total_distance": 0,
                    "total_time": 0,
                    "avg_hr": [],
                }

            summary[activity_type]["count"] += 1
            summary[activity_type]["total_distance"] += float(
                activity.get("distance") or 0
            )
            summary[activity_type]["total_time"] += int(
                activity.get("moving_time") or 0
            )
            if activity.get("average_heartrate"):
                summary[activity_type]["avg_hr"].append(
                    float(activity["average_heartrate"])
                )

        for activity_type, values in summary.items():
            if values["avg_hr"]:
                values["avg_heartrate"] = sum(values["avg_hr"]) / len(
                    values["avg_hr"]
                )
            values["total_distance_km"] = values["total_distance"] / 1000
            values["total_hours"] = values["total_time"] / 3600
            del values["avg_hr"]

        return summary

    def get_recent_activity(self, user_id: str, limit: int = 1) -> dict | None:
        """Get the most recent Strava activity with key stats."""
        result = (
            self.db.table("strava_activities")
            .select("name, type, distance, moving_time, start_date, average_heartrate")
            .eq("user_id", user_id)
            .order("start_date", desc=True)
            .limit(limit)
            .execute()
        )

        if not result.data:
            return None

        activity = result.data[0]
        return {
            "name": activity["name"],
            "type": activity["type"],
            "distance_km": float(activity.get("distance") or 0) / 1000,
            "duration_minutes": int(activity.get("moving_time") or 0) / 60,
            "date": activity["start_date"],
            "avg_hr": activity.get("average_heartrate"),
        }
