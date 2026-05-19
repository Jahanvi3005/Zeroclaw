# Configuration

All configuration comes from environment variables. `python-dotenv` auto-loads `.env` at import time from the working directory. See `.env.example` for a complete template.

## Required

| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` | Supabase project URL. Used for JWT verification (GoTrue) and registry REST calls. |
| `SUPABASE_ANON_KEY` | Public anon key — GoTrue uses it when validating user JWTs. |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key for registry DB access. **Bypasses RLS** — protect it. |

Missing any of these raises `RuntimeError` at module import (`config.py` line 17-18). Tests that import anything from `claw_proxy` must have these set, even if the values are fake.

## Supabase URLs

| Variable | Default | Purpose |
|----------|---------|---------|
| `SUPABASE_PUBLIC_URL` | `SUPABASE_URL` | Browser-facing Supabase URL used when rewriting signed Storage export links. In Docker local dev, this may be a VM/LAN address while `SUPABASE_URL` inside the proxy container points at `host.docker.internal`. |
| `SUPABASE_DOCKER_URL` | unset | Compose-only override that becomes the proxy container's `SUPABASE_URL`. Use it when the proxy container must reach local Supabase through `host.docker.internal` while browser links should keep using `SUPABASE_PUBLIC_URL`. |

## Feature flags

| Variable | Purpose |
|----------|---------|
| `TOKEN_ENCRYPTION_KEY` | Passphrase fed into Fernet for bearer-token encryption. **Also the activation switch** for the ZeroClaw proxy path — without it, the app starts with just the `/health` endpoint and none of the WS/admin routes. |

## Server

| Variable | Default | Purpose |
|----------|---------|---------|
| `ALLOWED_ORIGINS` | `http://localhost:8081` | Comma-separated browser origins allowed by CORS. |
| `LOG_LEVEL` | `INFO` | Logging level for the proxy. |

## Compose-only deployment helpers

These are consumed by [docker-compose.yml](../docker-compose.yml) before the container starts. The Python app does not read them directly.

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAW_PROXY_IMAGE` | `lifeatlas/claw-proxy:latest` | Image tag built or pulled for the proxy service. |
| `CLAW_PROXY_PORT` | `8000` | Host port published to the reverse proxy. |
| `ADMIN_HOST_DATA_DIR` | `/data/admin` | Host directory mounted into the proxy as `/admin-data`. |

## ZeroClaw / container orchestrator

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZEROCLAW_IMAGE` | `zeroclaw:latest` | Docker image the orchestrator spawns. |
| `ZEROCLAW_DATA_DIR` | `/data/zeroclaw` | Host path where per-user workspace volumes are mounted. |
| `ZEROCLAW_HOST_DATA_DIR` | `ZEROCLAW_DATA_DIR` | Docker-daemon-visible host path for per-user workspace volumes. In Docker deployments the proxy may see the same files at `/zeroclaw-data` while the host Docker daemon needs `/data/zeroclaw`. |
| `ZEROCLAW_PUSH_WEBHOOK_BASE_URL` | `http://172.17.0.1:8000` | Base URL containers use to reach the proxy's push endpoint. `172.17.0.1` is the default Docker bridge gateway; override when running the proxy in a container on a shared network. |
| `ZEROCLAW_NETWORK_MODE` | `host` | `host` (dev) publishes ports + uses `localhost:HOST_PORT`; `shared` (staging/prod) joins a Docker network and uses DNS. See [management-plane.md](management-plane.md#network-modes). |
| `ZEROCLAW_NETWORK_NAME` | `lifeatlas-net` | Docker network name for `shared` mode. |
| `ZEROCLAW_TEMPLATES_DIR` | orchestrator fallback path | Workspace scaffold templates. Per-user overrides via named subdirs. The Docker deployment sets this to `/app/templates`. |
| `LIFECYCLE_CHECK_INTERVAL_MINUTES` | `60` | How often `_lifecycle_loop` runs (volume temp cleanup, audit pruning, expired export cleanup). |
| `INACTIVE_CONTAINER_DAYS` | `30` | Days before an idle container would be stopped (stop path itself is TODO). |

## LifeAtlas tools

| Variable | Default | Purpose |
|----------|---------|---------|
| `LIFEATLAS_TEXT_PREVIEW_CHARS` | `4000` | Maximum inline extracted-text preview returned by `get_health_data_file_content` before requiring `full=true`. |

## LLM (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ZEROCLAW_LLM_API_KEY` | — | Injected into each container as the provider-specific key env var when known, for example `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, or `OPENAI_API_KEY`; otherwise as `ZEROCLAW_API_KEY`. If unset, the container falls back to local/no-key behavior. |
| `ZEROCLAW_LLM_PROVIDER` | `openrouter` (only if API key set) | Written into `config.toml` at provision. |
| `ZEROCLAW_LLM_MODEL` | — | Written into `config.toml` at provision. Only if API key set. |

## Admin plane

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_DATA_DIR` | `data/admin` | Where the encrypted admin/job/audit stores live. |
| `ADMIN_JWT_SECRET` | `dev-admin-jwt-secret` | HS256 secret for admin session JWTs. Set a stable secret in any non-dev deployment. |
| `ADMIN_JWT_TTL_SECONDS` | `1800` in Compose | Reserved for admin session JWT lifetime. The current mounted routes pass only `ADMIN_JWT_SECRET`; no route reads this env var yet. |
| `ADMIN_WEBAUTHN_RP_ID` | `claw-admin.local` | Passkey Relying Party ID. Must match the domain serving the admin UI. |
| `ADMIN_WEBAUTHN_RP_NAME` | `Claw admin` | Human-readable RP label shown in passkey prompts. |
| `ADMIN_WEBAUTHN_ORIGIN` | `http://localhost:8000` | Expected origin for WebAuthn ceremonies. Must exactly match what the browser sees. |
| `ADMIN_AUDIT_RETENTION_DAYS` | `90` | Audit log pruning horizon. |
| `USER_CONFIG_ALLOWLIST_PATH` | `data/user_config_allowlist.toml` | TOML file listing config paths the user endpoint can read/write. |

## Environment files convention

- `.env` — active local config (git-ignored).
- `.env.example` — checked-in template with every variable listed and dummy values.
- `.env.local`, `.env.staging`, `.env.testing`, `.env.old` — checked-in-ignored snapshots the author uses for specific environments. Nothing in the code reads these directly; they are copied to `.env` when switching environments.

## Precedence

1. Actual environment variables (from the shell, systemd unit, docker-compose `environment:`, etc.).
2. `.env` file in the working directory, loaded by `python-dotenv` at module import of `claw_proxy.config`.
3. Hardcoded defaults in `config.py`.

No further sources are consulted.
