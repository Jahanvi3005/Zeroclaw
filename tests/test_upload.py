import io
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tests.conftest import FAKE_USER


@pytest.fixture
def mock_orchestrator():
    orch = AsyncMock()
    orch.get = AsyncMock(
        return_value=MagicMock(
            container_id="c_abc",
            ws_url="ws://localhost:9000/ws/chat",
            http_url="http://localhost:9000",
            bearer_token="tok_secret",
            status="ready",
            current_session_id=None,
        )
    )
    orch.data_dir = "/data/zeroclaw"
    orch.host_data_dir = "/host/zeroclaw"
    orch.docker = MagicMock()
    return orch


@pytest.fixture
def mock_auth():
    async def auth(credentials=None):
        return FAKE_USER.copy()

    return auth


@pytest.fixture
def app(mock_orchestrator, mock_auth):
    from claw_proxy.files.upload import create_upload_router

    app = FastAPI()
    router = create_upload_router(orchestrator=mock_orchestrator, auth_fn=mock_auth)
    app.include_router(router)
    return app


class TestUploadFile:
    def test_upload_pdf_returns_annotation(self, app, mock_orchestrator):
        client = TestClient(app)

        with (
            patch("claw_proxy.files.upload.write_file_via_busybox") as mock_write,
            patch("claw_proxy.files.upload.time.time", return_value=1712764800.0),
        ):
            response = client.post(
                "/workspace/files",
                files={"file": ("report.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
                headers={"Authorization": "Bearer valid-jwt"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body == {
            "filename": "report.pdf",
            "path": "/zeroclaw-data/workspace/temp/1712764800000_report.pdf",
            "annotation": "[PDF:/zeroclaw-data/workspace/temp/1712764800000_report.pdf]",
        }
        mock_write.assert_called_once_with(
            mock_orchestrator.docker,
            f"/host/zeroclaw/{FAKE_USER['id']}",
            "workspace/temp/1712764800000_report.pdf",
            b"%PDF",
            local_volume_path=f"/data/zeroclaw/{FAKE_USER['id']}",
        )

    def test_upload_image_returns_image_annotation(self, app):
        client = TestClient(app)

        with (
            patch("claw_proxy.files.upload.write_file_via_busybox"),
            patch("claw_proxy.files.upload.time.time", return_value=1712764800.0),
        ):
            response = client.post(
                "/workspace/files",
                files={"file": ("photo.png", io.BytesIO(b"\x89PNG"), "image/png")},
                headers={"Authorization": "Bearer valid-jwt"},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["annotation"] == "[IMAGE:/zeroclaw-data/workspace/temp/1712764800000_photo.png]"

    def test_rejects_unsupported_mime_type(self, app):
        client = TestClient(app)

        response = client.post(
            "/workspace/files",
            files={"file": ("archive.zip", io.BytesIO(b"PK"), "application/zip")},
            headers={"Authorization": "Bearer valid-jwt"},
        )

        assert response.status_code == 400
        assert "unsupported" in response.json()["detail"].lower()

    def test_rejects_directory_filename(self, app):
        client = TestClient(app)

        response = client.post(
            "/workspace/files",
            files={"file": ("../etc/passwd", io.BytesIO(b"bad"), "text/plain")},
            headers={"Authorization": "Bearer valid-jwt"},
        )

        assert response.status_code == 400

    def test_rejects_too_large(self, app):
        client = TestClient(app)
        big_content = b"x" * (25 * 1024 * 1024 + 1)

        response = client.post(
            "/workspace/files",
            files={"file": ("big.md", io.BytesIO(big_content), "text/markdown")},
            headers={"Authorization": "Bearer valid-jwt"},
        )

        assert response.status_code == 400
        assert "large" in response.json()["detail"].lower()

    def test_no_container_returns_404(self, app, mock_orchestrator):
        mock_orchestrator.get = AsyncMock(return_value=None)
        client = TestClient(app)

        response = client.post(
            "/workspace/files",
            files={"file": ("report.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
            headers={"Authorization": "Bearer valid-jwt"},
        )

        assert response.status_code == 404

    def test_write_failure_returns_502(self, app):
        client = TestClient(app)

        with patch("claw_proxy.files.upload.write_file_via_busybox", side_effect=RuntimeError("boom")):
            response = client.post(
                "/workspace/files",
                files={"file": ("report.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
                headers={"Authorization": "Bearer valid-jwt"},
            )

        assert response.status_code == 502
