"""Append-only encrypted JSONL audit storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from claw_proxy.admin.auth import AdminContext
from claw_proxy.crypto import TokenCrypto


def _isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class AuditEntry:
    id: str
    timestamp: datetime
    admin_id: str
    auth_channel: str
    action: str
    target: dict | None = None
    params: dict = field(default_factory=dict)
    result: str = "success"
    error: str | None = None
    duration_ms: int = 0
    client_ip: str | None = None
    job_id: str | None = None


class AuditStore(Protocol):
    async def append(self, entry: AuditEntry) -> None: ...

    async def query(
        self,
        since: str | None = None,
        until: str | None = None,
        admin_id: str | None = None,
        action: str | None = None,
        target_user_id: str | None = None,
        job_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditEntry]: ...

    async def prune_older_than(self, days: int) -> int: ...


@dataclass
class AuditContext:
    admin: AdminContext
    client_ip: str | None = None


class EncryptedJsonlAuditStore:
    def __init__(self, path: str | Path, crypto: TokenCrypto) -> None:
        self._path = Path(path)
        self._crypto = crypto
        self._lock = asyncio.Lock()

    def _serialize(self, entry: AuditEntry) -> str:
        payload = asdict(entry)
        payload["timestamp"] = _isoformat(payload["timestamp"])
        return json.dumps(payload, separators=(",", ":"))

    def _deserialize(self, data: dict) -> AuditEntry:
        return AuditEntry(
            id=data["id"],
            timestamp=_parse_timestamp(data["timestamp"]),
            admin_id=data["admin_id"],
            auth_channel=data["auth_channel"],
            action=data["action"],
            target=data.get("target"),
            params=data.get("params", {}),
            result=data.get("result", "success"),
            error=data.get("error"),
            duration_ms=data.get("duration_ms", 0),
            client_ip=data.get("client_ip"),
            job_id=data.get("job_id"),
        )

    def _read_entries(self) -> list[AuditEntry]:
        if not self._path.exists():
            return []

        entries: list[AuditEntry] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            payload = json.loads(self._crypto.decrypt(line))
            entries.append(self._deserialize(payload))
        return entries

    def _write_entries(self, entries: list[AuditEntry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            ) as tmp_file:
                for entry in entries:
                    ciphertext = self._crypto.encrypt(self._serialize(entry))
                    tmp_file.write(ciphertext)
                    tmp_file.write("\n")
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_path = Path(tmp_file.name)
            tmp_path.replace(self._path)
            dir_fd = os.open(self._path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp_path is not None and tmp_path.exists() and tmp_path != self._path:
                tmp_path.unlink(missing_ok=True)

    async def append(self, entry: AuditEntry) -> None:
        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(self._crypto.encrypt(self._serialize(entry)))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())

    async def query(
        self,
        since: str | None = None,
        until: str | None = None,
        admin_id: str | None = None,
        action: str | None = None,
        target_user_id: str | None = None,
        job_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AuditEntry]:
        async with self._lock:
            rows = self._read_entries()

            since_dt = _parse_timestamp(since) if since is not None else None
            until_dt = _parse_timestamp(until) if until is not None else None

            filtered: list[AuditEntry] = []
            for row in rows:
                if since_dt is not None and row.timestamp < since_dt:
                    continue
                if until_dt is not None and row.timestamp > until_dt:
                    continue
                if admin_id is not None and row.admin_id != admin_id:
                    continue
                if action is not None and row.action != action:
                    continue
                if job_id is not None and row.job_id != job_id:
                    continue
                if target_user_id is not None:
                    target = row.target or {}
                    if target.get("type") != "user" or target.get("user_id") != target_user_id:
                        continue
                filtered.append(row)

            filtered.sort(key=lambda row: row.timestamp, reverse=True)
            if offset:
                filtered = filtered[offset:]
            if limit is not None:
                filtered = filtered[:limit]
            return filtered

    async def prune_older_than(self, days: int) -> int:
        async with self._lock:
            if not self._path.exists():
                return 0

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            rows = self._read_entries()
            kept = [row for row in rows if row.timestamp >= cutoff]
            removed = len(rows) - len(kept)
            self._write_entries(kept)
            return removed


async def sanitize_params(
    action: str,
    params: dict,
    *,
    container_id: str | None,
    schema_cache,
) -> dict:
    if action in {"config.set", "user.config.set"}:
        out = deepcopy(params)
        for update in out.get("updates", []):
            path = update.get("path", "")
            value = update.get("value", "")
            try:
                is_secret = (
                    await schema_cache.is_secret_path(container_id, path)
                    if schema_cache
                    else False
                )
            except Exception:
                is_secret = False
            if is_secret:
                update["value"] = "[REDACTED]"
                update["value_length"] = len(str(value))
        return out

    if action == "workspace.write":
        content = params.get("content", "")
        out = {key: value for key, value in params.items() if key != "content"}
        out["content_meta"] = {
            "size": len(content),
            "sha256": hashlib.sha256(str(content).encode()).hexdigest(),
        }
        return out

    if action == "workspace.patch":
        out = deepcopy(params)
        for op in out.get("operations", []):
            if "content" in op:
                content = op.pop("content")
                op["content_meta"] = {
                    "size": len(content),
                    "sha256": hashlib.sha256(str(content).encode()).hexdigest(),
                }
        return out

    return params


async def emit_audit(
    *,
    store: AuditStore,
    ctx: AuditContext,
    action: str,
    target: dict,
    params: dict,
    result: str,
    duration_ms: int,
    error: str | None = None,
    job_id: str | None = None,
    container_id: str | None = None,
    schema_cache=None,
) -> None:
    sanitized = await sanitize_params(
        action,
        params,
        container_id=container_id,
        schema_cache=schema_cache,
    )
    entry = AuditEntry(
        id=f"audit_{secrets.token_hex(8)}",
        timestamp=datetime.now(timezone.utc),
        admin_id=ctx.admin.admin_id,
        auth_channel=ctx.admin.channel,
        action=action,
        target=target,
        params=sanitized,
        result=result,
        error=error,
        duration_ms=duration_ms,
        client_ip=ctx.client_ip,
        job_id=job_id,
    )
    await store.append(entry)
