# Testing

How to run the suites, what's mocked where, and the non-obvious traps that cost debugging time in this codebase.

## Running tests

```bash
# Non-integration (default): mocks all external boundaries, ~25s
uv run pytest

# Integration only: requires local Docker daemon + a `zeroclaw:latest` image.
# LifeAtlas file-access integration tests also require local Supabase.
uv run pytest -m integration

# Everything
uv run pytest -m ""
```

pytest config lives in `pyproject.toml`:

- `asyncio_mode = "auto"` — any `async def test_...` is automatically picked up as an asyncio test. Don't use `asyncio.run()` in tests.
- `addopts = "-m 'not integration'"` — integration tests are deselected by default. You have to opt in.

## Mock boundaries

When writing a new test, these are the injection points the existing suite uses:

- `UpstreamConnection` — mocked in proxy tests. Don't test real WS in unit tests.
- `http_client` — mocked for Supabase / GoTrue / ZeroClaw REST calls. See also the cross-loop hazard below.
- `auth_fn` — injected into `create_ws_app()`, returns a fake user dict.
- `orchestrator` — `AsyncMock` in proxy tests.
- Docker SDK — mocked at `orchestrator.docker` module level.
- `job_store`, `admin_store`, `audit_store` — real `Encrypted*` implementations over `tmp_path` in admin tests (see `tests/admin/conftest.py`). Fast enough to not mock, and covers the encryption paths for free.

## Test files at a glance

### Core proxy

| File | Covers |
|------|--------|
| `tests/test_crypto.py` | Encryption roundtrip, wrong-key failure, ciphertext uniqueness. |
| `tests/test_protocol.py` | All translation functions, session command parsing. |
| `tests/test_db.py` | Registry CRUD with mocked Supabase REST. |
| `tests/test_orchestrator.py` | Provision (new, existing, with templates), stop. |
| `tests/test_upstream.py` | URL building, send. |
| `tests/test_shared_session.py` | `SharedSession` lifecycle, broadcasts, `get_connections`. |
| `tests/test_proxy.py` | Auth, connect, relay, session commands, persistence. |
| `tests/test_proxy_e2e.py` | Full connect-send-stream flow. |
| `tests/test_push.py` | Push webhook: auth, routing, delivery, export tags. |
| `tests/test_workspace.py` | Template resolution + copy, `USER.md` patching, busybox helpers, path normalization, temp cleanup. |
| `tests/test_upload.py` | Upload endpoint: binary files, MIME validation, annotations, size limit. |
| `tests/test_downloads.py` | `[DOWNLOAD:]` export tag processing: path validation, multiple tags, docker cp + busybox fallback, failure degradation. |
| `tests/test_storage.py` | Supabase Storage: bucket creation, upload, signed URLs, export cleanup. |
| `tests/test_lifeatlas_common.py` | LifeAtlas tool parameter parsing and active-profile resolution. |
| `tests/test_lifeatlas_helpers.py` | LifeAtlas helper query behavior with mocked Supabase chains. |
| `tests/test_lifeatlas_router.py` | Tool router auth, dispatch, parameter errors, helper failures, app mount. |
| `tests/test_lifeatlas_files.py` | `health_data_files` listing, extracted-text lookup, binary workspace fetch. |
| `tests/test_lifeatlas_log_events.py` | `log_events` listing and event-photo fetch behavior. |
| `tests/test_lifeatlas_save.py` | `save_to_library`: path validation, read bounds, MIME checks, upload/rollback, error mapping. |
| `tests/test_lifeatlas_skill_template.py` | ZeroClaw skill template shape and token rendering. |
| `tests/test_workspace_fetch.py` | Supabase Storage object download into `workspace/lifeatlas/`. |
| `tests/test_skills_retrofit.py` | CLI `claw-admin skills retrofit` command wrapper. |

### Management plane

| File | Covers |
|------|--------|
| `tests/admin/test_*.py` | Admin stores, auth (static-token, JWT, WebAuthn), operations, routes, jobs, audit, enrollment. Shared fixtures in `tests/admin/conftest.py`. |
| `tests/cli/test_*.py` | CLI config loader, HTTP client, command groups. |

### Integration (marker-gated)

| File | Covers |
|------|--------|
| `tests/integration/conftest.py` | Real Docker fixtures: `real_docker_client`, `zeroclaw_image`, `real_data_dir`, `cleanup_container`, `reclaim_volume`, autouse `stub_get_profile`. |
| `tests/integration/test_provision_real.py` | End-to-end provision + stop against a real ZeroClaw container. |
| `tests/integration/test_admin_ops_real.py` | End-to-end workspace write + config read through `AdminOperations` against a real container. |
| `tests/integration/test_full_e2e_real.py` | Full local journey: Supabase auth, WS provision/chat, health, upload, static-token admin config/workspace/lifecycle actions, restart chat, final stop/cleanup. |
| `tests/integration/test_lifeatlas_file_access.py` | Local Supabase + Docker coverage for listing/fetching `health_data_files`, event photos, `save_to_library`, and lifecycle cleanup of `workspace/lifeatlas/`. |

## Integration-test gotchas (learned the hard way — 2026-04-24)

