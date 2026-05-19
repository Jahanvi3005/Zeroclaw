from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


def _exec_returns(stdout: bytes, exit_code: int = 0, stderr: bytes | None = None):
    result = MagicMock()
    result.exit_code = exit_code
    result.output = stdout if stderr is None else (stdout, stderr)
    return result


@pytest.mark.asyncio
async def test_caches_per_version():
    from claw_proxy.admin.schema_cache import SchemaCache

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    container.exec_run.side_effect = [
        _exec_returns(b"0.7.3\n"),
        _exec_returns(
            json.dumps(
                {"properties": {"llm.api-key": {"x-secret": True}}}
            ).encode()
        ),
    ]

    cache = SchemaCache(docker_client)
    first = await cache.get_schema("c1")
    container.exec_run.side_effect = [_exec_returns(b"0.7.3\n")]
    second = await cache.get_schema("c1")

    assert first == second
    assert "llm.api-key" in first["properties"]


@pytest.mark.asyncio
async def test_is_secret_path():
    from claw_proxy.admin.schema_cache import SchemaCache

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    container.exec_run.side_effect = [
        _exec_returns(b"0.7.3\n"),
        _exec_returns(
            json.dumps(
                {
                    "properties": {
                        "llm.api-key": {"x-secret": True},
                        "llm.provider": {"type": "string"},
                    }
                }
            ).encode()
        ),
    ]

    cache = SchemaCache(docker_client)

    assert await cache.is_secret_path("c1", "llm.api-key") is True
    assert await cache.is_secret_path("c1", "llm.provider") is False


@pytest.mark.asyncio
async def test_schema_parses_demuxed_stdout_when_cli_logs_to_stderr():
    from claw_proxy.admin.schema_cache import SchemaCache

    docker_client = MagicMock()
    container = MagicMock()
    docker_client.containers.get.return_value = container
    schema = {"properties": {"autonomy.max-actions-per-hour": {"type": "integer"}}}
    container.exec_run.side_effect = [
        _exec_returns(b"0.7.3\n"),
        _exec_returns(
            json.dumps(schema).encode(),
            stderr=b"2026-04-24T00:00:00Z INFO config loaded\n",
        ),
    ]

    cache = SchemaCache(docker_client)

    assert await cache.get_schema("c1") == schema
    schema_call = container.exec_run.call_args_list[1]
    assert schema_call.kwargs["demux"] is True
    assert schema_call.kwargs["environment"] == {"RUST_LOG": "off"}
