# Docker deployment

This guide deploys `claw-auth-proxy` as a Docker container that can orchestrate per-user ZeroClaw containers through the Docker API. It is the deployment shape meant for staging and production: the proxy container and the ZeroClaw containers share a private Docker network, and the individual ZeroClaw containers do not publish host ports.

```
Browser / frontend
        |
        v
Reverse proxy with TLS
        |
        v
claw-proxy container -------------- Supabase
        |
        | DOCKER_HOST=tcp://docker-socket-proxy:2375
        v
docker-socket-proxy --------------- Docker daemon
        |
        v
zeroclaw-<user> containers on lifeatlas-net
```

The canonical checked-in stack lives in [docker-compose.yml](../docker-compose.yml). The equivalent [deploy/docker-compose.yml](../deploy/docker-compose.yml) is kept for workflows that already call Compose with `-f deploy/docker-compose.yml`. It starts:

- `docker-socket-proxy`, which exposes only the Docker API capabilities the orchestrator needs.
- `claw-proxy`, the FastAPI app from this repository.
- A shared Docker network named `lifeatlas-net`, where `claw-proxy` can reach user containers by DNS names such as `zeroclaw-12345678`.

## Assumptions

- You are deploying to one Linux host running Docker Engine and the Docker Compose plugin.
- The host Docker daemon has access to the ZeroClaw image named by `ZEROCLAW_IMAGE`.
- Supabase already exists for the LifeAtlas environment, and `deploy/migration.sql` has been applied once.
- A real TLS reverse proxy sits in front of `claw-proxy` for browser traffic.
- `/claw-admin/*` is restricted to an operator network, VPN, or equivalent trusted path.

## One-time host setup

Create the persistent host directories before starting Compose:

```bash
install -d -m 0750 -o 65534 -g 65534 /data/zeroclaw /data/admin
```

Command meaning:

- `install` creates filesystem entries with a chosen mode and owner.
- `-d` creates directories instead of copying files.
- `-m 0750` gives full access to the owner, read/execute to the group, and no access to others.
- `-o 65534 -g 65534` matches the `nobody` user used by ZeroClaw containers and the `user: "65534:65534"` setting in Compose.
- `/data/zeroclaw /data/admin` are the host paths mounted into the proxy as `/zeroclaw-data` and `/admin-data`.

If your host requires elevated privileges to create `/data`, run the same command through your normal host administration path.

Apply the Supabase registry migration:

- Open `deploy/migration.sql`.
- Run it once against the target Supabase project.
- Confirm the `container_registry` table exists before letting users connect.

## Images

Build or publish the proxy image:

```bash
docker build -t lifeatlas/claw-proxy:latest .
```

Command meaning:

- `docker build` builds an image from the Dockerfile.
- `-t lifeatlas/claw-proxy:latest` names the resulting image with the default tag used by `docker-compose.yml`.
- `.` uses the repository root as the build context.

Make sure the helper and ZeroClaw images are available to the same host Docker daemon:

```bash
docker image inspect busybox:latest zeroclaw:latest
```

Command meaning:

- `docker image inspect` checks that an image tag exists and prints its metadata.
- `busybox:latest` is used by the orchestrator for volume ownership and stopped-container file operations.
- `zeroclaw:latest` is the default `ZEROCLAW_IMAGE`; use your configured tag if different.

If you receive a "No such image" error for BusyBox, pull it:

```bash
docker pull busybox:latest
```

Command meaning:

- `docker pull` downloads an image into the host Docker daemon.
- `busybox:latest` is the tag the orchestrator references as `busybox`.

If ZeroClaw is missing, build, pull, or load ZeroClaw before starting the proxy. The image must exist in the host Docker daemon because the orchestrator asks Docker to create containers from it.

## Environment

Create a root `.env` file next to [docker-compose.yml](../docker-compose.yml). Compose reads it for variable interpolation, and the Compose file passes the needed values into the container.

Minimum production shape:

