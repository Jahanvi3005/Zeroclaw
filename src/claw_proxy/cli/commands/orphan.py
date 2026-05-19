"""`claw-admin orphan <list|remove>`."""

from __future__ import annotations

import asyncio
import json

import typer

from claw_proxy.cli import client as client_module


orphan_app = typer.Typer(help="orphan containers")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


@orphan_app.command("list")
def orphan_list(ctx: typer.Context) -> None:
    async def _run() -> None:
        data = await _client(ctx).get("/orphans")
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@orphan_app.command("remove")
def orphan_remove(ctx: typer.Context, container_id: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).delete(f"/orphans/{container_id}")
        typer.echo(json.dumps(data))

    asyncio.run(_run())
