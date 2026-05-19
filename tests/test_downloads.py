import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_save_tag_no_longer_matches():
    from claw_proxy.files.downloads import DOWNLOAD_TAG_RE

    assert DOWNLOAD_TAG_RE.search("[SAVE:/zeroclaw-data/foo.pdf]") is None


def test_download_tag_still_matches():
    from claw_proxy.files.downloads import DOWNLOAD_TAG_RE

    m = DOWNLOAD_TAG_RE.search("[DOWNLOAD:/zeroclaw-data/foo.pdf]")
    assert m is not None
    assert m.group(1) == "/zeroclaw-data/foo.pdf"


class TestReadFileViaDockerCpSync:
    def test_reads_first_regular_file_when_tar_starts_with_directory(self):
        from claw_proxy.files.downloads import _read_file_via_docker_cp_sync

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            directory = tarfile.TarInfo("temp")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)

            data = b"hello"
            file_info = tarfile.TarInfo("temp/doc.txt")
            file_info.size = len(data)
            tar.addfile(file_info, io.BytesIO(data))

        mock_container = MagicMock()
        mock_container.get_archive.return_value = ([archive.getvalue()], {})
        mock_docker = MagicMock()
        mock_docker.containers.get.return_value = mock_container

        result = _read_file_via_docker_cp_sync(
            mock_docker,
            "container-123",
            "/zeroclaw-data/workspace/temp/doc.txt",
        )

        assert result == b"hello"


class TestProcessDownloadTags:
    @pytest.mark.asyncio
    async def test_processes_chat_done(self):
        from claw_proxy.files.downloads import process_download_tags

        with patch("claw_proxy.files.downloads.upload_file", new=AsyncMock(return_value="user-123/exports/123_doc.pdf")) as upload, patch(
            "claw_proxy.files.downloads.get_signed_url", new=AsyncMock(return_value="https://signed.example/doc.pdf")
        ), patch(
            "claw_proxy.files.downloads._read_file_with_fallback_sync",
            return_value=b"pdf-bytes",
        ):
            result = await process_download_tags(
                {"type": "chat.done", "fullResponse": "Here: [DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"},
                user_id="user-123",
                container_id="container-123",
                volume_path="/data/zeroclaw/user-123",
                docker_client=MagicMock(),
                supabase_client=MagicMock(),
            )

        assert result["fullResponse"] == "Here: [doc.pdf](https://signed.example/doc.pdf)"
        upload.assert_awaited_once()
        assert upload.await_args.kwargs["permanent"] is False

    @pytest.mark.asyncio
    async def test_save_tag_in_push_message_is_unchanged(self):
        from claw_proxy.files.downloads import process_download_tags

        with patch("claw_proxy.files.downloads.upload_file", new=AsyncMock()) as upload:
            result = await process_download_tags(
                {"type": "push.message", "content": "Saved [SAVE:/zeroclaw-data/workspace/output.txt]"},
                user_id="user-123",
                container_id="container-123",
                volume_path="/data/zeroclaw/user-123",
                docker_client=MagicMock(),
                supabase_client=MagicMock(),
            )

        assert result["content"] == "Saved [SAVE:/zeroclaw-data/workspace/output.txt]"
        upload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_leaves_chunk_messages_unchanged(self):
        from claw_proxy.files.downloads import process_download_tags

        msg = {"type": "chat.chunk", "content": "[DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"}
        result = await process_download_tags(
            msg,
            user_id="user-123",
            container_id="container-123",
            volume_path="/data/zeroclaw/user-123",
            docker_client=MagicMock(),
            supabase_client=MagicMock(),
        )

        assert result == msg

    @pytest.mark.asyncio
    async def test_rejects_workspace_escape(self):
        from claw_proxy.files.downloads import process_download_tags

        result = await process_download_tags(
            {"type": "chat.done", "fullResponse": "[DOWNLOAD:/zeroclaw-data/workspace/temp/../../etc/passwd]"},
            user_id="user-123",
            container_id="container-123",
            volume_path="/data/zeroclaw/user-123",
            docker_client=MagicMock(),
            supabase_client=MagicMock(),
        )

        assert result["fullResponse"] == "[DOWNLOAD:/zeroclaw-data/workspace/temp/../../etc/passwd]"

    @pytest.mark.asyncio
    async def test_logs_warning_and_leaves_tag_when_export_fails(self, caplog):
        from claw_proxy.files.downloads import process_download_tags

        with patch("claw_proxy.files.downloads.upload_file", new=AsyncMock(side_effect=RuntimeError("boom"))), caplog.at_level("WARNING"):
            result = await process_download_tags(
                {"type": "chat.done", "fullResponse": "[DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"},
                user_id="user-123",
                container_id="container-123",
                volume_path="/data/zeroclaw/user-123",
                docker_client=MagicMock(),
                supabase_client=MagicMock(),
            )

        assert result["fullResponse"] == "[DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"
        assert "Export processing failed" in caplog.text

    @pytest.mark.asyncio
    async def test_path_completely_outside_container_ignored(self):
        """A tag referencing a path outside /zeroclaw-data entirely is left as-is."""
        from claw_proxy.files.downloads import process_download_tags

        result = await process_download_tags(
            {"type": "chat.done", "fullResponse": "File: [DOWNLOAD:/etc/passwd]"},
            user_id="user-123",
            container_id="container-123",
            volume_path="/data/zeroclaw/user-123",
            docker_client=MagicMock(),
            supabase_client=MagicMock(),
        )

        assert result["fullResponse"] == "File: [DOWNLOAD:/etc/passwd]"

    @pytest.mark.asyncio
    async def test_multiple_tags_only_downloads_replaced(self):
        """Only DOWNLOAD tags are processed; SAVE tags remain plain text."""
        from claw_proxy.files.downloads import process_download_tags

        with patch("claw_proxy.files.downloads.upload_file", new=AsyncMock(side_effect=[
            "user-123/exports/111_a.pdf",
        ])) as upload, patch("claw_proxy.files.downloads.get_signed_url", new=AsyncMock(side_effect=[
            "https://signed.example/a.pdf",
        ])), patch(
            "claw_proxy.files.downloads._read_file_with_fallback_sync",
            return_value=b"data",
        ):
            result = await process_download_tags(
                {
                    "type": "chat.done",
                    "fullResponse": (
                        "First: [DOWNLOAD:/zeroclaw-data/workspace/temp/a.pdf] "
                        "Second: [SAVE:/zeroclaw-data/workspace/temp/b.txt]"
                    ),
                },
                user_id="user-123",
                container_id="container-123",
                volume_path="/data/zeroclaw/user-123",
                docker_client=MagicMock(),
                supabase_client=MagicMock(),
            )

        assert result["fullResponse"] == (
            "First: [a.pdf](https://signed.example/a.pdf) "
            "Second: [SAVE:/zeroclaw-data/workspace/temp/b.txt]"
        )
        upload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_processes_transcript_message_content(self):
        from claw_proxy.files.downloads import process_transcript_download_tags

        with patch("claw_proxy.files.downloads.upload_file", new=AsyncMock(return_value="user-123/exports/123_doc.pdf")), patch(
            "claw_proxy.files.downloads.get_signed_url", new=AsyncMock(return_value="https://signed.example/doc.pdf")
        ), patch(
            "claw_proxy.files.downloads._read_file_with_fallback_sync",
            return_value=b"pdf-bytes",
        ):
            result = await process_transcript_download_tags(
                [
                    {"role": "assistant", "content": "Here: [DOWNLOAD:/zeroclaw-data/workspace/temp/doc.pdf]"},
                    {"role": "user", "content": "thanks"},
                ],
                user_id="user-123",
                container_id="container-123",
                volume_path="/data/zeroclaw/user-123",
                docker_client=MagicMock(),
                supabase_client=MagicMock(),
            )

        assert result == [
            {"role": "assistant", "content": "Here: [doc.pdf](https://signed.example/doc.pdf)"},
            {"role": "user", "content": "thanks"},
        ]


