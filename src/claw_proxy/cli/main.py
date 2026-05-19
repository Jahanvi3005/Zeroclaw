"""Typer entrypoint for `claw-admin`."""

from __future__ import annotations

import typer

from claw_proxy.cli.config import load_cli_config
from claw_proxy.cli.commands.admin import admin_app
from claw_proxy.cli.commands.config import config_app
from claw_proxy.cli.commands.container import container_app
from claw_proxy.cli.commands.job import job_app
from claw_proxy.cli.commands.orphan import orphan_app
from claw_proxy.cli.commands.skills import skills_app
from claw_proxy.cli.commands.workspace import workspace_app


app = typer.Typer(help="claw-admin: admin CLI for claw-auth-proxy")
app.add_typer(container_app, name="container")
app.add_typer(config_app, name="config")
app.add_typer(workspace_app, name="workspace")
app.add_typer(admin_app, name="admin")
app.add_typer(orphan_app, name="orphan")
app.add_typer(job_app, name="job")
app.add_typer(skills_app, name="skills")


@app.callback()
def _main(
    ctx: typer.Context,
    api_url: str = typer.Option(None, "--api-url"),
    token: str = typer.Option(None, "--token"),
    token_file: str = typer.Option(None, "--token-file"),
    config: str = typer.Option(None, "--config"),
):
    ctx.obj = load_cli_config(
        api_url=api_url,
        token=token,
        token_file=token_file,
        config_path=config,
    )


if __name__ == "__main__":
    app()
