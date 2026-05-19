"""Admin operations: thin layer over orchestrator, Docker SDK, and registry."""

from __future__ import annotations

import asyncio
import base64
import os
import time
import tomllib
import tomli_w
import httpx
from typing import Any


CAREFUL_DEFAULT_TIMEOUT = 300
CAREFUL_POLL_INTERVAL = 5


async def _wait_for_idle(activity_checker, user_id: str, timeout_s: int, poll_s: int):
    deadline = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        last_state = await activity_checker.get_state(user_id)
        if last_state.is_idle:
            return True, last_state
        await asyncio.sleep(poll_s)
    return False, last_state


def _flatten_dict(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_dict(value, dotted))
        else:
            flat[dotted] = value
    return flat


async def _health_ok(http_url: str, timeout: float = 10) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient() as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(f"{http_url}/health", timeout=2)
                if response.status_code == 200:
                    return True
            except httpx.RequestError:
                pass
            await asyncio.sleep(0.5)
    return False


def _read_config_file(config_path: str) -> dict[str, Any]:
    with open(config_path, "rb") as fh:
        return tomllib.load(fh)


def _read_config_path(config_path: str, dotted_path: str):
    data = _read_config_file(config_path)
    parts = dotted_path.split(".")
    cur: Any = data
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur.get(parts[-1]) if isinstance(cur, dict) else None


def _write_config_path(config_path: str, dotted_path: str, value) -> None:
    data = _read_config_file(config_path)
    parts = dotted_path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value
    with open(config_path, "wb") as fh:
        fh.write(tomli_w.dumps(data).encode())


