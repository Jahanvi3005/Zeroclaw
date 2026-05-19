"""Async batch job state, persisted as one encrypted JSON file per job."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from claw_proxy.admin.storage import EncryptedJsonFile
from claw_proxy.crypto import TokenCrypto


@dataclass
class JobTarget:
    user_id: str
    status: str
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    result: dict | None = None


@dataclass
class Job:
    id: str
    operation: str
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    params: dict
    targets: list[JobTarget]
    summary: dict


class AdminJobStore(Protocol):
    async def put(self, job: Job) -> None: ...

    async def get(self, job_id: str) -> Job | None: ...

    async def list_jobs(
        self, status: str | None = None, operation: str | None = None
    ) -> list[Job]: ...

    async def delete(self, job_id: str) -> None: ...

    async def list_ids(self) -> list[str]: ...


def _job_from_dict(data: dict) -> Job:
    return Job(
        id=data["id"],
        operation=data["operation"],
        status=data["status"],
        created_at=data["created_at"],
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        params=data.get("params", {}),
        targets=[JobTarget(**target) for target in data.get("targets", [])],
        summary=data.get("summary", {}),
    )


def _validate_job_id(job_id: str) -> str:
    if not job_id:
        raise ValueError("job_id must not be empty")
    if job_id in {".", ".."}:
        raise ValueError("job_id must be a simple path-safe name")
    if "/" in job_id or "\\" in job_id:
        raise ValueError("job_id must not contain path separators")
    if os.sep and os.sep in job_id:
        raise ValueError("job_id must not contain path separators")
    if os.altsep and os.altsep in job_id:
        raise ValueError("job_id must not contain path separators")
    return job_id


class EncryptedJsonJobStore:
    def __init__(self, dir_path: str | Path, crypto: TokenCrypto) -> None:
        self._dir = Path(dir_path)
        self._crypto = crypto
        self._lock = asyncio.Lock()

    def _path(self, job_id: str) -> Path:
        return self._dir / f"{_validate_job_id(job_id)}.json.enc"

    async def put(self, job: Job) -> None:
        async with self._lock:
            EncryptedJsonFile(self._path(job.id), self._crypto).write(asdict(job))

    async def get(self, job_id: str) -> Job | None:
        async with self._lock:
            data = EncryptedJsonFile(self._path(job_id), self._crypto).read(default=None)
            if data is None:
                return None
            return _job_from_dict(data)

    async def list_jobs(
        self, status: str | None = None, operation: str | None = None
    ) -> list[Job]:
        async with self._lock:
            if not self._dir.exists():
                return []

            jobs: list[Job] = []
            for path in self._dir.glob("*.json.enc"):
                data = EncryptedJsonFile(path, self._crypto).read(default=None)
                if not data:
                    continue
                if status is not None and data.get("status") != status:
                    continue
                if operation is not None and data.get("operation") != operation:
                    continue
                jobs.append(_job_from_dict(data))

            jobs.sort(key=lambda job: job.created_at, reverse=True)
            return jobs

    async def delete(self, job_id: str) -> None:
        async with self._lock:
            self._path(job_id).unlink(missing_ok=True)

    async def list_ids(self) -> list[str]:
        async with self._lock:
            if not self._dir.exists():
                return []
            return sorted(path.name.removesuffix(".json.enc") for path in self._dir.glob("*.json.enc"))


class JobRunner:
    def __init__(
        self,
        *,
        store: AdminJobStore,
        inter_target_delay_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.delay_s = inter_target_delay_seconds
        self._cancel_flags: dict[str, bool] = {}

    def cancel(self, job_id: str) -> None:
        self._cancel_flags[job_id] = True

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _target_summary_bucket(self, target: JobTarget) -> str:
        if target.status == "failed" or target.error:
            return "failed"
        if target.status == "skipped":
            return "skipped"
        if target.status in {"pending", "running"}:
            return "pending"
        return "success"

    async def run(
        self,
        job_id: str,
        per_target: Callable[[str, dict], Awaitable[dict]],
    ) -> None:
        job = await self.store.get(job_id)
        if not job:
            return

        job.status = "running"
        job.started_at = self._now()
        await self.store.put(job)

        cancelled = False
        for target in job.targets:
            if self._cancel_flags.get(job_id):
                cancelled = True
                break
            target.status = "running"
            target.started_at = self._now()
            await self.store.put(job)
            try:
                result = await per_target(target.user_id, job.params)
            except Exception as exc:  # noqa: BLE001
                target.status = "failed"
                target.error = str(exc)
                target.completed_at = self._now()
                await self.store.put(job)
                continue

            status = result.get("status", "success")
            target.status = status
            target.result = result
            target.error = result.get("error")
            target.completed_at = self._now()
            await self.store.put(job)
            if self.delay_s > 0:
                await asyncio.sleep(self.delay_s)

        summary = {"total": len(job.targets), "success": 0, "failed": 0, "skipped": 0, "pending": 0}
        for target in job.targets:
            summary[self._target_summary_bucket(target)] += 1
        job.summary = summary
        job.completed_at = self._now()
        job.status = "cancelled" if cancelled else (
            "failed" if summary["failed"] > 0 and summary["success"] == 0 else "complete"
        )
        await self.store.put(job)
        self._cancel_flags.pop(job_id, None)
