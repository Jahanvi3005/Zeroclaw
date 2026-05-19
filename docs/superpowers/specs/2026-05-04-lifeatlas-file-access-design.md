# LifeAtlas File Access for ZeroClaw

> **Status:** Implemented and merged on 2026-05-06. This design remains the
> rationale/reference; current operator-facing behavior is summarized in
> `docs/lifeatlas-tools.md` and `docs/files-and-push.md`.

## Context

`claw-auth-proxy` exposes read-only LifeAtlas health/training helpers to the
ZeroClaw agent via HTTP tools (see
`2026-04-28-lifeatlas-zeroclaw-tools-design.md`). It also has a tag-based file
flow where `[SAVE:..]` and `[DOWNLOAD:..]` tags in the agent's response are
post-processed by the proxy to upload files to a proxy-private Supabase bucket
(`workspace_files`) and substitute markdown links.

The agent currently cannot:

- Read the user's existing documents from LifeAtlas (PDFs, lab results, CSVs,
  images) stored in the website's `health_data` bucket and indexed in
  `health_data_files`.
- See or retrieve photos attached to logged events (medicine, food, training,
  exercise) stored in `event_photos` and referenced by `log_events.photo_path`.
- Discover that the user has a relevant document or photo in their library.

Additionally, when the agent uses `[SAVE:..]` to persist a file, the file lands
in the proxy-private `workspace_files` bucket where it is invisible to the
LifeAtlas website. Users cannot find or manage agent-saved files alongside
their other documents.

This spec adds agent-facing read access to the user's LifeAtlas documents and
event photos, and re-routes agent-saved files into the website's primary file
library so they become first-class LifeAtlas content.

## Goals

- Let the agent list and read user-uploaded documents stored in
  `health_data_files` (PDFs, CSVs, images).
- Let the agent list logged events from `log_events` (the existing intake-event
  table that the proxy currently has no visibility into) and fetch any photo
  attached to a specific event.
- Replace the `[SAVE:..]` tag with an explicit `save_to_library` HTTP tool that
  writes to the website's `health_data` bucket and inserts a `health_data_files`
  row, mirroring the contract enforced by
  `supabase/functions/upload-file/index.ts` in lifeatlas-core.
- Keep `[DOWNLOAD:..]` (transient inline-link sharing) unchanged.
- Auto-resolve active profile for every new tool, matching the existing
  read-tool pattern.
- Surface failures structurally (success / error_code / detail) so the agent
  can self-correct without relying on transcript inference.
- Retrofit the new tools onto already-provisioned workspaces, not just freshly
  initialized ones.

## Non-Goals

- No frontend changes. The website already lists `health_data_files` rows;
  agent-saved files appear there for free once the DB row is inserted.
- No agent vision over fetched photos in v1 (deferred until ZeroClaw learns to
  return image content blocks in tool results — see "Future work").
- No backfill of the existing legacy `workspace_files/{user_id}/permanent/*`
  content into `health_data`. Old links in old chat history continue to resolve
  until the URL itself expires (1 h TTL). Soft cutover.
- No rename of the `workspace_files` bucket. It continues to host
  `[DOWNLOAD:..]` exports.
- No widening of the website's MIME allowlist. The agent must convert
  unsupported types (e.g. markdown) to PDF before saving.
- No write side beyond `save_to_library`. The agent cannot edit timeline
  entries, log events, etc. through these tools.

## Constraints discovered

These shaped the design and are recorded so future readers don't have to
re-discover them.

1. **ZeroClaw tool results are text-only.** `ToolResult { success, output: String, error }`
   in `crates/zeroclaw-api/src/tool.rs` does not support multimodal content
   blocks. A tool cannot return an image directly to the model.
2. **`[IMAGE:path]` is parsed only on user-role messages.**
   `prepare_messages_for_provider()` in
   `crates/zeroclaw-providers/src/multimodal.rs` skips assistant and tool-result
   messages. A tool returning `[IMAGE:..]` text in its response does not cause
   the model to see the image.
3. **There is no synthetic-message injection channel.** The WS gateway accepts
   only `{"type":"message"}` frames materializing as user-role messages. The
   proxy cannot inject tool_result or assistant content from outside the model.
4. **`web_fetch` rejects binary content.** Allowed types are `text/html`,
   `text/plain`, `text/markdown`, `application/json`. PDFs and images are
   bounced. Default 500 KB cap. So delivering signed URLs to the agent is
   viable only for text content (which we already have inline via
   `extracted_content_for_bot`).
