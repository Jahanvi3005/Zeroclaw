# claw-auth-proxy

FastAPI auth proxy for LifeAtlas ZeroClaw agents. It authenticates browser
WebSocket sessions with Supabase JWTs, provisions one Dockerized ZeroClaw
container per user, relays chat traffic, handles workspace uploads/downloads,
and exposes operator tooling through an admin REST API plus the `claw-admin`
CLI.

## What It Does

`claw-auth-proxy` sits between the LifeAtlas frontend and per-user ZeroClaw
containers:

```text
Browser -> claw-auth-proxy -> ZeroClaw container
              |                    |
              |                    -> workspace volume
              -> Supabase
              -> Docker API
```

The proxy validates user identity once at WebSocket connect, then uses its own
per-container bearer token for container callbacks and LifeAtlas tool calls.
The result is isolated agent runtime per user, with centralized auth, storage,
and operator controls.

## Main Features

- Supabase JWT auth for `/zeroclaw/ws`.
- Docker orchestration for per-user ZeroClaw containers and persistent volumes.
- Multi-tab WebSocket fan-out through shared upstream sessions.
- Workspace upload/download flow with Supabase Storage signed links.
- Push webhook for proactive ZeroClaw messages.
- LifeAtlas context, file-library, event-photo, and save-to-library tools exposed to ZeroClaw skills.
- Admin REST plane at `/claw-admin/*` and a `claw-admin` Typer CLI.
- Docker Compose deployment with a socket proxy and shared container network.

## Quick Start

Install dependencies:

```bash
uv sync
```

Create local config:

```bash
cp .env.example .env
```

At minimum, fill in the Supabase variables in `.env`. To enable the ZeroClaw,
upload, push, tool, and admin routes, also set `TOKEN_ENCRYPTION_KEY`.

Run the app:

```bash
uv run uvicorn claw_proxy.app:app --reload --port 8000
```

Run tests:

```bash
uv run pytest
```

Integration tests need Docker and a local ZeroClaw image:

```bash
uv run pytest -m integration
```

## Important Paths

| Path | Purpose |
|---|---|
| `src/claw_proxy/app.py` | FastAPI app, route mounting, lifecycle task. |
| `src/claw_proxy/ws/` | Browser-to-ZeroClaw WebSocket relay and protocol translation. |
| `src/claw_proxy/containers/` | Docker lifecycle and workspace-volume helpers. |
| `src/claw_proxy/files/` | Upload, export, and Supabase Storage helpers. |
| `src/claw_proxy/tools/lifeatlas/` | Read-only LifeAtlas context tools for ZeroClaw. |
| `src/claw_proxy/admin/` | Admin REST implementation, auth, audit, jobs, operations. |
| `src/claw_proxy/cli/` | `claw-admin` command-line client. |
| `templates/` | Default and named workspace templates copied into new containers. |
| `deploy/` | systemd, reverse proxy, migration, and deployment helpers. |
| `docs/` | Focused architecture, protocol, deployment, and testing docs. |

## Common Commands

```bash
# Run local server
uv run uvicorn claw_proxy.app:app --reload --port 8000

# Run default non-integration suite
uv run pytest

# Run all tests, including Docker integration tests
uv run pytest -m ""

# Inspect admin CLI commands
uv run claw-admin --help

# Build the proxy container
docker build -t lifeatlas/claw-proxy:latest .
```

## Configuration

Configuration comes from environment variables, with `.env` loaded by
`python-dotenv`. See [docs/configuration.md](docs/configuration.md) and
[.env.example](.env.example) for the full list.

One important behavior: `TOKEN_ENCRYPTION_KEY` is both an encryption passphrase
and the feature flag for the full app. Without it, only `/health` is mounted.

## Documentation

- [Architecture](docs/architecture.md)
- [WebSocket protocol](docs/ws-protocol.md)
- [File transfer and push](docs/files-and-push.md)
- [LifeAtlas tools](docs/lifeatlas-tools.md)
- [Management plane and CLI](docs/management-plane.md)
- [Docker deployment](docs/deployment-docker.md)
- [Configuration reference](docs/configuration.md)
- [Testing](docs/testing.md)
- [Remaining work](docs/remaining-work.md)
