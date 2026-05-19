# claw-admin CLI Reference

`claw-admin` is the operator CLI for the admin plane mounted at
`/claw-admin/*`. It is a thin Typer client around the REST routes in
`src/claw_proxy/admin/routes.py`.

The examples below use `uv run claw-admin`, which works from a checkout of this
repository. If the package is installed in an environment where the script is on
`PATH`, the same commands can be run as `claw-admin ...`.

## Connection and Authentication

The CLI accepts these global options before the command group:

```bash
uv run claw-admin --api-url http://localhost:8000 --token-file ~/.config/claw-admin/token container list
```

| Option | Purpose |
|---|---|
| `--api-url` | Root proxy URL, for example `http://localhost:8000`. Do not include `/claw-admin`; the client appends it. |
| `--token` | Static admin bearer token passed directly on the command line. |
| `--token-file` | File containing the static admin bearer token. |
| `--config` | TOML config file path. Defaults to `~/.config/claw-admin/config.toml`. |

Typer also provides `--help`, `--install-completion`, and
`--show-completion`.

Config precedence is: CLI flags, then environment variables, then the config
file, then defaults. Supported environment variables are `CLAW_ADMIN_API_URL`,
`CLAW_ADMIN_TOKEN`, and `CLAW_ADMIN_TOKEN_FILE`. The default API URL is
`http://localhost:8000`, and the default token file is
`~/.config/claw-admin/token`.

A minimal config file looks like this:

```toml
api_url = "http://localhost:8000"
token_file = "~/.config/claw-admin/token"
```

Under the hood, every request includes `Accept: application/json`. If a token is
available, the CLI sends `Authorization: Bearer <token>`.

## Targets

Commands that take `TARGET` first resolve it through:

```text
GET /claw-admin/users?lookup=<TARGET>
```

Accepted target forms are:

| Target form | Example |
|---|---|
| User UUID | `4864a144-a285-41b1-ba36-66b2cbaf3a1e` |
| Exact email | `jane@example.com` |
| Container name prefix | `zeroclaw-4864a144` |
| Container ID prefix | `a1b2c3d4` |
| Exact first and last name | `Jane Example` |

If a lookup is ambiguous the admin API returns `409`; if it cannot be found it
returns `404`.

## Output Conventions

List commands print one compact JSON object per line. Status and schema commands
usually print pretty JSON. Mutation commands print either compact JSON or a short
summary line.

## container list

**Purpose:** List registered ZeroClaw containers.

**Example usage:**

```bash
uv run claw-admin container list --status ready
```

**Example output:**

```json
{"user_id":"4864a144-a285-41b1-ba36-66b2cbaf3a1e","container_id":"a1b2c3d4e5f6","status":"ready","http_url":"http://127.0.0.1:49153","last_active_at":"2026-05-06T15:12:28+00:00"}
```

**Under the hood:** Calls `GET /claw-admin/containers` and prints each item from
the returned `items` list. `--status` is passed as a status filter.
`--inactive-for-days` is accepted by the CLI and REST route, but the current
operation implementation does not apply an inactivity filter yet.

## container status

**Purpose:** Show the registry row for one user's container.

**Example usage:**

```bash
uv run claw-admin container status jane@example.com
```

**Example output:**

```json
{
  "user_id": "4864a144-a285-41b1-ba36-66b2cbaf3a1e",
  "container_id": "a1b2c3d4e5f6",
  "status": "ready",
  "http_url": "http://127.0.0.1:49153",
  "bearer_token": "claw-container-token-redacted",
  "last_active_at": "2026-05-06T15:12:28+00:00"
}
```

**Under the hood:** Resolves `TARGET` to a user ID, then calls
`GET /claw-admin/containers/{user_id}`. The server reads the container registry
entry for that user and returns it.

## container start

**Purpose:** Start or provision the target user's ZeroClaw container.

**Example usage:**

```bash
uv run claw-admin container start jane@example.com
```

**Example output:**

```json
{"status":"started"}
```

**Under the hood:** Resolves `TARGET`, then calls
`POST /claw-admin/containers/{user_id}/start`. The server delegates to the
orchestrator, which starts an existing container or provisions the user's
workspace and container when needed. The action is written to the admin audit
log.

## container stop

**Purpose:** Stop the target user's ZeroClaw container.

**Example usage:**

```bash
uv run claw-admin container stop jane@example.com --careful --careful-timeout-seconds 120
```

**Example output:**

```json
{"status":"stopped"}
```

If the container is not idle before the careful timeout:

```json
{"status":"skipped","reason":"not idle within timeout","signals":["active_websocket"]}
```

