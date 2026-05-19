from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ops(tmp_path):
    from claw_proxy.admin.operations import AdminOperations

    root = tmp_path / "u1" / "workspace"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text("hello", encoding="utf-8")
    (root / "notes" / "b.txt").write_text("world", encoding="utf-8")

    orchestrator = MagicMock()
    orchestrator.data_dir = str(tmp_path)

    registry = AsyncMock()
    registry.get.return_value = {"container_id": "c1", "user_id": "u1"}

    return AdminOperations(
        orchestrator=orchestrator,
        docker_client=MagicMock(),
        registry=registry,
    )


@pytest.mark.asyncio
async def test_list_workspace_files_recursive(ops):
    items = await ops.list_workspace_files("u1", path="", recursive=True)

    assert {item["path"] for item in items} == {"notes/a.md", "notes/b.txt"}


@pytest.mark.asyncio
async def test_read_workspace_file(ops):
    result = await ops.read_workspace_file("u1", path="notes/a.md")

    assert result == {"content": "hello", "encoding": "text"}


@pytest.mark.asyncio
async def test_write_workspace_file_create(ops):
    await ops.write_workspace_file("u1", path="notes/new.md", content="new", mode="create")

    result = await ops.read_workspace_file("u1", path="notes/new.md")
    assert result["content"] == "new"


@pytest.mark.asyncio
async def test_write_workspace_file_create_or_skip(ops):
    result = await ops.write_workspace_file(
        "u1",
        path="notes/a.md",
        content="x",
        mode="create_or_skip",
    )

    assert result == {"status": "skipped", "path": "notes/a.md"}


@pytest.mark.asyncio
async def test_delete_workspace_file(ops):
    result = await ops.delete_workspace_file("u1", path="notes/a.md")

    assert result == {"status": "deleted", "path": "notes/a.md"}
    remaining = await ops.list_workspace_files("u1", path="notes", recursive=False)
    assert [item["path"] for item in remaining] == ["notes/b.txt"]


@pytest.mark.asyncio
async def test_patch_workspace_file_replace_substring(ops):
    result = await ops.patch_workspace_file(
        "u1",
        path="notes/a.md",
        operations=[
            {
                "action": "replace_substring",
                "find": "hello",
                "replace": "patched",
            }
        ],
    )

    assert result == {"status": "patched", "path": "notes/a.md"}
    assert Path(ops._user_workspace_root("u1"), "notes", "a.md").read_text() == "patched"


@pytest.mark.asyncio
async def test_rejects_unsafe_path(ops):
    with pytest.raises(ValueError):
        await ops.read_workspace_file("u1", path="../escape")