class AdminOperations:
    def __init__(
        self,
        *,
        orchestrator,
        docker_client,
        registry,
        activity_checker=None,
        schema_cache=None,
    ) -> None:
        self.orchestrator = orchestrator
        self.docker = docker_client
        self.registry = registry
        self.activity_checker = activity_checker
        self.schema_cache = schema_cache

    async def list_containers(
        self,
        *,
        status: str | None = None,
        registry_health: str | None = None,
        email: str | None = None,
        email_contains: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        user_id: str | None = None,
        container_id: str | None = None,
        container_name: str | None = None,
        inactive_for_days: int | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "last_active_at",
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        rows = list(await self.registry.list_all())

        def keep(row: dict[str, Any]) -> bool:
            if status is not None and status != "all" and row.get("status") != status:
                return False
            if user_id is not None and row.get("user_id") != user_id:
                return False
            if container_id is not None and not str(row.get("container_id", "")).startswith(
                container_id
            ):
                return False
            if container_name is not None:
                expected = f"zeroclaw-{str(row.get('user_id', ''))[:8]}"
                if container_name != expected:
                    return False
            return True

        filtered = [row for row in rows if keep(row)]
        filtered.sort(key=lambda row: row.get(sort) or "", reverse=order == "desc")
        return filtered[offset : offset + limit]

    async def get_container(self, user_id: str) -> dict[str, Any] | None:
        return await self.registry.get(user_id)

    async def read_config(
        self, user_id: str, *, section_prefix: str | None = None
    ) -> dict[str, Any]:
        row = await self.registry.get(user_id)
        if not row:
            raise FileNotFoundError(f"no registry row for {user_id}")

        config_path = os.path.join(
            self.orchestrator.data_dir,
            user_id,
            ".zeroclaw",
            "config.toml",
        )
        if not os.path.exists(config_path):
            return {}

        with open(config_path, "rb") as fh:
            flat = _flatten_dict(tomllib.load(fh))

        if section_prefix is not None:
            flat = {k: v for k, v in flat.items() if k.startswith(section_prefix)}

        masked: dict[str, Any] = {}
        for key, value in flat.items():
            if self.schema_cache is not None:
                try:
                    if await self.schema_cache.is_secret_path(row["container_id"], key):
                        masked[key] = "****"
                        continue
                except Exception:
                    pass
                masked[key] = value
        return masked

    def _user_workspace_root(self, user_id: str) -> str:
        return os.path.join(self.orchestrator.data_dir, user_id, "workspace")

    def _safe_join(self, root: str, rel: str) -> str:
        rel = rel.lstrip("/")
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(os.path.realpath(root)):
            raise ValueError(f"path traversal rejected: {rel}")
        return full

    async def list_workspace_files(
        self, user_id: str, *, path: str = "", recursive: bool = False
    ) -> list[dict[str, Any]]:
        root = self._user_workspace_root(user_id)
        target = self._safe_join(root, path)
        if not os.path.exists(target):
            return []

        out: list[dict[str, Any]] = []
        if recursive:
            for dirpath, _, files in os.walk(target):
                for filename in files:
                    abs_path = os.path.join(dirpath, filename)
                    out.append(
                        {
                            "path": os.path.relpath(abs_path, root),
                            "size": os.path.getsize(abs_path),
                        }
                    )
        else:
            for entry in os.listdir(target):
                abs_path = os.path.join(target, entry)
                out.append(
                    {
                        "path": os.path.relpath(abs_path, root),
                        "size": os.path.getsize(abs_path) if os.path.isfile(abs_path) else None,
                        "is_dir": os.path.isdir(abs_path),
                    }
                )
        return out

    async def read_workspace_file(self, user_id: str, *, path: str) -> dict[str, Any]:
        root = self._user_workspace_root(user_id)
        full = self._safe_join(root, path)
        if not os.path.isfile(full):
            raise FileNotFoundError(path)
        try:
            with open(full, encoding="utf-8") as fh:
                return {"content": fh.read(), "encoding": "text"}
        except UnicodeDecodeError:
            with open(full, "rb") as fh:
                return {
                    "content": base64.b64encode(fh.read()).decode("ascii"),
                    "encoding": "base64",
                }

    async def write_workspace_file(
        self,
        user_id: str,
        *,
        path: str,
        content: str,
        mode: str = "overwrite",
        encoding: str = "text",
    ) -> dict[str, Any]:
        root = self._user_workspace_root(user_id)
        full = self._safe_join(root, path)
        exists = os.path.exists(full)
        if mode == "create" and exists:
            return {"status": "exists", "path": path}
        if mode == "create_or_skip" and exists:
            return {"status": "skipped", "path": path}
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if encoding == "base64":
            with open(full, "wb") as fh:
                fh.write(base64.b64decode(content))
        else:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        return {"status": "written", "path": path}

    async def delete_workspace_file(self, user_id: str, *, path: str) -> dict[str, Any]:
        root = self._user_workspace_root(user_id)
        full = self._safe_join(root, path)
        if os.path.isfile(full):
            os.unlink(full)
            return {"status": "deleted", "path": path}
        return {"status": "not_found", "path": path}

    async def patch_workspace_file(
        self, user_id: str, *, path: str, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        root = self._user_workspace_root(user_id)
        full = self._safe_join(root, path)
        if not os.path.isfile(full):
            raise FileNotFoundError(path)
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
        for op in operations:
            if op["action"] == "replace_substring":
                text = text.replace(op["find"], op["replace"])
            elif op["action"] == "replace_lines":
                lines = text.split("\n")
                start = int(op["start"])
                end = int(op["end"])
                lines[start:end] = op["replacement"].split("\n")
                text = "\n".join(lines)
            else:
                raise ValueError(f"unknown patch action: {op['action']}")
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        return {"status": "patched", "path": path}

    async def list_orphans(self) -> list[dict[str, Any]]:
        containers = await asyncio.to_thread(self.docker.containers.list, all=True)
        registered_ids = {row["container_id"] for row in await self.registry.list_all()}
        orphans: list[dict[str, Any]] = []
        for container in containers:
            name = getattr(container, "name", "") or ""
            if not name.startswith("zeroclaw-"):
                continue
            if container.id in registered_ids:
                continue
            state = (getattr(container, "attrs", {}) or {}).get("State", {})
            orphans.append(
                {
                    "container_id": container.id,
                    "name": name,
                    "status": state.get("Status", "unknown"),
                }
            )
        return orphans

    async def remove_orphan(self, container_id: str) -> dict[str, Any]:
        container = await asyncio.to_thread(self.docker.containers.get, container_id)
        try:
            await asyncio.to_thread(container.stop, timeout=5)
        except Exception:
            pass
        await asyncio.to_thread(container.remove, force=True)
        return {"status": "removed", "container_id": container_id}

    async def write_config(
        self,
        user_id: str,
        *,
        updates: list[dict[str, Any]],
        careful: bool = False,
        careful_timeout_seconds: int = CAREFUL_DEFAULT_TIMEOUT,
        _poll_interval_seconds: int = CAREFUL_POLL_INTERVAL,
    ) -> list[dict[str, Any]]:
        row = await self.registry.get(user_id)
        if not row:
            raise FileNotFoundError(f"no registry row for {user_id}")

        ok, state = await self._careful_or_proceed(
            user_id,
            careful,
            careful_timeout_seconds,
            _poll_interval_seconds,
        )
        if not ok:
            return [
                {
                    "path": update["path"],
                    "applied": False,
                    "rollback_available": False,
                    "requires_restart": False,
                    "error": "skipped: not idle within timeout",
                    "signals": state.signals if state else [],
                }
                for update in updates
            ]

        config_path = os.path.join(
            self.orchestrator.data_dir,
            user_id,
            ".zeroclaw",
            "config.toml",
        )
        is_running = row.get("status") == "ready"
        results: list[dict[str, Any]] = []

        if is_running:
            container = await asyncio.to_thread(self.docker.containers.get, row["container_id"])
            for update in updates:
                results.append(
                    await self._write_one_running(
                        container=container,
                        container_id=row["container_id"],
                        config_path=config_path,
                        http_url=row.get("http_url", ""),
                        path=update["path"],
                        value=update["value"],
                    )
                )
        else:
            for update in updates:
                results.append(
                    await self._write_one_stopped(
                        container_id=row["container_id"],
                        config_path=config_path,
                        path=update["path"],
                        value=update["value"],
                    )
                )
        return results

    async def _write_one_running(
        self,
        *,
        container,
        container_id: str,
        config_path: str,
        http_url: str,
        path: str,
        value,
    ) -> dict[str, Any]:
        if self.schema_cache is None:
            raise RuntimeError("schema_cache is required for config writes")
        is_secret = await self.schema_cache.is_secret_path(container_id, path)
        prior = None if is_secret else _read_config_path(config_path, path)

        result = await asyncio.to_thread(
            container.exec_run,
            ["zeroclaw", "config", "set", path, str(value)],
        )
        if result.exit_code != 0:
            return {
                "path": path,
                "applied": False,
                "rollback_available": False,
                "requires_restart": False,
                "error": f"zeroclaw config set exit {result.exit_code}: {result.output!r}",
            }

        healthy = await _health_ok(http_url, timeout=10) if http_url else True
        if healthy:
            return {
                "path": path,
                "applied": True,
                "rollback_available": False,
                "requires_restart": False,
                "error": None,
                "secret": is_secret,
            }

        if is_secret:
            return {
                "path": path,
                "applied": True,
                "rollback_available": False,
                "requires_restart": False,
                "error": "post-write health check failed",
                "secret": True,
                "post_write_health": "unhealthy",
            }

        await asyncio.to_thread(
            container.exec_run,
            ["zeroclaw", "config", "set", path, str(prior)],
        )
        return {
            "path": path,
            "applied": False,
            "rollback_available": True,
            "requires_restart": False,
            "error": "post-write health check failed; rolled back",
        }

    async def _write_one_stopped(
        self,
        *,
        container_id: str,
        config_path: str,
        path: str,
        value,
    ) -> dict[str, Any]:
        if self.schema_cache is None:
            raise RuntimeError("schema_cache is required for config writes")
        is_secret = await self.schema_cache.is_secret_path(container_id, path)
        if is_secret:
            return {
                "path": path,
                "applied": False,
                "rollback_available": False,
                "requires_restart": False,
                "error": "secret writes against stopped container are rejected (HTTP 409)",
            }
        _write_config_path(config_path, path, value)
        return {
            "path": path,
            "applied": True,
            "rollback_available": False,
            "requires_restart": True,
            "error": None,
        }

    async def _careful_or_proceed(
        self,
        user_id: str,
        careful: bool,
        careful_timeout_seconds: int,
        poll_s: int,
    ):
        if not careful or not self.activity_checker:
            return True, None
        return await _wait_for_idle(
            self.activity_checker,
            user_id,
            careful_timeout_seconds,
            poll_s,
        )

    async def start_container(self, user_id: str) -> dict[str, Any]:
        await self.orchestrator.start(user_id)
        return {"status": "started"}

    async def stop_container(
        self,
        user_id: str,
        *,
        careful: bool = False,
        careful_timeout_seconds: int = CAREFUL_DEFAULT_TIMEOUT,
        _poll_interval_seconds: int = CAREFUL_POLL_INTERVAL,
    ) -> dict[str, Any]:
        ok, state = await self._careful_or_proceed(
            user_id,
            careful,
            careful_timeout_seconds,
            _poll_interval_seconds,
        )
        if not ok:
            return {
                "status": "skipped",
                "reason": "not idle within timeout",
                "signals": state.signals if state else [],
            }
        await self.orchestrator.stop(user_id)
        return {"status": "stopped"}

    async def restart_container(
        self,
        user_id: str,
        *,
        careful: bool = False,
        careful_timeout_seconds: int = CAREFUL_DEFAULT_TIMEOUT,
        _poll_interval_seconds: int = CAREFUL_POLL_INTERVAL,
    ) -> dict[str, Any]:
        ok, state = await self._careful_or_proceed(
            user_id,
            careful,
            careful_timeout_seconds,
            _poll_interval_seconds,
        )
        if not ok:
            return {
                "status": "skipped",
                "reason": "not idle within timeout",
                "signals": state.signals if state else [],
            }
        await self.orchestrator.restart(user_id)
        return {"status": "restarted"}

    async def retrofit_skills(self) -> dict[str, Any]:
        targets: dict[str, str] = {}
        for row in await self.registry.list_all():
            user_id = row.get("user_id")
            token = row.get("bearer_token")
            if user_id and token:
                targets[str(user_id)] = str(token)
                self.orchestrator.token_map.setdefault(str(token), str(user_id))

        for token, user_id in dict(getattr(self.orchestrator, "token_map", {})).items():
            if user_id and token:
                targets.setdefault(str(user_id), str(token))

        updated = 0
        skipped = 0
        for user_id, token in targets.items():
            if await self.orchestrator._ensure_skills_current(user_id, token):
                updated += 1
            else:
                skipped += 1
        return {
            "checked": len(targets),
            "updated": updated,
            "skipped": skipped,
            "failed": 0,
        }