**Under the hood:** Resolves `TARGET`, then calls
`POST /claw-admin/containers/{user_id}/stop`. With `--careful`, the server polls
the activity checker until the user is idle or the timeout expires. If idle, it
delegates to `orchestrator.stop(user_id)`. The result is audited.

## container restart

**Purpose:** Restart the target user's ZeroClaw container.

**Example usage:**

```bash
uv run claw-admin container restart jane@example.com --careful --careful-timeout-seconds 300
```

**Example output:**

```json
{"status":"restarted"}
```

**Under the hood:** Resolves `TARGET`, then calls
`POST /claw-admin/containers/{user_id}/restart`. With `--careful`, the server
waits for idle activity signals before restarting. The orchestrator stops and
starts the container, preserving the user's workspace volume. The action is
audited.

## config show

**Purpose:** Display a user's ZeroClaw config as flattened dotted keys.

**Example usage:**

```bash
uv run claw-admin config show jane@example.com --section llm
```

**Example output:**

```json
{
  "llm.provider": "openai",
  "llm.model": "gpt-4.1",
  "llm.api_key": "****"
}
```

**Under the hood:** Resolves `TARGET`, then calls
`GET /claw-admin/containers/{user_id}/config`. The server reads
`<ZEROCLAW_DATA_DIR>/<user_id>/.zeroclaw/config.toml`, flattens nested TOML
tables into dotted paths, filters by `--section` if provided, and masks paths
marked secret by the ZeroClaw config schema.

## config set

**Purpose:** Set one ZeroClaw config value for a user.

**Example usage:**

```bash
uv run claw-admin config set jane@example.com llm.model gpt-4.1 --careful
```

To read the value from standard input:

```bash
printf '%s' 'secret-value' | uv run claw-admin config set jane@example.com llm.api_key --stdin --careful
```

**Example output:**

```json
{
  "results": [
    {
      "path": "llm.model",
      "applied": true,
      "rollback_available": false,
      "requires_restart": false,
      "error": null,
      "secret": false
    }
  ]
}
```

**Under the hood:** Resolves `TARGET`, then calls
`PATCH /claw-admin/containers/{user_id}/config` with one update. If the
container is running, the server executes `zeroclaw config set <path> <value>`
inside the container, checks `/health`, and rolls back non-secret values if the
post-write health check fails. If the container is stopped, non-secret values are
written directly into `config.toml` and reported as requiring restart. Secret
writes against stopped containers are rejected. `--careful` asks the server to
wait for idle before applying the update.

## config schema

**Purpose:** Print the ZeroClaw config schema for the target user's container.

**Example usage:**

```bash
uv run claw-admin config schema jane@example.com
```

**Example output:**

```json
{
  "properties": {
    "llm.model": {
      "type": "string",
      "description": "Model name"
    },
    "llm.api_key": {
      "type": "string",
      "x-secret": true
    }
  }
}
```

**Under the hood:** Resolves `TARGET`, fetches the user's container ID, then
calls `GET /claw-admin/containers/{user_id}/config/schema`. The server runs
`zeroclaw --version` and `zeroclaw config schema` inside the container and caches
schemas by ZeroClaw version.

## workspace ls

**Purpose:** List files in a user's workspace volume.

**Example usage:**

```bash
uv run claw-admin workspace ls jane@example.com --path skills/lifeatlas -r
```

**Example output:**

```json
{"path":"skills/lifeatlas/SKILL.toml","size":93}
{"path":"skills/lifeatlas/SKILL.md","size":2048}
```

**Under the hood:** Resolves `TARGET`, then calls
`GET /claw-admin/containers/{user_id}/workspace/files`. The server reads from
`<ZEROCLAW_DATA_DIR>/<user_id>/workspace`, rejects path traversal, and returns
directory entries. Non-recursive listings include directories; recursive
listings include files.

## workspace cat

**Purpose:** Print a workspace file's content.

**Example usage:**

```bash
uv run claw-admin workspace cat jane@example.com USER.md
```

**Example output:**

```text
# User

Name: Jane Example
```

**Under the hood:** Resolves `TARGET`, then calls
`GET /claw-admin/containers/{user_id}/workspace/files/{path}`. The server reads
the file from the workspace. Text files are returned as text; binary files are
returned by the server as base64, but the CLI prints only the `content` field, so
this command is mainly intended for text.

## workspace put

**Purpose:** Write a local text file into a user's workspace.

**Example usage:**

```bash
uv run claw-admin workspace put jane@example.com notes/operator-note.md --file /tmp/operator-note.md --mode create_or_skip
```

**Example output:**

```json
{"status":"written","path":"notes/operator-note.md"}
```

If `--mode create_or_skip` is used and the path already exists:

```json
{"status":"skipped","path":"notes/operator-note.md"}
```

