from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_supabase():
    client = MagicMock()
    client.storage.from_.return_value = MagicMock()
    return client


def _health_data_admin_client(insert_data=None, insert_raises=None):
    sb = MagicMock()
    bucket = sb.storage.from_.return_value
    bucket.upload.return_value = {}
    bucket.remove.return_value = {}
    table = sb.table.return_value
    if insert_raises:
        table.insert.return_value.execute.side_effect = insert_raises
    else:
        table.insert.return_value.execute.return_value.data = (
            [{"id": "newfile-uuid"}] if insert_data is None else insert_data
        )
    return sb


def _health_data_admin_client_with_upload_failure():
    sb = _health_data_admin_client()
    sb.storage.from_.return_value.upload.side_effect = RuntimeError("upload blew up")
    return sb


def _health_data_admin_client_with_bucket_lookup_failure():
    sb = _health_data_admin_client()
    sb.storage.from_.side_effect = RuntimeError("bucket lookup blew up")
    return sb


class TestEnsureBucketExists:
    @pytest.mark.asyncio
    async def test_creates_bucket_when_missing(self, mock_supabase):
        from claw_proxy.files.storage import ensure_bucket_exists

        mock_supabase.storage.list_buckets.return_value = []

        await ensure_bucket_exists(mock_supabase)

        mock_supabase.storage.create_bucket.assert_called_once_with(
            "workspace_files",
            options={"public": False},
        )

    @pytest.mark.asyncio
    async def test_skips_create_when_bucket_already_exists_as_dict(self, mock_supabase):
        from claw_proxy.files.storage import ensure_bucket_exists

        mock_supabase.storage.list_buckets.return_value = [{"name": "workspace_files"}]

        await ensure_bucket_exists(mock_supabase)

        mock_supabase.storage.create_bucket.assert_not_called()


class TestUploadFile:
    @pytest.mark.asyncio
    async def test_upload_file_uses_async_wrapper(self, mock_supabase):
        from claw_proxy.files.storage import upload_file

        bucket = mock_supabase.storage.from_.return_value

        path = await upload_file(
            mock_supabase,
            user_id="user-123",
            file_bytes=b"hello",
            filename="doc.pdf",
            content_type="application/pdf",
            permanent=False,
        )

        assert path.startswith("user-123/exports/")
        bucket.upload.assert_called_once()
        call = bucket.upload.call_args
        assert call.args[0] == path
        assert call.args[1] == b"hello"
        assert call.args[2] == {"content-type": "application/pdf"}

    @pytest.mark.asyncio
    async def test_permanent_upload_allows_overwrite(self, mock_supabase):
        from claw_proxy.files.storage import upload_file

        bucket = mock_supabase.storage.from_.return_value

        path = await upload_file(
            mock_supabase,
            user_id="user-123",
            file_bytes=b"hello",
            filename="doc.pdf",
            content_type="application/pdf",
            permanent=True,
        )

        assert path == "user-123/permanent/doc.pdf"
        bucket.upload.assert_called_once_with(
            "user-123/permanent/doc.pdf",
            b"hello",
            {"content-type": "application/pdf", "upsert": "true"},
        )


