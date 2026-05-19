from __future__ import annotations

import httpx

from claw_proxy.cli import client as client_module
from claw_proxy.cli.main import app


def _transport():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/admins"):
            return httpx.Response(200, json={"items": [{"id": "a1", "name": "Ada"}]})
        if path.endswith("/admins/static-tokens") and request.method == "POST":
            return httpx.Response(200, json={"token": "new-token", "label": "ops"})
        if path.endswith("/orphans"):
            return httpx.Response(200, json={"items": [{"container_id": "c1"}]})
        if path.endswith("/jobs"):
            return httpx.Response(200, json={"items": [{"id": "j1", "status": "created"}]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_admin_list(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "admin", "admin-list"])

    assert result.exit_code == 0
    assert '"id": "a1"' in result.output


def test_admin_token_create(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "admin", "token-create", "ops"])

    assert result.exit_code == 0
    assert "new-token" in result.output


def test_orphan_list(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "orphan", "list"])

    assert result.exit_code == 0
    assert "c1" in result.output


def test_job_list(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "job", "list"])

    assert result.exit_code == 0
    assert "j1" in result.output
