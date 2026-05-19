from unittest.mock import MagicMock

import pytest


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def select(self, value):
        self.calls.append(("select", value))
        return self

    def eq(self, key, value):
        self.calls.append(("eq", key, value))
        return self

    def maybe_single(self):
        self.calls.append(("maybe_single",))
        return self

    def execute(self):
        return FakeResult(self.data)


class FakeSupabase:
    def __init__(self, table_data):
        self.table_data = table_data
        self.queries = {}

    def table(self, name):
        query = FakeQuery(self.table_data.get(name))
        self.queries[name] = query
        return query


def test_parse_int_param_defaults_and_bounds():
    from claw_proxy.tools.lifeatlas.common import parse_int_param

    assert parse_int_param(None, default=7, minimum=1, maximum=90, name="days") == 7
    assert parse_int_param("12", default=7, minimum=1, maximum=90, name="days") == 12

    with pytest.raises(ValueError, match="days must be between 1 and 90"):
        parse_int_param("0", default=7, minimum=1, maximum=90, name="days")

    with pytest.raises(ValueError, match="days must be an integer"):
        parse_int_param("soon", default=7, minimum=1, maximum=90, name="days")


def test_parse_bool_param_accepts_true_false_only():
    from claw_proxy.tools.lifeatlas.common import parse_bool_param

    assert parse_bool_param(None, default=True, name="active_only") is True
    assert parse_bool_param("true", default=False, name="active_only") is True
    assert parse_bool_param("false", default=True, name="active_only") is False

    with pytest.raises(ValueError, match="active_only must be true or false"):
        parse_bool_param("yes", default=True, name="active_only")


def test_resolve_active_profile_prefers_active_profile():
    from claw_proxy.tools.lifeatlas.common import resolve_active_profile_id

    supabase = FakeSupabase(
        {
            "user_active_profiles": {"active_profile_id": "profile-active"},
            "user_profiles": {"id": "profile-default"},
        }
    )

    assert resolve_active_profile_id(supabase, "user-1") == "profile-active"


def test_resolve_active_profile_falls_back_to_default_profile():
    from claw_proxy.tools.lifeatlas.common import resolve_active_profile_id

    supabase = FakeSupabase(
        {
            "user_active_profiles": None,
            "user_profiles": {"id": "profile-default"},
        }
    )

    assert resolve_active_profile_id(supabase, "user-1") == "profile-default"


def test_resolve_active_profile_raises_when_missing():
    from claw_proxy.tools.lifeatlas.common import resolve_active_profile_id

    supabase = FakeSupabase({"user_active_profiles": None, "user_profiles": None})

    with pytest.raises(ValueError, match="No active profile found"):
        resolve_active_profile_id(supabase, "user-1")