class TestGetSignedUrl:
    @pytest.mark.asyncio
    async def test_returns_signed_url(self, mock_supabase, monkeypatch):
        from claw_proxy.files import storage

        bucket = mock_supabase.storage.from_.return_value
        bucket.create_signed_url.return_value = {"signedURL": "https://example.com/signed"}
        monkeypatch.setattr(storage, "SUPABASE_PUBLIC_URL", "https://example.com")

        result = await storage.get_signed_url(mock_supabase, "user-123/exports/123_doc.pdf", 3600)

        assert result == "https://example.com/signed"

    @pytest.mark.asyncio
    async def test_uses_default_bucket(self, mock_supabase, monkeypatch):
        from claw_proxy.files import storage

        bucket = mock_supabase.storage.from_.return_value
        bucket.create_signed_url.return_value = {
            "signedURL": (
                "http://internal:54321/storage/v1/object/sign/"
                "workspace_files/p/f.pdf?token=x"
            )
        }
        monkeypatch.setattr(storage, "SUPABASE_PUBLIC_URL", "https://storage.example")

        url = await storage.get_signed_url(mock_supabase, "p/f.pdf")

        assert url == (
            "https://storage.example/storage/v1/object/sign/"
            "workspace_files/p/f.pdf?token=x"
        )
        mock_supabase.storage.from_.assert_called_with(storage.BUCKET_NAME)

    @pytest.mark.asyncio
    async def test_uses_custom_bucket(self, mock_supabase, monkeypatch):
        from claw_proxy.files import storage

        bucket = mock_supabase.storage.from_.return_value
        bucket.create_signed_url.return_value = {
            "signedURL": (
                "http://internal:54321/storage/v1/object/sign/"
                "health_data/p/f.pdf?token=y"
            )
        }
        monkeypatch.setattr(storage, "SUPABASE_PUBLIC_URL", "https://storage.example")

        url = await storage.get_signed_url(mock_supabase, "p/f.pdf", bucket="health_data")

        assert url == (
            "https://storage.example/storage/v1/object/sign/"
            "health_data/p/f.pdf?token=y"
        )
        mock_supabase.storage.from_.assert_called_with("health_data")

    def test_rewrites_signed_url_to_public_supabase_origin(self):
        from claw_proxy.files.storage import _rewrite_signed_url_for_public_base

        result = _rewrite_signed_url_for_public_base(
            (
                "http://host.docker.internal:54321/storage/v1/object/sign/"
                "workspace_files/user-123/exports/doc.pdf?token=abc"
            ),
            "http://192.168.122.13:54321",
        )

        assert result == (
            "http://192.168.122.13:54321/storage/v1/object/sign/"
            "workspace_files/user-123/exports/doc.pdf?token=abc"
        )

    def test_rewrites_relative_signed_url_to_public_supabase_origin(self):
        from claw_proxy.files.storage import _rewrite_signed_url_for_public_base

        result = _rewrite_signed_url_for_public_base(
            "/storage/v1/object/sign/workspace_files/user-123/exports/doc.pdf?token=abc",
            "http://192.168.122.13:54321",
        )

        assert result == (
            "http://192.168.122.13:54321/storage/v1/object/sign/"
            "workspace_files/user-123/exports/doc.pdf?token=abc"
        )


class TestDeleteExpiredExports:
    @pytest.mark.asyncio
    async def test_deletes_old_exports_by_timestamp_prefix(self, mock_supabase):
        from claw_proxy.files.storage import delete_expired_exports

        bucket = mock_supabase.storage.from_.return_value
        bucket.list.return_value = [
            {"name": "1000_old.pdf"},
            {"name": "9999999999999_new.pdf"},
            {"name": "notimestamp.txt"},
        ]

        deleted = await delete_expired_exports(mock_supabase, "user-123", max_age_hours=24)

        assert deleted == 1
        bucket.remove.assert_called_once_with(["user-123/exports/1000_old.pdf"])