5. **Workspace tools are explicit.** `pdf_read(path)`, `glob_search(pattern)`,
   `file_write(path, content)` resolve relative to the workspace root. The
   agent can self-discover files dropped under `workspace/lifeatlas/` even
   without being told they exist.
6. **`health_data` bucket has a strict MIME allowlist:** `application/pdf`,
   `text/csv`, `image/png`, `image/jpeg`, `image/jpg`. Enforced by both the
   bucket policy and the `upload-file` edge function.
7. **`upload-file` edge function path layout is `{profile_id}/{timestamp}_{sanitized_name}`**,
   not `{user_id}/...`. The website's listing query filters by `profile_id`,
   so files with a missing or wrong `profile_id` on `health_data_files` are
   invisible to the user.
8. **Filename sanitization in the edge function** is `[^a-zA-Z0-9.-_]` → `_`.
   Stricter than the proxy's existing `sanitize_upload_basename`.
9. **`health_data_files.timeline_entry_id`** is a nullable FK that lets files
   attach to specific timeline entries. We expose it as a filter on
   `list_health_data_files` and as a `NULL` default on save.
10. **`extracted_content_for_bot`** caches AI-extracted text from
    `health_data_files`, keyed by `file_id`. Populated opportunistically by a
    separate worker (not the upload pipeline). Always check; never assume
    presence.

## Architecture

```
src/claw_proxy/
├── tools/lifeatlas/
│   ├── files.py             # NEW — FilesHelper (list_health_data_files, get_health_data_file_content)
│   ├── log_events.py        # NEW — LogEventsHelper (list_log_events, get_log_event_photo)
│   ├── save.py              # NEW — SaveHelper (save_to_library)
│   └── router.py            # EDITED — register new tool_name branches; sync→async migration
├── files/
│   ├── storage.py           # EDITED — add upload_to_health_data; bucket-parameterized signed URL
│   ├── workspace_fetch.py   # NEW — Supabase storage → container volume (read-side mirror of upload.py)
│   └── downloads.py         # EDITED — narrow DOWNLOAD_TAG_RE to DOWNLOAD only; drop SAVE branch
└── containers/
    └── workspace.py         # MAYBE EDITED — workspace path constants for `lifeatlas/`
```

Data plane:

```
Read flow (agent calls list_*/get_*):
  Agent → HTTP /zeroclaw/tools/lifeatlas/{name} → router → Helper → Supabase (DB + Storage)
                                                              └→ workspace_fetch → busybox → container volume
                                                              └→ JSON response (text inline OR workspace path)

Write flow (agent calls save_to_library):
  Agent → HTTP /zeroclaw/tools/lifeatlas/save_to_library → router → SaveHelper
        → read file from container via busybox/docker cp
        → upload_to_health_data: service-role bucket upload + health_data_files insert (atomic rollback)
        → mint signed URL → return {success, file_id, signed_url, markdown_link}

Transient share (unchanged):
  Agent emits [DOWNLOAD:path] → relay scans → downloads.py
        → bytes → workspace_files/{user_id}/exports/ (1 h TTL, lifecycle-cleaned)
        → tag rewritten to markdown link
```

Reuses:

- `resolve_active_profile_id()` from `tools/lifeatlas/common.py` for profile
  scoping (same helper read tools use).
- `write_file_via_busybox` / `read_file_via_busybox` from
  `containers/workspace.py` for volume IO (handles UID 65534 invariant).
- The existing token-based auth in `router.py` (`token` query param mapped to
  `user_id` via `token_map`).
- Service-role Supabase client (already used for admin operations).

## Tool surface

Five new agent-facing HTTP tools under `/zeroclaw/tools/lifeatlas/`:

| Tool | Purpose |
|---|---|
| `list_health_data_files` | Metadata-only list of user's documents; optional `timeline_entry_id` filter; per-row `has_extracted_text` flag. |
| `get_health_data_file_content` | Returns inline text (truncated by default; `full=true` for complete) when extraction is cached, else streams the binary into the workspace and returns a path. |
| `list_log_events` | Lists log events with `event_type`, `occurred_at`, `description`, `location`, and `has_photo` flag. Filter by `event_type` and `days`. |
| `get_log_event_photo` | Streams the photo for a specific event into the workspace; returns workspace path, container path, and a pre-formatted `image_tag` the agent pastes verbatim into its reply to display the photo to the user. |
| `save_to_library` | Saves a file from the workspace to the user's permanent `health_data` library; returns structured success/failure with `markdown_link` on success. |

