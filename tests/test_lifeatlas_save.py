"""Tests for SaveHelper.save_to_library."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from claw_proxy.tools.lifeatlas import save as save_module
from claw_proxy.tools.lifeatlas.save import SaveHelper


class _BusyboxExit(Exception):
    def __init__(self, exit_status):
        super().__init__(f"busybox exited {exit_status}")
        self.exit_status = exit_status


def _orch():
    orch = MagicMock()
    orch.docker = MagicMock()
    orch.data_dir = "/data/zeroclaw"
    orch.get = AsyncMock(return_value=MagicMock(container_id="c1"))
    return orch


def _supabase_with_profile(profile_id="p1"):
    sb = MagicMock()
    (
        sb.table.return_value.select.return_value.eq.return_value.maybe_single
        .return_value.execute.return_value.data
    ) = {"active_profile_id": profile_id}
    return sb


@pytest.mark.asyncio
async def test_save_happy_path(monkeypatch):
    read_file = AsyncMock(return_value=b"%PDF-1.4 ...")
    upload = AsyncMock(return_value=("p1/123_report.pdf", "newfile-uuid"))
    signed_url = AsyncMock(return_value="https://signed-url")
    monkeypatch.setattr(save_module, "_read_workspace_file", read_file)
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)
    monkeypatch.setattr(save_module, "get_signed_url", signed_url)

    supabase = _supabase_with_profile()
    orch = _orch()
    helper = SaveHelper(supabase, orch)

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/report.pdf",
    )

    assert result["success"] is True
    assert result["file_id"] == "newfile-uuid"
    assert result["filename"] == "report.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["size_bytes"] == len(b"%PDF-1.4 ...")
    assert result["signed_url"] == "https://signed-url"
    assert "[report.pdf]" in result["markdown_link"]
    assert result["markdown_link"].endswith("(https://signed-url)")

    read_file.assert_awaited_once_with(
        orch,
        orch.get.return_value,
        "u1",
        "workspace/output/report.pdf",
    )
    upload.assert_awaited_once_with(
        supabase,
        user_id="u1",
        profile_id="p1",
        file_bytes=b"%PDF-1.4 ...",
        filename="report.pdf",
        content_type="application/pdf",
    )
    signed_url.assert_awaited_once_with(
        supabase,
        "p1/123_report.pdf",
        bucket="health_data",
    )


@pytest.mark.asyncio
async def test_save_accepts_workspace_relative_path(monkeypatch):
    read_file = AsyncMock(return_value=b"a,b\n1,2\n")
    monkeypatch.setattr(save_module, "_read_workspace_file", read_file)
    monkeypatch.setattr(
        save_module,
        "upload_to_health_data",
        AsyncMock(return_value=("p1/123_report.csv", "file-csv")),
    )
    monkeypatch.setattr(save_module, "get_signed_url", AsyncMock(return_value=""))

    orch = _orch()
    helper = SaveHelper(_supabase_with_profile(), orch)

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="workspace/output/report.csv",
    )

    assert result["success"] is True
    assert result["markdown_link"] == "`report.csv`"
    read_file.assert_awaited_once_with(
        orch,
        orch.get.return_value,
        "u1",
        "workspace/output/report.csv",
    )


@pytest.mark.asyncio
async def test_save_accepts_container_absolute_path(monkeypatch):
    read_file = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    upload = AsyncMock(return_value=("p1/123_chart.png", "file-png"))
    monkeypatch.setattr(save_module, "_read_workspace_file", read_file)
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)
    monkeypatch.setattr(
        save_module,
        "get_signed_url",
        AsyncMock(return_value="https://signed-url"),
    )

    orch = _orch()
    helper = SaveHelper(_supabase_with_profile(), orch)

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="/zeroclaw-data/workspace/output/chart.png",
    )

    assert result["success"] is True
    read_file.assert_awaited_once_with(
        orch,
        orch.get.return_value,
        "u1",
        "workspace/output/chart.png",
    )
    assert upload.await_args.kwargs["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_save_rejects_unsupported_mime(monkeypatch):
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"# notes"),
    )
    upload = AsyncMock()
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/notes.md",
    )

    assert result["success"] is False
    assert result["error_code"] == "unsupported_mime"
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_rejects_path_traversal():
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="../etc/passwd",
    )

    assert result["success"] is False
    assert result["error_code"] == "path_invalid"


@pytest.mark.asyncio
async def test_save_returns_no_active_profile_when_resolution_fails(monkeypatch):
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    sb = MagicMock()
    (
        sb.table.return_value.select.return_value.eq.return_value.maybe_single
        .return_value.execute.return_value.data
    ) = None
    (
        sb.table.return_value.select.return_value.eq.return_value.eq.return_value
        .maybe_single.return_value.execute.return_value.data
    ) = None
    helper = SaveHelper(sb, _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "no_active_profile"
    assert result["detail"] == "No active LifeAtlas profile is selected"


@pytest.mark.asyncio
async def test_save_container_unavailable():
    orch = _orch()
    orch.get = AsyncMock(return_value=None)
    helper = SaveHelper(_supabase_with_profile(), orch)

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "container_unavailable"


@pytest.mark.asyncio
async def test_save_rejects_too_large_without_upload(monkeypatch):
    monkeypatch.setattr(save_module, "MAX_FETCH_SIZE", 4)
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"12345"),
    )
    upload = AsyncMock()
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "file_too_large"
    assert "limit is 4" in result["detail"]
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_workspace_file_preflights_before_docker_cp(monkeypatch):
    events = []

    def preflight_size(docker_client, volume_path, relative_path):
        events.append(("preflight", volume_path, relative_path))
        return 7

    def docker_cp(docker_client, container_id, container_path, max_size):
        events.append(("docker_cp", container_id, container_path, max_size))
        return b"content"

    def checked_busybox(*args):
        raise AssertionError("fallback should not run")

    monkeypatch.setattr(save_module, "_workspace_file_size_via_busybox", preflight_size)
    monkeypatch.setattr(save_module, "_read_file_via_docker_cp_bounded_sync", docker_cp)
    monkeypatch.setattr(save_module, "_read_file_via_checked_busybox", checked_busybox)

    orch = _orch()

    result = await save_module._read_workspace_file(
        orch,
        orch.get.return_value,
        "u1",
        "workspace/output/report.pdf",
    )

    assert result == b"content"
    assert events == [
        ("preflight", "/data/zeroclaw/u1", "workspace/output/report.pdf"),
        (
            "docker_cp",
            "c1",
            "/zeroclaw-data/workspace/output/report.pdf",
            save_module.MAX_FETCH_SIZE,
        ),
    ]


@pytest.mark.asyncio
async def test_read_workspace_file_falls_back_to_checked_busybox(monkeypatch):
    docker_cp = MagicMock(side_effect=RuntimeError("container archive unavailable"))
    checked_busybox = MagicMock(return_value=b"fallback")

    monkeypatch.setattr(
        save_module,
        "_workspace_file_size_via_busybox",
        MagicMock(return_value=8),
    )
    monkeypatch.setattr(save_module, "_read_file_via_docker_cp_bounded_sync", docker_cp)
    monkeypatch.setattr(save_module, "_read_file_via_checked_busybox", checked_busybox)

    orch = _orch()

    result = await save_module._read_workspace_file(
        orch,
        orch.get.return_value,
        "u1",
        "workspace/output/report.pdf",
    )

    assert result == b"fallback"
    checked_busybox.assert_called_once_with(
        orch.docker,
        "/data/zeroclaw/u1",
        "workspace/output/report.pdf",
        save_module.MAX_FETCH_SIZE,
    )


@pytest.mark.asyncio
async def test_preflight_oversize_prevents_read_and_upload(monkeypatch):
    monkeypatch.setattr(save_module, "MAX_FETCH_SIZE", 4)
    monkeypatch.setattr(
        save_module,
        "_workspace_file_size_via_busybox",
        MagicMock(return_value=5),
    )
    docker_cp = MagicMock()
    checked_busybox = MagicMock()
    upload = AsyncMock()
    monkeypatch.setattr(save_module, "_read_file_via_docker_cp_bounded_sync", docker_cp)
    monkeypatch.setattr(save_module, "_read_file_via_checked_busybox", checked_busybox)
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)

    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "file_too_large"
    docker_cp.assert_not_called()
    checked_busybox.assert_not_called()
    upload.assert_not_awaited()


def test_workspace_preflight_validates_resolved_regular_file_under_workspace(monkeypatch):
    calls = []

    def run_script(docker_client, *, volume_path, script, args, mode):
        calls.append(
            {
                "volume_path": volume_path,
                "script": script,
                "args": args,
                "mode": mode,
            }
        )
        return b"12\n"

    monkeypatch.setattr(save_module, "_run_busybox_script", run_script)

    size = save_module._workspace_file_size_via_busybox(
        MagicMock(),
        "/data/zeroclaw/u1",
        "workspace/output/report.pdf",
    )

    assert size == 12
    assert calls[0]["volume_path"] == "/data/zeroclaw/u1"
    assert calls[0]["args"] == ["/vol/workspace/output/report.pdf"]
    assert calls[0]["mode"] == "ro"
    assert "readlink -f" in calls[0]["script"]
    assert "/vol/workspace" in calls[0]["script"]
    assert "-f \"$resolved\"" in calls[0]["script"]


def test_checked_busybox_bounds_stdout_to_max_plus_one(monkeypatch):
    calls = []

    def run_script(docker_client, *, volume_path, script, args, mode):
        calls.append(
            {
                "volume_path": volume_path,
                "script": script,
                "args": args,
                "mode": mode,
            }
        )
        return b"12345"

    monkeypatch.setattr(save_module, "_run_busybox_script", run_script)

    with pytest.raises(save_module.WorkspaceFileTooLarge):
        save_module._read_file_via_checked_busybox(
            MagicMock(),
            "/data/zeroclaw/u1",
            "workspace/output/report.pdf",
            4,
        )

    assert calls[0]["args"] == ["/vol/workspace/output/report.pdf", "5", "4"]
    assert "head -c \"$2\"" in calls[0]["script"]
    assert "cat --" not in calls[0]["script"]


def test_bounded_docker_cp_uses_stat_size_before_reading_archive():
    import stat

    class PoisonStream:
        def __iter__(self):
            raise AssertionError("archive stream should not be consumed")

    container = MagicMock()
    container.get_archive.return_value = (
        PoisonStream(),
        {"size": 5, "mode": stat.S_IFREG | 0o644},
    )
    docker = MagicMock()
    docker.containers.get.return_value = container

    with pytest.raises(save_module.WorkspaceFileTooLarge):
        save_module._read_file_via_docker_cp_bounded_sync(
            docker,
            "c1",
            "/zeroclaw-data/workspace/output/report.pdf",
            4,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_status", [20, 21, 22])
async def test_save_maps_busybox_path_invalid_failures_to_path_invalid(
    monkeypatch,
    exit_status,
):
    def run_script(*args, **kwargs):
        raise _BusyboxExit(exit_status)

    monkeypatch.setattr(save_module, "_run_busybox_script", run_script)
    upload = AsyncMock()
    monkeypatch.setattr(save_module, "upload_to_health_data", upload)
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "path_invalid"
    assert result["detail"] == "Workspace path is missing or unavailable"
    upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_read_failure_detail_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(side_effect=RuntimeError("secret docker details")),
    )
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "container_unavailable"
    assert result["detail"] == "Could not read workspace file"
    assert "secret" not in result["detail"]


@pytest.mark.asyncio
async def test_save_storage_database_failure_maps_to_db_unavailable(monkeypatch):
    from claw_proxy.files.storage import HealthDataDatabaseError

    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    monkeypatch.setattr(
        save_module,
        "upload_to_health_data",
        AsyncMock(side_effect=HealthDataDatabaseError("insert leaked secret")),
    )
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "db_unavailable"
    assert result["detail"] == "Could not save file metadata to LifeAtlas"
    assert "secret" not in result["detail"]


@pytest.mark.asyncio
async def test_save_profile_db_failure_detail_is_sanitized(monkeypatch):
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    monkeypatch.setattr(
        save_module,
        "resolve_active_profile_id",
        MagicMock(side_effect=RuntimeError("database password leaked")),
    )
    helper = SaveHelper(MagicMock(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "db_unavailable"
    assert result["detail"] == "Could not resolve active LifeAtlas profile"
    assert "password" not in result["detail"]


@pytest.mark.asyncio
async def test_save_upload_failure_detail_is_sanitized(monkeypatch):
    from claw_proxy.files.storage import HealthDataStorageUploadError

    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    monkeypatch.setattr(
        save_module,
        "upload_to_health_data",
        AsyncMock(side_effect=HealthDataStorageUploadError("storage secret leaked")),
    )
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/r.pdf",
    )

    assert result["success"] is False
    assert result["error_code"] == "storage_unavailable"
    assert result["detail"] == "Could not save file to LifeAtlas storage"
    assert "secret" not in result["detail"]


@pytest.mark.asyncio
async def test_signed_url_failure_still_succeeds_with_inline_code_fallback(monkeypatch):
    monkeypatch.setattr(
        save_module,
        "_read_workspace_file",
        AsyncMock(return_value=b"%PDF-1.4 ..."),
    )
    monkeypatch.setattr(
        save_module,
        "upload_to_health_data",
        AsyncMock(return_value=("p1/123_report.pdf", "newfile-uuid")),
    )
    monkeypatch.setattr(
        save_module,
        "get_signed_url",
        AsyncMock(side_effect=RuntimeError("signed url secret")),
    )
    helper = SaveHelper(_supabase_with_profile(), _orch())

    result = await helper.save_to_library(
        user_id="u1",
        workspace_path="output/report.pdf",
    )

    assert result["success"] is True
    assert result["signed_url"] == ""
    assert result["markdown_link"] == "`report.pdf`"
