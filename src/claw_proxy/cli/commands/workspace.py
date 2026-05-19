"""`claw-admin workspace <ls|cat|put|rm|patch>`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from claw_proxy.cli import client as client_module


workspace_app = typer.Typer(help="workspace files")


def _client(ctx: typer.Context) -> client_module.AdminApiClient:
    cfg = ctx.obj
    return client_module.AdminApiClient(api_url=cfg.api_url, token=cfg.token)


async def _resolve_user_id(client: client_module.AdminApiClient, lookup: str) -> str:
    data = await client.get("/users", params={"lookup": lookup})
    return str(data["user_id"])


@workspace_app.command("ls")
def workspace_ls(
    ctx: typer.Context,
    target: str,
    path: str = typer.Option("", "--path"),
    recursive: bool = typer.Option(False, "-r", "--recursive"),
) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.get(
            f"/containers/{user_id}/workspace/files",
            params={"path": path, "recursive": recursive},
        )
        for item in data.get("items", []):
            typer.echo(json.dumps(item))

    asyncio.run(_run())


@workspace_app.command("cat")
def workspace_cat(ctx: typer.Context, target: str, path: str) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.get(f"/containers/{user_id}/workspace/files/{path}")
        typer.echo(str(data.get("content", "")))

    asyncio.run(_run())


@workspace_app.command("put")
def workspace_put(
    ctx: typer.Context,
    target: str,
    path: str,
    file: str = typer.Option(..., "--file"),
    mode: str = typer.Option("overwrite", "--mode"),
) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        content = Path(file).read_text(encoding="utf-8")
        data = await client.put(
            f"/containers/{user_id}/workspace/files/{path}",
            json={"content": content, "mode": mode},
        )
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@workspace_app.command("rm")
def workspace_rm(ctx: typer.Context, target: str, path: str) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.delete(f"/containers/{user_id}/workspace/files/{path}")
        typer.echo(json.dumps(data))

    asyncio.run(_run())


@workspace_app.command("patch")
def workspace_patch(
    ctx: typer.Context,
    target: str,
    path: str,
    find: str = typer.Option(..., "--find"),
    replace: str = typer.Option(..., "--replace"),
) -> None:
    async def _run() -> None:
        client = _client(ctx)
        user_id = await _resolve_user_id(client, target)
        data = await client.patch(
            f"/containers/{user_id}/workspace/files/{path}",
            json={
                "operations": [
                    {
                        "action": "replace_substring",
                        "find": find,
                        "replace": replace,
                    }
                ]
            },
        )
        typer.echo(json.dumps(data))

    asyncio.run(_run())
