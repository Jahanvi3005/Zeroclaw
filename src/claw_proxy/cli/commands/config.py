"""`claw-admin config <show|set|schema>`."""

from __future__ import annotations

import asyncio
import json
import sys

import typer

from claw_proxy.cli import client as client_module


config_app = typer.Typer(help="container config ops")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


async def _resolve_user_id(client: client_module.AdminApiClient, lookup: str) -> str:
    data = await client.get("/users", params={"lookup": lookup})
    return str(data["user_id"])


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    target: str,
    section: str | None = typer.Option(None, "--section"),
) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        params = {"section": section} if section is not None else None
        data = await client.get(f"/containers/{user_id}/config", params=params)
        typer.echo(json.dumps(data, indent=2))

    asyncio.run(_run())


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    target: str,
    path: str,
    value: str | None = typer.Argument(None),
    from_stdin: bool = typer.Option(False, "--stdin", help="Read the value from stdin."),
    careful: bool = typer.Option(False, "--careful"),
) -> None:
    if value is None and not from_stdin:
        raise typer.BadParameter("provide a value or pass --stdin")

    if from_stdin:
        value = sys.stdin.read()

    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.patch(
            f"/containers/{user_id}/config",
            json={
                "careful": careful,
                "updates": [{"path": path, "value": value}],
            },
        )
        typer.echo(json.dumps(data, indent=2))

    asyncio.run(_run())


@config_app.command("schema")
def config_schema(ctx: typer.Context, target: str) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.get(f"/containers/{user_id}/config/schema")
        typer.echo(json.dumps(data, indent=2))

    asyncio.run(_run())
