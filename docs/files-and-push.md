# File transfer and push

How user-uploaded files enter the container, how agent-generated files leave it,
how LifeAtlas library files move through the workspace, and how ZeroClaw pushes
proactive messages back to the browser.

## Upload flow

1. Frontend uploads via `POST /zeroclaw/workspace/files` (multipart, Supabase JWT auth).
2. Proxy validates type + size, writes to the container volume at `workspace/temp/{timestamp}_{filename}` via a throwaway root busybox.
3. Response: `{filename, path, annotation}`. The annotation is `[IMAGE:path]`, `[PDF:path]`, or `[FILE:path]`.
4. Frontend appends the annotation to the user's next chat message and sends it over the WS.
5. **Sequencing contract:** the WS message must not be sent until the upload REST call returns — otherwise the container may try to process a reference to a file that isn't on disk yet.

Implementation: `src/claw_proxy/files/upload.py`. 25MB cap.

### Supported types

`image/png`, `image/jpeg`, `image/webp`, `application/pdf`, `.docx`, `text/plain`, `text/markdown`, `text/csv`.

## Download flow

1. The agent emits `[DOWNLOAD:/zeroclaw-data/workspace/...]` in its response.
2. The proxy intercepts the tag in the relay path (`chat.done`) or the push webhook (`push.message`) — never in `chat.chunk`, because partial chunks would produce partial URLs.
3. Reads the file via docker cp (busybox fallback if the container is stopped).
4. Uploads to Supabase Storage bucket `workspace_files` under `{user_id}/exports/`.
5. Replaces the tag with a markdown link `[filename](signed-url)`. Export signed URLs expire in 1 hour.
6. Best-effort: if tag processing fails, the tag stays as-is in the message and the frame still goes out. Relay is never blocked by export failures.

Implementation: `src/claw_proxy/files/downloads.py` (tag processor — used by both the relay and the push webhook) and `src/claw_proxy/files/storage.py` (Supabase Storage wrapper).

`[SAVE:...]` is no longer a tag. Permanent saves now use the explicit
LifeAtlas `save_to_library` tool, which writes to the website's `health_data`
bucket and inserts a `health_data_files` row. See [lifeatlas-tools.md](lifeatlas-tools.md#file-access-tools).

## LifeAtlas library file flow

LifeAtlas file tools use the workspace as a staging area for files that already
exist in the website:

1. `list_health_data_files` lists non-deleted `health_data_files` rows for the active profile.
2. `get_health_data_file_content` returns cached extracted text when `extracted_content_for_bot` has content.
3. If there is no cached text, the proxy downloads from the `health_data` bucket and writes to `workspace/lifeatlas/files/{file_id}_{filename}` via busybox.
4. `list_log_events` lists recent `log_events` rows with `has_photo`.
5. `get_log_event_photo` downloads from `event_photos` into `workspace/lifeatlas/photos/{event_id}_{filename}` and returns an `[IMAGE:...]` tag the agent can paste into chat.
6. `save_to_library` reads a workspace file, validates size/path/MIME, uploads it to `health_data/{profile_id}/{timestamp}_{filename}`, inserts `health_data_files`, and returns a signed link.

Implementation: `src/claw_proxy/files/workspace_fetch.py`,
`src/claw_proxy/tools/lifeatlas/files.py`,
`src/claw_proxy/tools/lifeatlas/log_events.py`, and
`src/claw_proxy/tools/lifeatlas/save.py`.

## File lifecycle

- **Uploaded files** land in `workspace/temp/` with a timestamp prefix. A cleanup pass in `_lifecycle_loop` deletes files older than 24h.
- **`[DOWNLOAD:]` exports** live under `{user_id}/exports/` with 1-hour signed URLs. Expired objects are cleaned by `_lifecycle_loop`.
- **LifeAtlas fetched files** land in `workspace/lifeatlas/files/` and `workspace/lifeatlas/photos/`; `_lifecycle_loop` deletes files older than 24h.
- **`save_to_library` saves** live in the website-owned `health_data` bucket and are indexed by `health_data_files`; they are not lifecycle-cleaned by this proxy.
- **Cleanup runs** on an interval controlled by `LIFECYCLE_CHECK_INTERVAL_MINUTES` (default 60). It does the Supabase-side expired-export sweep and the volume-side temp/LifeAtlas fetched-file sweeps in a single pass.

## Push webhook

ZeroClaw containers call `POST /zeroclaw/push` on the proxy to push proactive messages into the frontend:

- Bearer-token auth: the token is validated via the in-memory `orchestrator.token_map` (bearer → user_id). Containers get their token at provision time via the `ZEROCLAW_TOKEN` env var.
- `content: str` required. `subject?: str` optional.
- Export tags in `content` are processed the same way as in the relay path.
- Fans out a `push.message` frame to all active WS connections for that user via `shared_session.get_connections()`.

If there are no open WS connections for the user, the push is dropped (a notification queue is deliberately out of scope — the spec treats push as a "best-effort interrupt while the user is active").

Implementation: `src/claw_proxy/push.py`.

## Why export tags instead of structured fields

Agents can't reliably emit structured JSON inline in a streaming response without breaking the text UX. Tags are inert-looking text that the proxy rewrites at substitution time, so the user sees a natural link and the agent writes them as plain text in any position. This matches how `[IMAGE:]` / `[PDF:]` uploads flow in the other direction.

Only transient downloads use this tag path now. Permanent LifeAtlas saves use
the structured `save_to_library` tool so the proxy can return clear error codes
for invalid paths, unsupported MIME types, missing profiles, and storage/DB
failures.