**Under the hood:** Resolves `TARGET`, reads the local `--file` as UTF-8 text,
then calls `PUT /claw-admin/containers/{user_id}/workspace/files/{path}`. The
server writes inside the workspace after path traversal checks. Common modes are
`overwrite`, `create`, and `create_or_skip`. The write is audited, with content
sanitized in the audit log.

## workspace rm

**Purpose:** Delete one file from a user's workspace.

**Example usage:**

```bash
uv run claw-admin workspace rm jane@example.com notes/operator-note.md
```

**Example output:**

```json
{"status":"deleted","path":"notes/operator-note.md"}
```

If the path is missing or not a file:

```json
{"status":"not_found","path":"notes/operator-note.md"}
```

**Under the hood:** Resolves `TARGET`, then calls
`DELETE /claw-admin/containers/{user_id}/workspace/files/{path}`. The server
only removes files, not directories, and rejects paths outside the workspace.
The delete is audited.

## workspace patch

**Purpose:** Replace text inside one workspace file.

**Example usage:**

```bash
uv run claw-admin workspace patch jane@example.com USER.md --find 'Old Name' --replace 'Jane Example'
```

**Example output:**

```json
{"status":"patched","path":"USER.md"}
```

**Under the hood:** Resolves `TARGET`, then calls
`PATCH /claw-admin/containers/{user_id}/workspace/files/{path}` with a single
`replace_substring` operation. The server reads the file as UTF-8 text, replaces
all occurrences of `--find` with `--replace`, writes the file back, and audits
the patch with sanitized parameters.

## admin admin-list

**Purpose:** List registered passkey admins.

**Example usage:**

```bash
uv run claw-admin admin admin-list
```

**Example output:**

```json
{"id":"admin_01","name":"Initial Admin","credentials":1,"created_at":"2026-05-06T14:50:00+00:00"}
```

**Under the hood:** Calls `GET /claw-admin/admins`. The server reads the
encrypted admin store and returns each admin's ID, name, credential count, and
creation time. WebAuthn credential material is not returned.

## admin admin-revoke

**Purpose:** Remove an admin by admin ID.

**Example usage:**

```bash
uv run claw-admin admin admin-revoke admin_01
```

**Example output:**

```json
{"status":"removed"}
```

**Under the hood:** Calls `DELETE /claw-admin/admins/{admin_id}`. The server
removes that admin from the encrypted admin store and writes an audit entry.

## admin token-list

**Purpose:** List static CLI token metadata.

**Example usage:**

```bash
uv run claw-admin admin token-list
```

**Example output:**

```json
{"label":"ops-vm","created_at":"2026-05-06T14:55:00+00:00","last_used_at":"2026-05-06T15:12:28+00:00"}
```

**Under the hood:** Calls `GET /claw-admin/admins/static-tokens`. The server
returns token labels and timestamps from the encrypted admin store. Token values
are hashed at rest and are never listed.

## admin token-create

**Purpose:** Create a new static bearer token for CLI/API use.

**Example usage:**

```bash
uv run claw-admin admin token-create ops-vm
```

**Example output:**

```json
{"token":"clawadm_example_token_printed_once","label":"ops-vm"}
```

**Under the hood:** Calls `POST /claw-admin/admins/static-tokens` with the
label. The server generates a token, stores only its hash and metadata, writes an
audit entry, and returns the raw token once.

## admin token-revoke

**Purpose:** Revoke a static bearer token by label.

**Example usage:**

```bash
uv run claw-admin admin token-revoke ops-vm
```

**Example output:**

```json
{"status":"revoked"}
```

**Under the hood:** Calls `DELETE /claw-admin/admins/static-tokens/{label}`.
The server deletes the token metadata/hash for that label and writes an audit
entry. Existing CLI sessions using that token stop authenticating after removal.

## admin invite

**Purpose:** Generate a short-lived enrollment token for registering another
passkey admin.

**Example usage:**

```bash
uv run claw-admin admin invite
```

**Example output:**

```json
{"enrollment_token":"invite_example_token","expires_in_minutes":15}
```

**Under the hood:** Calls `POST /claw-admin/admins/invite`. The server creates a
15-minute enrollment token in the encrypted admin store, records which admin
issued it, and writes an audit entry.

## admin audit

**Purpose:** Query admin audit events.

**Example usage:**

```bash
uv run claw-admin admin audit --since 2026-05-06T00:00:00+00:00 --action container.restart --limit 20
```

**Example output:**

```json
{"id":"evt_01","timestamp":"2026-05-06T15:10:00+00:00","admin_id":"static:ops-vm","action":"container.restart","target":{"type":"user","user_id":"4864a144-a285-41b1-ba36-66b2cbaf3a1e"},"params":{"careful":true,"careful_timeout_seconds":300},"result":"restarted","duration_ms":0,"client_ip":"127.0.0.1"}
```

