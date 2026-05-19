from __future__ import annotations

import httpx
import pytest

from claw_proxy.cli.client import AdminApiClient


@pytest.mark.asyncio
async def test_get_sets_bearer_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["path"] = request.url.path
        return httpx.Response(200, json={"items": []})

    transport = httpx.MockTransport(handler)
    client = AdminApiClient(api_url="http://x", token="tok", transport=transport)
    result = await client.get("/containers")

    assert captured["auth"] == "Bearer tok"
    assert captured["path"] == "/claw-admin/containers"
    assert result == {"items": []}
