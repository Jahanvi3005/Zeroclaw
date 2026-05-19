"""`claw-admin skills <retrofit>`."""

from __future__ import annotations

import asyncio

import typer

from claw_proxy.cli import client as client_module


skills_app = typer.Typer(help="container skill files")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


async def _post(ctx: typer.Context, path: str, **kwargs) -> dict:
    return await _client(ctx).post(path, **kwargs)


@skills_app.command("retrofit")
def retrofit(ctx: typer.Context) -> None:
    """Re-render the LifeAtlas skill template on current container workspaces."""

    async def _run() -> None:
        result = await _post(ctx, "/skills/retrofit")
        typer.echo(
            f"Checked {result.get('checked', 0)} containers. "
            f"Updated {result.get('updated', 0)}, skipped {result.get('skipped', 0)}"
        )

    asyncio.run(_run())
