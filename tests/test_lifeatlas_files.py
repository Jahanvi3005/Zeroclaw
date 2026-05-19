"""Tests for tools/lifeatlas/files.FilesHelper."""

from unittest.mock import AsyncMock

import pytest


class FakeResult:
    def __init__(self, data):
        self.data = data


_MISSING = object()


class FakeQuery:
    def __init__(self, table_name, rows, enforce_filters=False):
        self.table_name = table_name
        self.rows = rows
        self.calls = []
        self.single = False
        self.enforce_filters = enforce_filters

    def select(self, value):
        self.calls.append(("select", value))
        return self

    def eq(self, key, value):
        self.calls.append(("eq", key, value))
        return self

    def in_(self, key, value):
        self.calls.append(("in", key, value))
        return self

    def is_(self, key, value):
        self.calls.append(("is", key, value))
        return self

    def order(self, key, desc=False):
        self.calls.append(("order", key, desc))
        return self

    def maybe_single(self):
        self.calls.append(("maybe_single",))
        self.single = True
        return self

    def execute(self):
        rows = self._filtered_rows()
        if self.single and isinstance(rows, list):
            return FakeResult(rows[0] if rows else None)
        return FakeResult(rows)

    def _filtered_rows(self):
        if not self.enforce_filters or not isinstance(self.rows, list):
            return self.rows

        rows = self.rows
        for call in self.calls:
            match call:
                case ("eq", key, value):
                    rows = [row for row in rows if row.get(key, _MISSING) == value]
                case ("in", key, values):
                    rows = [row for row in rows if row.get(key, _MISSING) in values]
                case ("is", key, "null"):
                    rows = [row for row in rows if row.get(key) is None]
                case _:
                    pass
        return rows


class FakeSupabase:
    def __init__(self, table_rows, enforce_filters=False):
        self.table_rows = table_rows
        self.enforce_filters = enforce_filters
        self.queries = []

    def table(self, name):
        query = FakeQuery(
            name,
            self.table_rows.get(name, []),
            enforce_filters=self.enforce_filters,
        )
        self.queries.append(query)
        return query


class NoRowMaybeSingleReturnsNoneSupabase(FakeSupabase):
    def table(self, name):
        query = NoRowMaybeSingleReturnsNoneQuery(
            name,
            self.table_rows.get(name, []),
            enforce_filters=self.enforce_filters,
        )
        self.queries.append(query)
        return query


class NoRowMaybeSingleReturnsNoneQuery(FakeQuery):
    def execute(self):
        rows = self._filtered_rows()
        if self.single and isinstance(rows, list) and not rows:
            return None
        return super().execute()


def _query_for(db, table_name):
    return next(query for query in db.queries if query.table_name == table_name)


def test_list_returns_metadata_and_extracted_flag():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    rows = [
        {
            "id": "f1",
            "filename": "labs.pdf",
            "content_type": "application/pdf",
            "file_size": 1234,
            "uploaded_at": "2026-04-10T00:00:00Z",
            "timeline_entry_id": None,
        },
        {
            "id": "f2",
            "filename": "scan.png",
            "content_type": "image/png",
            "file_size": 5678,
            "uploaded_at": "2026-04-11T00:00:00Z",
            "timeline_entry_id": "te-1",
        },
    ]
    db = FakeSupabase(
        {
            "health_data_files": rows,
            "extracted_content_for_bot": [{"file_id": "f1"}],
        }
    )
    helper = FilesHelper(db, fetch_to_workspace=None)

    result = helper.list_health_data_files(user_id="u1", profile_id="p1")

    assert result["count"] == 2
    by_id = {file["file_id"]: file for file in result["files"]}
    assert by_id["f1"]["has_extracted_text"] is True
    assert by_id["f2"]["has_extracted_text"] is False
    assert by_id["f2"]["timeline_entry_id"] == "te-1"

    files_query = _query_for(db, "health_data_files")
    assert ("eq", "user_id", "u1") in files_query.calls
    assert ("eq", "profile_id", "p1") in files_query.calls
    assert ("is", "deleted_at", "null") in files_query.calls
    assert ("order", "uploaded_at", True) in files_query.calls

    extracted_query = _query_for(db, "extracted_content_for_bot")
    assert ("in", "file_id", ["f1", "f2"]) in extracted_query.calls


def test_list_filters_by_timeline_entry_when_provided():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    db = FakeSupabase(
        {
            "health_data_files": [
                {
                    "id": "f1",
                    "filename": "labs.pdf",
                    "content_type": "application/pdf",
                    "file_size": 1234,
                    "uploaded_at": "2026-04-10T00:00:00Z",
                    "timeline_entry_id": "te-1",
                }
            ],
            "extracted_content_for_bot": [],
        }
    )
    helper = FilesHelper(db, fetch_to_workspace=None)

    result = helper.list_health_data_files(
        user_id="u1", profile_id="p1", timeline_entry_id="te-1"
    )

    assert result["count"] == 1
    files_query = _query_for(db, "health_data_files")
    assert ("eq", "timeline_entry_id", "te-1") in files_query.calls


def test_list_empty():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    db = FakeSupabase({"health_data_files": []})
    helper = FilesHelper(db, fetch_to_workspace=None)

    result = helper.list_health_data_files(user_id="u1", profile_id="p1")

    assert result == {"files": [], "count": 0}
    assert [query.table_name for query in db.queries] == ["health_data_files"]


_HD_ROW = {
    "id": "f1",
    "user_id": "u1",
    "profile_id": "p1",
    "filename": "labs.pdf",
    "file_path": "p1/123_labs.pdf",
    "content_type": "application/pdf",
    "deleted_at": None,
}


