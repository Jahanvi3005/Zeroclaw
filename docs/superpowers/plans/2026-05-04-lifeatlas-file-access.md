# LifeAtlas File Access Implementation Plan

> **Status:** Implemented and merged on 2026-05-06. The checklist below is
> retained as the execution plan/history, not a current list of remaining work.
> Current behavior is documented in `docs/lifeatlas-tools.md` and
> `docs/files-and-push.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add agent-facing read access to user `health_data_files` documents and `log_events` photos, and replace the `[SAVE:..]` tag with a `save_to_library` HTTP tool that writes to the website's `health_data` bucket.

**Architecture:** Five new HTTP tools under `/zeroclaw/tools/lifeatlas/`. Read tools list metadata then stream binary content into the container workspace volume (or return cached extracted text). Write tool reads a workspace file via the existing busybox helper, uploads to `health_data` via service-role Supabase client, and inserts a `health_data_files` row with the same atomic-rollback contract as the website's `upload-file` edge function. `[DOWNLOAD:..]` tag flow is left intact; `[SAVE:..]` tag handling is removed.

**Tech Stack:** Python 3.11+, FastAPI, Supabase Python client, Docker SDK, pytest with `-m integration` marker, `uv` for dependency management.

---

## Reference: spec

Full design in `docs/superpowers/specs/2026-05-04-lifeatlas-file-access-design.md`. Open it in a side pane while working — task descriptions reference its sections by name.

## Reference: critical existing code

Historical references from the time this plan was written; current behavior may
have moved on. Read these files once before starting; tasks repeatedly assume
you know what's in them.

- `src/claw_proxy/tools/lifeatlas/router.py` — current router, sync-style `_call_tool`, branches per tool name, query-param parsing helpers (`_parse_int_param`, `_parse_bool_param`, `_profile_id_for`).
- `src/claw_proxy/tools/lifeatlas/common.py` — `run_sync`, `parse_int_param`, `parse_bool_param`, `resolve_active_profile_id`.
- `src/claw_proxy/files/storage.py` — `upload_file`, `get_signed_url`, `_delete_expired_exports_sync`. `BUCKET_NAME = "workspace_files"` is hardcoded.
- `src/claw_proxy/files/downloads.py` — `DOWNLOAD_TAG_RE = re.compile(r"\[(DOWNLOAD|SAVE):([^\]]+)\]")`, `_materialize_signed_url`, `process_download_tags`.
- `src/claw_proxy/files/upload.py` — current upload pattern (good template for fetch_to_workspace).
- `src/claw_proxy/containers/workspace.py` — `write_file_via_busybox`, `read_file_via_busybox`, `relative_path_to_container_path`, `normalize_workspace_relative_path`, `find_expired_temp_uploads`, `render_lifeatlas_skill_token` (currently only renders the token placeholder).
- `src/claw_proxy/containers/orchestrator.py` — `WORKSPACE_DIRS = ("workspace/temp",)`, `ContainerOrchestrator.get`, `templates_dir` resolution.
- `src/claw_proxy/app.py:228+` — `_lifecycle_loop` orchestration.
- `tests/test_lifeatlas_router.py`, `tests/test_lifeatlas_helpers.py`, `tests/test_storage.py`, `tests/test_downloads.py`, `tests/test_workspace.py` — existing test patterns for mock Supabase, mock orchestrator, async helpers.

## Reference: gotchas (from CLAUDE.md)

- **Volumes are owned by UID 65534**. Host-side IO running as a different UID will get `PermissionError`. Always use `write_file_via_busybox` / `read_file_via_busybox`. Integration tests use the `reclaim_volume` fixture.
- **`config.py:24` is a module-level `httpx.AsyncClient`** — do not call `asyncio.run()` multiple times in a single test. Use `pytest.mark.asyncio`.
- **Provisioned containers force `ZEROCLAW_GATEWAY_HOST=0.0.0.0`** — already handled.

## File structure (full plan scope)

**New files:**
- `src/claw_proxy/files/workspace_fetch.py` — Supabase storage → container workspace
- `src/claw_proxy/tools/lifeatlas/files.py` — `FilesHelper`
- `src/claw_proxy/tools/lifeatlas/log_events.py` — `LogEventsHelper`
- `src/claw_proxy/tools/lifeatlas/save.py` — `SaveHelper`
- `src/claw_proxy/cli/commands/skills.py` — `claw-admin skills retrofit` command
- `tests/test_workspace_fetch.py`
- `tests/test_lifeatlas_files.py`
- `tests/test_lifeatlas_log_events.py`
- `tests/test_lifeatlas_save.py`
- `tests/test_skills_retrofit.py`
- `tests/integration/test_lifeatlas_file_access.py`

**Modified files:**
- `src/claw_proxy/tools/lifeatlas/router.py` — async migration, register 5 new tool branches, add new query params + UUID parser
- `src/claw_proxy/files/storage.py` — `upload_to_health_data`, parameterize `get_signed_url` with bucket
- `src/claw_proxy/files/downloads.py` — narrow `DOWNLOAD_TAG_RE`, drop SAVE branch
- `src/claw_proxy/containers/workspace.py` — full skill template render (`render_lifeatlas_skill`), plus `find_expired_lifeatlas_files`, plus path constants for `workspace/lifeatlas/{files,photos}`
- `src/claw_proxy/containers/orchestrator.py` — extend `WORKSPACE_DIRS`, add `_ensure_skills_current` hook on container start
- `src/claw_proxy/app.py:_lifecycle_loop` — sweep `workspace/lifeatlas/{files,photos}/`
- `src/claw_proxy/cli/main.py` — register the new `skills` command group
- `templates/default/workspace/skills/lifeatlas/SKILL.toml` — 5 new `[[tools]]` blocks, bump `version` to `0.2.0`
- `templates/default/workspace/skills/lifeatlas/SKILL.md` — three new paragraphs
- `tests/test_lifeatlas_router.py`, `tests/test_storage.py`, `tests/test_downloads.py`, `tests/test_lifeatlas_skill_template.py` — additions/edits where noted

---

## Phase 0 — Foundational

### Task 0.1: Add new exception types to `tools/lifeatlas/common.py`

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/common.py`
- Test: `tests/test_lifeatlas_common.py`

- [ ] **Step 1: Add exception classes**

Append to `src/claw_proxy/tools/lifeatlas/common.py`:

```python
class ToolLookupError(Exception):
    """Entity (file, event) not found or not owned by the requesting user."""


class ContainerNotProvisioned(Exception):
    """User has no active ZeroClaw container."""


class FetchTooLarge(Exception):
    """Storage object exceeds MAX_FETCH_SIZE."""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"object is {size_bytes} bytes; limit is {MAX_FETCH_SIZE}")
        self.size_bytes = size_bytes


class StorageFetchError(Exception):
    """Bucket download failed (object missing, RLS, network)."""


MAX_FETCH_SIZE = 25 * 1024 * 1024  # 25 MB, matches MAX_FILE_SIZE in files/upload.py
```

- [ ] **Step 2: Run existing test suite to confirm no regression**

Run: `uv run pytest tests/test_lifeatlas_common.py -v`
Expected: PASS (existing tests still green; no new tests yet — exceptions are scaffolding).

- [ ] **Step 3: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/common.py
git commit -m "feat(lifeatlas): add exception types for file-access tools"
```

---

### Task 0.2: Parameterize `get_signed_url` with `bucket` argument

**Files:**
- Modify: `src/claw_proxy/files/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_storage.py`:

```python
import pytest
from unittest.mock import MagicMock

from claw_proxy.files.storage import get_signed_url, BUCKET_NAME


@pytest.mark.asyncio
async def test_get_signed_url_default_bucket():
    sb = MagicMock()
    sb.storage.from_.return_value.create_signed_url.return_value = {"signedURL": "https://x"}
    url = await get_signed_url(sb, "p/f.pdf")
    assert url == "https://x"
    sb.storage.from_.assert_called_with(BUCKET_NAME)


@pytest.mark.asyncio
async def test_get_signed_url_custom_bucket():
    sb = MagicMock()
    sb.storage.from_.return_value.create_signed_url.return_value = {"signedURL": "https://y"}
    url = await get_signed_url(sb, "p/f.pdf", bucket="health_data")
    assert url == "https://y"
    sb.storage.from_.assert_called_with("health_data")
```

- [ ] **Step 2: Run test — expect fail**

Run: `uv run pytest tests/test_storage.py::test_get_signed_url_custom_bucket -v`
Expected: FAIL — `get_signed_url` doesn't accept `bucket` keyword.

- [ ] **Step 3: Update implementation**

Replace `_get_signed_url_sync` and `get_signed_url` in `src/claw_proxy/files/storage.py`:

```python
def _get_signed_url_sync(supabase_client, storage_path: str, expires_in: int, bucket: str) -> str:
    sb_bucket = supabase_client.storage.from_(bucket)
    result = sb_bucket.create_signed_url(storage_path, expires_in)
    signed_url = result.get("signedURL") or result.get("signedUrl")
    if not signed_url:
        raise KeyError(f"Missing signed URL in storage response for {storage_path}")
    return signed_url