### 1. The shared `http_client` is a cross-loop hazard

`src/claw_proxy/config.py:24` is a **module-level** `httpx.AsyncClient(timeout=30)`. It binds to whichever event loop first makes a request on it and holds connection-pool state there. Any test that:

- Calls `asyncio.run()` multiple times (each creates + closes a loop), or
- Uses pytest-asyncio's default function-scoped loops **and** touches `claw_proxy.db` / `claw_proxy.auth` across tests

…will crash on the second use with `RuntimeError: Event loop is closed` inside `anyio._backends._asyncio._transport.close()`.

**How the integration suite avoids this:**

- Tests are `async def` and let pytest-asyncio (auto mode) own the loop for the whole test body — no per-call `asyncio.run()`.
- `tests/integration/conftest.py` has an autouse `stub_get_profile` fixture that replaces `claw_proxy.db.get_profile` with an empty stub. Integration tests already mock the registry, so they don't actually need Supabase — removing the real HTTP call takes the shared client out of the critical path.

If you ever refactor `config.py` to a lazy-per-loop `get_http_client()`, delete both mitigations and the memory note `shared-http-client-loop-hazard.md`.

### 2. Volume UID mismatch between production and tests

The orchestrator chowns every provisioned volume to UID 65534 (`nobody`) via a root busybox container so the ZeroClaw process inside the container can read/write it. `AdminOperations` methods (`write_workspace_file`, `read_config`, etc.) do **host-side IO** on that volume — fine in production, where the proxy process itself runs as UID 65534 with the volume shared into the proxy container.

In tests running as UID 1000, host-side IO against a 65534-owned dir gives `PermissionError: [Errno 13]`.

**Fix:** use the `reclaim_volume` fixture after provisioning to chown the volume back to the test UID via another busybox run:

```python
async def test_something(real_data_dir, reclaim_volume, ...):
    info = await orch.provision(user_id)
    reclaim_volume(os.path.join(str(real_data_dir), user_id))
    # ...host-side IO works now
```

The `real_data_dir` fixture also runs a chown-back in its teardown so pytest's `tmp_path_factory` can delete the tree without whining about `PermissionError`.

Code paths that need to write one file into a 65534-owned workspace should use
`write_file_via_busybox()`. It first tries the Docker-host-visible staging dir
inside the volume, then falls back to a host temp-dir bind mount if local
staging is not writable. The fallback is what keeps VM integration tests green
when the proxy process and container UID do not match.

### 3. Docker healthcheck ≠ externally reachable

ZeroClaw's Docker healthcheck runs `zeroclaw status --format=exit-code` **inside** the container, so it passes whenever the daemon is alive — even if the daemon only bound to `127.0.0.1` and is unreachable through Docker's published-port proxy. This masked the real failure for a long time.

The orchestrator now forces `ZEROCLAW_GATEWAY_HOST=0.0.0.0` + `ZEROCLAW_GATEWAY_ALLOW_PUBLIC_BIND=true` + `ZEROCLAW_REQUIRE_PAIRING=false` (see [architecture.md](architecture.md#container-config-strategy)), so provisioned containers are actually reachable on their published ports. Without those, integration tests silently time out at the 30s `_wait_healthy` mark.

If you see `Connection reset by peer` from an otherwise-"healthy" container during manual testing, check the env dict first.

### 4. Pre-fix garbage in `/tmp/pytest-of-*/garbage-*`

Tests that failed before the above fixes left 65534-owned dirs in `/tmp/pytest-of-*/garbage-*/`. Pytest tries to clean them on startup, can't (UID mismatch), and spams `PermissionError` warnings. One-time cleanup:

```bash
docker run --rm --user root -v /tmp/pytest-of-thd:/pytest busybox \
  sh -c "chown -R $(id -u):$(id -g) /pytest/garbage-*; rm -rf /pytest/garbage-*"
```

Post-fix runs don't create new garbage, because `real_data_dir` chowns back at teardown.

## Integration-test prerequisites checklist

Before `uv run pytest -m integration`:

- [ ] Docker daemon running and `docker ps` works as your user (user in the `docker` group).
- [ ] `zeroclaw:latest` image present: `docker images zeroclaw | grep latest`. Override with `ZEROCLAW_TEST_IMAGE=...` if you want a specific tag.
- [ ] No stale `zeroclaw-*` containers from previous runs: `docker rm -f $(docker ps -aq --filter name=zeroclaw-)` if in doubt.
- [ ] `.env` has `TOKEN_ENCRYPTION_KEY` set (otherwise `config.py` will fail on import during collection).
- [ ] For `test_full_e2e_real.py` and `test_lifeatlas_file_access.py`, `.env` points `SUPABASE_URL` at local Supabase (`http://127.0.0.1:54321` or `http://localhost:54321`) with matching anon and service-role keys. These tests intentionally skip for hosted Supabase URLs.

## Related memory notes

- `shared-http-client-loop-hazard.md` — the cross-loop trap, referenced above.
- `zeroclaw-container-env.md` — the full env-var set needed for provisioned containers.
- `zeroclaw-self-update-blocked.md` — why `zeroclaw update` fails in the current image (UID mismatch on `/usr/local/bin`).