def _supabase_file_content(row=_HD_ROW, extracted_text=None):
    extracted_rows = (
        [] if extracted_text is None else [{"file_id": "f1", "content": extracted_text}]
    )
    return FakeSupabase(
        {
            "health_data_files": [] if row is None else [row],
            "extracted_content_for_bot": extracted_rows,
        },
        enforce_filters=True,
    )


@pytest.mark.asyncio
async def test_get_content_cached_short():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    db = _supabase_file_content(extracted_text="short text")
    helper = FilesHelper(db, fetch_to_workspace=AsyncMock())

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result["text"] == "short text"
    assert result["text_truncated"] is False
    assert result["text_total_chars"] == len("short text")
    assert set(result) == {
        "file_id",
        "filename",
        "content_type",
        "text",
        "text_truncated",
        "text_total_chars",
    }
    assert "workspace_path" not in result

    files_query = _query_for(db, "health_data_files")
    assert ("eq", "id", "f1") in files_query.calls
    assert ("eq", "user_id", "u1") in files_query.calls
    assert ("eq", "profile_id", "p1") in files_query.calls
    assert ("is", "deleted_at", "null") in files_query.calls

    extracted_query = _query_for(db, "extracted_content_for_bot")
    assert ("eq", "file_id", "f1") in extracted_query.calls


@pytest.mark.asyncio
async def test_get_content_cached_truncated_default():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    long_text = "x" * 10000
    db = _supabase_file_content(extracted_text=long_text)
    helper = FilesHelper(db, fetch_to_workspace=AsyncMock())

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result["text_truncated"] is True
    assert len(result["text"]) == 4000
    assert result["text_total_chars"] == 10000
    assert "truncation_note" in result


@pytest.mark.asyncio
async def test_get_content_cached_full_override():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    long_text = "y" * 10000
    db = _supabase_file_content(extracted_text=long_text)
    helper = FilesHelper(db, fetch_to_workspace=AsyncMock())

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
        full=True,
    )

    assert result["text"] == long_text
    assert result["text_truncated"] is False


@pytest.mark.asyncio
async def test_get_content_cached_empty_string():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    db = _supabase_file_content(extracted_text="")
    helper = FilesHelper(db, fetch_to_workspace=AsyncMock())

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result["text"] == ""
    assert result["text_truncated"] is False
    assert result["text_total_chars"] == 0


@pytest.mark.asyncio
async def test_get_content_binary_when_no_extraction():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    fetch = AsyncMock(
        return_value={
            "workspace_path": "lifeatlas/files/f1_labs.pdf",
            "container_path": "/zeroclaw-data/workspace/lifeatlas/files/f1_labs.pdf",
            "filename": "labs.pdf",
            "size_bytes": 1234,
        }
    )
    db = _supabase_file_content(extracted_text=None)
    helper = FilesHelper(db, fetch_to_workspace=fetch)

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result == {
        "file_id": "f1",
        "filename": "labs.pdf",
        "content_type": "application/pdf",
        "workspace_path": "lifeatlas/files/f1_labs.pdf",
        "container_path": "/zeroclaw-data/workspace/lifeatlas/files/f1_labs.pdf",
    }
    assert "text" not in result
    fetch.assert_awaited_once()
    assert fetch.call_args.kwargs == {
        "user_id": "u1",
        "bucket": "health_data",
        "storage_path": "p1/123_labs.pdf",
        "dest_subdir": "files",
        "dest_basename_prefix": "f1",
        "original_filename": "labs.pdf",
    }


@pytest.mark.asyncio
async def test_get_content_binary_when_cached_lookup_returns_none_response():
    from claw_proxy.tools.lifeatlas.files import FilesHelper

    fetch = AsyncMock(
        return_value={
            "workspace_path": "lifeatlas/files/f1_labs.pdf",
            "container_path": "/zeroclaw-data/workspace/lifeatlas/files/f1_labs.pdf",
            "filename": "labs.pdf",
            "size_bytes": 1234,
        }
    )
    db = NoRowMaybeSingleReturnsNoneSupabase(
        {
            "health_data_files": [_HD_ROW],
            "extracted_content_for_bot": [],
        },
        enforce_filters=True,
    )
    helper = FilesHelper(db, fetch_to_workspace=fetch)

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result["workspace_path"] == "lifeatlas/files/f1_labs.pdf"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_content_uses_thread_for_sync_supabase_queries(monkeypatch):
    from claw_proxy.tools.lifeatlas import files

    calls = []
    original_to_thread = files.asyncio.to_thread

    async def recording_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(files.asyncio, "to_thread", recording_to_thread)
    db = _supabase_file_content(extracted_text="threaded")
    helper = files.FilesHelper(db, fetch_to_workspace=AsyncMock())

    result = await helper.get_health_data_file_content(
        user_id="u1",
        profile_id="p1",
        file_id="f1",
    )

    assert result["text"] == "threaded"
    assert calls == ["_lookup_file_row", "_lookup_cached_content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row",
    [
        None,
        {**_HD_ROW, "user_id": "someone-else"},
        {**_HD_ROW, "profile_id": "other-profile"},
        {**_HD_ROW, "deleted_at": "2026-05-01T00:00:00Z"},
    ],
)
async def test_get_content_invalid_or_deleted_file_raises_lookup_error(row):
    from claw_proxy.tools.lifeatlas.files import FilesHelper
    from claw_proxy.tools.lifeatlas.common import ToolLookupError

    db = _supabase_file_content(row=row, extracted_text="cached despite invalid row")
    helper = FilesHelper(db, fetch_to_workspace=AsyncMock())

    with pytest.raises(ToolLookupError, match="file f1 not found"):
        await helper.get_health_data_file_content(
            user_id="u1",
            profile_id="p1",
            file_id="f1",
        )
