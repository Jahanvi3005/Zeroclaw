# WebSocket protocol

Frontend connects to `ws://host/zeroclaw/ws?token=<jwt>`. The proxy translates between this frontend-facing protocol and ZeroClaw's native WS + REST interfaces. Translation lives in `src/claw_proxy/ws/protocol.py` (pure functions, no I/O) and the relay logic in `src/claw_proxy/ws/proxy.py`.

## Client → Server

| Type | Payload | Notes |
|---|---|---|
| `message` | `content: str` | Chat message. Goes through per-session `send_lock` + `streaming_done` gating — only one tab's message streams at a time. |
| `session.list` | — | Returns `session.list.result`. |
| `session.create` | `name?: str` | Creates new session; returns `session.created`. |
| `session.switch` | `sessionId: str` | Switches the **calling tab only** to the given session. Causes an upstream WS reconnect. |
| `session.delete` | `sessionId: str` | Deletes the session. Force-moves other tabs on the deleted session to the default. |
| `session.rename` | `sessionId: str`, `name: str` | Renames the session; broadcasts `session.renamed` to all user tabs. |

## Server → Client

| Type | Payload | Fan-out |
|---|---|---|
| `connected` | `sessionId`, `messages[]`, `messageCount` | Sent once per tab on connect. |
| `chat.chunk` | `content: str` | Streaming chunk. Broadcast to all tabs on the same session. |
| `chat.done` | `fullResponse: str` | Stream complete. Export tags (`[DOWNLOAD:]`) are replaced with signed URLs before this frame goes out. Broadcast to all tabs on the session. Permanent LifeAtlas saves use the `save_to_library` tool instead of `[SAVE:]`. |
| `chat.tool_call` | `name`, `args` | Tool use; broadcast like `chat.chunk`. |
| `chat.tool_result` | `result` | Tool result; broadcast like `chat.chunk`. |
| `session.switched` | `sessionId`, `messages[]`, `messageCount`, `reason?` | Navigation result. `reason: "deleted"` on force-moves. Only the calling tab receives it unless forced. |
| `session.created` | `sessionId`, `name` | Only to the creating tab. |
| `session.renamed` | `sessionId`, `name` | Broadcast to all user tabs. |
| `session.deleted` | `sessionId`, `newSessionId?` | Broadcast to all user tabs. `newSessionId` present on the deleting tab. |
| `session.defaultChanged` | `sessionId` | Broadcast when the default session changes as a consequence of a delete. |
| `session.list.result` | `sessions[]` with `active: "current" \| "other" \| "none"` | Reply to `session.list`. |
| `push.message` | `content`, `subject?` | Proactive push from ZeroClaw (via `POST /zeroclaw/push`). Export tags replaced before dispatch. Broadcast to all user tabs. |
| `status` | `message: str` | Status updates (starting, reconnecting, switching). Use on failure paths — see [architecture.md "Upstream restart-and-retry"](architecture.md#key-design-decisions). |
| `error` | `code`, `message` | Generic error — details logged server-side. |

## Error codes

| Code | Meaning |
|---|---|
| `AUTH_FAILED` | JWT invalid / expired / not yet valid, or user not in DB. |
| `CONTAINER_UNAVAILABLE` | No container row in registry for this user, or orchestrator couldn't provision. |
| `INVALID_MESSAGE` | Malformed JSON, unknown type, or missing required field. |
| `UPSTREAM_ERROR` | ZeroClaw WS refused or closed unexpectedly (after the single retry via `orchestrator.restart`). |

## Ordering rules worth knowing

- **Export-tag substitution happens on `chat.done`, not on `chat.chunk`.** Chunks stream through untouched for latency; the full response is scanned exactly once at `chat.done` to rewrite tags. Tag processing failures are best-effort: the tag stays as-is, the frame still goes through.
- **Switches are per-tab, deletes are cross-tab, renames broadcast.** This is the contract the frontend expects; violating it causes tabs to desync without an obvious error.
- **`status` frame can arrive before the first real frame after a reconnect attempt.** Tests that assert "the first message is X" need to tolerate or consume a preceding `status`.
