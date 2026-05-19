"""`claw-admin admin <admin-list|admin-revoke|token-list|token-create|token-revoke|invite|audit>`."""

from __future__ import annotations

import asyncio
import json

import typer

from claw_proxy.cli import client as client_module


admin_app = typer.Typer(help="admin self-management")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


@admin_app.command("admin-list")
def admin_list(ctx: typer.Context) -> None:
    async def _run() -> None:
        data = await _client(ctx).get("/admins")
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@admin_app.command("admin-revoke")
def admin_revoke(ctx: typer.Context, admin_id: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).delete(f"/admins/{admin_id}")
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@admin_app.command("token-list")
def token_list(ctx: typer.Context) -> None:
    async def _run() -> None:
        data = await _client(ctx).get("/admins/static-tokens")
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@admin_app.command("token-create")
def token_create(ctx: typer.Context, label: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).post("/admins/static-tokens", json={"label": label})
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@admin_app.command("token-revoke")
def token_revoke(ctx: typer.Context, label: str) -> None:
    async def _run() -> None:
        data = await _client(ctx).delete(f"/admins/static-tokens/{label}")
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@admin_app.command("invite")
def invite(ctx: typer.Context) -> None:
    async def _run() -> None:
        data = await _client(ctx).post("/admins/invite")
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@admin_app.command("audit")
def audit(
    ctx: typer.Context,
    since: str | None = typer.Option(None, "--since"),
    action: str | None = typer.Option(None, "--action"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    async def _run() -> None:
        params: dict[str, object] = {"limit": limit}
        if since is not None:
            params["since"] = since
        if action is not None:
            params["action"] = action
        data = await _client(ctx).get("/audit", params=params)
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())
