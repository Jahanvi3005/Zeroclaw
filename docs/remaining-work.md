# Remaining work

Known gaps, deferred items, and pointers to the specs that track them.

## Planned but not done

- **Lifecycle cleanup: inactive containers.** `_lifecycle_loop` in `app.py` handles file cleanup (expired Supabase exports + old volume temp files) but the container stop/remove portion is still TODO. Needs: query `container_registry` for rows older than `INACTIVE_CONTAINER_DAYS`, call `orchestrator.stop()`, update `status` to `stopped`.

- **Integrate into LifeAtlas backend.** Mount the proxy in the main LifeAtlas Python backend so there's one deployable unit. Port the `httpx.RequestError` backstop from `src/claw_proxy/app.py`. The standalone deploy (rsync + systemd via `deploy/`) is already working for now.

- **Admin container update story.** The REST routes `POST /containers/{user}/update`, `GET /containers/{user}/update/status`, and `POST /batch/update` all exist as 501 stubs (`src/claw_proxy/admin/update_stubs.py`). Implementation was deferred until the ZeroClaw image-update protocol settles. The CLI doesn't expose these yet.

## External dependencies to verify

- **ZeroClaw LifeAtlas channel support.** The proxy push webhook and session relay are live, but proactive push requires the deployed ZeroClaw image to include the LifeAtlas channel that calls the proxy webhook. If that image support is missing, `POST /zeroclaw/push` can work in isolation while real in-container push is still absent.

## Known issues

- **`zeroclaw update` inside a provisioned container fails.** The image ships `/usr/local/bin` root-owned but ZeroClaw runs as UID 65534, so the Phase 3 backup step can't write into it. See memory note `zeroclaw-self-update-blocked.md`. Workaround: rebuild + redeploy instead of in-place self-update.

- **Garbage `/tmp/pytest-of-*/garbage-*` dirs from pre-2026-04-24 test runs.** Owned by UID 65534, pytest can't clean them. One-time cleanup documented in [testing.md](testing.md#4-pre-fix-garbage-in-tmppytest-of-garbage-).

## Spec and plan index

| Date | Scope | Doc |
|---|---|---|
| 2026-03-25 | Original zeroclaw-proxy design (WS relay, Docker orchestration, Supabase auth) | `docs/superpowers/specs/2026-03-25-zeroclaw-proxy-design.md` |
| 2026-04-03 | Workspace files + session persistence | `docs/superpowers/specs/2026-04-03-workspace-files-and-session-persistence.md`, `docs/superpowers/plans/2026-04-03-workspace-files-and-session-persistence.md` |
| 2026-04-08 | Shared-session fan-out | `docs/superpowers/plans/2026-04-08-shared-session-fanout.md` |
| 2026-04-10 | File uploads and downloads | `docs/superpowers/specs/2026-04-10-file-uploads-and-downloads.md`, `docs/superpowers/plans/2026-04-10-file-uploads-and-downloads.md` |
| 2026-04-22 | Management plane (admin REST + CLI) | `docs/superpowers/specs/2026-04-22-management-plane-design.md`, `docs/superpowers/plans/2026-04-22-management-plane.md` |
| 2026-04-28 | LifeAtlas ZeroClaw read-only helper tools | `docs/superpowers/specs/2026-04-28-lifeatlas-zeroclaw-tools-design.md`, `docs/lifeatlas-tools.md` |
| 2026-05-04 | LifeAtlas file access, event photos, and `save_to_library` | `docs/superpowers/specs/2026-05-04-lifeatlas-file-access-design.md`, `docs/superpowers/plans/2026-05-04-lifeatlas-file-access.md`, `docs/lifeatlas-tools.md` |

## History

- **Old OpenClaw proxy** (HTTP/SSE-based, shared gateway model) is preserved on branch `openclaw-archive`. The current codebase is the WS-native rewrite.
- **2026-05-06:** LifeAtlas file access branch merged. Added `health_data_files`/`log_events` tools, workspace fetches into `workspace/lifeatlas/`, `save_to_library` writes to the website `health_data` library, version-aware skill updates, `claw-admin skills retrofit`, and integration coverage against local Supabase.
- **2026-04-29:** PR #6 merged the management plane branch into `main`; follow-up `93559fd` removed test warnings from the default suite.
- **2026-04-28:** LifeAtlas read-only helper tools landed in the proxy and new workspace template. See [lifeatlas-tools.md](lifeatlas-tools.md).
- **Reports:**
  - `docs/2026-04-09-delegation-ws-timeout-report.md` — investigation into upstream WS timeouts during long agent runs.
  - `docs/2026-04-12-local-zeroclaw-smoke-test-transcript.md` — manual smoke run transcript, useful as a reference for what the provisioned container looks like in practice.
