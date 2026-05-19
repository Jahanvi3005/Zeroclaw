from __future__ import annotations

import httpx

from claw_proxy.cli import client as client_module
from claw_proxy.cli.main import app


def _transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/users"):
            assert request.url.params["lookup"] == "u@x"
            return httpx.Response(200, json={"user_id": "u1"})
        if request.url.path.endswith("/containers"):
            return httpx.Response(200, json={"items": [{"user_id": "u1", "status": "ready"}]})
        if request.url.path.endswith("/containers/u1/start"):
            return httpx.Response(200, json={"status": "started"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_container_list(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "container", "list"])

    assert result.exit_code == 0
    assert '"user_id": "u1"' in result.output


def test_container_start_resolves_lookup(runner, monkeypatch):
    original = client_module.AdminApiClient
    monkeypatch.setattr(
        client_module,
        "AdminApiClient",
        lambda **kw: original(**{**kw, "transport": _transport()}),
    )

    result = runner.invoke(app, ["--token", "tok", "container", "start", "u@x"])

    assert result.exit_code == 0
    assert "started" in result.output
