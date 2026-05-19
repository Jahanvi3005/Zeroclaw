"""Tests for the claw-admin skills retrofit command."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from typer.testing import CliRunner

from claw_proxy.cli.main import app


def test_skills_retrofit_calls_admin_endpoint(tmp_path):
    runner = CliRunner()
    with patch(
        "claw_proxy.cli.commands.skills._post",
        new_callable=AsyncMock,
    ) as mock_post:
        mock_post.return_value = {
            "checked": 5,
            "updated": 0,
            "skipped": 5,
            "failed": 0,
        }
        result = runner.invoke(
            app,
            [
                "--config",
                str(tmp_path / "missing.toml"),
                "--token",
                "tok",
                "skills",
                "retrofit",
            ],
        )

    assert result.exit_code == 0
    assert "Checked 5 containers. Updated 0, skipped 5" in result.stdout
    assert "Retrofitted" not in result.stdout
    mock_post.assert_awaited_once()
    assert mock_post.await_args.args[1] == "/skills/retrofit"
