"""Tests for LogEventsHelper."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from claw_proxy.tools.lifeatlas.common import ToolLookupError
from claw_proxy.tools.lifeatlas.log_events import LogEventsHelper


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

    def order(self, key, desc=False):
        self.calls.append(("order", key, desc))
        return self

    def limit(self, value):
        self.calls.append(("limit", value))
        return self

    def maybe_single(self):
        self.calls.append(("maybe_single",))
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


def _query_for(db, table_name):
    return next(query for query in db.queries if query.table_name == table_name)


def _gte_value(query, key):
    return next(call[2] for call in query.calls if call[:2] == ("gte", key))


def test_list_returns_events_with_has_photo_and_query_contract():
    rows = [
        {
            "id": "e1",
            "event_type": "medicine",
            "occurred_at": "2026-04-29T09:00:00Z",
            "location": "home",
            "description": "took ibuprofen 200mg",
            "photo_path": "u/log_events/x.jpg",
        },
        {
            "id": "e2",
            "event_type": "food",
            "occurred_at": "2026-04-29T12:00:00Z",
            "location": None,
            "description": None,
            "photo_path": None,
        },
    ]
    db = FakeSupabase({"log_events": rows})
    helper = LogEventsHelper(db, fetch_to_workspace=AsyncMock())

    result = helper.list_log_events(user_id="u1", profile_id="p1")

    assert result["count"] == 2
    by_id = {event["event_id"]: event for event in result["events"]}
    assert by_id["e1"]["has_photo"] is True
    assert by_id["e2"]["has_photo"] is False
    assert by_id["e1"]["event_type"] == "medicine"

    query = _query_for(db, "log_events")
    assert (
        "select",
        "id, event_type, occurred_at, location, description, photo_path",
    ) in query.calls
    assert ("eq", "user_id", "u1") in query.calls
    assert ("eq", "profile_id", "p1") in query.calls
    assert ("order", "occurred_at", True) in query.calls
    assert ("limit", 50) in query.calls

    cutoff = _gte_value(query, "occurred_at")
    cutoff_dt = datetime.fromisoformat(cutoff)
    now = datetime.now(timezone.utc)
    assert now - timedelta(days=31) < cutoff_dt < now


def test_list_filters_by_event_type():
    rows = [
        {
            "id": "e1",
            "event_type": "medicine",
            "occurred_at": "2026-04-29T09:00:00Z",
            "location": None,
            "description": None,
            "photo_path": None,
        }
    ]
    db = FakeSupabase({"log_events": rows})
    helper = LogEventsHelper(db, fetch_to_workspace=AsyncMock())

    result = helper.list_log_events(
        user_id="u1",
        profile_id="p1",
        event_type="medicine",
        days=7,
        limit=10,
    )

    assert result["count"] == 1
    query = _query_for(db, "log_events")
    assert ("eq", "event_type", "medicine") in query.calls
    assert ("limit", 10) in query.calls
    cutoff = _gte_value(query, "occurred_at")
    cutoff_dt = datetime.fromisoformat(cutoff)
    now = datetime.now(timezone.utc)
    assert now - timedelta(days=8) < cutoff_dt < now


@pytest.mark.asyncio
async def test_get_photo_returns_image_tag():
    row = {"id": "e1", "profile_id": "p1", "photo_path": "u1/log_events/cat.jpg"}
    db = FakeSupabase({"log_events": row})
    fetch = AsyncMock(
        return_value={
            "workspace_path": "lifeatlas/photos/e1_cat.jpg",
            "container_path": "/zeroclaw-data/workspace/lifeatlas/photos/e1_cat.jpg",
            "filename": "cat.jpg",
            "size_bytes": 4096,
        }
    )
    helper = LogEventsHelper(db, fetch_to_workspace=fetch)

    result = await helper.get_log_event_photo(
        user_id="u1", profile_id="p1", event_id="e1"
    )

    assert result["workspace_path"] == "lifeatlas/photos/e1_cat.jpg"
    assert (
        result["image_tag"]
        == "[IMAGE:/zeroclaw-data/workspace/lifeatlas/photos/e1_cat.jpg]"
    )
    assert result["content_type"].startswith("image/")

    query = _query_for(db, "log_events")
    assert ("select", "id, profile_id, photo_path") in query.calls
    assert ("eq", "id", "e1") in query.calls
    assert ("eq", "user_id", "u1") in query.calls
    assert ("eq", "profile_id", "p1") in query.calls
    assert ("maybe_single",) in query.calls

    fetch.assert_awaited_once()
    _, kwargs = fetch.call_args
    assert kwargs["user_id"] == "u1"
    assert kwargs["bucket"] == "event_photos"
    assert kwargs["storage_path"] == "u1/log_events/cat.jpg"
    assert kwargs["dest_subdir"] == "photos"
    assert kwargs["dest_basename_prefix"] == "e1"
    assert kwargs["original_filename"] == "cat.jpg"


@pytest.mark.asyncio
async def test_get_photo_delegates_lookup_to_thread(monkeypatch):
    row = {"id": "e1", "profile_id": "p1", "photo_path": "u1/log_events/cat.jpg"}
    db = FakeSupabase({"log_events": row})
    fetch = AsyncMock(
        return_value={
            "workspace_path": "lifeatlas/photos/e1_cat.jpg",
            "container_path": "/zeroclaw-data/workspace/lifeatlas/photos/e1_cat.jpg",
            "filename": "cat.jpg",
            "size_bytes": 4096,
        }
    )
    helper = LogEventsHelper(db, fetch_to_workspace=fetch)
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    await helper.get_log_event_photo(user_id="u1", profile_id="p1", event_id="e1")

    assert len(calls) == 1
    func, args, kwargs = calls[0]
    assert func == helper._lookup_event_photo_row
    assert args == ("u1", "p1", "e1")
    assert kwargs == {}


@pytest.mark.asyncio
async def test_get_photo_defaults_unknown_content_type_to_jpeg():
    row = {"id": "e1", "profile_id": "p1", "photo_path": "u1/log_events/capture"}
    db = FakeSupabase({"log_events": row})
    fetch = AsyncMock(
        return_value={
            "workspace_path": "lifeatlas/photos/e1_capture",
            "container_path": "/zeroclaw-data/workspace/lifeatlas/photos/e1_capture",
            "filename": "capture",
            "size_bytes": 4096,
        }
    )
    helper = LogEventsHelper(db, fetch_to_workspace=fetch)

    result = await helper.get_log_event_photo(
        user_id="u1", profile_id="p1", event_id="e1"
    )

    assert result["content_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_get_photo_no_attached_photo():
    row = {"id": "e1", "profile_id": "p1", "photo_path": None}
    db = FakeSupabase({"log_events": row})
    fetch = AsyncMock()
    helper = LogEventsHelper(db, fetch_to_workspace=fetch)

    result = await helper.get_log_event_photo(
        user_id="u1", profile_id="p1", event_id="e1"
    )

    assert "No photo" in result["message"]
    assert result["event_id"] == "e1"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_photo_event_not_found():
    db = FakeSupabase({"log_events": None})
    helper = LogEventsHelper(db, fetch_to_workspace=AsyncMock())

    with pytest.raises(ToolLookupError):
        await helper.get_log_event_photo(
            user_id="u1", profile_id="p1", event_id="missing"
        )