class TestUploadToHealthData:
    @pytest.mark.asyncio
    async def test_upload_to_health_data_happy_path(self):
        from claw_proxy.files.storage import HEALTH_DATA_BUCKET, upload_to_health_data

        sb = _health_data_admin_client()

        path, file_id = await upload_to_health_data(
            sb,
            user_id="u1",
            profile_id="p1",
            file_bytes=b"hello",
            filename="lab results.pdf",
            content_type="application/pdf",
        )

        assert path.startswith("p1/")
        assert path.endswith("_lab_results.pdf")
        assert file_id == "newfile-uuid"
        bucket = sb.storage.from_.return_value
        sb.storage.from_.assert_called_with(HEALTH_DATA_BUCKET)
        bucket.upload.assert_called_once_with(
            path,
            b"hello",
            {"content-type": "application/pdf", "upsert": "false"},
        )
        sb.table.assert_called_once_with("health_data_files")
        sb.table.return_value.insert.assert_called_once_with(
            {
                "user_id": "u1",
                "profile_id": "p1",
                "filename": "lab results.pdf",
                "file_path": path,
                "file_size": 5,
                "content_type": "application/pdf",
            }
        )

    @pytest.mark.asyncio
    async def test_upload_to_health_data_rejects_unsupported_mime(self):
        from claw_proxy.files.storage import UnsupportedHealthDataMime, upload_to_health_data

        sb = _health_data_admin_client()

        with pytest.raises(UnsupportedHealthDataMime):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="p1",
                file_bytes=b"# md",
                filename="report.md",
                content_type="text/markdown",
            )

        sb.storage.from_.return_value.upload.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_to_health_data_atomically_rolls_back_on_db_failure(self):
        from claw_proxy.files.storage import HealthDataDatabaseError, upload_to_health_data

        sb = _health_data_admin_client(insert_raises=RuntimeError("insert blew up"))

        with pytest.raises(HealthDataDatabaseError):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="p1",
                file_bytes=b"pdf",
                filename="x.pdf",
                content_type="application/pdf",
            )

        bucket = sb.storage.from_.return_value
        uploaded_path = bucket.upload.call_args.args[0]
        bucket.remove.assert_called_once_with([uploaded_path])

    @pytest.mark.asyncio
    async def test_upload_to_health_data_wraps_no_insert_row_as_database_error(self):
        from claw_proxy.files.storage import HealthDataDatabaseError, upload_to_health_data

        sb = _health_data_admin_client(insert_data=[])

        with pytest.raises(HealthDataDatabaseError):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="p1",
                file_bytes=b"pdf",
                filename="x.pdf",
                content_type="application/pdf",
            )

        bucket = sb.storage.from_.return_value
        uploaded_path = bucket.upload.call_args.args[0]
        bucket.remove.assert_called_once_with([uploaded_path])

    @pytest.mark.asyncio
    async def test_upload_to_health_data_wraps_bucket_upload_failure(self):
        from claw_proxy.files.storage import (
            HealthDataStorageUploadError,
            upload_to_health_data,
        )

        sb = _health_data_admin_client_with_upload_failure()

        with pytest.raises(HealthDataStorageUploadError):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="p1",
                file_bytes=b"pdf",
                filename="x.pdf",
                content_type="application/pdf",
            )

        sb.table.assert_not_called()
        sb.storage.from_.return_value.remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_to_health_data_wraps_bucket_lookup_failure(self):
        from claw_proxy.files.storage import (
            HealthDataStorageUploadError,
            upload_to_health_data,
        )

        sb = _health_data_admin_client_with_bucket_lookup_failure()

        with pytest.raises(HealthDataStorageUploadError):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="p1",
                file_bytes=b"pdf",
                filename="x.pdf",
                content_type="application/pdf",
            )

        sb.table.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_to_health_data_sanitizes_filename_under_profile_prefix(self):
        from claw_proxy.files.storage import upload_to_health_data

        sb = _health_data_admin_client()

        path, _ = await upload_to_health_data(
            sb,
            user_id="u1",
            profile_id="p1",
            file_bytes=b"pdf",
            filename="../lab results!!.pdf",
            content_type="application/pdf",
        )

        assert path.startswith("p1/")
        assert path.endswith("_lab_results__.pdf")
        assert path.count("/") == 1

    @pytest.mark.asyncio
    async def test_upload_to_health_data_redacts_rollback_failure_log(self, caplog):
        from claw_proxy.files.storage import HealthDataDatabaseError, upload_to_health_data

        sb = _health_data_admin_client(insert_raises=RuntimeError("insert blew up"))
        bucket = sb.storage.from_.return_value
        bucket.remove.side_effect = RuntimeError("remove blew up")

        with pytest.raises(HealthDataDatabaseError):
            await upload_to_health_data(
                sb,
                user_id="u1",
                profile_id="profile-secret",
                file_bytes=b"pdf",
                filename="lab results.pdf",
                content_type="application/pdf",
            )

        messages = [record.getMessage() for record in caplog.records]
        assert "profile-secret" not in "\n".join(messages)
        assert "lab_results.pdf" not in "\n".join(messages)
        assert any(record.exc_info for record in caplog.records)
