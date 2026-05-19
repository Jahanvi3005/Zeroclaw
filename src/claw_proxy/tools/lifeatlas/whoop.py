"""Read-only helpers for Whoop recovery, sleep, strain, and heart-rate data."""

from __future__ import annotations

from datetime import datetime, timedelta


class WhoopHelper:
    """Retrieve and summarize Whoop rows from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_recent_recovery(self, user_id: str, days: int = 7) -> dict:
        """Get recovery scores and HRV for recent days."""
        start_date = (datetime.now().date() - timedelta(days=days)).isoformat()
        result = (
            self.db.table("whoop_recovery")
            .select("day, score, resting_heart_rate, hrv_rmssd")
            .eq("user_id", user_id)
            .gte("day", start_date)
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No Whoop recovery data for last {days} days"}

        records = result.data[:days]
        scores = [row["score"] for row in records if row.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        hrv_values = [row["hrv_rmssd"] for row in records if row.get("hrv_rmssd") is not None]
        avg_hrv = sum(hrv_values) / len(hrv_values) if hrv_values else None
        latest = records[0]

        return {
            "period_days": days,
            "days_tracked": len(records),
            "avg_recovery_score": round(avg_score, 1),
            "avg_hrv_rmssd": round(avg_hrv, 1) if avg_hrv is not None else None,
            "latest": {
                "day": latest["day"],
                "score": latest.get("score"),
                "resting_heart_rate": latest.get("resting_heart_rate"),
                "hrv_rmssd": latest.get("hrv_rmssd"),
            },
        }

    def get_recent_sleep(self, user_id: str, days: int = 7) -> dict:
        """Get sleep summary for recent days."""
        start_date = (datetime.now().date() - timedelta(days=days)).isoformat()
        result = (
            self.db.table("whoop_sleep")
            .select("day, duration, score")
            .eq("user_id", user_id)
            .gte("day", start_date)
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No Whoop sleep data for last {days} days"}

        records = result.data[:days]
        total_duration = sum((row.get("duration") or 0) for row in records)
        scores = [row["score"] for row in records if row.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        latest = records[0]

        return {
            "period_days": days,
            "nights_tracked": len(records),
            "avg_sleep_score": round(avg_score, 1),
            "total_sleep_hours": round(total_duration / 3600, 1) if total_duration else 0,
            "latest": {
                "day": latest["day"],
                "score": latest.get("score"),
                "duration_hours": round((latest.get("duration") or 0) / 3600, 1),
            },
        }

    def get_recent_strain(self, user_id: str, days: int = 7) -> dict:
        """Get strain scores and activity metrics for recent days."""
        start_date = (datetime.now().date() - timedelta(days=days)).isoformat()
        result = (
            self.db.table("whoop_strain")
            .select("day, score, kilojoule, average_heart_rate, max_heart_rate")
            .eq("user_id", user_id)
            .gte("day", start_date)
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No Whoop strain data for last {days} days"}

        records = result.data[:days]
        scores = [row["score"] for row in records if row.get("score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0
        latest = records[0]

        return {
            "period_days": days,
            "days_tracked": len(records),
            "avg_strain_score": round(avg_score, 1),
            "latest": {
                "day": latest["day"],
                "score": latest.get("score"),
                "kilojoule": latest.get("kilojoule"),
                "average_heart_rate": latest.get("average_heart_rate"),
                "max_heart_rate": latest.get("max_heart_rate"),
            },
        }

    def get_heart_rate_trends(self, user_id: str, days: int = 7) -> dict:
        """Get heart-rate trends for recent days."""
        start_date = (datetime.now().date() - timedelta(days=days)).isoformat()
        result = (
            self.db.table("whoop_heartrate")
            .select("day, average_hr, min_hr, max_hr")
            .eq("user_id", user_id)
            .gte("day", start_date)
            .order("day", desc=True)
            .execute()
        )

        if not result.data:
            return {"message": f"No Whoop heart rate data for last {days} days"}

        records = result.data[:days]
        min_hrs = [row["min_hr"] for row in records if row.get("min_hr") is not None]
        avg_resting = sum(min_hrs) / len(min_hrs) if min_hrs else None
        latest = records[0]

        return {
            "period_days": days,
            "avg_resting_hr": round(avg_resting, 1) if avg_resting is not None else None,
            "latest": {
                "day": latest["day"],
                "min_hr": latest.get("min_hr"),
                "average_hr": latest.get("average_hr"),
                "max_hr": latest.get("max_hr"),
            },
            "trend": (
                "elevated"
                if avg_resting is not None
                and latest.get("min_hr") is not None
                and latest["min_hr"] > avg_resting + 3
                else "normal"
            ),
        }

    def get_recovery_status(self, user_id: str) -> dict:
        """Combine Whoop recovery and sleep into a recovery status."""
        recovery = self.get_recent_recovery(user_id, days=3)
        sleep = self.get_recent_sleep(user_id, days=3)

        if "message" in recovery or "message" in sleep:
            return {"message": "Insufficient Whoop recovery data"}

        recovery_score = recovery.get("avg_recovery_score") or 0
        sleep_score = sleep.get("avg_sleep_score") or 0
        combined = (recovery_score + sleep_score) / 2 if (recovery_score or sleep_score) else 0

        if combined >= 85:
            status = "excellent"
            recommendation = "Whoop shows you're well-recovered; high-intensity training is appropriate."
        elif combined >= 75:
            status = "good"
            recommendation = "Recovery looks solid; moderate to high intensity training is fine."
        elif combined >= 65:
            status = "moderate"
            recommendation = "Consider lighter training or technique work today."
        else:
            status = "poor"
            recommendation = "Prioritize rest and recovery; avoid high intensity."

        return {
            "status": status,
            "recovery_score": recovery_score,
            "sleep_score": sleep_score,
            "recommendation": recommendation,
        }
