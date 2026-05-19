"""Read-only helpers for LifeAtlas training plans and load metrics."""

from __future__ import annotations

from datetime import datetime, timedelta


class TrainingHelper:
    """Retrieve training plans, sessions, and load metrics from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_active_plan(self, user_id: str) -> dict:
        """Get the user's active training plan summary."""
        result = (
            self.db.table("training_plans")
            .select(
                "id, plan_name, start_date, end_date, event_date, event_type, "
                "fitness_level, weekly_hours_available, is_active"
            )
            .eq("user_id", user_id)
            .eq("is_active", True)
            .maybe_single()
            .execute()
        )

        if not getattr(result, "data", None):
            return {"message": "No active training plan found"}

        plan = result.data
        return {
            "plan_name": plan.get("plan_name"),
            "start_date": plan.get("start_date"),
            "end_date": plan.get("end_date"),
            "event_date": plan.get("event_date"),
            "event_type": plan.get("event_type"),
            "fitness_level": plan.get("fitness_level"),
            "weekly_hours_available": plan.get("weekly_hours_available"),
        }

    def get_upcoming_sessions(self, user_id: str, days: int = 7) -> dict:
        """Get training sessions in the next N days for active plans."""
        plans = (
            self.db.table("training_plans")
            .select("id")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .execute()
        )

        if not getattr(plans, "data", None):
            return {"message": "No active plan", "sessions": []}

        plan_ids = [plan["id"] for plan in plans.data]
        today = datetime.now().date()
        end_date = today + timedelta(days=days)

        result = (
            self.db.table("training_sessions")
            .select(
                "title, session_type, status, scheduled_date, duration_minutes, "
                "distance_km, target_tss, description"
            )
            .in_("plan_id", plan_ids)
            .gte("scheduled_date", today.isoformat())
            .lte("scheduled_date", end_date.isoformat())
            .order("scheduled_date", desc=False)
            .execute()
        )

        if not getattr(result, "data", None):
            return {"sessions": [], "count": 0}

        sessions = [
            {
                "title": session.get("title"),
                "session_type": session.get("session_type"),
                "status": session.get("status"),
                "scheduled_date": session.get("scheduled_date"),
                "duration_minutes": session.get("duration_minutes"),
                "distance_km": session.get("distance_km"),
                "target_tss": session.get("target_tss"),
                "description": session.get("description"),
            }
            for session in result.data
        ]
        return {"sessions": sessions, "count": len(sessions)}

    def get_recent_load_metrics(self, user_id: str, days: int = 7) -> dict:
        """Get recent training load metrics for the user."""
        start_date = (datetime.now().date() - timedelta(days=days)).isoformat()
        result = (
            self.db.table("training_load_metrics")
            .select(
                "metric_date, daily_tss, weekly_tss, acute_load, chronic_load, "
                "acwr, fatigue_score, form_score, readiness_score"
            )
            .eq("user_id", user_id)
            .gte("metric_date", start_date)
            .order("metric_date", desc=True)
            .limit(days)
            .execute()
        )

        if not getattr(result, "data", None):
            return {"message": f"No load metrics for last {days} days", "metrics": []}

        latest = result.data[0]
        return {
            "period_days": days,
            "latest": {
                "metric_date": latest.get("metric_date"),
                "daily_tss": latest.get("daily_tss"),
                "weekly_tss": latest.get("weekly_tss"),
                "acute_load": latest.get("acute_load"),
                "chronic_load": latest.get("chronic_load"),
                "acwr": latest.get("acwr"),
                "fatigue_score": latest.get("fatigue_score"),
                "form_score": latest.get("form_score"),
                "readiness_score": latest.get("readiness_score"),
            },
            "metrics": [
                {
                    "date": metric.get("metric_date"),
                    "daily_tss": metric.get("daily_tss"),
                    "acwr": metric.get("acwr"),
                    "form_score": metric.get("form_score"),
                }
                for metric in result.data
            ],
        }