async def get_signed_url(
    supabase_client,
    storage_path: str,
    expires_in: int = 3600,
    bucket: str = BUCKET_NAME,
) -> str:
    return await asyncio.to_thread(
        _get_signed_url_sync, supabase_client, storage_path, expires_in, bucket,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS (new tests + existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/files/storage.py tests/test_storage.py
git commit -m "refactor(storage): parameterize get_signed_url with bucket"
```

---

### Task 0.3: Migrate `tools/lifeatlas/router.py` from sync to async dispatch

**Why:** New tools need async (storage IO + workspace writes). The existing router wraps the entire sync `_call_tool` in `asyncio.to_thread`. We change the topology so individual sync helpers are wrapped per-call, and async helpers can be `await`ed directly.

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/router.py`
- Test: `tests/test_lifeatlas_router.py`

- [ ] **Step 1: Make `_call_tool` async, wrap each existing sync call individually**

In `src/claw_proxy/tools/lifeatlas/router.py`:

Change signature: `def _call_tool(...) -> dict[str, Any]:` → `async def _call_tool(...) -> dict[str, Any]:`

For every existing branch like:

```python
if tool_name == "get_weekly_training":
    return strava.get_weekly_summary(user_id, get_weeks_ago())
```

Wrap in `await asyncio.to_thread(...)`:

```python
if tool_name == "get_weekly_training":
    return await asyncio.to_thread(strava.get_weekly_summary, user_id, get_weeks_ago())
```

For branches that resolve `profile_id` first (`get_timeline_entries` etc.), wrap profile resolution too:

```python
if tool_name == "get_timeline_entries":
    profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
    return await asyncio.to_thread(
        timeline.get_timeline_entries,
        user_id, profile_id, get_timeline_limit(), params.get("entry_type"),
    )
```

Add `import asyncio` at the top if not already present.

In the `lifeatlas_tool` route handler, change:

```python
result = await run_sync(_call_tool, supabase, user_id, tool_name, params)
```

to:

```python
result = await _call_tool(supabase, user_id, tool_name, params)
```

`run_sync` import becomes unused; remove it from the import line (still used elsewhere — keep the symbol exported in `common.py`).

- [ ] **Step 2: Run existing router tests**

Run: `uv run pytest tests/test_lifeatlas_router.py -v`
Expected: PASS — every existing tool still dispatches correctly. If any test mocks `run_sync`, update it to mock `asyncio.to_thread` or the helper directly.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest`
Expected: PASS (`305 passed, 3 deselected` per `93559fd` baseline).

- [ ] **Step 4: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/router.py tests/test_lifeatlas_router.py
git commit -m "refactor(lifeatlas/router): convert _call_tool to async, wrap sync helpers per-call"
```

---

## Phase 1 — Workspace fetch

### Task 1.1: Create `files/workspace_fetch.py` with `fetch_to_workspace`

**Files:**
- Create: `src/claw_proxy/files/workspace_fetch.py`
- Test: `tests/test_workspace_fetch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_workspace_fetch.py`:

```python
"""Tests for files/workspace_fetch.fetch_to_workspace."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from claw_proxy.files.workspace_fetch import fetch_to_workspace
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    StorageFetchError,
)


def _orchestrator(volume="/data/zeroclaw/u1", docker=None):
    orch = MagicMock()
    orch.docker = docker or MagicMock()
    orch.data_dir = "/data/zeroclaw"
    orch.get = AsyncMock(return_value=MagicMock(container_id="c1"))
    return orch


def _supabase(bucket_bytes=b"hello"):
    sb = MagicMock()
    sb.storage.from_.return_value.download.return_value = bucket_bytes
    return sb


@pytest.mark.asyncio
async def test_fetch_writes_bytes_to_workspace(monkeypatch):
    written = {}

    def fake_busybox(docker, volume_path, relative_path, content):
        written["volume"] = volume_path
        written["relative"] = relative_path
        written["content"] = content

    monkeypatch.setattr(
        "claw_proxy.files.workspace_fetch.write_file_via_busybox", fake_busybox,
    )
    orch = _orchestrator()
    sb = _supabase(b"hello world")

    out = await fetch_to_workspace(
        orchestrator=orch, supabase=sb, user_id="u1",
        bucket="health_data", storage_path="p/f.pdf",
        dest_subdir="files", dest_basename_prefix="abc-123",
        original_filename="lab results.pdf",
    )
    assert out["workspace_path"] == "lifeatlas/files/abc-123_lab_results.pdf"
    assert out["container_path"] == "/zeroclaw-data/workspace/lifeatlas/files/abc-123_lab_results.pdf"
    assert out["filename"] == "lab_results.pdf"
    assert out["size_bytes"] == 11
    assert written["relative"] == "workspace/lifeatlas/files/abc-123_lab_results.pdf"
    assert written["content"] == b"hello world"


@pytest.mark.asyncio
async def test_fetch_raises_when_no_container():
    orch = _orchestrator()
    orch.get = AsyncMock(return_value=None)
    with pytest.raises(ContainerNotProvisioned):
        await fetch_to_workspace(
            orchestrator=orch, supabase=_supabase(), user_id="u1",
            bucket="health_data", storage_path="p/f.pdf",
            dest_subdir="files", dest_basename_prefix="abc",
            original_filename="f.pdf",
        )


@pytest.mark.asyncio
async def test_fetch_raises_when_too_large(monkeypatch):
    monkeypatch.setattr(
        "claw_proxy.files.workspace_fetch.write_file_via_busybox", lambda *a, **k: None,
    )
    sb = _supabase(b"x" * (26 * 1024 * 1024))
    with pytest.raises(FetchTooLarge):
        await fetch_to_workspace(
            orchestrator=_orchestrator(), supabase=sb, user_id="u1",
            bucket="health_data", storage_path="p/f.pdf",
            dest_subdir="files", dest_basename_prefix="abc",
            original_filename="f.pdf",
        )


@pytest.mark.asyncio
async def test_fetch_translates_storage_failure():
    sb = MagicMock()
    sb.storage.from_.return_value.download.side_effect = RuntimeError("404 not found")
    with pytest.raises(StorageFetchError):
        await fetch_to_workspace(
            orchestrator=_orchestrator(), supabase=sb, user_id="u1",
            bucket="health_data", storage_path="p/missing.pdf",
            dest_subdir="files", dest_basename_prefix="abc",
            original_filename="missing.pdf",
        )
```

- [ ] **Step 2: Run tests — expect import fail**

Run: `uv run pytest tests/test_workspace_fetch.py -v`
Expected: FAIL — `claw_proxy.files.workspace_fetch` doesn't exist.

- [ ] **Step 3: Implement `fetch_to_workspace`**

Create `src/claw_proxy/files/workspace_fetch.py`:

```python
"""Stream Supabase storage objects into the container workspace volume."""

from __future__ import annotations

import asyncio
import logging
import re

from claw_proxy.containers.workspace import (
    relative_path_to_container_path,
    write_file_via_busybox,
)
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    MAX_FETCH_SIZE,
    StorageFetchError,
)

log = logging.getLogger(__name__)

# Match the website upload-file edge function's filename sanitization.
_LIFEATLAS_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")


def _sanitize_basename(name: str) -> str:
    cleaned = _LIFEATLAS_FILENAME_RE.sub("_", name).strip("._")
    return cleaned or "file"


def _download_sync(supabase_client, bucket: str, storage_path: str) -> bytes:
    try:
        return supabase_client.storage.from_(bucket).download(storage_path)
    except Exception as exc:
        raise StorageFetchError(str(exc)) from exc


async def fetch_to_workspace(
    *,
    orchestrator,
    supabase,
    user_id: str,
    bucket: str,
    storage_path: str,
    dest_subdir: str,         # "files" | "photos"
    dest_basename_prefix: str,
    original_filename: str,
) -> dict:
    container = await orchestrator.get(user_id)
    if not container:
        raise ContainerNotProvisioned()

    safe_name = _sanitize_basename(original_filename)
    relative_path = (
        f"workspace/lifeatlas/{dest_subdir}/{dest_basename_prefix}_{safe_name}"
    )
    container_path = relative_path_to_container_path(relative_path)
    agent_relative = relative_path[len("workspace/"):]

    raw = await asyncio.to_thread(_download_sync, supabase, bucket, storage_path)
    if len(raw) > MAX_FETCH_SIZE:
        raise FetchTooLarge(len(raw))

    await asyncio.to_thread(
        write_file_via_busybox,
        orchestrator.docker,
        f"{orchestrator.data_dir}/{user_id}",
        relative_path,
        raw,
    )
    log.info(
        "fetch_to_workspace: user=%s bucket=%s size=%d dest=%s",
        user_id[:8], bucket, len(raw), agent_relative,
    )
    return {
        "workspace_path": agent_relative,
        "container_path": container_path,
        "filename": safe_name,
        "size_bytes": len(raw),
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_workspace_fetch.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/files/workspace_fetch.py tests/test_workspace_fetch.py
git commit -m "feat(files): add workspace_fetch.fetch_to_workspace"
```

---

## Phase 2 — Read-side: health data files

### Task 2.1: `FilesHelper.list_health_data_files`

**Files:**
- Create: `src/claw_proxy/tools/lifeatlas/files.py`
- Test: `tests/test_lifeatlas_files.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_lifeatlas_files.py`:

```python
"""Tests for tools/lifeatlas/files.FilesHelper."""
import pytest
from unittest.mock import MagicMock

from claw_proxy.tools.lifeatlas.files import FilesHelper


def _supabase_with_files(rows, extracted_ids=()):
    """Build a mock supabase client whose .table().select()....execute() returns rows."""
    sb = MagicMock()

    def _table(name):
        tbl = MagicMock()
        if name == "health_data_files":
            chain = tbl.select.return_value
            chain.eq.return_value.eq.return_value.is_.return_value.order.return_value.execute.return_value.data = rows
            chain.eq.return_value.eq.return_value.eq.return_value.is_.return_value.order.return_value.execute.return_value.data = rows
            return tbl
        if name == "extracted_content_for_bot":
            tbl.select.return_value.in_.return_value.execute.return_value.data = [
                {"file_id": fid} for fid in extracted_ids
            ]
            return tbl
        return tbl

    sb.table.side_effect = _table
    return sb


def test_list_returns_metadata_and_extracted_flag():
    rows = [
        {"id": "f1", "filename": "labs.pdf", "content_type": "application/pdf",
         "file_size": 1234, "uploaded_at": "2026-04-10T00:00:00Z",
         "timeline_entry_id": None},
        {"id": "f2", "filename": "scan.png", "content_type": "image/png",
         "file_size": 5678, "uploaded_at": "2026-04-11T00:00:00Z",
         "timeline_entry_id": "te-1"},
    ]
    sb = _supabase_with_files(rows, extracted_ids=("f1",))
    helper = FilesHelper(sb, fetch_to_workspace=None)

    result = helper.list_health_data_files(user_id="u1", profile_id="p1")
    assert result["count"] == 2
    by_id = {f["file_id"]: f for f in result["files"]}
    assert by_id["f1"]["has_extracted_text"] is True
    assert by_id["f2"]["has_extracted_text"] is False
    assert by_id["f2"]["timeline_entry_id"] == "te-1"


def test_list_empty():
    sb = _supabase_with_files([])
    helper = FilesHelper(sb, fetch_to_workspace=None)
    result = helper.list_health_data_files(user_id="u1", profile_id="p1")
    assert result == {"files": [], "count": 0}
```

(If your existing test pattern uses different mock-supabase scaffolding, adapt to match — the assertion is what matters: extracted_ids drives `has_extracted_text`.)

- [ ] **Step 2: Run tests — expect import fail**

Run: `uv run pytest tests/test_lifeatlas_files.py::test_list_returns_metadata_and_extracted_flag -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create `FilesHelper` with `list_health_data_files`**

Create `src/claw_proxy/tools/lifeatlas/files.py`:

```python
"""Read-only helpers for LifeAtlas health_data_files."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from claw_proxy.tools.lifeatlas.common import ToolLookupError


class FilesHelper:
    """Retrieve documents from the user's LifeAtlas library."""

    def __init__(self, supabase, fetch_to_workspace: Callable[..., Awaitable[dict]]):
        self.db = supabase
        self.fetch_to_workspace = fetch_to_workspace

    def list_health_data_files(
        self,
        user_id: str,
        profile_id: str,
        timeline_entry_id: str | None = None,
    ) -> dict[str, Any]:
        query = (
            self.db.table("health_data_files")
            .select("id, filename, content_type, file_size, uploaded_at, timeline_entry_id")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
        )
        if timeline_entry_id:
            query = query.eq("timeline_entry_id", timeline_entry_id)
        rows = query.is_("deleted_at", "null").order("uploaded_at", desc=True).execute().data or []

        if not rows:
            return {"files": [], "count": 0}

        ids = [row["id"] for row in rows]
        extracted = (
            self.db.table("extracted_content_for_bot")
            .select("file_id")
            .in_("file_id", ids)
            .execute()
            .data
            or []
        )
        extracted_ids = {row["file_id"] for row in extracted}

        files = [
            {
                "file_id": row["id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "file_size": row["file_size"],
                "uploaded_at": row["uploaded_at"],
                "timeline_entry_id": row.get("timeline_entry_id"),
                "has_extracted_text": row["id"] in extracted_ids,
            }
            for row in rows
        ]
        return {"files": files, "count": len(files)}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_lifeatlas_files.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/files.py tests/test_lifeatlas_files.py
git commit -m "feat(lifeatlas): add FilesHelper.list_health_data_files"
```

---

### Task 2.2: `FilesHelper.get_health_data_file_content` — cached path with truncation

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/files.py`
- Modify: `tests/test_lifeatlas_files.py`

- [ ] **Step 1: Add the constant + tests**

Append to `tests/test_lifeatlas_files.py`:

```python
import pytest
from unittest.mock import AsyncMock

# Mock supabase that returns a single row + extracted text
def _supabase_row(row, extracted_text=None):
    sb = MagicMock()

    def _table(name):
        tbl = MagicMock()
        if name == "health_data_files":
            (tbl.select.return_value.eq.return_value.eq.return_value.eq.return_value
                .is_.return_value.maybe_single.return_value.execute.return_value.data) = row
            return tbl
        if name == "extracted_content_for_bot":
            data = {"content": extracted_text} if extracted_text else None
            (tbl.select.return_value.eq.return_value.maybe_single.return_value
                .execute.return_value.data) = data
            return tbl
        return tbl

    sb.table.side_effect = _table
    return sb


_HD_ROW = {
    "id": "f1", "profile_id": "p1",
    "filename": "labs.pdf", "file_path": "p1/123_labs.pdf",
    "content_type": "application/pdf",
}


@pytest.mark.asyncio
async def test_get_content_cached_short():
    sb = _supabase_row(_HD_ROW, extracted_text="short text")
    helper = FilesHelper(sb, fetch_to_workspace=AsyncMock())
    result = await helper.get_health_data_file_content(
        user_id="u1", profile_id="p1", file_id="f1",
    )
    assert result["text"] == "short text"
    assert result["text_truncated"] is False
    assert result["text_total_chars"] == len("short text")
    assert "workspace_path" not in result


@pytest.mark.asyncio
async def test_get_content_cached_truncated_default():
    long_text = "x" * 10000
    sb = _supabase_row(_HD_ROW, extracted_text=long_text)
    helper = FilesHelper(sb, fetch_to_workspace=AsyncMock())
    result = await helper.get_health_data_file_content(
        user_id="u1", profile_id="p1", file_id="f1",
    )
    assert result["text_truncated"] is True
    assert len(result["text"]) == 4000
    assert result["text_total_chars"] == 10000
    assert "truncation_note" in result


@pytest.mark.asyncio
async def test_get_content_cached_full_override():
    long_text = "y" * 10000
    sb = _supabase_row(_HD_ROW, extracted_text=long_text)
    helper = FilesHelper(sb, fetch_to_workspace=AsyncMock())
    result = await helper.get_health_data_file_content(
        user_id="u1", profile_id="p1", file_id="f1", full=True,
    )
    assert result["text"] == long_text
    assert result["text_truncated"] is False
```

- [ ] **Step 2: Run tests — expect fail**

Run: `uv run pytest tests/test_lifeatlas_files.py::test_get_content_cached_short -v`
Expected: FAIL — `get_health_data_file_content` not implemented.

- [ ] **Step 3: Implement cached path**

Add to `src/claw_proxy/tools/lifeatlas/files.py`:

```python
import os

DEFAULT_TEXT_PREVIEW_CHARS = int(os.environ.get("LIFEATLAS_TEXT_PREVIEW_CHARS", "4000"))


# Inside FilesHelper:

    async def get_health_data_file_content(
        self,
        user_id: str,
        profile_id: str,
        file_id: str,
        full: bool = False,
    ) -> dict[str, Any]:
        row_resp = (
            self.db.table("health_data_files")
            .select("id, profile_id, filename, file_path, content_type")
            .eq("id", file_id)
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .is_("deleted_at", "null")
            .maybe_single()
            .execute()
        )
        row = getattr(row_resp, "data", None)
        if not row:
            raise ToolLookupError(f"file {file_id} not found")

        cache_resp = (
            self.db.table("extracted_content_for_bot")
            .select("content")
            .eq("file_id", file_id)
            .maybe_single()
            .execute()
        )
        cached = getattr(cache_resp, "data", None)
        cached_text = cached.get("content") if cached else None

        if cached_text:
            return self._format_cached(row, cached_text, full)

        # Binary path lands in Task 2.3.
        raise NotImplementedError("binary fetch path arrives in Task 2.3")

    @staticmethod
    def _format_cached(row: dict, text: str, full: bool) -> dict:
        total = len(text)
        if full or total <= DEFAULT_TEXT_PREVIEW_CHARS:
            return {
                "file_id": row["id"],
                "filename": row["filename"],
                "content_type": row["content_type"],
                "text": text,
                "text_truncated": False,
                "text_total_chars": total,
            }
        preview = text[:DEFAULT_TEXT_PREVIEW_CHARS]
        return {
            "file_id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "text": preview,
            "text_truncated": True,
            "text_total_chars": total,
            "truncation_note": (
                f"Truncated at {DEFAULT_TEXT_PREVIEW_CHARS} of {total} chars. "
                f"Call this tool again with full=true for the complete text."
            ),
        }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_lifeatlas_files.py -v`
Expected: cached-path tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/files.py tests/test_lifeatlas_files.py
git commit -m "feat(lifeatlas): add cached extraction + truncation in get_health_data_file_content"
```

---

### Task 2.3: `FilesHelper.get_health_data_file_content` — binary streaming path + ownership errors

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/files.py`
- Modify: `tests/test_lifeatlas_files.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_lifeatlas_files.py`:

```python
@pytest.mark.asyncio
async def test_get_content_binary_when_no_extraction():
    sb = _supabase_row(_HD_ROW, extracted_text=None)
    fetch = AsyncMock(return_value={
        "workspace_path": "lifeatlas/files/f1_labs.pdf",
        "container_path": "/zeroclaw-data/workspace/lifeatlas/files/f1_labs.pdf",
        "filename": "labs.pdf",
        "size_bytes": 1234,
    })
    helper = FilesHelper(sb, fetch_to_workspace=fetch)

    result = await helper.get_health_data_file_content(
        user_id="u1", profile_id="p1", file_id="f1",
    )
    assert result["workspace_path"] == "lifeatlas/files/f1_labs.pdf"
    assert result["container_path"].endswith("lifeatlas/files/f1_labs.pdf")
    assert "text" not in result
    fetch.assert_awaited_once()
    args, kwargs = fetch.call_args
    assert kwargs["bucket"] == "health_data"
    assert kwargs["storage_path"] == "p1/123_labs.pdf"
    assert kwargs["dest_subdir"] == "files"
    assert kwargs["dest_basename_prefix"] == "f1"


@pytest.mark.asyncio
async def test_get_content_lookup_error_when_row_missing():
    sb = _supabase_row(None)
    helper = FilesHelper(sb, fetch_to_workspace=AsyncMock())
    with pytest.raises(ToolLookupError):
        await helper.get_health_data_file_content(
            user_id="u1", profile_id="p1", file_id="missing",
        )


# At top of file:
from claw_proxy.tools.lifeatlas.common import ToolLookupError
```

- [ ] **Step 2: Run tests — expect NotImplementedError fail**

Run: `uv run pytest tests/test_lifeatlas_files.py::test_get_content_binary_when_no_extraction -v`
Expected: FAIL — `NotImplementedError` from Task 2.2 stub.

- [ ] **Step 3: Replace `NotImplementedError` with binary fetch call**

In `FilesHelper.get_health_data_file_content`, replace:

```python
        # Binary path lands in Task 2.3.
        raise NotImplementedError("binary fetch path arrives in Task 2.3")
```

with:

```python
        fetched = await self.fetch_to_workspace(
            user_id=user_id,
            bucket="health_data",
            storage_path=row["file_path"],
            dest_subdir="files",
            dest_basename_prefix=file_id,
            original_filename=row["filename"],
        )
        return {
            "file_id": row["id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "workspace_path": fetched["workspace_path"],
            "container_path": fetched["container_path"],
        }
```

Note: `fetch_to_workspace` is injected as a partial that already binds `orchestrator` and `supabase`. The wiring happens in Task 2.4.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_lifeatlas_files.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/files.py tests/test_lifeatlas_files.py
git commit -m "feat(lifeatlas): add binary fetch + ownership check in get_health_data_file_content"
```

---

### Task 2.4: Wire `FilesHelper` into the router

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/router.py`
- Modify: `tests/test_lifeatlas_router.py`

- [ ] **Step 1: Write router-dispatch tests**

Append to `tests/test_lifeatlas_router.py`:

```python
import pytest
from fastapi.testclient import TestClient

# Adapt the existing fixture pattern in this file. The new tests assert:
# - GET /zeroclaw/tools/lifeatlas/list_health_data_files?token=T returns 200 with files[]
# - GET .../get_health_data_file_content?token=T&file_id=UUID&full=true is dispatched
# - missing file_id returns 400 ToolParameterError
# - ToolLookupError → 404 with error="not_found"
# - ContainerNotProvisioned → 409 with error="container_not_provisioned"
# - FetchTooLarge → 413 with error="file_too_large"
# - StorageFetchError → 502 with error="storage_unavailable"

# Replace the existing router test fixtures' supabase mock so it returns
# health_data_files rows for the list test, and a single row for the
# get-content tests (mirroring tests/test_lifeatlas_files.py).
```

(Use the existing fixture style in `tests/test_lifeatlas_router.py`. The exact mock structure depends on what's already there — copy the pattern of an existing tool test like `get_timeline_entries`, then adapt to the new tools.)

- [ ] **Step 2: Run tests — expect 404 / dispatch-not-found**

Run: `uv run pytest tests/test_lifeatlas_router.py -v -k health_data`
Expected: FAIL — unknown tool.

- [ ] **Step 3: Add new query params + UUID parser to router**

Edit `src/claw_proxy/tools/lifeatlas/router.py`:

In the `lifeatlas_tool` route signature, add:

```python
file_id: str | None = Query(default=None),
event_id: str | None = Query(default=None),
timeline_entry_id: str | None = Query(default=None),
full: str | None = Query(default=None),
workspace_path: str | None = Query(default=None),
event_type: str | None = Query(default=None),
```

Pass them in the `params` dict.

Add a UUID validator helper near the existing parsers:

```python
import uuid


def _require_uuid(params: dict, name: str) -> str:
    raw = params.get(name)
    if not raw:
        raise ToolParameterError(f"{name} is required")
    try:
        uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise ToolParameterError(f"{name} must be a UUID") from exc
    return raw
```

- [ ] **Step 4: Add `FilesHelper` import and `fetch_to_workspace` partial**

At the top of `_call_tool` (or in the surrounding closure), add:

```python
from functools import partial

from claw_proxy.files.workspace_fetch import fetch_to_workspace as _fetch_to_workspace
from claw_proxy.tools.lifeatlas.files import FilesHelper

# Inside _call_tool, after the existing helper instantiations:
files_fetch = partial(_fetch_to_workspace, orchestrator=orchestrator, supabase=supabase)
files = FilesHelper(supabase, files_fetch)
```

`orchestrator` must be available inside `_call_tool`. It's not, today — `_call_tool` only takes `(supabase, user_id, tool_name, params)`. Add `orchestrator` to the signature and pass it from the route handler. Update `create_lifeatlas_tools_router` to accept `get_orchestrator` (similar to `get_supabase_client`):

```python
def create_lifeatlas_tools_router(
    token_map: dict[str, str],
    get_supabase_client,
    get_orchestrator,  # NEW
) -> APIRouter:
    ...
    async def lifeatlas_tool(...):
        ...
        supabase = get_supabase_client()
        orchestrator = get_orchestrator()  # NEW
        result = await _call_tool(supabase, orchestrator, user_id, tool_name, params)
        ...
```

The caller in `app.py` must be updated to pass `get_orchestrator`. Search for `create_lifeatlas_tools_router(` in `app.py`; add `get_orchestrator=lambda: _orchestrator`.

- [ ] **Step 5: Add the dispatch branches**

```python
if tool_name == "list_health_data_files":
    profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
    return await asyncio.to_thread(
        files.list_health_data_files, user_id, profile_id,
        params.get("timeline_entry_id"),
    )

if tool_name == "get_health_data_file_content":
    file_id = _require_uuid(params, "file_id")
    profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
    full = _parse_bool_param(params.get("full"), default=False, name="full")
    return await files.get_health_data_file_content(user_id, profile_id, file_id, full)
```

- [ ] **Step 6: Map new exceptions in the route handler**

Below the existing `except UnknownToolError:` / `except ToolParameterError:` blocks:

```python
except ToolLookupError as exc:
    return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})
except ContainerNotProvisioned:
    return JSONResponse(
        status_code=409,
        content={"error": "container_not_provisioned",
                 "detail": "Container not yet ready; try again shortly."},
    )
except FetchTooLarge as exc:
    return JSONResponse(
        status_code=413,
        content={"error": "file_too_large",
                 "detail": f"File is {exc.size_bytes} bytes; the fetch limit is 25 MB."},
    )
except StorageFetchError:
    return JSONResponse(status_code=502, content={"error": "storage_unavailable"})
except ValueError as exc:
    if "active profile" in str(exc):
        return JSONResponse(
            status_code=409,
            content={"error": "no_active_profile", "detail": str(exc)},
        )
    raise
```

Also add at the top of the file:

```python
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    StorageFetchError,
    ToolLookupError,
    parse_bool_param,
    parse_int_param,
    resolve_active_profile_id,
    run_sync,  # keep — still imported elsewhere
)
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_lifeatlas_router.py -v`
Expected: PASS for new tests + existing tests.
Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 8: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/router.py src/claw_proxy/app.py tests/test_lifeatlas_router.py
git commit -m "feat(lifeatlas): wire FilesHelper into router with new exception mapping"
```

---

## Phase 3 — Read-side: log events

### Task 3.1: `LogEventsHelper.list_log_events`

**Files:**
- Create: `src/claw_proxy/tools/lifeatlas/log_events.py`
- Test: `tests/test_lifeatlas_log_events.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_lifeatlas_log_events.py`:

```python
"""Tests for LogEventsHelper."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
import pytest

from claw_proxy.tools.lifeatlas.log_events import LogEventsHelper


def _supabase_with_events(rows):
    sb = MagicMock()
    chain = (
        sb.table.return_value.select.return_value.eq.return_value
        .eq.return_value.gte.return_value.order.return_value.limit.return_value
    )
    chain.execute.return_value.data = rows
    chain.eq.return_value.execute.return_value.data = rows
    return sb


def test_list_returns_events_with_has_photo():
    rows = [
        {"id": "e1", "event_type": "medicine", "occurred_at": "2026-04-29T09:00:00Z",
         "location": "home", "description": "took ibuprofen 200mg",
         "photo_path": "u/log_events/x.jpg"},
        {"id": "e2", "event_type": "food", "occurred_at": "2026-04-29T12:00:00Z",
         "location": None, "description": None, "photo_path": None},
    ]
    sb = _supabase_with_events(rows)
    helper = LogEventsHelper(sb, fetch_to_workspace=AsyncMock())
    result = helper.list_log_events(user_id="u1", profile_id="p1")
    assert result["count"] == 2
    by_id = {e["event_id"]: e for e in result["events"]}
    assert by_id["e1"]["has_photo"] is True
    assert by_id["e2"]["has_photo"] is False
    assert by_id["e1"]["event_type"] == "medicine"


def test_list_filters_by_event_type():
    rows = [{"id": "e1", "event_type": "medicine", "occurred_at": "2026-04-29T09:00:00Z",
             "location": None, "description": None, "photo_path": None}]
    sb = _supabase_with_events(rows)
    helper = LogEventsHelper(sb, fetch_to_workspace=AsyncMock())
    result = helper.list_log_events(user_id="u1", profile_id="p1", event_type="medicine")
    assert result["count"] == 1
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_log_events.py::test_list_returns_events_with_has_photo -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `src/claw_proxy/tools/lifeatlas/log_events.py`:

```python
"""Read-only helpers for LifeAtlas log_events."""

from __future__ import annotations

import mimetypes
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from claw_proxy.tools.lifeatlas.common import ToolLookupError


class LogEventsHelper:
    """Retrieve logged events (medicine, food, training, etc.) and attached photos."""

    def __init__(self, supabase, fetch_to_workspace: Callable[..., Awaitable[dict]]):
        self.db = supabase
        self.fetch_to_workspace = fetch_to_workspace

    def list_log_events(
        self,
        user_id: str,
        profile_id: str,
        event_type: str | None = None,
        days: int = 30,
        limit: int = 50,
    ) -> dict[str, Any]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query = (
            self.db.table("log_events")
            .select("id, event_type, occurred_at, location, description, photo_path")
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .gte("occurred_at", cutoff)
            .order("occurred_at", desc=True)
            .limit(limit)
        )
        if event_type:
            query = query.eq("event_type", event_type)
        rows = query.execute().data or []

        events = [
            {
                "event_id": row["id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "location": row.get("location"),
                "description": row.get("description"),
                "has_photo": bool(row.get("photo_path")),
            }
            for row in rows
        ]
        return {"events": events, "count": len(events)}
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_lifeatlas_log_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/log_events.py tests/test_lifeatlas_log_events.py
git commit -m "feat(lifeatlas): add LogEventsHelper.list_log_events"
```

---

### Task 3.2: `LogEventsHelper.get_log_event_photo`

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/log_events.py`
- Modify: `tests/test_lifeatlas_log_events.py`

- [ ] **Step 1: Append failing tests**

```python
def _supabase_with_event(row):
    sb = MagicMock()
    (sb.table.return_value.select.return_value.eq.return_value
        .eq.return_value.eq.return_value.maybe_single.return_value
        .execute.return_value.data) = row
    return sb


@pytest.mark.asyncio
async def test_get_photo_returns_image_tag():
    row = {"id": "e1", "profile_id": "p1", "photo_path": "u1/log_events/cat.jpg"}
    sb = _supabase_with_event(row)
    fetch = AsyncMock(return_value={
        "workspace_path": "lifeatlas/photos/e1_cat.jpg",
        "container_path": "/zeroclaw-data/workspace/lifeatlas/photos/e1_cat.jpg",
        "filename": "cat.jpg",
        "size_bytes": 4096,
    })
    helper = LogEventsHelper(sb, fetch_to_workspace=fetch)
    result = await helper.get_log_event_photo(user_id="u1", profile_id="p1", event_id="e1")
    assert result["workspace_path"] == "lifeatlas/photos/e1_cat.jpg"
    assert result["image_tag"] == "[IMAGE:/zeroclaw-data/workspace/lifeatlas/photos/e1_cat.jpg]"
    assert result["content_type"].startswith("image/")
    fetch.assert_awaited_once()
    args, kwargs = fetch.call_args
    assert kwargs["bucket"] == "event_photos"


@pytest.mark.asyncio
async def test_get_photo_no_attached_photo():
    row = {"id": "e1", "profile_id": "p1", "photo_path": None}
    sb = _supabase_with_event(row)
    helper = LogEventsHelper(sb, fetch_to_workspace=AsyncMock())
    result = await helper.get_log_event_photo(user_id="u1", profile_id="p1", event_id="e1")
    assert "No photo" in result["message"]
    assert result["event_id"] == "e1"


@pytest.mark.asyncio
async def test_get_photo_event_not_found():
    sb = _supabase_with_event(None)
    helper = LogEventsHelper(sb, fetch_to_workspace=AsyncMock())
    from claw_proxy.tools.lifeatlas.common import ToolLookupError
    with pytest.raises(ToolLookupError):
        await helper.get_log_event_photo(user_id="u1", profile_id="p1", event_id="missing")
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_log_events.py::test_get_photo_returns_image_tag -v`
Expected: FAIL — method not implemented.

- [ ] **Step 3: Implement**

Add to `LogEventsHelper`:

```python
    async def get_log_event_photo(
        self, user_id: str, profile_id: str, event_id: str,
    ) -> dict[str, Any]:
        row_resp = (
            self.db.table("log_events")
            .select("id, profile_id, photo_path")
            .eq("id", event_id)
            .eq("user_id", user_id)
            .eq("profile_id", profile_id)
            .maybe_single()
            .execute()
        )
        row = getattr(row_resp, "data", None)
        if not row:
            raise ToolLookupError(f"event {event_id} not found")

        if not row.get("photo_path"):
            return {"message": "No photo attached to this event", "event_id": event_id}

        original_name = posixpath.basename(row["photo_path"])
        fetched = await self.fetch_to_workspace(
            user_id=user_id,
            bucket="event_photos",
            storage_path=row["photo_path"],
            dest_subdir="photos",
            dest_basename_prefix=event_id,
            original_filename=original_name,
        )
        content_type = mimetypes.guess_type(fetched["filename"])[0] or "image/jpeg"
        return {
            "event_id": event_id,
            "filename": fetched["filename"],
            "content_type": content_type,
            "workspace_path": fetched["workspace_path"],
            "container_path": fetched["container_path"],
            "image_tag": f"[IMAGE:{fetched['container_path']}]",
        }
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_lifeatlas_log_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/log_events.py tests/test_lifeatlas_log_events.py
git commit -m "feat(lifeatlas): add LogEventsHelper.get_log_event_photo"
```

---

### Task 3.3: Wire `LogEventsHelper` into the router

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/router.py`
- Modify: `tests/test_lifeatlas_router.py`

- [ ] **Step 1: Add tests**

Append to `tests/test_lifeatlas_router.py` — assertions analogous to the FilesHelper tests in Task 2.4 but for `list_log_events` (with `event_type`, `days`, `limit`) and `get_log_event_photo` (with `event_id`, the no-photo response, and `ToolLookupError` → 404).

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_router.py -v -k log_events`
Expected: FAIL — unknown tools.

- [ ] **Step 3: Add dispatch branches**

In `_call_tool`:

```python
from claw_proxy.tools.lifeatlas.log_events import LogEventsHelper
log_events = LogEventsHelper(supabase, files_fetch)  # reuse the partial from Task 2.4

if tool_name == "list_log_events":
    profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
    return await asyncio.to_thread(
        log_events.list_log_events, user_id, profile_id,
        params.get("event_type"),
        _parse_int_param(params.get("days"), default=30, minimum=1, maximum=365, name="days"),
        _parse_int_param(params.get("limit"), default=50, minimum=1, maximum=200, name="limit"),
    )

if tool_name == "get_log_event_photo":
    event_id = _require_uuid(params, "event_id")
    profile_id = await asyncio.to_thread(_profile_id_for, supabase, user_id)
    return await log_events.get_log_event_photo(user_id, profile_id, event_id)
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_lifeatlas_router.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/router.py tests/test_lifeatlas_router.py
git commit -m "feat(lifeatlas): wire LogEventsHelper into router"
```

---

## Phase 4 — Lifecycle sweep for `workspace/lifeatlas/`

### Task 4.1: Add `find_expired_lifeatlas_files` in `containers/workspace.py`

**Files:**
- Modify: `src/claw_proxy/containers/workspace.py`
- Modify: `tests/test_workspace.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_workspace.py`:

```python
def test_find_expired_lifeatlas_files_handles_missing_dir(tmp_path):
    from claw_proxy.containers.workspace import find_expired_lifeatlas_files
    assert find_expired_lifeatlas_files(str(tmp_path)) == []


def test_find_expired_lifeatlas_files_returns_old_paths(tmp_path):
    import os, time
    from claw_proxy.containers.workspace import find_expired_lifeatlas_files

    files_dir = tmp_path / "workspace" / "lifeatlas" / "files"
    photos_dir = tmp_path / "workspace" / "lifeatlas" / "photos"
    files_dir.mkdir(parents=True)
    photos_dir.mkdir(parents=True)
    fresh = files_dir / "abc_fresh.pdf"
    fresh.write_bytes(b"x")
    stale_doc = files_dir / "abc_stale.pdf"
    stale_doc.write_bytes(b"x")
    stale_photo = photos_dir / "ev_stale.jpg"
    stale_photo.write_bytes(b"x")

    old = time.time() - 25 * 3600
    os.utime(stale_doc, (old, old))
    os.utime(stale_photo, (old, old))

    expired = find_expired_lifeatlas_files(str(tmp_path), max_age_hours=24)
    expired_set = set(expired)
    assert "workspace/lifeatlas/files/abc_stale.pdf" in expired_set
    assert "workspace/lifeatlas/photos/ev_stale.jpg" in expired_set
    assert "workspace/lifeatlas/files/abc_fresh.pdf" not in expired_set
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_workspace.py -v -k expired_lifeatlas`
Expected: FAIL — function not found.

- [ ] **Step 3: Implement**

Append to `src/claw_proxy/containers/workspace.py`:

```python
def find_expired_lifeatlas_files(volume_path: str, max_age_hours: int = 24) -> list[str]:
    """Return relative paths of fetched LifeAtlas files older than max_age_hours."""
    base = os.path.join(volume_path, "workspace", "lifeatlas")
    if not os.path.isdir(base):
        return []
    cutoff = time.time() - max_age_hours * 3600
    expired: list[str] = []
    for subdir in ("files", "photos"):
        dir_path = os.path.join(base, subdir)
        if not os.path.isdir(dir_path):
            continue
        for name in os.listdir(dir_path):
            full = os.path.join(dir_path, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    expired.append(f"workspace/lifeatlas/{subdir}/{name}")
            except FileNotFoundError:
                continue
    return expired
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/containers/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): add find_expired_lifeatlas_files for lifecycle sweep"
```

---

### Task 4.2: Wire the sweep into `_lifecycle_loop`

**Files:**
- Modify: `src/claw_proxy/app.py`

- [ ] **Step 1: Locate `_lifecycle_loop` (line ~228)**

Inside the per-user loop, after the existing `expired_temp` cleanup, add:

```python
expired_lifeatlas = await asyncio.to_thread(
    find_expired_lifeatlas_files,
    volume_path,
    export_max_age_hours,
)
await asyncio.to_thread(
    delete_files_via_busybox,
    _orchestrator.docker,
    volume_path,
    expired_lifeatlas,
)
```

Add the import at the top of `app.py`:

```python
from claw_proxy.containers.workspace import (
    delete_files_via_busybox,
    find_expired_lifeatlas_files,
    find_expired_temp_uploads,
)
```

(Adjust to match the existing import structure for the workspace module.)

- [ ] **Step 2: Add a focused test**

If `tests/test_proxy.py` (or a similar file) has a `_lifecycle_loop` test, add an assertion that `find_expired_lifeatlas_files` is called once per user during the lifecycle pass. If no such test exists, add one in `tests/test_proxy.py` that monkeypatches both functions and runs one tick.

- [ ] **Step 3: Run**

Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 4: Commit**

```bash
git add src/claw_proxy/app.py tests/test_proxy.py
git commit -m "feat(lifecycle): sweep workspace/lifeatlas/{files,photos} on each pass"
```

---

### Task 4.3: Extend `WORKSPACE_DIRS` in orchestrator

**Files:**
- Modify: `src/claw_proxy/containers/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Update `WORKSPACE_DIRS`**

In `src/claw_proxy/containers/orchestrator.py`:

```python
WORKSPACE_DIRS = (
    "workspace/temp",
    "workspace/lifeatlas/files",
    "workspace/lifeatlas/photos",
)
```

- [ ] **Step 2: Add/update a test**

In `tests/test_orchestrator.py`, find any test asserting `WORKSPACE_DIRS` is created on container provision. Update assertion to include the two new dirs.

- [ ] **Step 3: Run**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/claw_proxy/containers/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): provision workspace/lifeatlas/{files,photos} dirs"
```

---

## Phase 5 — Write side: `save_to_library`

### Task 5.1: `upload_to_health_data` in `files/storage.py`

**Files:**
- Modify: `src/claw_proxy/files/storage.py`
- Modify: `tests/test_storage.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_storage.py`:

```python
import pytest
from unittest.mock import MagicMock

from claw_proxy.files.storage import (
    HEALTH_DATA_BUCKET,
    UnsupportedHealthDataMime,
    upload_to_health_data,
)


def _admin_client(insert_data=None, insert_raises=None):
    sb = MagicMock()
    bucket = sb.storage.from_.return_value
    bucket.upload.return_value = {}
    bucket.remove.return_value = {}
    table = sb.table.return_value
    if insert_raises:
        table.insert.return_value.execute.side_effect = insert_raises
    else:
        table.insert.return_value.execute.return_value.data = (
            insert_data or [{"id": "newfile-uuid"}]
        )
    return sb


@pytest.mark.asyncio
async def test_upload_to_health_data_happy_path():
    sb = _admin_client()
    path, file_id = await upload_to_health_data(
        sb, user_id="u1", profile_id="p1",
        file_bytes=b"hello", filename="lab results.pdf",
        content_type="application/pdf",
    )
    assert path.startswith("p1/")
    assert path.endswith("_lab_results.pdf")
    assert file_id == "newfile-uuid"
    sb.storage.from_.assert_called_with(HEALTH_DATA_BUCKET)


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_mime():
    sb = _admin_client()
    with pytest.raises(UnsupportedHealthDataMime):
        await upload_to_health_data(
            sb, user_id="u1", profile_id="p1",
            file_bytes=b"# md", filename="report.md",
            content_type="text/markdown",
        )
    sb.storage.from_.return_value.upload.assert_not_called()


@pytest.mark.asyncio
async def test_upload_atomically_rolls_back_on_db_failure():
    sb = _admin_client(insert_raises=RuntimeError("insert blew up"))
    with pytest.raises(RuntimeError):
        await upload_to_health_data(
            sb, user_id="u1", profile_id="p1",
            file_bytes=b"pdf", filename="x.pdf", content_type="application/pdf",
        )
    sb.storage.from_.return_value.remove.assert_called_once()
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_storage.py -v -k health_data`
Expected: FAIL — symbols not exported.

- [ ] **Step 3: Implement in `src/claw_proxy/files/storage.py`**

Append:

```python
import re

HEALTH_DATA_BUCKET = "health_data"
HEALTH_DATA_ALLOWED_MIME = frozenset([
    "application/pdf", "text/csv",
    "image/png", "image/jpeg", "image/jpg",
])
_HEALTH_DATA_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")


class UnsupportedHealthDataMime(ValueError):
    """Content type not in the website-supported allowlist."""


def _sanitize_health_data_filename(name: str) -> str:
    cleaned = _HEALTH_DATA_FILENAME_RE.sub("_", name).strip("._")
    return cleaned or "file"


def _upload_to_health_data_sync(
    supabase_admin,
    *,
    user_id: str,
    profile_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> tuple[str, str]:
    """Mirrors supabase/functions/upload-file/index.ts in lifeatlas-core.

    See docs/superpowers/specs/2026-05-04-lifeatlas-file-access-design.md.
    Drift surface: keep this in sync with the edge function.
    """
    if content_type not in HEALTH_DATA_ALLOWED_MIME:
        raise UnsupportedHealthDataMime(content_type)

    timestamp = int(time.time() * 1000)
    safe_name = _sanitize_health_data_filename(filename)
    path = f"{profile_id}/{timestamp}_{safe_name}"

    bucket = supabase_admin.storage.from_(HEALTH_DATA_BUCKET)
    bucket.upload(path, file_bytes, {
        "content-type": content_type,
        "upsert": "false",
    })

    try:
        inserted = (
            supabase_admin.table("health_data_files")
            .insert({
                "user_id": user_id,
                "profile_id": profile_id,
                "filename": filename,           # original, matches edge function
                "file_path": path,
                "file_size": len(file_bytes),
                "content_type": content_type,
            })
            .execute()
        )
        if not getattr(inserted, "data", None):
            raise RuntimeError("health_data_files insert returned no row")
        file_id = inserted.data[0]["id"]
    except Exception:
        try:
            bucket.remove([path])
        except Exception:
            log.warning("rollback of orphaned %s failed", path, exc_info=True)
        raise
    return path, file_id


async def upload_to_health_data(
    supabase_admin,
    *,
    user_id: str,
    profile_id: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
) -> tuple[str, str]:
    return await asyncio.to_thread(
        _upload_to_health_data_sync,
        supabase_admin,
        user_id=user_id,
        profile_id=profile_id,
        file_bytes=file_bytes,
        filename=filename,
        content_type=content_type,
    )
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_storage.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/files/storage.py tests/test_storage.py
git commit -m "feat(storage): add upload_to_health_data with atomic rollback"
```

---

### Task 5.2: `SaveHelper` in `tools/lifeatlas/save.py`

**Files:**
- Create: `src/claw_proxy/tools/lifeatlas/save.py`
- Test: `tests/test_lifeatlas_save.py`

- [ ] **Step 1: Write tests**

Create `tests/test_lifeatlas_save.py`:

```python
"""Tests for SaveHelper.save_to_library."""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from claw_proxy.tools.lifeatlas.save import SaveHelper


def _orch():
    orch = MagicMock()
    orch.docker = MagicMock()
    orch.data_dir = "/data/zeroclaw"
    orch.get = AsyncMock(return_value=MagicMock(container_id="c1"))
    return orch


def _supabase_with_profile(profile_id="p1"):
    sb = MagicMock()
    (sb.table.return_value.select.return_value.eq.return_value
        .maybe_single.return_value.execute.return_value.data) = {"active_profile_id": profile_id}
    return sb


@pytest.mark.asyncio
async def test_save_happy_path(monkeypatch):
    monkeypatch.setattr(
        "claw_proxy.tools.lifeatlas.save._read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    monkeypatch.setattr(
        "claw_proxy.tools.lifeatlas.save.upload_to_health_data",
        AsyncMock(return_value=("p1/123_report.pdf", "newfile-uuid")),
    )
    monkeypatch.setattr(
        "claw_proxy.tools.lifeatlas.save.get_signed_url",
        AsyncMock(return_value="https://signed-url"),
    )

    helper = SaveHelper(_supabase_with_profile(), _orch())
    result = await helper.save_to_library(user_id="u1", workspace_path="output/report.pdf")
    assert result["success"] is True
    assert result["file_id"] == "newfile-uuid"
    assert result["signed_url"] == "https://signed-url"
    assert "[report.pdf]" in result["markdown_link"]
    assert result["markdown_link"].endswith("(https://signed-url)")


@pytest.mark.asyncio
async def test_save_rejects_unsupported_mime(monkeypatch):
    monkeypatch.setattr(
        "claw_proxy.tools.lifeatlas.save._read_workspace_file",
        AsyncMock(return_value=b"# notes"),
    )
    helper = SaveHelper(_supabase_with_profile(), _orch())
    result = await helper.save_to_library(user_id="u1", workspace_path="output/notes.md")
    assert result["success"] is False
    assert result["error_code"] == "unsupported_mime"


@pytest.mark.asyncio
async def test_save_rejects_path_traversal():
    helper = SaveHelper(_supabase_with_profile(), _orch())
    result = await helper.save_to_library(user_id="u1", workspace_path="../etc/passwd")
    assert result["success"] is False
    assert result["error_code"] == "path_invalid"


@pytest.mark.asyncio
async def test_save_returns_no_active_profile_when_resolution_fails():
    sb = MagicMock()
    (sb.table.return_value.select.return_value.eq.return_value
        .maybe_single.return_value.execute.return_value.data) = None
    # is_default fallback also returns nothing
    (sb.table.return_value.select.return_value.eq.return_value
        .eq.return_value.maybe_single.return_value.execute.return_value.data) = None
    helper = SaveHelper(sb, _orch())
    result = await helper.save_to_library(user_id="u1", workspace_path="output/r.pdf")
    assert result["success"] is False
    assert result["error_code"] == "no_active_profile"


@pytest.mark.asyncio
async def test_save_container_unavailable():
    orch = _orch()
    orch.get = AsyncMock(return_value=None)
    helper = SaveHelper(_supabase_with_profile(), orch)
    result = await helper.save_to_library(user_id="u1", workspace_path="output/r.pdf")
    assert result["success"] is False
    assert result["error_code"] == "container_unavailable"
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_save.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

Create `src/claw_proxy/tools/lifeatlas/save.py`:

```python
"""SaveHelper — agent-callable archive-to-LifeAtlas-library tool."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import posixpath
from typing import Any

from claw_proxy.containers.workspace import (
    container_path_to_relative_path,
    normalize_workspace_relative_path,
    relative_path_to_container_path,
    read_file_via_busybox,
)
from claw_proxy.files.downloads import _read_file_via_docker_cp_sync
from claw_proxy.files.storage import (
    HEALTH_DATA_ALLOWED_MIME,
    HEALTH_DATA_BUCKET,
    UnsupportedHealthDataMime,
    get_signed_url,
    upload_to_health_data,
)
from claw_proxy.tools.lifeatlas.common import (
    MAX_FETCH_SIZE,
    resolve_active_profile_id,
)

log = logging.getLogger(__name__)


def _failure(error_code: str, detail: str) -> dict[str, Any]:
    return {"success": False, "error_code": error_code, "detail": detail}


async def _read_workspace_file(orchestrator, container, user_id: str, relative_path: str) -> bytes:
    """Read a workspace file via docker cp first, busybox fallback."""
    container_path = relative_path_to_container_path(relative_path)
    volume_path = f"{orchestrator.data_dir}/{user_id}"
    try:
        return await asyncio.to_thread(
            _read_file_via_docker_cp_sync,
            orchestrator.docker, container.container_id, container_path,
        )
    except Exception:
        return await asyncio.to_thread(
            read_file_via_busybox, orchestrator.docker, volume_path, relative_path,
        )


class SaveHelper:
    def __init__(self, supabase, orchestrator):
        self.db = supabase
        self.orch = orchestrator

    async def save_to_library(self, user_id: str, workspace_path: str | None) -> dict[str, Any]:
        if not workspace_path:
            return _failure("path_invalid", "workspace_path is required")

        # Normalize: agent-relative ("output/r.pdf") and container-absolute both accepted.
        try:
            if workspace_path.startswith("/"):
                relative = container_path_to_relative_path(workspace_path)
            else:
                # Agent-relative — prepend "workspace/" if missing.
                candidate = (
                    workspace_path
                    if workspace_path.startswith("workspace/") or workspace_path == "workspace"
                    else f"workspace/{workspace_path}"
                )
                relative = normalize_workspace_relative_path(candidate)
        except ValueError as exc:
            return _failure("path_invalid", str(exc))

        container = await self.orch.get(user_id)
        if not container:
            return _failure("container_unavailable", "Container not provisioned")

        try:
            file_bytes = await _read_workspace_file(self.orch, container, user_id, relative)
        except Exception as exc:
            log.warning("save read failed user=%s: %s", user_id[:8], exc, exc_info=True)
            return _failure("container_unavailable", f"Could not read file: {exc}")

        if len(file_bytes) > MAX_FETCH_SIZE:
            return _failure("file_too_large", f"File is {len(file_bytes)} bytes; limit is {MAX_FETCH_SIZE}")

        filename = posixpath.basename(relative)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if content_type not in HEALTH_DATA_ALLOWED_MIME:
            return _failure(
                "unsupported_mime",
                f"Only PDF, CSV, PNG, and JPEG can be saved. Got {content_type}.",
            )

        try:
            profile_id = await asyncio.to_thread(resolve_active_profile_id, self.db, user_id)
        except ValueError as exc:
            return _failure("no_active_profile", str(exc))

        try:
            path, file_id = await upload_to_health_data(
                self.db, user_id=user_id, profile_id=profile_id,
                file_bytes=file_bytes, filename=filename, content_type=content_type,
            )
        except UnsupportedHealthDataMime as exc:
            return _failure("unsupported_mime", str(exc))
        except Exception as exc:
            log.warning("save upload failed user=%s: %s", user_id[:8], exc, exc_info=True)
            return _failure("storage_unavailable", f"Upload failed: {exc}")

        try:
            signed = await get_signed_url(self.db, path, bucket=HEALTH_DATA_BUCKET)
        except Exception as exc:
            log.warning("save signed-url failed user=%s: %s", user_id[:8], exc, exc_info=True)
            # File is uploaded; we just couldn't mint a URL. Still report success structurally.
            signed = ""

        log.info(
            "save success user=%s profile=%s content_type=%s size=%d",
            user_id[:8], profile_id[:8], content_type, len(file_bytes),
        )
        return {
            "success": True,
            "file_id": file_id,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(file_bytes),
            "signed_url": signed,
            "signed_url_expires_in": 3600,
            "markdown_link": f"[{filename}]({signed})" if signed else f"`{filename}`",
        }
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_lifeatlas_save.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/save.py tests/test_lifeatlas_save.py
git commit -m "feat(lifeatlas): add SaveHelper.save_to_library"
```

---

### Task 5.3: Wire `SaveHelper` into the router

**Files:**
- Modify: `src/claw_proxy/tools/lifeatlas/router.py`
- Modify: `tests/test_lifeatlas_router.py`

- [ ] **Step 1: Add tests**

In `tests/test_lifeatlas_router.py`, add tests that:
- `GET /zeroclaw/tools/lifeatlas/save_to_library?token=T&workspace_path=output/r.pdf` returns 200 with `success=true`.
- The same endpoint with an invalid path returns 200 with `success=false, error_code=path_invalid`.

(Always 200 — the tool returns structured failures, not HTTP errors.)

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_router.py -v -k save_to_library`
Expected: FAIL — unknown tool.

- [ ] **Step 3: Add dispatch branch**

In `_call_tool`:

```python
from claw_proxy.tools.lifeatlas.save import SaveHelper

save = SaveHelper(supabase, orchestrator)

if tool_name == "save_to_library":
    return await save.save_to_library(user_id, params.get("workspace_path"))
```

- [ ] **Step 4: Run**

Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/tools/lifeatlas/router.py tests/test_lifeatlas_router.py
git commit -m "feat(lifeatlas): register save_to_library route"
```

---

## Phase 6 — Drop `[SAVE:..]` tag handling

### Task 6.1: Narrow `DOWNLOAD_TAG_RE`

**Files:**
- Modify: `src/claw_proxy/files/downloads.py`
- Modify: `tests/test_downloads.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_downloads.py`:

```python
import re
from claw_proxy.files.downloads import DOWNLOAD_TAG_RE


def test_save_tag_no_longer_matches():
    assert DOWNLOAD_TAG_RE.search("[SAVE:/zeroclaw-data/foo.pdf]") is None


def test_download_tag_still_matches():
    m = DOWNLOAD_TAG_RE.search("[DOWNLOAD:/zeroclaw-data/foo.pdf]")
    assert m is not None
    assert m.group(1) == "/zeroclaw-data/foo.pdf"
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_downloads.py -v -k tag`
Expected: FAIL — current regex matches both.

- [ ] **Step 3: Update regex and `_materialize_signed_url`**

In `src/claw_proxy/files/downloads.py`:

```python
DOWNLOAD_TAG_RE = re.compile(r"\[DOWNLOAD:([^\]]+)\]")
```

The match group changes from 2 captures (`tag_type`, `path`) to 1 (just the path). Update `process_download_tags`:

Find this block:

```python
for match in reversed(list(DOWNLOAD_TAG_RE.finditer(text))):
    tag_type, container_path = match.groups()
    ...
    signed = await asyncio.wait_for(
        _materialize_signed_url(
            tag_type=tag_type,
            container_path=container_path,
            ...
        ),
        ...
    )
```

Change to:

```python
for match in reversed(list(DOWNLOAD_TAG_RE.finditer(text))):
    container_path = match.group(1)
    ...
    signed = await asyncio.wait_for(
        _materialize_signed_url(
            container_path=container_path,
            ...
        ),
        ...
    )
```

In `_materialize_signed_url`, drop the `tag_type` parameter and unconditionally call `upload_file(..., permanent=False)` — the SAVE branch is gone. The function body becomes:

```python
async def _materialize_signed_url(
    *,
    container_path: str,
    user_id: str,
    container_id: str,
    volume_path: str,
    docker_client,
    supabase_client,
) -> str:
    relative_path = container_path_to_relative_path(container_path)
    file_bytes = await asyncio.to_thread(
        _read_file_with_fallback_sync,
        docker_client, container_id, container_path, volume_path,
    )
    filename = relative_path.rsplit("/", 1)[-1]
    storage_path = await upload_file(
        supabase_client,
        user_id=user_id, file_bytes=file_bytes,
        filename=filename, content_type=_guess_content_type(filename),
        permanent=False,
    )
    return await get_signed_url(supabase_client, storage_path)
```

- [ ] **Step 4: Update existing tests that assert SAVE-tag behavior**

Search `tests/test_downloads.py` for any test mentioning "SAVE" or `permanent=True`. Either delete or update them — `[SAVE:..]` is no longer processed.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_downloads.py -v`
Expected: PASS.
Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add src/claw_proxy/files/downloads.py tests/test_downloads.py
git commit -m "refactor(downloads): drop SAVE tag handling, narrow regex to DOWNLOAD"
```

---

## Phase 7 — Skill template + retrofit

### Task 7.1: Update `SKILL.toml`

**Files:**
- Modify: `templates/default/workspace/skills/lifeatlas/SKILL.toml`
- Modify: `tests/test_lifeatlas_skill_template.py`

- [ ] **Step 1: Add test that checks new tool entries exist**

Append to `tests/test_lifeatlas_skill_template.py`:

```python
import tomllib
from pathlib import Path


def test_skill_toml_has_new_tools():
    path = Path("templates/default/workspace/skills/lifeatlas/SKILL.toml")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    assert cfg["skill"]["version"] == "0.2.0"
    tool_names = {t["name"] for t in cfg["tools"]}
    for required in (
        "list_health_data_files",
        "get_health_data_file_content",
        "list_log_events",
        "get_log_event_photo",
        "save_to_library",
    ):
        assert required in tool_names
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_lifeatlas_skill_template.py -v`
Expected: FAIL — version still 0.1.0.

- [ ] **Step 3: Edit `SKILL.toml`**

Change `version = "0.1.0"` → `version = "0.2.0"`.

Append the five new `[[tools]]` blocks (full content in spec §7 of the design doc, replicated here for engineer convenience):

```toml
[[tools]]
name = "list_health_data_files"
description = "List the user's documents (PDFs, lab results, CSVs, images) saved in their LifeAtlas library. Returns metadata only — call get_health_data_file_content for the actual content. Use this before asking the user about documents you might already have access to."
kind = "http"
command = "http://172.17.0.1:8000/zeroclaw/tools/lifeatlas/list_health_data_files?token={{LIFEATLAS_TOOL_TOKEN}}&timeline_entry_id={{timeline_entry_id}}"
[tools.args]
timeline_entry_id = "Optional UUID of a timeline entry. If set, only files attached to that entry are returned. Leave empty to list all files."

[[tools]]
name = "get_health_data_file_content"
description = "Retrieve the content of a specific document by file_id. Returns extracted text inline when available (truncated to 4000 chars by default; pass full=true for the complete text). For documents without cached extraction, returns a workspace_path you can read with pdf_read or other workspace tools."
kind = "http"
command = "http://172.17.0.1:8000/zeroclaw/tools/lifeatlas/get_health_data_file_content?token={{LIFEATLAS_TOOL_TOKEN}}&file_id={{file_id}}&full={{full}}"
[tools.args]
file_id = "UUID of the file from list_health_data_files."
full = "true to return the complete extracted text, false (default) for a truncated preview."

[[tools]]
name = "list_log_events"
description = "List the user's logged events (medicine intake, food, training, exercise, etc.) for the active profile. Each event may include a description, location, and an attached photo flag (has_photo). Use get_log_event_photo to retrieve a specific event's photo when relevant."
kind = "http"
command = "http://172.17.0.1:8000/zeroclaw/tools/lifeatlas/list_log_events?token={{LIFEATLAS_TOOL_TOKEN}}&event_type={{event_type}}&days={{days}}&limit={{limit}}"
[tools.args]
event_type = "Optional free-text filter — e.g. 'medicine', 'food', 'training', 'exercise'. Leave empty for all types."
days = "Number of recent days to include, from 1 to 365. Default: 30."
limit = "Maximum events to return, from 1 to 200. Default: 50."

[[tools]]
name = "get_log_event_photo"
description = "Retrieve the photo attached to a specific log event. Returns a workspace_path for the downloaded image and a pre-formatted image_tag (e.g. '[IMAGE:/zeroclaw-data/workspace/lifeatlas/photos/...]') that you can paste verbatim into your reply to display the photo to the user. Returns a 'no photo attached' message if the event has no photo."
kind = "http"
command = "http://172.17.0.1:8000/zeroclaw/tools/lifeatlas/get_log_event_photo?token={{LIFEATLAS_TOOL_TOKEN}}&event_id={{event_id}}"
[tools.args]
event_id = "UUID of the event from list_log_events."

[[tools]]
name = "save_to_library"
description = "Save a file from your workspace to the user's permanent LifeAtlas library, where it appears in their file list on the website. Returns a structured success/failure response: on success, includes a markdown_link you can paste into your reply. Supported file types: PDF, CSV, PNG, JPEG. For unsupported types (markdown, text, etc.), generate a PDF first or use [DOWNLOAD:] for transient sharing."
kind = "http"
command = "http://172.17.0.1:8000/zeroclaw/tools/lifeatlas/save_to_library?token={{LIFEATLAS_TOOL_TOKEN}}&workspace_path={{workspace_path}}"
[tools.args]
workspace_path = "Workspace-relative path to the file you want to save (e.g. 'output/report.pdf', 'temp/1745923844_lab.pdf'). Must resolve inside your workspace."
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_lifeatlas_skill_template.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add templates/default/workspace/skills/lifeatlas/SKILL.toml tests/test_lifeatlas_skill_template.py
git commit -m "feat(skill): add 5 new file-access tools to SKILL.toml; bump version 0.2.0"
```

---

### Task 7.2: Update `SKILL.md`

**Files:**
- Modify: `templates/default/workspace/skills/lifeatlas/SKILL.md`

- [ ] **Step 1: Append three paragraphs to `SKILL.md`**

```markdown
Use `list_health_data_files` and `get_health_data_file_content` for the user's
saved documents — lab results, PDFs they've uploaded, CSVs of health data,
images of medical records. The list tool returns metadata; the content tool
returns either extracted text (truncated to 4000 chars by default; pass
`full=true` for the rest) or a workspace path you can open with `pdf_read`.
Prefer the truncated preview first and only request `full=true` if the user's
question demands the complete text.

Use `list_log_events` and `get_log_event_photo` for the user's logged
observations — medicine intake, food entries, training sessions, exercise,
and similar events. Each event has a `has_photo` flag; if true and the photo
is relevant to the user's question, fetch it with `get_log_event_photo` and
paste the returned `image_tag` verbatim into your reply to display it.

Use `save_to_library` to persist a file from your workspace to the user's
permanent LifeAtlas library. The user will see it on the website's file
list. The tool accepts PDF, CSV, PNG, and JPEG; if you need to save other
content, generate a PDF first (e.g. with `file_write`) or use `[DOWNLOAD:..]`
for a one-time shareable link instead. The success response includes a
`markdown_link` you can paste into your reply so the user can grab the file
without leaving the chat.
```

- [ ] **Step 2: Commit**

```bash
git add templates/default/workspace/skills/lifeatlas/SKILL.md
git commit -m "docs(skill): describe new file-access and save tools in SKILL.md"
```

---

### Task 7.3: Add `render_lifeatlas_skill` (full re-render) in `containers/workspace.py`

**Why:** `render_lifeatlas_skill_token` only replaces the token placeholder. Retrofit needs to copy the full template from `templates/default/workspace/skills/lifeatlas/` into the container's volume, then run the token replacement.

**Files:**
- Modify: `src/claw_proxy/containers/workspace.py`
- Modify: `tests/test_workspace.py`

- [ ] **Step 1: Add tests**

```python
def test_read_skill_version_returns_value(tmp_path):
    from claw_proxy.containers.workspace import read_installed_skill_version
    skill_dir = tmp_path / "workspace" / "skills" / "lifeatlas"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.toml").write_text(
        '[skill]\nname = "lifeatlas"\nversion = "0.1.0"\n'
    )
    assert read_installed_skill_version(str(tmp_path)) == "0.1.0"


def test_read_skill_version_returns_none_when_missing(tmp_path):
    from claw_proxy.containers.workspace import read_installed_skill_version
    assert read_installed_skill_version(str(tmp_path)) is None


def test_render_lifeatlas_skill_overwrites_with_new_template(tmp_path, monkeypatch):
    """Full re-render copies template files into the volume and replaces token."""
    from claw_proxy.containers.workspace import render_lifeatlas_skill

    template_dir = tmp_path / "templates" / "default" / "workspace" / "skills" / "lifeatlas"
    template_dir.mkdir(parents=True)
    (template_dir / "SKILL.toml").write_text(
        '[skill]\nname = "lifeatlas"\nversion = "0.2.0"\n'
        '[[tools]]\nname = "x"\ncommand = "TOKEN={{LIFEATLAS_TOOL_TOKEN}}"\n'
    )
    (template_dir / "SKILL.md").write_text("md template")

    volume = tmp_path / "vol"
    skill_dir = volume / "workspace" / "skills" / "lifeatlas"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.toml").write_text("# old content")

    docker_client = MagicMock()
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.write_file_via_busybox",
        lambda d, v, p, c: (skill_dir / Path(p).name).write_text(
            c if isinstance(c, str) else c.decode()
        ),
    )

    render_lifeatlas_skill(
        docker_client=docker_client,
        templates_dir=str(tmp_path / "templates"),
        volume_path=str(volume),
        token="abc-token",
    )
    rendered = (skill_dir / "SKILL.toml").read_text()
    assert "version = \"0.2.0\"" in rendered
    assert "TOKEN=abc-token" in rendered
    assert (skill_dir / "SKILL.md").read_text() == "md template"
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_workspace.py -v -k skill`
Expected: FAIL — `read_installed_skill_version` / `render_lifeatlas_skill` don't exist.

- [ ] **Step 3: Implement**

Append to `src/claw_proxy/containers/workspace.py`:

```python
import tomllib


def read_installed_skill_version(volume_path: str) -> str | None:
    skill_toml = os.path.join(
        volume_path, "workspace", "skills", "lifeatlas", "SKILL.toml",
    )
    if not os.path.isfile(skill_toml):
        return None
    try:
        with open(skill_toml, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return cfg.get("skill", {}).get("version")


def read_template_skill_version(templates_dir: str) -> str | None:
    skill_toml = os.path.join(
        templates_dir, "default", "workspace", "skills", "lifeatlas", "SKILL.toml",
    )
    if not os.path.isfile(skill_toml):
        return None
    with open(skill_toml, "rb") as f:
        cfg = tomllib.load(f)
    return cfg.get("skill", {}).get("version")


def render_lifeatlas_skill(
    *,
    docker_client,
    templates_dir: str,
    volume_path: str,
    token: str,
) -> int:
    """Copy the LifeAtlas skill template files into the volume, replacing token.

    Overwrites existing SKILL.toml / SKILL.md. Used both for first-init
    rendering and for retrofit when the template version is newer than the
    installed version.
    """
    src_dir = os.path.join(
        templates_dir, "default", "workspace", "skills", "lifeatlas",
    )
    if not os.path.isdir(src_dir):
        return 0

    written = 0
    for filename in ("SKILL.toml", "SKILL.md"):
        src_path = os.path.join(src_dir, filename)
        if not os.path.isfile(src_path):
            continue
        with open(src_path, encoding="utf-8") as f:
            content = f.read().replace(LIFEATLAS_TOOL_TOKEN_PLACEHOLDER, token)
        relative = f"workspace/skills/lifeatlas/{filename}"
        write_file_via_busybox(docker_client, volume_path, relative, content)
        written += 1
    return written
```

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_workspace.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/containers/workspace.py tests/test_workspace.py
git commit -m "feat(workspace): add full skill render + version detection"
```

---

### Task 7.4: Add `_ensure_skills_current` hook to orchestrator

**Files:**
- Modify: `src/claw_proxy/containers/orchestrator.py`
- Modify: `tests/test_orchestrator.py`

- [ ] **Step 1: Add tests**

```python
@pytest.mark.asyncio
async def test_ensure_skills_current_no_op_when_versions_match(monkeypatch):
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.read_installed_skill_version",
        lambda v: "0.2.0",
    )
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.read_template_skill_version",
        lambda t: "0.2.0",
    )
    rendered = []
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.render_lifeatlas_skill",
        lambda **k: rendered.append(k),
    )
    orch = make_orchestrator(...)  # use existing test fixture
    await orch._ensure_skills_current(user_id="u1", token="T")
    assert rendered == []


@pytest.mark.asyncio
async def test_ensure_skills_current_renders_when_version_drift(monkeypatch):
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.read_installed_skill_version",
        lambda v: "0.1.0",
    )
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.read_template_skill_version",
        lambda t: "0.2.0",
    )
    rendered = []
    monkeypatch.setattr(
        "claw_proxy.containers.workspace.render_lifeatlas_skill",
        lambda **k: rendered.append(k),
    )
    orch = make_orchestrator(...)
    await orch._ensure_skills_current(user_id="u1", token="T")
    assert len(rendered) == 1
    assert rendered[0]["token"] == "T"
```

(Adapt `make_orchestrator(...)` to whatever fixture / helper `tests/test_orchestrator.py` already uses to construct the class.)

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_orchestrator.py -v -k ensure_skills_current`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Implement**

In `src/claw_proxy/containers/orchestrator.py`:

```python
from claw_proxy.containers.workspace import (
    WORKSPACE_INIT_SENTINEL,
    read_installed_skill_version,
    read_template_skill_version,
    render_lifeatlas_skill,
    render_lifeatlas_skill_token,
)


# Inside ContainerOrchestrator:

    async def _ensure_skills_current(self, user_id: str, token: str) -> None:
        """Re-render LifeAtlas skill if the template version is newer than installed."""
        volume_path = f"{self.data_dir}/{user_id}"
        installed = await asyncio.to_thread(read_installed_skill_version, volume_path)
        template = await asyncio.to_thread(read_template_skill_version, self.templates_dir)
        if installed == template:
            return
        log.info(
            "retrofit lifeatlas skill user=%s installed=%s template=%s",
            user_id[:8], installed, template,
        )
        await asyncio.to_thread(
            render_lifeatlas_skill,
            docker_client=self.docker,
            templates_dir=self.templates_dir,
            volume_path=volume_path,
            token=token,
        )
```

Call `_ensure_skills_current` from the container provision/start path. Find the existing place where `render_lifeatlas_skill_token` is called (search `render_lifeatlas_skill_token(`) and add a call to `_ensure_skills_current` BEFORE it (so token rendering happens against the freshest template).

- [ ] **Step 4: Run**

Run: `uv run pytest tests/test_orchestrator.py -v`
Expected: PASS.
Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/containers/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): retrofit lifeatlas skill on container start when version drifts"
```

---

### Task 7.5: Admin CLI command — `claw-admin skills retrofit`

**Files:**
- Create: `src/claw_proxy/cli/commands/skills.py`
- Modify: `src/claw_proxy/cli/main.py`
- Test: `tests/test_skills_retrofit.py`

- [ ] **Step 1: Write tests**

Create `tests/test_skills_retrofit.py`:

```python
"""Tests for the claw-admin skills retrofit command."""
from typer.testing import CliRunner
from unittest.mock import patch, MagicMock

from claw_proxy.cli.main import app


def test_skills_retrofit_calls_admin_endpoint():
    runner = CliRunner()
    with patch("claw_proxy.cli.commands.skills._post") as mock_post:
        mock_post.return_value = {"retrofitted": 3}
        result = runner.invoke(app, ["skills", "retrofit"])
    assert result.exit_code == 0
    assert "3" in result.stdout
    mock_post.assert_called_once()
```

(Adapt to whatever HTTP-call helper the existing CLI commands use — e.g. `claw_proxy/cli/client.py`.)

- [ ] **Step 2: Run — expect fail**

Run: `uv run pytest tests/test_skills_retrofit.py -v`
Expected: FAIL — command doesn't exist.

- [ ] **Step 3: Implement**

Create `src/claw_proxy/cli/commands/skills.py`:

```python
"""CLI: skills management."""
import typer

from claw_proxy.cli.client import post

skills_app = typer.Typer(help="Manage container skill files.")


def _post(path, **kwargs):
    return post(path, **kwargs)


@skills_app.command("retrofit")
def retrofit():
    """Re-render the LifeAtlas skill template on every running container."""
    result = _post("/claw-admin/skills/retrofit")
    typer.echo(f"Retrofitted {result.get('retrofitted', 0)} containers.")
```

Then create the matching admin endpoint in `src/claw_proxy/admin/` — find the existing pattern (e.g. how `claw-admin job` endpoints are mounted) and add a route `/claw-admin/skills/retrofit` that walks `_orchestrator.token_map` and calls `_orchestrator._ensure_skills_current(user_id, token)` for each.

In `src/claw_proxy/cli/main.py`:

```python
from claw_proxy.cli.commands.skills import skills_app

app.add_typer(skills_app, name="skills")
```

- [ ] **Step 4: Run**

Run: `uv run pytest`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/claw_proxy/cli/commands/skills.py src/claw_proxy/cli/main.py src/claw_proxy/admin/ tests/test_skills_retrofit.py
git commit -m "feat(cli): add 'claw-admin skills retrofit' for manual skill update"
```

---

## Phase 8 — Integration tests

### Task 8.1: End-to-end integration test

**Files:**
- Create: `tests/integration/test_lifeatlas_file_access.py`

- [ ] **Step 1: Author the integration test**

Create `tests/integration/test_lifeatlas_file_access.py`:

```python
"""End-to-end integration tests for LifeAtlas file access tools.

Requires real Docker + a Supabase test project (see existing integration fixtures).
"""
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_user(...):
    """Provision a user, profile, container; clean up after."""
    # Use the same fixtures as tests/integration/test_<existing>.py.
    ...


@pytest.mark.asyncio
async def test_list_health_data_files_end_to_end(seeded_user, http_client):
    user_token, user_id, profile_id = seeded_user
    # Seed a health_data_files row + storage object via the test Supabase client.
    # ...
    # Call list_health_data_files via HTTP.
    resp = await http_client.get(
        f"/zeroclaw/tools/lifeatlas/list_health_data_files?token={user_token}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1


@pytest.mark.asyncio
async def test_get_health_data_file_content_streams_to_workspace(seeded_user, http_client):
    # Seed a file WITHOUT extracted_content_for_bot.
    # Call get_health_data_file_content.
    # Assert response has workspace_path; verify file exists in the volume.
    ...


@pytest.mark.asyncio
async def test_get_log_event_photo_returns_image_tag(seeded_user, http_client):
    # Seed a log_events row + event_photos object.
    # Call get_log_event_photo. Assert image_tag, workspace_path; verify volume.
    ...


@pytest.mark.asyncio
async def test_save_to_library_inserts_health_data_files_row(seeded_user, http_client):
    # Write a small PDF into the user's workspace via the orchestrator helper.
    # Call save_to_library. Assert success=true, row exists in health_data_files,
    # storage object exists at {profile_id}/{ts}_*.pdf.
    ...


@pytest.mark.asyncio
async def test_save_to_library_rejects_markdown(seeded_user, http_client):
    # Write a markdown file. Call save_to_library.
    # Assert success=false, error_code='unsupported_mime'.
    ...
```

(Use the actual fixture symbols from existing integration tests in `tests/integration/`.)

- [ ] **Step 2: Run integration suite**

Run: `uv run pytest -m integration -v tests/integration/test_lifeatlas_file_access.py`
Expected: PASS (requires Docker running and Supabase test creds in env).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_lifeatlas_file_access.py
git commit -m "test(integration): end-to-end coverage for lifeatlas file access tools"
```

---

### Task 8.2: Lifecycle regression integration test

**Files:**
- Modify: `tests/integration/test_lifeatlas_file_access.py`

- [ ] **Step 1: Add the lifecycle test**

```python
@pytest.mark.asyncio
async def test_lifecycle_sweep_removes_old_lifeatlas_files(seeded_user, http_client):
    """Fetched files older than 24h are removed by the lifecycle pass."""
    # Fetch a file (warms the workspace path).
    # Manually mtime-age it 25h backward.
    # Trigger one lifecycle pass via the admin endpoint (or call directly).
    # Assert the file is gone.
    ...
```

- [ ] **Step 2: Run**

Run: `uv run pytest -m integration -v -k lifecycle_sweep`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_lifeatlas_file_access.py
git commit -m "test(integration): assert lifecycle sweep removes stale lifeatlas files"
```

---

## Final verification

- [ ] **Run the full default suite (excludes integration)**

Run: `uv run pytest`
Expected: every test green; the count should be roughly the original `305 passed` plus the new tests added across these tasks.

- [ ] **Run the integration suite (Docker required)**

Run: `uv run pytest -m integration`
Expected: green.

- [ ] **Manual smoke test (optional but recommended)**

Start the dev server, attach via the LifeAtlas frontend chat, and ask the agent: "List my recent lab files." Confirm the list comes back. Ask it to fetch one. Confirm it summarizes. Ask it to save a generated PDF. Confirm the file appears in the website's file list.

- [ ] **Update CLAUDE.md "Recent work"**

Append a one-line entry under `## Recent work` summarizing this implementation.

- [ ] **Final commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE): note lifeatlas file-access tools shipped"
```
