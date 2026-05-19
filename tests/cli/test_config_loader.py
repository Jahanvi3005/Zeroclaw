from __future__ import annotations

from claw_proxy.cli.config import load_cli_config


def test_flags_take_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_ADMIN_API_URL", "http://env-host:8000")
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text('api_url = "http://file-host:9000"\ntoken_file = "/tmp/tk"\n')

    res = load_cli_config(
        api_url="http://flag-host:1",
        token=None,
        token_file=None,
        config_path=str(cfg_path),
    )
    assert res.api_url == "http://flag-host:1"


def test_env_falls_back_when_no_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAW_ADMIN_API_URL", "http://env-host:8000")

    res = load_cli_config(
        api_url=None,
        token=None,
        token_file=None,
        config_path=str(tmp_path / "missing.toml"),
    )
    assert res.api_url == "http://env-host:8000"


def test_token_loaded_from_file(tmp_path):
    f = tmp_path / "tk"
    f.write_text("file-token\n", encoding="utf-8")

    res = load_cli_config(
        api_url=None,
        token=None,
        token_file=str(f),
        config_path=str(tmp_path / "missing.toml"),
    )
    assert res.token == "file-token"