```dotenv
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=sb_publishable_...
SUPABASE_SERVICE_ROLE_KEY=sb_secret_...

TOKEN_ENCRYPTION_KEY=replace-with-a-long-random-secret
ADMIN_JWT_SECRET=replace-with-a-different-long-random-secret

ALLOWED_ORIGINS=https://lifeatlas.example.com
CLAW_PROXY_IMAGE=lifeatlas/claw-proxy:latest
CLAW_PROXY_PORT=8000
ZEROCLAW_IMAGE=zeroclaw:latest
ZEROCLAW_HOST_DATA_DIR=/data/zeroclaw
ADMIN_HOST_DATA_DIR=/data/admin

ADMIN_WEBAUTHN_RP_ID=proxy.example.com
ADMIN_WEBAUTHN_RP_NAME=Claw Admin
ADMIN_WEBAUTHN_ORIGIN=https://proxy.example.com

# Optional cloud LLM settings for newly provisioned ZeroClaw containers.
# ZEROCLAW_LLM_API_KEY=sk-...
# ZEROCLAW_LLM_PROVIDER=openrouter
# ZEROCLAW_LLM_MODEL=anthropic/claude-sonnet-4-20250514
```

Important details:

- `TOKEN_ENCRYPTION_KEY` is also the feature flag. Without it, the app exposes `/health` only; `/zeroclaw/*` and `/claw-admin/*` are not mounted.
- `ADMIN_JWT_SECRET` should be stable and secret. Changing it invalidates active admin sessions.
- `SUPABASE_SERVICE_ROLE_KEY` bypasses RLS. Treat the `.env` file like a secret.
- `ALLOWED_ORIGINS` must include the browser frontend origin, not necessarily the proxy origin.
- In this Compose setup, `ZEROCLAW_NETWORK_MODE`, `ZEROCLAW_PUSH_WEBHOOK_BASE_URL`, `ZEROCLAW_DATA_DIR`, `ZEROCLAW_TEMPLATES_DIR`, `ADMIN_DATA_DIR`, and `USER_CONFIG_ALLOWLIST_PATH` are set by [docker-compose.yml](../docker-compose.yml). Override the Compose file only if the in-container mount layout changes.
- `ZEROCLAW_HOST_DATA_DIR` and `ADMIN_HOST_DATA_DIR` are host paths. Leave them at `/data/zeroclaw` and `/data/admin` unless your host stores persistent app data somewhere else.

To generate a random secret on a host with OpenSSL:

```bash
openssl rand -base64 48
```

Command meaning:

- `openssl rand` generates cryptographic random bytes.
- `-base64` prints those bytes in a copy-friendly text form.
- `48` requests 48 random bytes before base64 encoding.

## Start the stack

Run Compose from the repository root:

```bash
docker compose up -d --build
```

Command meaning:

- `docker compose` runs the Compose plugin.
- `up` creates or updates the services and network.
- `-d` keeps the stack running in the background.
- `--build` rebuilds the proxy image before starting if the Dockerfile or source changed.

Check service state:

```bash
docker compose ps
```

Command meaning:

- `ps` lists the services in this Compose project and their current state.

Watch first-start logs:

```bash
docker compose logs -f claw-proxy
```

Command meaning:

- `logs` prints service logs.
- `-f` follows new log lines until interrupted.
- `claw-proxy` limits output to the FastAPI service.

On first start, the admin bootstrap prints a one-time enrollment token and static CLI token at warning level. Store them out of band. They are not printed again after the encrypted admin store exists in `/data/admin`.

Health check:

```bash
curl -fsS http://127.0.0.1:8000/health
```

Command meaning:

- `curl` performs an HTTP request.
- `-f` exits non-zero on HTTP 400+ responses.
- `-sS` hides progress output but still prints errors.
- `http://127.0.0.1:8000/health` hits the host-published proxy health endpoint.

Expected output:

```json
{"status":"ok"}
```

## Reverse proxy

Terminate TLS in front of port `8000`. Use [deploy/Caddyfile.example](../deploy/Caddyfile.example) or [deploy/nginx.conf.example](../deploy/nginx.conf.example) as a starting point.

Minimum routing requirements:

- Public frontend traffic can reach `/health` and `/zeroclaw/*`.
- WebSocket upgrade headers are preserved for `/zeroclaw/ws`.
- `/claw-admin/*` is restricted by VPN CIDR, private network, identity-aware proxy, or another operator-only control.
- `ADMIN_WEBAUTHN_RP_ID` is the exact browser-visible host, for example `proxy.example.com`.
- `ADMIN_WEBAUTHN_ORIGIN` is the exact browser-visible origin, for example `https://proxy.example.com`.

## First user container

The proxy provisions a ZeroClaw container lazily when an authenticated user first opens the WebSocket. In shared mode:

- The container is named `zeroclaw-<first-8-user-id-chars>`.
- The container joins `lifeatlas-net`.
- No ZeroClaw port is published to the host.
- The proxy reaches it at `http://zeroclaw-<prefix>:42617`.
- The container calls back to the proxy at `http://claw-proxy:8000/zeroclaw/push`.

After a user connects, verify Docker created the container:

```bash
docker ps --filter name=zeroclaw-
```

Command meaning:

- `docker ps` lists running containers.
- `--filter name=zeroclaw-` limits the list to containers whose names include `zeroclaw-`.

## Updates

For a local rebuild:

```bash
docker compose up -d --build claw-proxy
```

Command meaning:

- `docker compose` runs the Compose plugin.
- `up` creates or updates services.
- `-d` keeps the service running in the background.
- `--build` rebuilds the proxy image before recreating the service.
- `claw-proxy` limits the operation to the proxy service.

For a registry-backed deploy:

```bash
docker compose pull claw-proxy
docker compose up -d claw-proxy
```

Command meaning:

- `docker compose` runs the Compose plugin.
- `pull claw-proxy` fetches the image for the `claw-proxy` service.
- `up -d claw-proxy` recreates only that service if its image or configuration changed.

Existing user containers are not recreated just because the proxy image changes. To roll out a new ZeroClaw image to a user, stop/remove/reprovision through the admin plane or a deliberate container maintenance process. The in-container `zeroclaw update` path is currently not the deployment mechanism.

## Backups

Back up these together:

- `/data/zeroclaw` for per-user workspaces.
- `/data/admin` for encrypted admin, job, and audit stores.
- Supabase `container_registry`.
- The current `TOKEN_ENCRYPTION_KEY`.

The encryption key is part of the backup set. Without it, stored container bearer tokens and admin data cannot be decrypted.

## Troubleshooting

`/zeroclaw/ws` or `/claw-admin/*` returns 404:

- `TOKEN_ENCRYPTION_KEY` is missing from the container environment.
- Check `docker compose exec claw-proxy env` and restart after fixing `.env`.

The proxy exits on startup with a shared-network error:

- `ZEROCLAW_NETWORK_MODE=shared` only runs inside Docker. Start with the Compose file, or use `ZEROCLAW_NETWORK_MODE=host` for local non-container development.

Provisioning fails with an image error:

- The host Docker daemon cannot find `ZEROCLAW_IMAGE`.
- Run `docker image inspect <tag>` on the host and fix the tag in `.env` or publish the image.

Provisioning or admin bootstrap hits permission errors:

- `/data/zeroclaw` and `/data/admin` are probably not writable by UID/GID `65534`.
- Re-check the host directory ownership and mode from the one-time setup step.

The proxy cannot talk to Docker:

- Confirm `docker-socket-proxy` is running in Compose.
- Confirm `DOCKER_HOST=tcp://docker-socket-proxy:2375` is present inside `claw-proxy`.
- Do not expose `docker-socket-proxy` outside the Compose internal network.

Push messages do not appear:

- The proxy endpoint may be live, but the deployed ZeroClaw image must include the LifeAtlas channel support that calls the proxy webhook.
- Confirm the container has `ZEROCLAW_CHANNELS_LIFEATLAS_ENABLED=true`, `ZEROCLAW_CHANNELS_LIFEATLAS_WEBHOOK_URL`, and `ZEROCLAW_CHANNELS_LIFEATLAS_AUTH_TOKEN`.

LifeAtlas skill tools return `401`:

- The generated `workspace/skills/lifeatlas/SKILL.toml` probably has a missing or stale token.
- New workspaces get the token rendered during first provision. Existing initialized workspaces are checked on provision/start/restart; run `uv run claw-admin skills retrofit` to force a retrofit pass across known containers.