All tools auto-resolve the active profile; none accept a `profile_id`
parameter.

### Response shapes

| Tool / case | Response keys |
|---|---|
| `list_health_data_files` | `files[].{file_id, filename, content_type, file_size, uploaded_at, timeline_entry_id, has_extracted_text}, count` |
| `get_health_data_file_content` (cached, short) | `file_id, filename, content_type, text, text_truncated=false, text_total_chars` |
| `get_health_data_file_content` (cached, truncated) | same + `text_truncated=true, truncation_note` |
| `get_health_data_file_content` (cached, `full=true`) | same as cached-short, with full text |
| `get_health_data_file_content` (binary) | `file_id, filename, content_type, workspace_path, container_path` |
| `list_log_events` | `events[].{event_id, event_type, occurred_at, location, description, has_photo}, count` |
| `get_log_event_photo` (present) | `event_id, filename, content_type, workspace_path, container_path, image_tag` |
| `get_log_event_photo` (no photo) | `message, event_id` |
| `save_to_library` (success) | `success=true, file_id, filename, content_type, size_bytes, signed_url, signed_url_expires_in, markdown_link` |
| `save_to_library` (failure) | `success=false, error_code, detail` |

Truncation cap: `LIFEATLAS_TEXT_PREVIEW_CHARS` env var, default 4000.

Workspace paths in responses use two forms:

- `workspace_path` — workspace-relative (`lifeatlas/files/...`,
  `lifeatlas/photos/...`); used with `pdf_read`, `glob_search`.
- `container_path` — absolute (`/zeroclaw-data/workspace/lifeatlas/...`); used
  inside `[IMAGE:..]` tags. `image_tag` on photo responses is a convenience —
  the pre-formatted `[IMAGE:{container_path}]` string.

## Read flow internals

### `files/workspace_fetch.py`

```python
async def fetch_to_workspace(
    *, orchestrator, supabase, user_id,
    bucket: str, storage_path: str,
    dest_subdir: str, dest_basename_prefix: str, original_filename: str,
) -> dict:
    container = await orchestrator.get(user_id)
    if not container:
        raise ContainerNotProvisioned()

    safe_name = sanitize_basename(original_filename)
    relative_path = f"workspace/lifeatlas/{dest_subdir}/{dest_basename_prefix}_{safe_name}"
    container_path = relative_path_to_container_path(relative_path)
    agent_relative = relative_path[len("workspace/"):]   # "lifeatlas/files/..." | "lifeatlas/photos/..."

    raw = await asyncio.to_thread(_download_sync, supabase, bucket, storage_path)
    if len(raw) > MAX_FETCH_SIZE:
        raise FetchTooLarge(len(raw))

    await asyncio.to_thread(
        write_file_via_busybox,
        orchestrator.docker, f"{orchestrator.data_dir}/{user_id}",
        relative_path, raw,
    )
    return {
        "workspace_path": agent_relative,
        "container_path": container_path,
        "filename": safe_name,
        "size_bytes": len(raw),
    }
```

`MAX_FETCH_SIZE = 25 MB` (matches the upload cap). Path is deterministic on
the id prefix, so repeat fetches overwrite the same destination — idempotent
by construction.

### Sequence: `get_health_data_file_content(file_id, full=False)`

1. `SELECT id, profile_id, filename, file_path, content_type FROM health_data_files
   WHERE id = file_id AND user_id = $u AND profile_id = $p AND deleted_at IS NULL`
   → row, or `ToolLookupError`.
2. `SELECT content FROM extracted_content_for_bot WHERE file_id = $id LIMIT 1`
   → cached text, or null.
3. **Cached path:** apply truncation if `len(text) > TEXT_PREVIEW_CHARS` and
   `full=False`; otherwise return full text. Always include `text_total_chars`
   and `text_truncated`.
4. **Binary path:** `fetch_to_workspace(bucket="health_data",
   storage_path=row.file_path, dest_subdir="files",
   dest_basename_prefix=file_id, original_filename=row.filename)`. Return
   `{file_id, filename, content_type, workspace_path, container_path}`.

### Sequence: `get_log_event_photo(event_id)`

1. `SELECT id, profile_id, photo_path FROM log_events
   WHERE id = event_id AND user_id = $u AND profile_id = $p`
   → row, or `ToolLookupError`.
