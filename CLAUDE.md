# claw-auth-proxy

FastAPI WebSocket auth proxy for ZeroClaw, part of the LifeAtlas project. Authenticates users via Supabase JWT, manages per-user ZeroClaw containers via Docker, maintains a bidirectional WS relay with protocol translation, and exposes LifeAtlas context/file tools to ZeroClaw. Ships an admin REST plane + `claw-admin` CLI at `/claw-admin/*`.

## Quick reference

```bash
# Run dev server
uv run uvicorn claw_proxy.app:app --reload --port 8000

# Run tests — default skips integration tests (which need local Docker)
uv run pytest
uv run pytest -m integration    # or -m "" for all

# Add / update dependencies
uv add <package>
```

**Entry points.** Main app: `src/claw_proxy/app.py` (FastAPI factory — mounts the ZeroClaw sub-app when `TOKEN_ENCRYPTION_KEY` is set, plus the admin sub-app at `/claw-admin/*`). Frontend WS handler: `src/claw_proxy/ws/proxy.py`. Admin CLI entry: `claw-admin` (source: `src/claw_proxy/cli/main.py`, registered in `pyproject.toml`).

**Architecture in one diagram.**

```
Browser ──WS─> Auth Proxy ──WS─> ZeroClaw container
                  │                │
             Orchestrator ───Docker API
                  │                │
                  <──POST /push────┘
                  │
               Supabase (JWT, container_registry, profiles, storage)
```

JWT validated once at WS connect. Proxy then uses its own per-container bearer token. Each user gets an isolated Docker container with a persistent volume. Multi-tab fan-out via `SharedSession`. Management plane is a separate FastAPI sub-app with passkey or static-token auth.

## Documentation index

Each doc is self-contained — load the one that matches your task. Don't read them all at session start.

| Working on… | Read |
|---|---|
| Where a piece of code lives; why the system is shaped this way | [docs/architecture.md](docs/architecture.md) |
| Admin REST, CLI, network modes, audit, careful mode, deploy assets | [docs/management-plane.md](docs/management-plane.md) |
| Deploying the proxy as a Docker container that orchestrates ZeroClaw containers | [docs/deployment-docker.md](docs/deployment-docker.md) |
| WS message types (client↔server) and error codes | [docs/ws-protocol.md](docs/ws-protocol.md) |
| Upload / download flows, push webhook, export tag rewriting | [docs/files-and-push.md](docs/files-and-push.md) |
| LifeAtlas helper/file tools and generated ZeroClaw skill | [docs/lifeatlas-tools.md](docs/lifeatlas-tools.md) |
| Running tests, mock boundaries, integration gotchas | [docs/testing.md](docs/testing.md) |
| Env var reference, `.env` precedence | [docs/configuration.md](docs/configuration.md) |
| Known gaps, deferred items, spec/plan index, history | [docs/remaining-work.md](docs/remaining-work.md) |

Specs and implementation plans live under `docs/superpowers/specs/` and `docs/superpowers/plans/`; the index in `docs/remaining-work.md` maps dates to topics.

## Gotchas worth keeping in mind

- **Container volumes are chowned to UID 65534** (`nobody`) so ZeroClaw can read/write them. Host-side IO running as a different UID gets `PermissionError`. In production, the proxy itself runs as 65534 so this is fine; in tests, use the `reclaim_volume` fixture. See [testing.md](docs/testing.md#2-volume-uid-mismatch-between-production-and-tests).
- **`src/claw_proxy/config.py:24` is a module-level `httpx.AsyncClient`.** It binds to the first event loop that uses it. Don't call `asyncio.run()` multiple times in a test that touches `db.py` / `auth.py` — you'll get `RuntimeError: Event loop is closed` on subsequent calls. See [testing.md](docs/testing.md#1-the-shared-http_client-is-a-cross-loop-hazard).
- **Provisioned containers force `ZEROCLAW_GATEWAY_HOST=0.0.0.0` + `ALLOW_PUBLIC_BIND=true` + `REQUIRE_PAIRING=false`.** Without the first two, Docker's in-container healthcheck still passes but `/health` over the host-mapped port gives "Connection reset by peer". The env vars are hardcoded in `orchestrator.py`; see [architecture.md](docs/architecture.md#container-config-strategy).
- **LifeAtlas skill updates are version-aware.** New workspaces get `workspace/skills/lifeatlas/*` with the per-container token rendered into the HTTP tool URLs. Existing initialized workspaces are checked on provision/start/restart, and operators can run `claw-admin skills retrofit`; see [lifeatlas-tools.md](docs/lifeatlas-tools.md#workspace-skill).
- **LifeAtlas fetched files are temporary workspace files.** `get_health_data_file_content` and `get_log_event_photo` can stream Supabase objects into `workspace/lifeatlas/{files,photos}`. The lifecycle loop sweeps those after the same 24h window as `workspace/temp/`.
- **Session switching reconnects the upstream WS** while keeping the frontend WS alive. Switch/create are per-tab; delete force-moves other tabs; rename broadcasts. Tests that assert "the first upstream frame is X" must tolerate a preceding `status` frame on failure-retry paths.
- **`TOKEN_ENCRYPTION_KEY` is a feature flag.** Without it the app starts with just `/health`; the ZeroClaw WS, push webhook, upload, and admin mounts are all skipped. Useful for bring-up and CI; confusing when you forget and wonder why `/zeroclaw/ws` 404s.

## Recent work

- **2026-05-06 — LifeAtlas file access merged.** Added `health_data_files` and `log_events` file/photo tools, `save_to_library` for writing agent files into the website `health_data` library, lifecycle cleanup for `workspace/lifeatlas/`, version-aware skill updates, `claw-admin skills retrofit`, and local-Supabase integration coverage. Default suite: `435 passed, 9 deselected`.
- **2026-04-29 — PR #6 merge + warning cleanup.** Resolved `backend-mgmt-tools` conflicts against `main`, preserving admin-plane routes and the LifeAtlas tool router. Follow-up commit `93559fd` removed PyJWT/Pydantic/AsyncMock test warnings. Default suite at the time: `305 passed, 3 deselected`.
- **2026-04-28 — LifeAtlas helper tools for ZeroClaw.** Added proxy-hosted read-only tools under `/zeroclaw/tools/lifeatlas/{tool_name}`, domain helpers in `src/claw_proxy/tools/lifeatlas/`, and a generated `workspace/skills/lifeatlas` ZeroClaw skill template. See [docs/lifeatlas-tools.md](docs/lifeatlas-tools.md).
- **2026-04-24 — integration suite actually runs end-to-end.** Diagnosed three stacked failures: orchestrator missing the gateway-bind env vars, module-level httpx client leaking transports across per-test event loops, and volume UID mismatch blocking admin-ops host-side IO. Fixes: commit `c862f0e` (orchestrator env), commit `765ebff` (async tests + profile stub + `reclaim_volume` fixture). Full debugging story in [docs/testing.md](docs/testing.md#integration-test-gotchas-learned-the-hard-way--2026-04-24).
- **2026-04-23 — management plane gap-fill.** Verification against `docs/superpowers/plans/2026-04-22-management-plane.md` found 4 gaps (missing `/batch/config.set`, `GET /jobs/{id}`, `DELETE /jobs/{id}`; unregistered `config_app` in CLI; missing compose `version:`). Fixed across commits `df20355`, `f388d3c`, `15f17fa`.
