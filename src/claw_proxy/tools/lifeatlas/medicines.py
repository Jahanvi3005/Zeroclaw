"""Read-only helpers for LifeAtlas medicines."""

from __future__ import annotations


class MedicinesHelper:
    """Retrieve profile-scoped current medicines from Supabase."""

    def __init__(self, supabase):
        self.db = supabase

    def get_current_medicines(self, user_id: str, profile_id: str | None) -> dict:
        """Get current medications for a profile."""
        if not profile_id:
            return {"message": "No active profile selected"}

        result = (
            self.db.table("user_medicines")
            .select(
                "dosage_amount, frequency_per_day, notes, "
                "medicines(name, description, dosage_form, strength)"
            )
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .execute()
        )

        if not result.data:
            return {"message": "No medications found", "medicines": []}

        medicines = []
        for row in result.data:
            medicine = row.get("medicines") or {}
            if isinstance(medicine, list):
                medicine = medicine[0] if medicine else {}

            medicines.append(
                {
                    "name": medicine.get("name"),
                    "description": medicine.get("description"),
                    "dosage_form": medicine.get("dosage_form"),
                    "strength": medicine.get("strength"),
                    "dosage_amount": row.get("dosage_amount"),
                    "frequency_per_day": row.get("frequency_per_day"),
                    "notes": row.get("notes"),
                }
            )

        return {"medicines": medicines, "count": len(medicines)}
