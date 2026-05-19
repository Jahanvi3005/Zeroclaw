# Management plane

The admin REST API and `claw-admin` CLI ship in `src/claw_proxy/admin/` and `src/claw_proxy/cli/`, mounted at `/claw-admin/*`. Authentication: WebAuthn passkey (for a future dashboard) or static bearer token (for the CLI and scripts).

## File map

| File | Purpose |
|------|---------|
| `src/claw_proxy/admin/app.py` | `create_admin_app()` factory; mounts the admin router with operations + job runner. |
| `src/claw_proxy/admin/auth.py` | Static-token bcrypt verify, session JWT (PyJWT), WebAuthn register/login, `require_admin` FastAPI dep. |
| `src/claw_proxy/admin/storage.py` | `AdminStore` Protocol + `EncryptedJsonAdminStore`. Dataclasses: `Admin`, `WebAuthnCredential`, `StaticToken`, `EnrollmentToken`. |
| `src/claw_proxy/admin/jobs.py` | `AdminJobStore` (file-per-job) + `JobRunner` with cancellation + concurrency limit. |
| `src/claw_proxy/admin/audit.py` | `AuditStore` (per-line Fernet-encrypted JSONL), `sanitize_params`, `emit_audit`. |
| `src/claw_proxy/admin/enrollment.py` | First-run bootstrap + invite tokens. |
| `src/claw_proxy/admin/resolver.py` | Free-form lookup → `user_id` (UUID, email, container name, docker id prefix, First Last). |
| `src/claw_proxy/admin/activity.py` | `ActivityChecker` Protocol; v1 checks WS connections + `last_active_at`. |
| `src/claw_proxy/admin/schema_cache.py` | Per-ZeroClaw-version schema cache used for secret detection (`x-secret` metadata). |
| `src/claw_proxy/admin/operations.py` | All admin business logic: orchestrator + Docker SDK + registry, including LifeAtlas skill retrofit. |
| `src/claw_proxy/admin/routes.py` | All `/claw-admin/*` REST routes. Thin handlers — logic lives in `operations.py`. |
| `src/claw_proxy/admin/update_stubs.py` | 501-returning stubs for container update flows (deferred until ZeroClaw image update story lands). |
| `src/claw_proxy/user_config.py` | User-facing `/zeroclaw/user/config` endpoint with TOML allowlist. |
| `src/claw_proxy/cli/` | `claw-admin` Typer CLI. Entrypoint registered in `pyproject.toml`. Subcommands: `container`, `config`, `workspace`, `admin`, `orphan`, `job`, `skills`. |

For a command-by-command CLI reference with examples and implementation notes,
see [claw-admin-cli.md](claw-admin-cli.md).

## Network modes

`ZEROCLAW_NETWORK_MODE=host` (dev) keeps port publishing: `http://localhost:HOST_PORT`. `ZEROCLAW_NETWORK_MODE=shared` (staging/prod) joins `lifeatlas-net` and uses deterministic DNS (`http://zeroclaw-{user_id[:8]}:42617`). The orchestrator branches on this at provision and on the URL-build path in `get()`. Switch via env; no code change needed.

## Admin auth bootstrap

On first start, the proxy logs:

- A one-time **enrollment token** (15-min TTL) used to register the first passkey.
- A one-time **static CLI token** used for `claw-admin` auth.

Both are printed once, at WARNING level. Persist them out-of-band; they are never re-displayed. Subsequent admins are invited via `POST /claw-admin/admins/invite`; static tokens are rotated via `POST/DELETE /claw-admin/admins/static-tokens`.

`bootstrap_if_empty()` in `enrollment.py` is idempotent — it only fires when the admin store has no admins and no tokens, so restarting an already-bootstrapped proxy prints nothing.

## Activity / careful mode

State-changing ops accept `{careful: true, careful_timeout_seconds: 300}`. The proxy polls `ActivityChecker.get_state(user_id)` until idle (no WS tabs and `last_active_at` older than 60s by default) or skips on timeout. The response includes a `signals` list describing why it stayed busy (e.g. `["ws_open:2", "last_active_age:12s"]`). This is what keeps admin restarts from yanking a user mid-stream.

