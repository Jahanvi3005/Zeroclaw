"""Read-only helpers for Oura sleep, readiness, and heart-rate data."""

from __future__ import annotations

from datetime import datetime, timedelta


class OuraHelper:
    """Retrieve and summarize Oura rows from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_recent_sleep(self, user_id: str, days: int = 7) -> dict:
        """Get sleep summary for recent days."""
        hours_back = days * 24 + 12
        start_time = datetime.now() - timedelta(hours=hours_back)

        result = (
            self.db.table("oura_sleep")
            .select("start_datetime, end_datetime, duration, score")
            .eq("user_id", user_id)
            .gte("start_datetime", start_time.isoformat())
            .order("start_datetime", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No sleep data found for last {days} days"}

        sleep_records = result.data[:days]
        total_duration = sum((row.get("duration") or 0) for row in sleep_records)
        scores = [row["score"] for row in sleep_records if row.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        avg_duration = total_duration / len(sleep_records) / 3600
        latest = sleep_records[0]
        latest_duration = latest.get("duration") or 0

        return {
            "period_days": days,
            "nights_tracked": len(sleep_records),
            "avg_sleep_score": round(avg_score, 1),
            "avg_duration_hours": round(avg_duration, 1),
            "total_sleep_hours": round(total_duration / 3600, 1),
            "last_night": {
                "score": latest.get("score"),
                "duration_hours": round(latest_duration / 3600, 1),
                "date": latest["start_datetime"],
            },
        }

    def get_recent_readiness(self, user_id: str, days: int = 7) -> dict:
        """Get readiness scores for recent days."""
        start_date = datetime.now().date() - timedelta(days=days)

        result = (
            self.db.table("oura_readiness")
            .select("day, score, contributors")
            .eq("user_id", user_id)
            .gte("day", str(start_date))
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No readiness data found for last {days} days"}

        scores = [row["score"] for row in result.data if row.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        today = result.data[0]
        today_score = today.get("score")

        return {
            "period_days": days,
            "avg_readiness_score": round(avg_score, 1),
            "today": {
                "score": today_score,
                "contributors": today.get("contributors"),
                "date": today["day"],
            },
            "trend": "improving" if (today_score or 0) > avg_score else "declining",
        }

    def get_recovery_status(self, user_id: str) -> dict:
        """Get current recovery status combining sleep and readiness."""
        sleep = self.get_recent_sleep(user_id, days=3)
        readiness = self.get_recent_readiness(user_id, days=3)

        if "message" in sleep or "message" in readiness:
            return {"message": "Insufficient recovery data"}

        sleep_score = sleep["avg_sleep_score"]
        readiness_score = readiness["avg_readiness_score"]

        if sleep_score >= 85 and readiness_score >= 85:
            status = "excellent"
            recommendation = "You're well-recovered and ready for high-intensity training"
        elif sleep_score >= 75 and readiness_score >= 75:
            status = "good"
            recommendation = "Recovery is solid, moderate to high intensity training appropriate"
        elif sleep_score >= 65 and readiness_score >= 65:
            status = "moderate"
            recommendation = "Consider lighter training or focus on technique work"
        else:
            status = "poor"
            recommendation = "Prioritize rest and recovery, avoid high intensity"

        return {
            "status": status,
            "sleep_score": sleep_score,
            "readiness_score": readiness_score,
            "recommendation": recommendation,
        }

    def get_heart_rate_trends(self, user_id: str, days: int = 7) -> dict:
        """Get resting heart-rate trends."""
        start_date = datetime.now().date() - timedelta(days=days)

        result = (
            self.db.table("oura_heartrate")
            .select("day, average_hr, min_hr, max_hr")
            .eq("user_id", user_id)
            .gte("day", str(start_date))
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No heart rate data for last {days} days"}

        min_hrs = [row["min_hr"] for row in result.data if row.get("min_hr") is not None]
        avg_resting = sum(min_hrs) / len(min_hrs) if min_hrs else None
        latest = result.data[0]
        return {
            "period_days": days,
            "avg_resting_hr": round(avg_resting, 1) if avg_resting is not None else None,
            "today_resting_hr": latest.get("min_hr"),
            "today_avg_hr": latest.get("average_hr"),
            "trend": (
                "elevated"
                if avg_resting is not None
                and latest.get("min_hr") is not None
                and latest["min_hr"] > avg_resting + 3
                else "normal"
            ),
        }
