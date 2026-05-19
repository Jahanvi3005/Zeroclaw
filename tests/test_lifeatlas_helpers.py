class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, table_name, rows):
        self.table_name = table_name
        self.rows = rows
        self.calls = []

    def select(self, value):
        self.calls.append(("select", value))
        return self

    def eq(self, key, value):
        self.calls.append(("eq", key, value))
        return self

    def gte(self, key, value):
        self.calls.append(("gte", key, value))
        return self

    def lte(self, key, value):
        self.calls.append(("lte", key, value))
        return self

    def order(self, key, desc=False):
        self.calls.append(("order", key, desc))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        self.rows = self.rows[:value]
        return self

    def maybe_single(self):
        self.calls.append(("maybe_single",))
        return self

    def in_(self, key, value):
        self.calls.append(("in", key, value))
        return self

    def is_(self, key, value):
        self.calls.append(("is", key, value))
        return self

    def execute(self):
        return FakeResult(self.rows)


class FakeSupabase:
    def __init__(self, table_rows):
        self.table_rows = table_rows
        self.queries = []

    def table(self, name):
        query = FakeQuery(name, self.table_rows.get(name, []))
        self.queries.append(query)
        return query


def test_strava_weekly_summary_aggregates_by_activity_type():
    from claw_proxy.tools.lifeatlas.strava import StravaHelper

    db = FakeSupabase(
        {
            "strava_activities": [
                {
                    "type": "Run",
                    "distance": 5000,
                    "moving_time": 1800,
                    "total_elevation_gain": 20,
                    "average_heartrate": 145,
                },
                {
                    "type": "Run",
                    "distance": 7000,
                    "moving_time": 2400,
                    "total_elevation_gain": 30,
                    "average_heartrate": 150,
                },
            ]
        }
    )

    result = StravaHelper(db).get_weekly_summary("user-1", weeks_back=0)

    assert result["Run"]["count"] == 2
    assert result["Run"]["total_distance_km"] == 12
    assert round(result["Run"]["total_hours"], 2) == 1.17
    assert result["Run"]["avg_heartrate"] == 147.5


def test_oura_recovery_status_handles_insufficient_data():
    from claw_proxy.tools.lifeatlas.oura import OuraHelper

    db = FakeSupabase({"oura_sleep": [], "oura_readiness": []})

    assert OuraHelper(db).get_recovery_status("user-1") == {
        "message": "Insufficient recovery data"
    }


def test_whoop_heart_rate_trends_ignores_null_min_hr():
    from claw_proxy.tools.lifeatlas.whoop import WhoopHelper

    db = FakeSupabase(
        {
            "whoop_heartrate": [
                {"day": "2026-04-28", "average_hr": 70, "min_hr": None, "max_hr": 120},
                {"day": "2026-04-27", "average_hr": 68, "min_hr": 52, "max_hr": 115},
            ]
        }
    )

    result = WhoopHelper(db).get_heart_rate_trends("user-1", days=7)

    assert result["avg_resting_hr"] == 52
    assert result["latest"]["min_hr"] is None
    assert result["trend"] == "normal"


def test_timeline_entry_types_returns_sorted_unique_values():
    from claw_proxy.tools.lifeatlas.timeline import TimelineHelper

    db = FakeSupabase(
        {
            "timeline_entries": [
                {"entry_type": "travel"},
                {"entry_type": "injury"},
                {"entry_type": "travel"},
                {"entry_type": None},
            ]
        }
    )

    result = TimelineHelper(db).get_entry_types("user-1", "profile-1")

    assert result == {"entry_types": ["injury", "travel"], "count": 2}


def test_timeline_entries_requires_profile():
    from claw_proxy.tools.lifeatlas.timeline import TimelineHelper

    result = TimelineHelper(FakeSupabase({})).get_timeline_entries("user-1", None)

    assert result == {"message": "No active profile selected"}


def test_medicines_flattens_join_result():
    from claw_proxy.tools.lifeatlas.medicines import MedicinesHelper

    db = FakeSupabase(
        {
            "user_medicines": [
                {
                    "dosage_amount": "10mg",
                    "frequency_per_day": 1,
                    "notes": "morning",
                    "medicines": {
                        "name": "ExampleMed",
                        "description": "Example",
                        "dosage_form": "tablet",
                        "strength": "10mg",
                    },
                }
            ]
        }
    )

    result = MedicinesHelper(db).get_current_medicines("user-1", "profile-1")

    assert result["count"] == 1
    assert result["medicines"][0]["name"] == "ExampleMed"
    assert result["medicines"][0]["dosage_amount"] == "10mg"


def test_health_context_bmr_returns_latest_record():
    from claw_proxy.tools.lifeatlas.health_context import HealthContextHelper

    db = FakeSupabase(
        {
            "bmr_data": [
                {
                    "weight_kg": 70,
                    "height_cm": 180,
                    "age": 35,
                    "bmr": 1650,
                    "created_at": "2026-04-28T00:00:00Z",
                }
            ]
        }
    )

    result = HealthContextHelper(db).get_bmr_summary("user-1", "profile-1")

    assert result == {"weight_kg": 70, "height_cm": 180, "age": 35, "bmr": 1650}


def test_training_active_plan_returns_plan_summary():
    from claw_proxy.tools.lifeatlas.training import TrainingHelper

    db = FakeSupabase(
        {
            "training_plans": {
                "id": "plan-1",
                "plan_name": "Base Build",
                "start_date": "2026-04-01",
                "end_date": "2026-06-01",
                "event_date": "2026-06-15",
                "event_type": "marathon",
                "fitness_level": "intermediate",
                "weekly_hours_available": 6,
                "is_active": True,
            }
        }
    )

    result = TrainingHelper(db).get_active_plan("user-1")

    assert result["plan_name"] == "Base Build"
    assert result["weekly_hours_available"] == 6


def test_events_selected_races_splits_upcoming():
    from claw_proxy.tools.lifeatlas.events import EventsHelper

    db = FakeSupabase(
        {
            "user_selected_events": [
                {
                    "event_id": "race-1",
                    "selected_at": "2026-01-01",
                    "notes": "A race",
                    "triathlon_events": {
                        "name": "Future Race",
                        "event_date": "2999-01-01",
                        "location": "Stockholm",
                        "event_type": "triathlon",
                        "difficulty": "hard",
                    },
                }
            ]
        }
    )

    result = EventsHelper(db).get_selected_races("user-1")

    assert result["count"] == 1
    assert result["upcoming"][0]["name"] == "Future Race"