2. If `photo_path IS NULL` → `{message: "No photo attached", event_id}`.
3. Else `fetch_to_workspace(bucket="event_photos", storage_path=row.photo_path,
   dest_subdir="photos", dest_basename_prefix=event_id,
   original_filename=basename(row.photo_path))` → return with `image_tag` set
   to `f"[IMAGE:{container_path}]"`.

### Workspace layout

```
/zeroclaw-data/workspace/
├── temp/                                           # browser uploads (existing)
│   └── {timestamp}_{filename}
└── lifeatlas/                                      # NEW — agent-fetched LifeAtlas content
    ├── files/{file_id}_{filename}                  # health_data documents
    └── photos/{event_id}_{filename}                # event_photos images
```

### Lifecycle

`_lifecycle_loop` gains a sweep over `workspace/lifeatlas/{files,photos}/` with
the same 24 h TTL as `workspace/temp/`. Same per-tick interval
(`LIFECYCLE_CHECK_INTERVAL_MINUTES`, default 60). Re-fetch is cheap, so eviction
is invisible to the agent — a stale id-based reference is just a re-fetch
away.

## Write flow internals

### `files/storage.py` additions

```python
HEALTH_DATA_BUCKET = "health_data"
HEALTH_DATA_ALLOWED_MIME = frozenset([
    "application/pdf", "text/csv",
    "image/png", "image/jpeg", "image/jpg",
])
_HEALTH_DATA_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")


class UnsupportedHealthDataMime(ValueError): ...


def _sanitize_health_data_filename(name: str) -> str:
    return _HEALTH_DATA_FILENAME_RE.sub("_", name)


def _upload_to_health_data_sync(
    supabase_admin, *,
    user_id, profile_id, file_bytes, filename, content_type,
) -> tuple[str, str]:
    if content_type not in HEALTH_DATA_ALLOWED_MIME:
        raise UnsupportedHealthDataMime(content_type)

    timestamp = int(time.time() * 1000)
    safe_name = _sanitize_health_data_filename(filename)
    path = f"{profile_id}/{timestamp}_{safe_name}"

    bucket = supabase_admin.storage.from_(HEALTH_DATA_BUCKET)
    bucket.upload(path, file_bytes, {"content-type": content_type, "upsert": "false"})

    try:
        inserted = supabase_admin.table("health_data_files").insert({
            "user_id": user_id, "profile_id": profile_id,
            "filename": filename,         # original name preserved on row
            "file_path": path,            # sanitized path used for storage
            "file_size": len(file_bytes), "content_type": content_type,
        }).execute()
        if not inserted.data:
            raise RuntimeError("health_data_files insert returned no row")
        file_id = inserted.data[0]["id"]
    except Exception:
        try:
            bucket.remove([path])
        except Exception:
            log.warning("rollback of orphaned %s failed", path, exc_info=True)
        raise
    return path, file_id
```

Mirrors `supabase/functions/upload-file/index.ts` exactly (path layout, MIME
allowlist, atomic rollback, filename preservation). Drift surface annotated
with a comment pointing at the edge function.

`get_signed_url` gains a `bucket: str = BUCKET_NAME` parameter so callers can
target `health_data` without touching the workspace_files-default code path.

### `tools/lifeatlas/save.py`

```python
class SaveHelper:
    def __init__(self, supabase, orchestrator):
        self.db = supabase
        self.orch = orchestrator

    async def save_to_library(self, user_id: str, workspace_path: str) -> dict:
        # 1. Validate workspace_path (sandboxed, normalized, no traversal)
        # 2. Look up container; read bytes via _read_file_with_fallback_sync
        # 3. Validate size <= MAX_FETCH_SIZE
        # 4. Determine content_type via mimetypes.guess_type; check allowlist
        # 5. resolve_active_profile_id
        # 6. upload_to_health_data (atomic bucket + DB)
        # 7. Mint signed URL
        # 8. Return {success: true, ...} including markdown_link
        # On any structured failure, return {success: false, error_code, detail} — never raise.
```

Returning structured failures (rather than raising) keeps the router branch
trivial — no exception-mapping table is needed for this tool. Same pattern as
ZeroClaw's `web_fetch`.

### `[DOWNLOAD:..]` simplification

