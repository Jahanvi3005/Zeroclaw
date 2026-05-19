# Architecture

System-level map of `claw-auth-proxy`: where code lives, how the pieces fit, and the design decisions that shape it.

## Runtime diagram

```
Browser ──WS (proxy protocol)──> Auth Proxy ──WS (zeroclaw protocol)──> ZeroClaw container
                                     │              │                         │
                                     │    Container Orchestrator ────Docker API│
                                     │              │                         │
                                     │<──POST /push─┼─────────────────────────┘
                                     │              │
                                     └──────────────┴── Supabase DB + Storage
                                                         ├── container_registry
                                                         ├── JWT verification
                                                         ├── user profiles
                                                         └── workspace_files, health_data, event_photos
```

The proxy authenticates via Supabase JWT once at WS connect, then routes to the user's ZeroClaw container using its own bearer token. Each user gets an isolated container with a persistent workspace volume.

## Source layout

Source lives under `src/claw_proxy/` (installable as the `claw_proxy` package). `uv sync` installs it editable, so `uv run uvicorn claw_proxy.app:app` and `uv run pytest` both pick up changes without reinstall.

| File | Purpose |
|------|---------|
| `src/claw_proxy/app.py` | FastAPI factory: CORS, lifespan hooks (bucket init, workspace dir verify), conditional ZeroClaw mount with push/upload routers, admin sub-app mount, lifecycle cleanup task (expired Supabase exports + volume temp files), httpx transport error backstop. Uvicorn entrypoint `claw_proxy.app:app`. |
| `src/claw_proxy/ws/proxy.py` | Frontend WS handler — the core module. Auth, container lookup, joins `SharedSession` for upstream sharing, fan-out relay, session commands, transcript fetch via REST. |
| `src/claw_proxy/ws/shared_session.py` | `SharedSession` dataclass + per-user state (`_sessions`, `_user_locks`). Pure mechanism for multi-tab fan-out: register/unregister sessions, add/remove tabs, broadcast, `get_connections` snapshot for push. |
| `src/claw_proxy/ws/upstream.py` | ZeroClaw WS client: connect, send, receive, session switching via reconnect. |
| `src/claw_proxy/ws/protocol.py` | Message translation between frontend protocol and ZeroClaw native WS. Pure functions, no I/O. |
| `src/claw_proxy/containers/orchestrator.py` | Container lifecycle: provision, stop, restart, health check. Docker SDK with asyncio. Per-user volume chowned to 65534 via busybox. Workspace scaffold from templates, `config.toml` patching. |
| `src/claw_proxy/containers/workspace.py` | Workspace file management: template resolution/copy, `USER.md` patching, `config.toml` patching (incl. delegate agents), LifeAtlas skill rendering, busybox helpers with path safety (`normalize_workspace_relative_path`), temp/LifeAtlas fetched-file expiry. |
| `src/claw_proxy/files/upload.py` | REST `POST /workspace/files` — uploads files to `workspace/temp/`. Returns annotation tag. 25MB limit. See [files-and-push.md](files-and-push.md). |
| `src/claw_proxy/files/downloads.py` | Shared `[DOWNLOAD:]` tag processing. docker cp + busybox fallback. Used by relay and push. Permanent saves use `save_to_library`. |
| `src/claw_proxy/files/workspace_fetch.py` | Supabase Storage object download into `workspace/lifeatlas/{files,photos}` for LifeAtlas file/photo tools. |
| `src/claw_proxy/files/storage.py` | Supabase Storage ops: bucket mgmt (`workspace_files`), transient export upload, signed URLs, export cleanup, and `health_data` upload + `health_data_files` insert for `save_to_library`. |
| `src/claw_proxy/push.py` | `POST /push` webhook. Bearer-token auth via `orchestrator.token_map`. Processes export tags, fans out `push.message`. |
| `src/claw_proxy/tools/lifeatlas/` | LifeAtlas helper tools exposed at `/zeroclaw/tools/lifeatlas/{tool_name}` for ZeroClaw skills, including file listing/fetching, event photos, and `save_to_library`. See [lifeatlas-tools.md](lifeatlas-tools.md). |
| `src/claw_proxy/db.py` | Container registry CRUD via Supabase REST. Encrypted tokens. Activity tracking. Profile fetch from `decrypted_profiles` view. |
| `src/claw_proxy/crypto.py` | Fernet token encryption/decryption. SHA-256 key derivation. |
| `src/claw_proxy/auth.py` | Supabase JWT verification via remote GoTrue call. |
| `src/claw_proxy/config.py` | Env var loading, shared `httpx.AsyncClient` singleton, ZeroClaw config (feature-flagged), lazy Supabase client. ⚠️ The shared client is a cross-loop hazard in tests — see [testing.md](testing.md). |
| `templates/` | Workspace scaffold templates (at repo root, not inside the package). `default/` = base; per-user overrides in named subdirs matched by first name. Includes generated ZeroClaw skills such as `send-files` and `lifeatlas`. |

Management plane (`admin/`, `cli/`, `user_config.py`) documented separately in [management-plane.md](management-plane.md).

## Key design decisions

**WebSocket relay.** ZeroClaw speaks WS natively. Per-user containers map to per-user WS connections. The proxy owns the frontend protocol, decoupling frontend from ZeroClaw internals.

