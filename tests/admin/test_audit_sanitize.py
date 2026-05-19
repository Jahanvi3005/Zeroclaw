from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_secret_value_redacted_for_config_set():
    from claw_proxy.admin.audit import sanitize_params

    schema_cache = AsyncMock()
    schema_cache.is_secret_path.side_effect = lambda cid, p: p.endswith(".api-key")
    params = {
        "updates": [
            {"path": "llm.api-key", "value": "secret123"},
            {"path": "llm.model", "value": "claude"},
        ]
    }

    out = await sanitize_params(
        "config.set",
        params,
        container_id="c1",
        schema_cache=schema_cache,
    )

    secret_upd = next(u for u in out["updates"] if u["path"] == "llm.api-key")
    assert secret_upd["value"] == "[REDACTED]"
    assert secret_upd["value_length"] == len("secret123")
    plain_upd = next(u for u in out["updates"] if u["path"] == "llm.model")
    assert plain_upd["value"] == "claude"


@pytest.mark.asyncio
async def test_workspace_write_content_replaced_by_size_and_sha():
    from claw_proxy.admin.audit import sanitize_params

    params = {"path": "notes/x.md", "content": "hello world"}
    out = await sanitize_params(
        "workspace.write",
        params,
        container_id=None,
        schema_cache=None,
    )

    assert "content" not in out
    assert out["content_meta"]["size"] == len("hello world")
    assert "sha256" in out["content_meta"]


@pytest.mark.asyncio
async def test_unknown_action_passes_through():
    from claw_proxy.admin.audit import sanitize_params

    out = await sanitize_params(
        "container.restart",
        {"a": 1},
        container_id=None,
        schema_cache=None,
    )

    assert out == {"a": 1}
