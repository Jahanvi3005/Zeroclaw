"""Per-version schema cache for ZeroClaw config schema lookups."""

from __future__ import annotations

import asyncio
import json
from typing import Any


def _stdout_bytes(output) -> bytes:
    if isinstance(output, tuple):
        stdout, _stderr = output
        return stdout or b"{}"
    return output or b"{}"


class SchemaCache:
    def __init__(self, docker_client) -> None:
        self.docker = docker_client
        self._by_version: dict[str, dict[str, Any]] = {}
        self._container_version: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def _container_version_string(self, container_id: str) -> str:
        cached = self._container_version.get(container_id)
        if cached is not None:
            return cached

        container = await asyncio.to_thread(self.docker.containers.get, container_id)
        result = await asyncio.to_thread(container.exec_run, ["zeroclaw", "--version"])
        output = (result.output or b"").decode().strip()
        version = output.split()[-1] if output else "unknown"
        self._container_version[container_id] = version
        return version

    async def get_schema(self, container_id: str) -> dict[str, Any]:
        async with self._lock:
            version = await self._container_version_string(container_id)
            cached = self._by_version.get(version)
            if cached is not None:
                return cached

            container = await asyncio.to_thread(self.docker.containers.get, container_id)
            result = await asyncio.to_thread(
                container.exec_run,
                ["zeroclaw", "config", "schema"],
                environment={"RUST_LOG": "off"},
                demux=True,
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    f"zeroclaw config schema exit {result.exit_code}: {result.output!r}"
                )
            schema = json.loads(_stdout_bytes(result.output).decode())
            self._by_version[version] = schema
            return schema

    async def is_secret_path(self, container_id: str, path: str) -> bool:
        schema = await self.get_schema(container_id)
        prop = schema.get("properties", {}).get(path, {})
        return bool(prop.get("x-secret"))