class TestReadFileWithFallback:
    def test_uses_docker_cp_when_available(self):
        """Primary path: docker get_archive succeeds."""
        from claw_proxy.files.downloads import _read_file_with_fallback_sync

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            data = b"content"
            info = tarfile.TarInfo("doc.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        mock_container = MagicMock()
        mock_container.get_archive.return_value = ([archive.getvalue()], {})
        mock_docker = MagicMock()
        mock_docker.containers.get.return_value = mock_container

        result = _read_file_with_fallback_sync(
            mock_docker,
            "container-123",
            "/zeroclaw-data/workspace/temp/doc.txt",
            "/data/zeroclaw/user-123",
        )

        assert result == b"content"

    def test_falls_back_to_busybox_when_docker_cp_fails(self):
        """Fallback: get_archive raises, busybox read is used instead."""
        from claw_proxy.files.downloads import _read_file_with_fallback_sync

        mock_container = MagicMock()
        mock_container.get_archive.side_effect = Exception("container stopped")
        mock_docker = MagicMock()
        mock_docker.containers.get.return_value = mock_container

        with patch("claw_proxy.containers.workspace.read_file_via_busybox", return_value=b"fallback data") as mock_busybox:
            result = _read_file_with_fallback_sync(
                mock_docker,
                "container-123",
                "/zeroclaw-data/workspace/temp/doc.txt",
                "/data/zeroclaw/user-123",
            )

        assert result == b"fallback data"
        mock_busybox.assert_called_once_with(
            mock_docker,
            "/data/zeroclaw/user-123",
            "workspace/temp/doc.txt",
        )