**Under the hood:** Calls `GET /claw-admin/audit` with `limit` and optional
`since` and `action` filters. The server queries the encrypted audit log and
returns matching rows. The REST API supports additional filters, but the CLI
currently exposes only `--since`, `--action`, and `--limit`.

## orphan list

**Purpose:** Find ZeroClaw-looking Docker containers that are not in the proxy's
registry.

**Example usage:**

```bash
uv run claw-admin orphan list
```

**Example output:**

```json
{"container_id":"f00dbabe1234","name":"zeroclaw-4864a144","status":"exited"}
```

**Under the hood:** Calls `GET /claw-admin/orphans`. The server asks Docker for
all containers, keeps names starting with `zeroclaw-`, removes any container IDs
present in the container registry, and returns the remainder.

## orphan remove

**Purpose:** Stop and remove an orphaned ZeroClaw container.

**Example usage:**

```bash
uv run claw-admin orphan remove f00dbabe1234
```

**Example output:**

```json
{"status":"removed","container_id":"f00dbabe1234"}
```

**Under the hood:** Calls `DELETE /claw-admin/orphans/{container_id}`. The
server gets the Docker container, tries to stop it with a short timeout, ignores
stop errors, then removes it with Docker's force flag. The action is audited.

## job list

**Purpose:** List persisted async admin jobs.

**Example usage:**

```bash
uv run claw-admin job list --status running
```

**Example output:**

```json
{"id":"j_7a8b9c0d1e2f","operation":"restart","status":"running","created_at":"2026-05-06T15:00:00+00:00","summary":{"total":5,"success":2,"failed":0,"skipped":0,"pending":3}}
```

**Under the hood:** Calls `GET /claw-admin/jobs` with an optional status filter.
The server reads encrypted job files from the admin job store and returns one
summary per job. The CLI can list and inspect jobs, but it does not currently
create batch jobs; those are created by REST routes such as `/batch/restart` and
`/batch/config.set`.

## job status

**Purpose:** Show the full persisted state of one async admin job.

**Example usage:**

```bash
uv run claw-admin job status j_7a8b9c0d1e2f
```

**Example output:**

```json
{
  "id": "j_7a8b9c0d1e2f",
  "operation": "restart",
  "status": "complete",
  "created_at": "2026-05-06T15:00:00+00:00",
  "started_at": "2026-05-06T15:00:01+00:00",
  "completed_at": "2026-05-06T15:00:08+00:00",
  "params": {
    "careful": true,
    "careful_timeout_seconds": 300
  },
  "targets": [
    {
      "user_id": "4864a144-a285-41b1-ba36-66b2cbaf3a1e",
      "status": "restarted",
      "error": null,
      "started_at": "2026-05-06T15:00:01+00:00",
      "completed_at": "2026-05-06T15:00:03+00:00",
      "result": {
        "status": "restarted"
      }
    }
  ],
  "summary": {
    "total": 1,
    "success": 1,
    "failed": 0,
    "skipped": 0,
    "pending": 0
  }
}
```

**Under the hood:** Calls `GET /claw-admin/jobs/{job_id}`. The server decrypts
and returns the full job record, including per-target status, timestamps, errors,
and operation results.

## job cancel

**Purpose:** Request cancellation of an async admin job.

**Example usage:**

```bash
uv run claw-admin job cancel j_7a8b9c0d1e2f
```

**Example output:**

```json
{"status":"cancellation_requested"}
```

**Under the hood:** Calls `DELETE /claw-admin/jobs/{job_id}`. The server sets an
in-memory cancellation flag for the job runner. Cancellation is cooperative: the
currently running target is allowed to finish, and later targets are not started.

## skills retrofit

**Purpose:** Check every known current workspace and update the LifeAtlas
ZeroClaw skill files only when the bundled template is newer.

**Example usage:**

```bash
uv run claw-admin skills retrofit
```

**Example output:**

```text
Checked 5 containers. Updated 0, skipped 5
```

The REST response behind that CLI summary has this shape:

```json
{
  "checked": 5,
  "updated": 0,
  "skipped": 5,
  "failed": 0
}
```

**Under the hood:** Calls `POST /claw-admin/skills/retrofit`. The server builds a
target set from registered containers with bearer tokens plus the orchestrator's
in-memory token map. For each target it checks
`workspace/skills/lifeatlas/SKILL.toml` against the bundled template version. If
the installed version is current, it increments `skipped`. If the template is
newer or missing, it rewrites only `workspace/skills/lifeatlas/SKILL.toml` and
`workspace/skills/lifeatlas/SKILL.md`, rendering the correct per-container tool
token, and increments `updated`. The action is audited once for the system.
