"""`claw-admin container <list|status|start|stop|restart>`."""

from __future__ import annotations

import asyncio
import json

import typer

from claw_proxy.cli import client as client_module


container_app = typer.Typer(help="container ops")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


async def _resolve_user_id(client: client_module.AdminApiClient, lookup: str) -> str:
    data = await client.get("/users", params={"lookup": lookup})
    return str(data["user_id"])


@container_app.command("list")
def container_list(
    ctx: typer.Context,
    status: str | None = typer.Option(None, "--status"),
    inactive_for_days: int | None = typer.Option(None, "--inactive-for-days"),
) -> None:
    async def _run() -> None:
        params: dict[str, object] = {}
        if status is not None:
            params["status"] = status
        if inactive_for_days is not None:
            params["inactive_for_days"] = inactive_for_days
        data = await _client(ctx).get("/containers", params=params or None)
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@container_app.command("status")
def container_status(ctx: typer.Context, target: str) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.get(f"/containers/{user_id}")
        typer.echo(json.dumps(data, indent=2))

    asyncio.run(_run())


def _make_lifecycle_command(action: str):
    def _command(
        ctx: typer.Context,
        target: str,
        careful: bool = typer.Option(False, "--careful"),
        careful_timeout_seconds: int = typer.Option(300, "--careful-timeout-seconds"),
    ) -> None:
        async def _run() -> None:
            client = _client(ctx)
            user_id = await _resolve_user_id(client, target)
            path = f"/containers/{user_id}/{action}"
            if action == "start":
                data = await client.post(path)
            else:
                data = await client.post(
                    path,
                    json={
                        "careful": careful,
                        "careful_timeout_seconds": careful_timeout_seconds,
                    },
                )
            typer.echo(json.dumps(data))

        asyncio.run(_run())

    return _command


container_app.command("start")(_make_lifecycle_command("start"))
container_app.command("stop")(_make_lifecycle_command("stop"))
container_app.command("restart")(_make_lifecycle_command("restart"))
