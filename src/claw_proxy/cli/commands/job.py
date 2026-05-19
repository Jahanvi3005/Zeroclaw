"""`claw-admin job <list|status|cancel>`."""

from __future__ import annotations

import asyncio
import json

import typer

from claw_proxy.cli import client as client_module


job_app = typer.Typer(help="async batch jobs")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


@job_app.command("list")
def job_list(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status"),
) -> None:
    async def _run() -> None:
        params = {"status": status} if status is not None else None
        data = await _client(ctx).get("/jobs", params=params)
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@job_app.command("status")
def job_status(ctx: typer.Context, job_id: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).get(f"/jobs/{job_id}")
        typer.echo(json.dumps(data, indent=2))

    asyncio.run(_run())


@job_app.command("cancel")
def job_cancel(ctx: typer.Context, job_id: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).delete(f"/jobs/{job_id}")
        typer.echo(json.dumps(data))

    asyncio.run(_run())