**Multi-tab session sharing.** Multiple browser tabs on the same `(user_id, session_id)` share one upstream WebSocket via `SharedSession`. A per-session relay task fans out upstream messages to all subscribed tabs. Per-user `asyncio.Lock` serializes session lifecycle ops (join/leave/switch/create/delete). Per-session `send_lock` + `streaming_done` event enforces strict chat serialization — only one tab's message streams at a time.

**JWT validated once at connect.** After auth, the proxy uses its own bearer token to talk to containers. No mid-session re-validation.

**Session switching via upstream reconnect.** ZeroClaw selects sessions at WS connect time (`?session_id=X`). Switching requires closing and reopening the upstream WS while keeping the frontend WS alive. Per-tab: `session.switch` / `session.create` affect only the caller. Cross-tab: `session.delete` force-moves affected tabs to the default (or a new session), `session.rename` broadcasts to all user tabs.

**Bearer tokens encrypted at rest.** Fernet encryption in `container_registry.bearer_token_enc`. Key from `TOKEN_ENCRYPTION_KEY` env var. DB compromise alone can't yield container access.

**Feature-flagged activation.** ZeroClaw proxy only activates when `TOKEN_ENCRYPTION_KEY` is set. Without it, the app starts with just the health endpoint. This keeps dev + CI lightweight and allows a half-deployed state during bring-up.

**Proxy-hosted LifeAtlas tools.** ZeroClaw containers call LifeAtlas helpers through proxy-hosted HTTP skill URLs. The proxy maps the per-container token to a `user_id`, keeps Supabase service-role credentials in-process, and ignores caller-supplied user/profile identity. Most tools are read-only; `save_to_library` is the narrow write path for adding agent-created PDF/CSV/PNG/JPEG files to the website's `health_data` library. See [lifeatlas-tools.md](lifeatlas-tools.md).

**Error handling.** Generic messages to client, full details logged server-side. Container internals never leak to the browser.

**Upstream restart-and-retry.** If the upstream WS fails to join/create a `SharedSession` (container stopped/crashed while registry still says "ready"), the proxy announces a `status` frame, calls `orchestrator.restart(user_id)`, and retries once before sending `UPSTREAM_ERROR`. Same pattern for mid-session reconnect. Tests that read the first frame on failure must consume the `status` frame first.

## Container orchestrator

- Provisions Docker containers on first user login with host-assigned ports (in `host` network mode) or deterministic DNS names (in `shared` network mode).
- Per-user `asyncio.Lock` prevents duplicate provisioning.
- Volume ownership set to 65534/nobody via throwaway root busybox container.
- Health check polls `GET /health` on the published port until ready (default 30s timeout).
- Auto-cleanup on registry insert failure: stops and removes the container.
- Background `_lifecycle_loop` stub for inactive container cleanup (container stop/remove portion is still TODO; file cleanup is live for expired Supabase exports, `workspace/temp/`, and `workspace/lifeatlas/` fetched files).

### Workspace initialization

On first provision, the orchestrator scaffolds the container volume from `templates/`:

1. Copies `templates/default/` → volume root (`.zeroclaw/config.toml`, `workspace/` with seed files and subdirs).
2. Overlays per-user template if one exists (matched by first name from Supabase profile).
3. Patches `config.toml` with the deployment's LLM provider/model (`[providers]` and all `[agents.*]` sections).
4. Patches `workspace/USER.md` with the user's name and date of birth.
5. Ensures the installed LifeAtlas skill is current when the template version is newer, then renders the per-container token into `workspace/skills/lifeatlas/SKILL.toml` and `SKILL.md` if that skill exists.
6. Writes `.workspace_initialized` sentinel to skip on subsequent provisions.

Existing initialized workspaces are also checked for LifeAtlas skill drift on
provision, start, restart, and `claw-admin skills retrofit`.

### Container config strategy

| Setting | Mechanism | Why |
|---|---|---|
| Provider / model | Written into `config.toml` at provision | Delegate agents inherit the same provider without env-var plumbing |
| LLM API key | Provider-specific env var such as `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, or `OPENAI_API_KEY` | Avoids ZeroClaw treating generic `API_KEY` as a separate `default` provider profile |
| Gateway / autonomy | Baked into template `config.toml` | Rarely changes; lives with the template |
| Push webhook + channel | Env vars | Per-container bearer token + deployment-specific URL |
| Gateway bind host | `ZEROCLAW_GATEWAY_HOST=0.0.0.0` + `ZEROCLAW_GATEWAY_ALLOW_PUBLIC_BIND=true` env vars | ZeroClaw defaults to `127.0.0.1:42617`, which is unreachable through Docker's published-port proxy. Without this, Docker's in-container healthcheck still passes but `/health` over the host-mapped port gives "Connection reset by peer". |
| Pairing | `ZEROCLAW_REQUIRE_PAIRING=false` env var | The proxy already authenticates every request with the per-container bearer token; container-level pairing would add a redundant auth step for the proxy to perform on every startup. Revisit if deployment topology ever allows untrusted callers to reach the container directly. |

## Networking

Two modes, selected by `ZEROCLAW_NETWORK_MODE`:

- **`host`** (dev default): Docker publishes the container port to a random host port. URLs are `http://localhost:HOST_PORT`. Simple; works without a Docker network.
- **`shared`** (staging/prod): container joins the `lifeatlas-net` Docker network. URLs are `http://zeroclaw-{user_id[:8]}:42617` using Docker DNS. No port publishing. Production network isolation replaces port-level auth.

See [management-plane.md](management-plane.md#network-modes) for switch mechanics and the orchestrator-side branching.