Batch ops (`POST /batch/restart`, `POST /batch/config.set`) use the same flag per-target and dispatch through `JobRunner` so the dashboard can watch progress via `GET /jobs/{id}`.

## Audit log

JSONL at `data/admin/audit.jsonl.enc` — each line is individually Fernet-encrypted. This makes appends O(1) without re-encrypting the whole file, and corruption of one line doesn't kill the whole log.

Default 90-day retention. `_lifecycle_loop` in `app.py` calls `audit_store.prune_older_than(ADMIN_AUDIT_RETENTION_DAYS)` which rewrites the file atomically.

`sanitize_params()` in `audit.py` is the redaction surface:
- `config.set` values whose path is a secret (per `schema_cache.is_secret_path`) are replaced with `"***REDACTED***"`.
- `workspace.write` / `workspace.patch` content is replaced with `{size, sha256}` so the log doesn't become a content archive.

## Batch jobs

`POST /batch/restart` and `POST /batch/config.set` each kick off an async `Job` stored in `data/admin/jobs/{job_id}.enc`:

- `GET /jobs` — list, filter by status/operation.
- `GET /jobs/{id}` — full record including per-target state + last-attempt error.
- `DELETE /jobs/{id}` — requests cancellation (cooperative; in-flight targets finish, pending ones skip).

The CLI `claw-admin job list/status/cancel` wraps these endpoints.

## Skill retrofit

`POST /skills/retrofit` runs the LifeAtlas skill version check across known
containers. It uses the in-memory token map plus registry rows to find targets,
then calls `ContainerOrchestrator._ensure_skills_current()` for each one. This
updates `workspace/skills/lifeatlas/SKILL.toml` and `SKILL.md` only when the
template version is newer than the installed version.

The CLI wrapper is:

```bash
uv run claw-admin skills retrofit
```

## User-facing config endpoint

`/zeroclaw/user/config` (`GET`/`PUT`) lets the regular user — authenticated via their Supabase JWT — change a **curated subset** of container config via an allowlist file (default: `data/user_config_allowlist.toml`). Reads mask secrets. Writes reject any path not in the allowlist with 403. Writes go through the same health-check-and-rollback path that admin config writes use. See `src/claw_proxy/user_config.py`.

## Deployment assets

| File | Purpose |
|------|---------|
| `deploy/migration.sql` | `container_registry` table DDL (includes `current_session_id` column). Apply once per Supabase project. |
| `deploy/log_config.yaml` | Uvicorn logging config with timestamps, logger names, levels. Pass via `--log-config deploy/log_config.yaml`. |
| `deploy/claw-proxy.service` | systemd **system** service template. Copy to `/etc/systemd/system/`, `sudo systemctl enable --now claw-proxy`. Runs as `clawgateway` (must be in `docker` group). Requires `/home/clawgateway/logs/` to exist before first start. |
| `deploy/deploy.sh` | rsync-based deploy script — pushes working tree to remote host and restarts the `claw-proxy` systemd unit. |
| `docker-compose.yml` | Canonical Compose stack for shared-network-mode deployment: socket-proxy + claw-proxy on `lifeatlas-net`. |
| `deploy/docker-compose.yml` | Equivalent Compose stack kept for workflows that already call `-f deploy/docker-compose.yml`. |
| `deploy/Caddyfile.example`, `nginx.conf.example` | Reverse-proxy configs that gate `/claw-admin/*` by VPN CIDR. |

For the operator runbook that ties these files together, see [deployment-docker.md](deployment-docker.md).

## Commit / spec references

- Plan: `docs/superpowers/plans/2026-04-22-management-plane.md` (14 phases).
- Spec: `docs/superpowers/specs/2026-04-22-management-plane-design.md`.
- Filling the four gaps found during verification (2026-04-23): commits `df20355`, `f388d3c`, `15f17fa`.