`DOWNLOAD_TAG_RE` narrows from `\[(DOWNLOAD|SAVE):([^\]]+)\]` to
`\[DOWNLOAD:([^\]]+)\]`. The `tag_type` fork in `_materialize_signed_url`
disappears; only the workspace_files-write path remains. Existing
`[DOWNLOAD:..]` behavior unchanged.

### Tag asymmetry (intentional)

- **`[DOWNLOAD:path]`** accepts any MIME type; the file lives 1 h in
  `workspace_files`. Used for transient inline-link sharing.
- **`save_to_library(workspace_path)`** requires a website-supported MIME
  (`application/pdf`, `text/csv`, `image/png`, `image/jpeg`, `image/jpg`); the
  file is persisted in `health_data` with a `health_data_files` row and
  appears on the website's file list.

The skill prompt teaches the distinction in one sentence so the agent picks
the right tool for each intent.

## Error handling

### New exception types

```python
class ToolLookupError(Exception):       """File / event not found or not owned by user."""
class ContainerNotProvisioned(Exception): """No active container for user."""
class FetchTooLarge(Exception):         """Storage object exceeds MAX_FETCH_SIZE."""
class StorageFetchError(Exception):     """Bucket download failed."""
```

### Router-level mapping (read tools)

| Exception | Status | Body |
|---|---|---|
| `ToolLookupError` | 404 | `{"error": "not_found", "detail": <text>}` |
| `ContainerNotProvisioned` | 409 | `{"error": "container_not_provisioned", "detail": "..."}` |
| `FetchTooLarge` | 413 | `{"error": "file_too_large", "detail": "File is N MB; the fetch limit is 25 MB."}` |
| `StorageFetchError` | 502 | `{"error": "storage_unavailable", "detail": "..."}` |
| `ValueError` (no active profile) | 409 | `{"error": "no_active_profile", "detail": "..."}` |
| anything else | 502 | `{"error": "tool_unavailable"}` (existing fallback) |

### `save_to_library` error codes

Returned in the 200 OK response body as `{success: false, error_code, detail}`:

| `error_code` | Meaning |
|---|---|
| `path_invalid` | Workspace path missing, escapes sandbox, or resolves to nothing |
| `file_too_large` | > 25 MB |
| `unsupported_mime` | Not in the website allowlist |
| `no_active_profile` | `resolve_active_profile_id` raised |
| `storage_unavailable` | Bucket upload failed |
| `db_unavailable` | `health_data_files` insert failed (after rollback) |
| `container_unavailable` | Couldn't read file from container |

### Empty-result vs error semantics

- "No files for this timeline entry" → empty list, not error.
- "Event has no photo" → `{message, event_id}`, not error.
- Errors are reserved for "the agent asked for something specific and we
  cannot deliver."

### Logging

Existing convention preserved: `user_id[:8]` only, never full UUIDs, never
filenames. Failures at WARNING; successful writes at INFO with size and
content type only.

## Skill template + retrofit

`templates/default/workspace/skills/lifeatlas/SKILL.toml` gains five new
`[[tools]]` blocks (`list_health_data_files`,
`get_health_data_file_content`, `list_log_events`, `get_log_event_photo`,
`save_to_library`) and bumps `version = "0.1.0"` → `version = "0.2.0"`.

`SKILL.md` gains three short paragraphs describing when to use each new tool
group, including the truncation pattern, the photo-display via `image_tag`,
and the asymmetry between `save_to_library` and `[DOWNLOAD:]`.

### Retrofit on container start

At design time, CLAUDE.md noted that skills were template-installed only on
first workspace init. This change shipped with new tools, so the implementation
adds a version check on provision/start/restart:

```python
async def _ensure_skills_current(self, container_info, user_id):
    template_version = read_template_version()
    installed_version = await read_installed_skill_version(container_info)
    if installed_version == template_version:
        return
    await render_skill_template(container_info, user_id)   # overwrites SKILL.toml + SKILL.md
```

Safe because skill files are template-owned, not user-owned. Idempotent:
same version → no-op. New users get the same code path (no installed version
→ render).

A manual fallback admin command — `claw-admin skills retrofit` — walks known
container targets from the token map and registry rows and triggers the same
path, useful if any containers were started before the auto-retrofit hook
landed.

## Testing

Unit tests (mock supabase + orchestrator) live under
`tests/tools/lifeatlas/{test_files,test_log_events,test_save,test_router}.py`
and `tests/files/{test_storage,test_workspace_fetch,test_downloads}.py`.

Coverage:

