# LifeAtlas tools

LifeAtlas health, training, timeline, medicine, event, and file-library helpers
are exposed to ZeroClaw as HTTP skill tools. The proxy keeps Supabase
service-role credentials out of the container and derives the user from the
container's bearer token.

## Request flow

```text
ZeroClaw skill tool
  -> GET /zeroclaw/tools/lifeatlas/{tool_name}?token=<container-token>&...
  -> proxy maps token to user_id through orchestrator.token_map
  -> proxy resolves the active profile when the helper is profile-scoped
  -> proxy runs Supabase helper logic in a worker thread when it is sync
  -> file tools may stream objects into workspace/lifeatlas/ via busybox
  -> save_to_library may read a workspace file and write it to health_data
  -> JSON result returns to ZeroClaw
```

The router is mounted by `src/claw_proxy/app.py` only when
`TOKEN_ENCRYPTION_KEY` is set, because that is also when the ZeroClaw sub-app is
active.

## Code map

| File | Purpose |
|---|---|
| `src/claw_proxy/tools/lifeatlas/router.py` | FastAPI route, token auth, dispatch, parameter parsing, error mapping. |
| `src/claw_proxy/tools/lifeatlas/common.py` | Supabase result helpers, active-profile resolution, bounded query parsing, sync-to-thread wrapper. |
| `src/claw_proxy/tools/lifeatlas/strava.py` | Strava summaries and recent workout. |
| `src/claw_proxy/tools/lifeatlas/oura.py` | Oura sleep, readiness, recovery, and heart-rate helpers. |
| `src/claw_proxy/tools/lifeatlas/whoop.py` | Whoop recovery, sleep, strain, and heart-rate helpers. |
| `src/claw_proxy/tools/lifeatlas/timeline.py` | Timeline entries and injuries. |
| `src/claw_proxy/tools/lifeatlas/medicines.py` | Current medicines. |
| `src/claw_proxy/tools/lifeatlas/training.py` | Training plan, upcoming sessions, and load metrics. |
| `src/claw_proxy/tools/lifeatlas/events.py` | User events and selected races. |
| `src/claw_proxy/tools/lifeatlas/health_context.py` | Healthcare summary, conditions, BMR, and life-balance context. |
| `src/claw_proxy/tools/lifeatlas/files.py` | `health_data_files` list/read helpers, including extracted-text lookup. |
| `src/claw_proxy/tools/lifeatlas/log_events.py` | `log_events` list/photo helpers. |
| `src/claw_proxy/tools/lifeatlas/save.py` | `save_to_library` workspace-to-LifeAtlas-library helper. |
| `src/claw_proxy/files/workspace_fetch.py` | Supabase Storage object download into `workspace/lifeatlas/{files,photos}`. |
| `src/claw_proxy/files/storage.py` | `workspace_files` export storage plus `health_data` uploads for saved library files. |
| `templates/default/workspace/skills/lifeatlas/` | ZeroClaw skill template copied into newly provisioned workspaces. |

## Endpoint

```text
GET /zeroclaw/tools/lifeatlas/{tool_name}
```

Authentication is a `token` query parameter, not an HTTP header. This is
intentional: ZeroClaw HTTP skill tools are GET-only URL templates and do not
send custom headers. The router never accepts `user_id` or `profile_id` from
the caller.

Error responses:

| Status | Meaning |
|---|---|
| `401` | Missing or unknown token. |
| `404` | Unknown tool name, or requested file/event not found for this user/profile. |
| `400` | Invalid query parameter. |
| `409` | No active LifeAtlas profile, or container not provisioned for a workspace file operation. |
| `413` | Storage object or workspace file exceeds the 25 MB fetch/save limit. |
| `502` | Supabase/helper failure. Full details stay in server logs. |

## Tool surface

| Tool | Parameters | Scope |
|---|---|---|
| `get_weekly_training` | `weeks_ago` | user |
| `get_last_workout` | none | user |
| `get_recovery_status` | none | user |
| `get_recent_sleep` | `days` | user |
| `get_recent_readiness` | `days` | user |
| `get_heart_rate_trends` | `days` | user |
| `get_whoop_recovery_status` | none | user |
| `get_whoop_recent_recovery` | `days` | user |
| `get_whoop_recent_sleep` | `days` | user |
| `get_whoop_recent_strain` | `days` | user |
| `get_whoop_heart_rate_trends` | `days` | user |
| `get_timeline_entry_types` | none | active profile |
| `get_timeline_entries` | `limit`, `entry_type` | active profile |
| `get_injuries` | `active_only` | active profile |
| `get_current_medicines` | none | active profile |
| `get_active_training_plan` | none | user |
| `get_upcoming_sessions` | `days` | user |
| `get_recent_load_metrics` | `days` | user |
| `get_user_events` | `upcoming_only`, `limit` | user |
| `get_selected_races` | none | user |
| `get_healthcare_summary` | none | active profile |
| `get_conditions` | none | active profile |
| `get_bmr_summary` | none | active profile |
| `get_life_balance_summary` | none | active profile |
| `list_health_data_files` | `timeline_entry_id` | active profile |
| `get_health_data_file_content` | `file_id`, `full` | active profile |
| `list_log_events` | `event_type`, `days`, `limit` | active profile |
| `get_log_event_photo` | `event_id` | active profile |
| `save_to_library` | `workspace_path` | active profile write |

