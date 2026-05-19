from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def ops(tmp_path):
    from claw_proxy.admin.operations import AdminOperations

    user_dir = tmp_path / "u1"
    config_dir = user_dir / ".zeroclaw"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(
        """
[llm]
provider = "openrouter"
api-key = "secret-xyz"
model = "gpt-4o"
""".strip(),
        encoding="utf-8",
    )

    schema_cache = AsyncMock()
    schema_cache.is_secret_path.side_effect = lambda cid, path: path.endswith(".api-key")

    orchestrator = MagicMock()
    orchestrator.data_dir = str(tmp_path)

    registry = AsyncMock()
    registry.get.return_value = {"container_id": "c1", "user_id": "u1"}

    return AdminOperations(
        orchestrator=orchestrator,
        docker_client=MagicMock(),
        registry=registry,
        schema_cache=schema_cache,
    )


@pytest.mark.asyncio
async def test_read_config_masks_secrets(ops):
    cfg = await ops.read_config("u1")

    assert cfg["llm.provider"] == "openrouter"
    assert cfg["llm.api-key"] == "****"
    assert cfg["llm.model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_read_config_filters_by_section(ops):
    cfg = await ops.read_config("u1", section_prefix="llm.")

    assert all(key.startswith("llm.") for key in cfg)
