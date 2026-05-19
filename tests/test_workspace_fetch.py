from unittest.mock import AsyncMock, MagicMock

import pytest

from claw_proxy.files.workspace_fetch import fetch_to_workspace
from claw_proxy.tools.lifeatlas.common import (
    ContainerNotProvisioned,
    FetchTooLarge,
    MAX_FETCH_SIZE,
    StorageFetchError,
)


@pytest.fixture
def mock_orchestrator():
    orchestrator = AsyncMock()
    orchestrator.get = AsyncMock(return_value=MagicMock(container_id="container-abc"))
    orchestrator.data_dir = "/zeroclaw-data"
    orchestrator.host_data_dir = "/data/zeroclaw"
    orchestrator.docker = MagicMock()
    return orchestrator


def mock_supabase_download(content: bytes):
    supabase = MagicMock()
    bucket = supabase.storage.from_.return_value
    bucket.download.return_value = content
    return supabase


@pytest.mark.asyncio
async def test_fetch_writes_bytes_to_workspace(monkeypatch, mock_orchestrator):
    written = {}

    def fake_write_file_via_busybox(
        docker, volume_path, relative_path, content, *, local_volume_path=None
    ):
        written["docker"] = docker
        written["volume_path"] = volume_path
        written["relative_path"] = relative_path
        written["content"] = content
        written["local_volume_path"] = local_volume_path

    monkeypatch.setattr(
        "claw_proxy.files.workspace_fetch.write_file_via_busybox",
        fake_write_file_via_busybox,
    )
    supabase = mock_supabase_download(b"hello world")

    result = await fetch_to_workspace(
        orchestrator=mock_orchestrator,
        supabase=supabase,
        user_id="user-123",
        bucket="lifeatlas",
        storage_path="private/user-123/lab_results.pdf",
        dest_subdir="files",
        dest_basename_prefix="abc-123",
        original_filename="lab_results.pdf",
    )

    assert result["workspace_path"] == "lifeatlas/files/abc-123_lab_results.pdf"
    assert (
        result["container_path"]
        == "/zeroclaw-data/workspace/lifeatlas/files/abc-123_lab_results.pdf"
    )
    assert result["filename"] == "lab_results.pdf"
    assert result["size_bytes"] == 11
    assert written["docker"] is mock_orchestrator.docker
    assert written["volume_path"] == "/data/zeroclaw/user-123"
    assert written["local_volume_path"] == "/zeroclaw-data/user-123"
    assert written["relative_path"] == "workspace/lifeatlas/files/abc-123_lab_results.pdf"
    assert written["content"] == b"hello world"


@pytest.mark.asyncio
async def test_fetch_raises_when_no_container(mock_orchestrator):
    mock_orchestrator.get.return_value = None
    supabase = mock_supabase_download(b"hello world")

    with pytest.raises(ContainerNotProvisioned):
        await fetch_to_workspace(
            orchestrator=mock_orchestrator,
            supabase=supabase,
            user_id="user-123",
            bucket="lifeatlas",
            storage_path="private/user-123/lab_results.pdf",
            dest_subdir="files",
            dest_basename_prefix="abc-123",
            original_filename="lab_results.pdf",
        )


@pytest.mark.asyncio
async def test_fetch_raises_when_too_large(monkeypatch, mock_orchestrator):
    mock_write = MagicMock()
    monkeypatch.setattr(
        "claw_proxy.files.workspace_fetch.write_file_via_busybox",
        mock_write,
    )
    supabase = mock_supabase_download(b"x" * (MAX_FETCH_SIZE + 1))

    with pytest.raises(FetchTooLarge):
        await fetch_to_workspace(
            orchestrator=mock_orchestrator,
            supabase=supabase,
            user_id="user-123",
            bucket="lifeatlas",
            storage_path="private/user-123/large.pdf",
            dest_subdir="files",
            dest_basename_prefix="abc-123",
            original_filename="large.pdf",
        )
    mock_write.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_rejects_bad_dest_subdir(mock_orchestrator):
    supabase = mock_supabase_download(b"hello world")

    with pytest.raises(ValueError, match="dest_subdir"):
        await fetch_to_workspace(
            orchestrator=mock_orchestrator,
            supabase=supabase,
            user_id="user-123",
            bucket="lifeatlas",
            storage_path="private/user-123/lab_results.pdf",
            dest_subdir="../files",
            dest_basename_prefix="abc-123",
            original_filename="lab_results.pdf",
        )


@pytest.mark.asyncio
async def test_fetch_rejects_bad_dest_basename_prefix(mock_orchestrator):
    supabase = mock_supabase_download(b"hello world")

    with pytest.raises(ValueError, match="dest_basename_prefix"):
        await fetch_to_workspace(
            orchestrator=mock_orchestrator,
            supabase=supabase,
            user_id="user-123",
            bucket="lifeatlas",
            storage_path="private/user-123/lab_results.pdf",
            dest_subdir="files",
            dest_basename_prefix="abc/123",
            original_filename="lab_results.pdf",
        )


@pytest.mark.asyncio
async def test_fetch_translates_storage_failure(mock_orchestrator):
    supabase = MagicMock()
    supabase.storage.from_.return_value.download.side_effect = RuntimeError("missing")

    with pytest.raises(StorageFetchError):
        await fetch_to_workspace(
            orchestrator=mock_orchestrator,
            supabase=supabase,
            user_id="user-123",
            bucket="lifeatlas",
            storage_path="private/user-123/missing.pdf",
            dest_subdir="files",
            dest_basename_prefix="abc-123",
            original_filename="missing.pdf",
        )