- Read helpers: ownership filtering, soft-delete exclusion,
  `timeline_entry_id` filter, `has_extracted_text` flag, truncation default,
  `full=true` override, container-not-provisioned, fetch-too-large,
  storage-error mapping.
- `SaveHelper`: happy path, MIME rejection, path traversal, absolute path,
  oversize, container missing, bucket-upload fail, DB-insert fail with
  rollback, rollback-fail logging, no-active-profile.
- `upload_to_health_data`: path layout, content-type passthrough, row shape
  matches edge function, atomic rollback.
- Router: new dispatch branches, query-param parsing, UUID validation,
  exception mapping, regression for existing tools after sync→async migration.
- `downloads.py`: regex narrowed to DOWNLOAD; mixed-tag input handles each tag
  per its rule.

Integration test (`-m integration`,
`tests/integration/test_lifeatlas_files_e2e.py`) — requires real Docker and a
test Supabase project. Walks the full flow: seed file rows, list, fetch
(cached + uncached), event photos, save, MIME-rejection. Uses the existing
`reclaim_volume` fixture for UID 65534 cleanup. Each test is namespaced by a
random UUID-prefixed test user.

Lifecycle regression: integration test that fetches a file, ages its mtime
artificially, runs the lifecycle pass, asserts removal.

### Out of test scope

- Edge function correctness (lifeatlas-core's responsibility).
- ZeroClaw `pdf_read`, `glob_search`, `[IMAGE:..]` parsing (ZeroClaw's
  responsibility).
- Frontend rendering of `markdown_link` (frontend's responsibility).

## Out of scope and future work

- **Agent vision over fetched photos.** ZeroClaw tool results are text-only
  and `[IMAGE:..]` parses on user messages only (constraints 1, 2). Until
  ZeroClaw learns to return image content blocks in tool results, the model
  itself cannot "see" a photo the agent fetched on the same turn. The chosen
  v1 path lets the agent display photos to the user (via `image_tag` in its
  reply); the model reasons over event metadata, not pixel content. Path 2 in
  the brainstorm — patching ZeroClaw — is the upgrade target.
- **Caching `pdf_read` output back into `extracted_content_for_bot`.** When
  the agent extracts text from a previously-uncached PDF, that text could be
  persisted for future zero-fetch reads. Cross-cutting (touches schema
  ownership). Defer.
- **Refreshing TTL on access for fetched files.** Long sessions could see
  eviction-then-refetch loops. Cheap to refetch, but if it becomes annoying,
  extend `_lifecycle_loop` to skip files touched within the window.
- **Widening the edge function's MIME allowlist.** Markdown / plain text /
  docx are the most likely casualties of the current list. Coordinated with
  lifeatlas-core, not in this spec.
- **Migrating legacy `workspace_files/{user_id}/permanent/*` content into
  `health_data`.** Soft cutover for v1. If telemetry shows enough legacy
  content, a one-shot backfill job could be written.
- **Renaming `workspace_files`.** Now misnomer (downloads-only). Supabase
  doesn't support direct rename; create-new + copy + repoint is more churn
  than the value. Skip.
- **Symmetric DOWNLOAD failure notices.** `[DOWNLOAD:..]` keeps its
  leave-verbatim failure behavior (existing). If consistency with SAVE is
  desired later, port the inline-notice substitution from the discarded §5
  amendment.
- **Per-call timeline_entry_id linkage on save.** `save_to_library` inserts
  `timeline_entry_id = NULL`. If the agent learns to save artifacts attached
  to a specific timeline entry, add an optional `timeline_entry_id` parameter.

## Verifications required during implementation

- **`event_photos` content-type inference.** `mimetypes.guess_type` on the
  storage path's extension should reliably return one of the bucket-allowed
  types. Add an integration assertion.
- **Skill version detection.** Confirm the on-container `SKILL.toml` parses
  cleanly enough to extract `version` even when ZeroClaw is mid-startup. If
  not, fall back to a sentinel file under `workspace/skills/lifeatlas/`.
- **Atomic rollback observability.** Verify (in integration) that an injected
  DB-insert failure leaves no orphaned bucket object.
- **`_lifecycle_loop` doesn't sweep root-owned files.** If anything under
  `workspace/lifeatlas/` ends up root-owned (unlikely but possible if busybox
  semantics shift), confirm the sweep doesn't choke.
- **`save_to_library` path validation.** Test edge cases: trailing slashes,
  symlinks (if busybox cp follows them), unicode filenames.
