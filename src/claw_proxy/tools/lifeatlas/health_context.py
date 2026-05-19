"""Read-only helpers for LifeAtlas health context."""

from __future__ import annotations


class HealthContextHelper:
    """Retrieve profile-scoped healthcare, condition, BMR, and balance data."""

    def __init__(self, supabase):
        self.db = supabase

    def get_healthcare_summary(self, user_id: str, profile_id: str | None) -> dict:
        """Get healthcare form summary for a profile."""
        if not profile_id:
            return {"message": "No active profile selected"}

        result = (
            self.db.table("healthcare_forms")
            .select(
                "allergies, current_medications, chronic_conditions, "
                "family_medical_history, blood_group"
            )
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .maybe_single()
            .execute()
        )

        if not result.data:
            return {"message": "No healthcare form found"}

        data = result.data
        return {
            "allergies": data.get("allergies"),
            "current_medications": data.get("current_medications"),
            "chronic_conditions": data.get("chronic_conditions"),
            "family_medical_history": data.get("family_medical_history"),
            "blood_group": data.get("blood_group"),
        }

    def get_conditions(self, user_id: str, profile_id: str | None) -> dict:
        """Get user conditions joined with condition metadata."""
        if not profile_id:
            return {"message": "No active profile selected"}

        result = (
            self.db.table("user_conditions")
            .select("notes, conditions(name, description)")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .execute()
        )

        if not result.data:
            return {"message": "No conditions found", "conditions": []}

        conditions = []
        for row in result.data:
            condition = row.get("conditions") or {}
            if isinstance(condition, list):
                condition = condition[0] if condition else {}

            conditions.append(
                {
                    "name": condition.get("name"),
                    "description": condition.get("description"),
                    "notes": row.get("notes"),
                }
            )

        return {"conditions": conditions, "count": len(conditions)}

    def get_bmr_summary(self, user_id: str, profile_id: str | None) -> dict:
        """Get latest BMR data for a profile."""
        if not profile_id:
            return {"message": "No active profile selected"}

        result = (
            self.db.table("bmr_data")
            .select("weight_kg, height_cm, age, bmr, created_at")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result.data:
            return {"message": "No BMR data found"}

        data = result.data[0]
        return {
            "weight_kg": data.get("weight_kg"),
            "height_cm": data.get("height_cm"),
            "age": data.get("age"),
            "bmr": data.get("bmr"),
        }

    def get_life_balance_summary(self, user_id: str, profile_id: str | None) -> dict:
        """Get latest life balance scan for a profile."""
        if not profile_id:
            return {"message": "No active profile selected"}

        result = (
            self.db.table("life_balance_scans")
            .select("stability_score, scores, created_at")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not result.data:
            return {"message": "No life balance scan found"}

        data = result.data[0]
        return {
            "stability_score": data.get("stability_score"),
            "scores": data.get("scores"),
            "created_at": data.get("created_at"),
        }