Parameter bounds:

| Parameter | Bounds / values | Default |
|---|---|---|
| `days` | integer `1..90` | `7` |
| `limit` | integer `1..100` | `50` for timeline, `20` for events |
| `weeks_ago` | integer `0..52` | `0` |
| `active_only` | `true` or `false` | `true` |
| `upcoming_only` | `true` or `false` | `true` |
| `entry_type` | optional exact timeline entry type | none |
| `timeline_entry_id` | optional UUID | none |
| `file_id` | required UUID for `get_health_data_file_content` | none |
| `event_id` | required UUID for `get_log_event_photo` | none |
| `full` | `true` or `false` | `false` |
| `event_type` | optional exact log-event type | none |
| `days` for `list_log_events` | integer `1..365` | `30` |
| `limit` for `list_log_events` | integer `1..200` | `50` |
| `workspace_path` | workspace-relative path or `/zeroclaw-data/workspace/...` | required |

## File access tools

`list_health_data_files` lists non-deleted `health_data_files` rows for the
active profile. It returns metadata and a `has_extracted_text` flag so the
agent can decide whether to call `get_health_data_file_content`.

`get_health_data_file_content` first checks `extracted_content_for_bot`. If
cached text exists, the tool returns it inline, truncated to
`LIFEATLAS_TEXT_PREVIEW_CHARS` characters by default (`4000`; pass `full=true`
for all text). If no cached text exists, the proxy downloads the object from
the `health_data` bucket, writes it to `workspace/lifeatlas/files/`, and returns
both a workspace-relative path and an absolute container path.

`list_log_events` lists recent `log_events` rows for the active profile with a
`has_photo` flag. `get_log_event_photo` downloads the event photo from the
`event_photos` bucket into `workspace/lifeatlas/photos/` and returns an
`image_tag` the agent can paste into chat.

`save_to_library` is the only write-capable LifeAtlas tool. It reads a file
from the user's workspace, validates that it is inside `workspace/`, enforces
the 25 MB limit, accepts only PDF, CSV, PNG, and JPEG, uploads to the website's
`health_data` bucket, inserts a `health_data_files` row for the active profile,
and returns a structured success/failure object. If the DB insert fails after
upload, the proxy removes the uploaded object as a rollback.

## Workspace skill

Newly provisioned workspaces get:

```text
workspace/skills/lifeatlas/SKILL.toml
workspace/skills/lifeatlas/SKILL.md
```

The default template contains `{{LIFEATLAS_TOOL_TOKEN}}` placeholders.
`ContainerOrchestrator.provision()` renders the token after copying templates,
so the generated skill URLs contain the per-container bearer token.

Skill updates are version-aware:

- First initialization writes the template and renders the current token.
- Existing initialized workspaces are checked on provision, start, and restart.
- `_ensure_skills_current()` updates the skill when the template version is
  newer than the installed version, and leaves matching or newer installed
  versions alone.
- Operators can force a pass across known containers with
  `claw-admin skills retrofit`, which calls `POST /claw-admin/skills/retrofit`.
- Rendering edits only `workspace/skills/lifeatlas/SKILL.toml` and `SKILL.md`
  and refuses symlinks or paths outside the user volume.

## Security notes

- LifeAtlas tools are read-only except `save_to_library`, which writes only to
  the active profile's `health_data` library with the website MIME allowlist.
- Supabase service-role credentials stay in the proxy process.
- Tool auth derives `user_id` from the container token; model-controlled user
  or profile selection is ignored.
- The token is currently embedded in the tool URL because of ZeroClaw's HTTP
  skill interface. Do not log full request URLs for this route.
- Profile-scoped tools use `user_active_profiles.active_profile_id`, falling
  back to the default profile in `user_profiles`.
- Binary files fetched for tool use land under `workspace/lifeatlas/` and are
  swept by the lifecycle loop after the same 24-hour window as temp uploads.

## Tests

| Test file | Covers |
|---|---|
| `tests/test_lifeatlas_common.py` | Parameter parsing and active-profile resolution. |
| `tests/test_lifeatlas_helpers.py` | Domain helper query behavior with mocked Supabase chains. |
| `tests/test_lifeatlas_files.py` | File-listing, extracted-text, and workspace-fetch behavior. |
| `tests/test_lifeatlas_log_events.py` | Log-event listing and photo fetch responses. |
| `tests/test_lifeatlas_save.py` | `save_to_library` validation, reads, uploads, rollback, and errors. |
| `tests/test_lifeatlas_router.py` | Auth, dispatch, parameter errors, app mount. |
| `tests/test_lifeatlas_skill_template.py` | Skill template shape and token rendering. |
| `tests/test_workspace_fetch.py` | Supabase Storage object download into the workspace. |
| `tests/test_skills_retrofit.py` | CLI wrapper for skill retrofit. |
| `tests/integration/test_lifeatlas_file_access.py` | Local Supabase + Docker end-to-end coverage for LifeAtlas file access. |
