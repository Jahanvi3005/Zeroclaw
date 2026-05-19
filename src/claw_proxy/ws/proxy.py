"""Frontend-facing WebSocket handler with protocol translation.

Authenticates via JWT, joins or creates a SharedSession for the user's
target session_id, and relays messages between the frontend and the
container's upstream WebSocket.
"""

import asyncio
import json
import logging
import uuid
from typing import Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from claw_proxy.files.downloads import process_download_tags, process_transcript_download_tags
from claw_proxy.ws.protocol import (
    connected_msg,
    error_msg,
    parse_session_command,
    status_msg,
    translate_downstream,
    translate_upstream,
)
from claw_proxy.ws.shared_session import (
    SharedSession,
    add_tab,
    broadcast_to_session,
    broadcast_to_user,
    get_connections,
    get_session,
    get_ws_session,
    get_user_lock,
    list_user_sessions,
    register_session,
    remove_tab,
    unregister_session,
)
from claw_proxy.ws.upstream import UpstreamConnection

log = logging.getLogger(__name__)

__all__ = ["create_ws_app"]


async def fetch_transcript(
    http_url: str, bearer_token: str, session_id: str, limit: int = 50
) -> list[dict]:
    """Fetch session transcript from ZeroClaw REST API."""
    from claw_proxy.config import http_client

    try:
        resp = await http_client.get(
            f"{http_url}/api/sessions/{session_id}/messages",
            params={"limit": limit},
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            # ZeroClaw returns {"messages": [...], ...} envelope
            messages = data.get("messages", data) if isinstance(data, dict) else data
            if not isinstance(messages, list):
                messages = []
            return messages[-limit:] if len(messages) > limit else messages
        if resp.status_code == 404:
            return []  # endpoint not available yet (ZeroClaw PR pending)
        log.warning("Transcript fetch failed: %s %s", resp.status_code, resp.text)
        return []
    except Exception as e:
        log.warning("Transcript fetch error: %s", e)
        return []


async def _process_transcript_exports(
    messages: list[dict],
    *,
    user_id: str,
    container_info,
    volume_path: str,
    get_supabase_client: Callable | None,
    docker_client,
) -> list[dict]:
    if get_supabase_client is None or docker_client is None:
        return messages
    try:
        return await process_transcript_download_tags(
            messages,
            user_id=user_id,
            container_id=container_info.container_id,
            volume_path=volume_path,
            docker_client=docker_client,
            supabase_client=get_supabase_client(),
        )
    except Exception as exc:
        log.warning("Transcript export processing failed for %s: %s", user_id[:8], exc)
        return messages


async def fetch_session_list(http_url: str, bearer_token: str) -> list[dict]:
    """Fetch the known session list from ZeroClaw."""
    from claw_proxy.config import http_client

    try:
        resp = await http_client.get(
            f"{http_url}/api/sessions",
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            # ZeroClaw may return {"sessions": [...]} envelope or a bare list
            if isinstance(data, dict):
                data = data.get("sessions", [])
            return data if isinstance(data, list) else []
        return []
    except Exception as e:
        log.warning("Session list fetch error: %s", e)
        return []


def _session_lookup(sessions: list[dict]) -> dict[str, dict]:
    return {
        session_id: session
        for session in sessions
        if (session_id := session.get("session_id"))
    }


def _orchestrator_host_data_dir(orchestrator) -> str:
    host_data_dir = getattr(orchestrator, "host_data_dir", None)
    if isinstance(host_data_dir, str) and host_data_dir:
        return host_data_dir
    return orchestrator.data_dir


async def _persist_registry_session_id(
    registry,
    user_id: str,
    session_id: str | None,
) -> None:
    if not registry:
        return
    try:
        await registry.update_session_id(user_id, session_id)
    except Exception as e:
        log.warning("Failed to persist current_session_id: %s", e)


async def _persist_session_name(
    http_url: str,
    bearer_token: str,
    session_id: str,
    name: str | None,
) -> bool:
    if not name:
        return False

    from claw_proxy.config import http_client

    try:
        resp = await http_client.put(
            f"{http_url}/api/sessions/{session_id}",
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            },
            json={"name": name},
            timeout=10,
        )
    except Exception as e:
        log.warning("Persist session name failed for %s: %s", session_id[:8], e)
        return False

    if resp.status_code not in (200, 204):
        log.warning("Failed to persist session name for %s: %s", session_id[:8], resp.text)
        return False
    return True


def _sync_confirmed_session(session: SharedSession, remote: dict | None) -> None:
    session.confirmed = True
    if remote is None:
        return

    remote_name = remote.get("name")
    if session.display_name is None and remote_name is not None:
        session.display_name = remote_name
    if remote_name is not None and remote_name == session.display_name:
        session.needs_name_persist = False
    session.message_count = max(session.message_count, remote.get("message_count", 0) or 0)


async def _promote_session_if_persisted(
    session: SharedSession,
    container_info,
    user_id: str,
    registry,
) -> None:
    if session.confirmed:
        return

    # ZeroClaw can lag briefly before the first exchanged message becomes
    # visible via REST, so give it a short grace window before we give up.
    remote: dict | None = None
    for attempt in range(3):
        sessions = await fetch_session_list(
            container_info.http_url,
            container_info.bearer_token,
        )
        remote = _session_lookup(sessions).get(session.session_id)
        if remote is not None:
            break
        if attempt < 2:
            await asyncio.sleep(0.05)

    if remote is None:
        return

    _sync_confirmed_session(session, remote)
    await _persist_registry_session_id(registry, user_id, session.session_id)
    if session.needs_name_persist and session.display_name:
        if await _persist_session_name(
            container_info.http_url,
            container_info.bearer_token,
            session.session_id,
            session.display_name,
        ):
            session.needs_name_persist = False


# --- Relay (per SharedSession) ---


async def _relay_shared(
    session: SharedSession,
    *,
    get_supabase_client: Callable | None = None,
    docker_client=None,
) -> None:
    """Read from session.upstream and fan out translated messages to all tabs.

    On chat.done / error / unrecoverable disconnect, sets streaming_done so
    any waiting sender unblocks. On unrecoverable disconnect, closes all
    tabs and exits — the per-tab finally blocks will then clean up.
    """
    retries = 0
    max_retries = 3
    try:
        while True:
            msg = await session.upstream.recv()
            if msg is None:
                if retries >= max_retries:
                    await broadcast_to_session(
                        session,
                        error_msg("UPSTREAM_ERROR", "Lost connection to assistant"),
                    )
                    session.streaming_done.set()
                    for ws in list(session.connections):
                        try:
                            await ws.close()
                        except Exception:
                            pass
                    return
                retries += 1
                delay = min(2 ** retries, 8)
                await broadcast_to_session(
                    session, status_msg("Reconnecting to assistant...")
                )
                await asyncio.sleep(delay)
                if await session.upstream.connect(session.session_id):
                    retries = 0
                continue
            retries = 0
            translated = translate_upstream(msg)
            if not translated:
                continue
            if (
                translated.get("type") == "chat.done"
                and get_supabase_client is not None
                and docker_client is not None
            ):
                translated = await process_download_tags(
                    translated,
                    user_id=session.user_id,
                    container_id=session.container_id,
                    volume_path=session.volume_path,
                    docker_client=docker_client,
                    supabase_client=get_supabase_client(),
                )
            await broadcast_to_session(session, translated)
            t = translated.get("type")
            if t == "chat.done":
                session.message_count += 2
                session.streaming_done.set()
            elif t == "error":
                session.streaming_done.set()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("Relay error for session %s: %s", session.session_id[:8], e)
        await broadcast_to_session(
            session, error_msg("UPSTREAM_ERROR", "Internal proxy error")
        )
        session.streaming_done.set()


# --- SharedSession lifecycle (caller must hold user lock) ---


async def _join_or_create_session(
    user_id: str,
    session_id: str,
    container_info,
    ws: WebSocket,
    *,
    confirmed: bool = False,
    display_name: str | None = None,
    needs_name_persist: bool = False,
    name: str | None = None,
    get_supabase_client: Callable | None = None,
    docker_client=None,
    volume_path: str | None = None,
) -> tuple[SharedSession, dict | None]:
    """Join an existing SharedSession or create a new one. Holds: user lock.

    Returns (session, start_msg) — start_msg is the upstream session_start
    reply for a freshly created session, or None when joining an existing one.
    """
    existing = get_session(user_id, session_id)
    if existing is not None:
        if confirmed:
            existing.confirmed = True
        if display_name is not None:
            existing.display_name = display_name
        existing.needs_name_persist = (
            existing.needs_name_persist or needs_name_persist
        )
        add_tab(existing, ws)
        return existing, None

    upstream = UpstreamConnection(
        ws_url=container_info.ws_url,
        bearer_token=container_info.bearer_token,
    )
    start = await upstream.connect(session_id, name=name)
    if not start:
        raise RuntimeError("upstream connect failed")

    session = SharedSession(
        user_id=user_id,
        session_id=session_id,
        upstream=upstream,
        confirmed=confirmed,
        display_name=display_name,
        needs_name_persist=needs_name_persist,
        message_count=start.get("message_count", 0),
        container_id=container_info.container_id,
        volume_path=volume_path or "",
    )
    add_tab(session, ws)
    register_session(session)
    session.relay_task = asyncio.create_task(
        _relay_shared(
            session,
            get_supabase_client=get_supabase_client,
            docker_client=docker_client,
        )
    )
    return session, start


async def _force_move_tab(
    ws: WebSocket,
    from_session: SharedSession,
    target_session_id: str,
    container_info,
    *,
    confirmed: bool = False,
    reason: str = "deleted",
    get_supabase_client: Callable | None = None,
    docker_client=None,
) -> SharedSession:
    """Move ws out of from_session into target_session_id (joining or
    creating). Sends an unsolicited session.switched frame so the frontend
    re-renders. Caller holds the user lock.
    """
    await _leave_session(from_session, ws)
    new_session, _start = await _join_or_create_session(
        from_session.user_id,
        target_session_id,
        container_info,
        ws,
        confirmed=confirmed,
        get_supabase_client=get_supabase_client,
        docker_client=docker_client,
        volume_path=from_session.volume_path,
    )
    messages = await fetch_transcript(
        container_info.http_url, container_info.bearer_token, target_session_id
    )
    messages = await _process_transcript_exports(
        messages,
        user_id=from_session.user_id,
        container_info=container_info,
        volume_path=from_session.volume_path,
        get_supabase_client=get_supabase_client,
        docker_client=docker_client,
    )
    try:
        await ws.send_json(
            {
                "type": "session.switched",
                "sessionId": target_session_id,
                "messages": messages,
                "messageCount": len(messages),
                "reason": reason,
            }
        )
    except Exception:
        pass  # ws may be dead; cleanup happens in finally
    return new_session


async def _leave_session(session: SharedSession, ws: WebSocket) -> None:
    """Remove ws from session. Tear down if empty. Holds: user lock."""
    is_empty = remove_tab(session, ws)
    if not is_empty:
        return
    unregister_session(session)
    if session.relay_task:
        session.relay_task.cancel()
        try:
            await session.relay_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # In test environments each WS connection may run on its own event
            # loop; awaiting a task from a different loop raises RuntimeError.
            # The cancel() already fired — just let the task die on its loop.
            pass
    await session.upstream.disconnect()


# --- Send serialization (caller does NOT hold user lock) ---


async def _send_chat_serialized(
    session: SharedSession,
    translated: dict,
    origin_ws: WebSocket,
) -> None:
    """Strict serialization: hold the per-session send_lock from upstream
    send until the relay observes chat.done (or error). Tabs sending
    concurrently on the same session queue up.
    """
    async with session.send_lock:
        session.streaming_done.clear()
        await session.upstream.send(translated)
        await broadcast_to_session(
            session,
            {"type": "message", "content": translated.get("content", "")},
            exclude_ws=origin_ws,
        )
        try:
            await asyncio.wait_for(session.streaming_done.wait(), timeout=180)
        except asyncio.TimeoutError:
            log.warning(
                "Stream timeout for session %s", session.session_id[:8]
            )


# --- Session commands ---


async def handle_session_command(
    cmd: dict,
    ws: WebSocket,
    session: SharedSession,
    container_info,
    user_id: str,
    registry,
    *,
    get_supabase_client: Callable | None = None,
    docker_client=None,
) -> SharedSession:
    """Handle a session command (list/switch/create/rename/delete).

    Returns the (possibly new) SharedSession the WS belongs to after the
    command runs. Caller holds the per-user lock.
    """
    from claw_proxy.config import http_client

    action = cmd["action"]
    http_url = container_info.http_url
    token = container_info.bearer_token
    headers = {"Authorization": f"Bearer {token}"}

    if action == "list":
        sessions_list = await fetch_session_list(http_url, token)
        remote_lookup = _session_lookup(sessions_list)
        for session_id, remote in remote_lookup.items():
            existing = get_session(user_id, session_id)
            if existing is not None and existing.confirmed:
                _sync_confirmed_session(existing, remote)

        def _activity(sid: str | None) -> str:
            if not sid:
                return "none"
            existing = get_session(user_id, sid)
            if existing is None or not existing.confirmed:
                return "none"
            if ws in existing.connections:
                return "current"
            return "other"

        await ws.send_json(
            {
                "type": "session.list.result",
                "sessions": _merge_session_lists(user_id, ws, sessions_list, _activity),
            }
        )
        return session

    if action == "switch":
        target_session_id = cmd["sessionId"]
        remote_sessions = await fetch_session_list(http_url, token)
        remote_lookup = _session_lookup(remote_sessions)
        existing_target = get_session(user_id, target_session_id)
        target_confirmed = target_session_id in remote_lookup or bool(
            existing_target and existing_target.confirmed
        )

        await ws.send_json(status_msg("Switching session..."))
        try:
            await _leave_session(session, ws)
        except Exception as e:
            log.error("session.switch leave error: %s", e)
            await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to switch session"))
            return session
        try:
            new_session, _start_msg = await _join_or_create_session(
                user_id,
                target_session_id,
                container_info,
                ws,
                confirmed=target_confirmed,
                get_supabase_client=get_supabase_client,
                docker_client=docker_client,
                volume_path=session.volume_path,
            )
        except Exception as e:
            log.error("session.switch join error: %s", e)
            # Roll back: try to rejoin the original session so the ws has a home.
            try:
                recovered, _ = await _join_or_create_session(
                    user_id,
                    session.session_id,
                    container_info,
                    ws,
                    confirmed=session.confirmed,
                    display_name=session.display_name,
                    needs_name_persist=session.needs_name_persist,
                    get_supabase_client=get_supabase_client,
                    docker_client=docker_client,
                    volume_path=session.volume_path,
                )
                await ws.send_json(
                    error_msg("UPSTREAM_ERROR", "Failed to switch session")
                )
                return recovered
            except Exception as rollback_err:
                log.error("session.switch rollback failed: %s", rollback_err)
                await ws.send_json(
                    error_msg("UPSTREAM_ERROR", "Failed to switch session")
                )
                await ws.close()
                return session  # ws is closing; caller unwinds via finally
        if target_confirmed:
            _sync_confirmed_session(new_session, remote_lookup.get(target_session_id))
        messages = await fetch_transcript(
            container_info.http_url, container_info.bearer_token, target_session_id
        )
        messages = await _process_transcript_exports(
            messages,
            user_id=user_id,
            container_info=container_info,
            volume_path=new_session.volume_path,
            get_supabase_client=get_supabase_client,
            docker_client=docker_client,
        )
        await ws.send_json(
            {
                "type": "session.switched",
                "sessionId": target_session_id,
                "messages": messages,
                "messageCount": len(messages),
            }
        )
        if target_confirmed:
            await _persist_registry_session_id(registry, user_id, target_session_id)
        return new_session

    if action == "create":
        new_session_id = str(uuid.uuid4())
        name = cmd.get("name")
        await ws.send_json(status_msg("Creating new session..."))
        try:
            await _leave_session(session, ws)
        except Exception as e:
            log.error("session.create leave error: %s", e)
            await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to create session"))
            return session
        try:
            new_session, _start = await _join_or_create_session(
                user_id,
                new_session_id,
                container_info,
                ws,
                display_name=name,
                needs_name_persist=bool(name),
                name=name,
                get_supabase_client=get_supabase_client,
                docker_client=docker_client,
                volume_path=session.volume_path,
            )
        except Exception as e:
            log.error("session.create error: %s", e)
            # Roll back: try to rejoin the original session so the ws has a home.
            try:
                recovered, _ = await _join_or_create_session(
                    user_id,
                    session.session_id,
                    container_info,
                    ws,
                    confirmed=session.confirmed,
                    display_name=session.display_name,
                    needs_name_persist=session.needs_name_persist,
                    get_supabase_client=get_supabase_client,
                    docker_client=docker_client,
                    volume_path=session.volume_path,
                )
                await ws.send_json(
                    error_msg("UPSTREAM_ERROR", "Failed to create session")
                )
                return recovered
            except Exception as rollback_err:
                log.error("session.create rollback failed: %s", rollback_err)
                await ws.send_json(
                    error_msg("UPSTREAM_ERROR", "Failed to create session")
                )
                await ws.close()
                return session  # ws is closing; caller unwinds via finally
        await ws.send_json(
            {
                "type": "session.created",
                "sessionId": new_session_id,
                "name": name,
            }
        )
        return new_session

    if action == "rename":
        target_session_id = cmd["sessionId"]
        name = cmd["name"]
        existing = get_session(user_id, target_session_id)
        if existing is not None and not existing.confirmed:
            existing.display_name = name
            existing.needs_name_persist = True
            await broadcast_to_user(
                user_id,
                {
                    "type": "session.renamed",
                    "sessionId": target_session_id,
                    "name": name,
                },
            )
            return session

        try:
            resp = await http_client.put(
                f"{http_url}/api/sessions/{target_session_id}",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": name},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                if existing is not None:
                    existing.display_name = name
                    existing.needs_name_persist = False
                await broadcast_to_user(
                    user_id,
                    {
                        "type": "session.renamed",
                        "sessionId": target_session_id,
                        "name": name,
                    },
                )
            else:
                await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to rename session"))
        except Exception as e:
            log.error("session.rename error: %s", e)
            await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to rename session"))
        return session

    if action == "delete":
        target_session_id = cmd["sessionId"]
        target_session = get_session(user_id, target_session_id)
        target_is_draft = target_session is not None and not target_session.confirmed

        # Determine the current default from the registry
        try:
            row = await registry.get(user_id) if registry else None
        except Exception:
            row = None
        current_default = (row or {}).get("current_session_id") if isinstance(row, dict) else None

        if not target_is_draft:
            try:
                resp = await http_client.delete(
                    f"{http_url}/api/sessions/{target_session_id}",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code not in (200, 204):
                    await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to delete session"))
                    return session
            except Exception as e:
                log.error("session.delete REST error: %s", e)
                await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to delete session"))
                return session

        new_session_for_caller = session  # may change below
        new_default: str | None = None
        deleting_current = (target_session_id == session.session_id)

        if deleting_current:
            # Caller gets a brand new session. Use split-try so a failed
            # upstream connect can roll back cleanly.
            new_id = str(uuid.uuid4())
            try:
                await _leave_session(session, ws)
            except Exception as e:
                log.error("session.delete leave error: %s", e)
                await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to delete session"))
                return session
            try:
                new_session_for_caller, _start = await _join_or_create_session(
                    user_id,
                    new_id,
                    container_info,
                    ws,
                    get_supabase_client=get_supabase_client,
                    docker_client=docker_client,
                    volume_path=session.volume_path,
                )
            except Exception as e:
                log.error("session.delete create error: %s", e)
                await ws.send_json(error_msg("UPSTREAM_ERROR", "Failed to recreate session"))
                await ws.close()
                return session

            # Send session.switched so the frontend re-renders on the new session
            try:
                await ws.send_json(
                    {
                        "type": "session.switched",
                        "sessionId": new_id,
                        "messages": [],
                        "messageCount": 0,
                        "reason": "deleted",
                    }
                )
            except Exception:
                pass

            # Determine the move target for OTHER tabs that were on X
            if current_default == target_session_id:
                move_target = new_id
                new_default = None
            else:
                move_target = current_default or new_id
        else:
            # Deleting "other" session
            if current_default == target_session_id:
                new_default = session.session_id if session.confirmed else None
                move_target = new_default or session.session_id
            else:
                move_target = current_default or session.session_id

        if move_target == current_default and current_default is not None:
            move_confirmed = True
        elif move_target == session.session_id:
            move_confirmed = session.confirmed
        else:
            move_confirmed = False

        # Force-move any tabs that were on the deleted session
        deleted_group = get_session(user_id, target_session_id)
        if deleted_group is not None and move_target is not None:
            for other_ws in list(deleted_group.connections):
                if other_ws is ws:
                    continue  # caller already moved out above
                try:
                    await _force_move_tab(
                        other_ws,
                        deleted_group,
                        move_target,
                        container_info,
                        confirmed=move_confirmed,
                        reason="deleted",
                        get_supabase_client=get_supabase_client,
                        docker_client=docker_client,
                    )
                except Exception as e:
                    log.error("Force-move failed: %s", e)

        # Persist new default only when the deleted session was the stored one.
        if current_default == target_session_id:
            await _persist_registry_session_id(registry, user_id, new_default)

        # Reply to caller (delete-current only; delete-other only broadcasts)
        if deleting_current:
            try:
                await ws.send_json(
                    {
                        "type": "session.deleted",
                        "sessionId": target_session_id,
                        "newSessionId": new_session_for_caller.session_id,
                    }
                )
            except Exception:
                pass

        # Broadcast deleted to all user tabs
        await broadcast_to_user(
            user_id, {"type": "session.deleted", "sessionId": target_session_id}
        )
        if new_default is not None:
            await broadcast_to_user(
                user_id,
                {"type": "session.defaultChanged", "sessionId": new_default},
            )
        return new_session_for_caller

    await ws.send_json(error_msg("INVALID_MESSAGE", f"Unsupported session action: {action}"))
    return session


def _merge_session_lists(
    user_id: str,
    ws: WebSocket,
    sessions_list: list[dict],
    activity_fn: Callable[[str | None], str],
) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for remote in sessions_list:
        session_id = remote.get("session_id")
        if not session_id:
            continue
        order.append(session_id)
        merged[session_id] = {
            "sessionId": session_id,
            "name": remote.get("name"),
            "createdAt": remote.get("created_at"),
            "lastActivity": remote.get("last_activity"),
            "messageCount": remote.get("message_count", 0),
        }

    for local in list_user_sessions(user_id):
        if not local.confirmed:
            continue
        entry = merged.get(local.session_id)
        if entry is None:
            continue
        if local.display_name is not None:
            entry["name"] = local.display_name
        entry["messageCount"] = max(entry.get("messageCount", 0), local.message_count)

    return [
        {
            **merged[session_id],
            "active": activity_fn(session_id),
        }
        for session_id in order
    ]


# --- Endpoint ---


def create_ws_app(
    orchestrator,
    auth_fn: Callable,
    default_session_id: str | None = None,
    get_supabase_client: Callable | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        token = ws.query_params.get("token")
        await ws.accept()

        if not token:
            await ws.send_json(error_msg("AUTH_FAILED", "Missing token"))
            await ws.close(code=4001)
            return

        user = await auth_fn(token)
        if not user:
            await ws.send_json(error_msg("AUTH_FAILED", "Invalid or expired token"))
            await ws.close(code=4001)
            return

        user_id = user["id"]
        log.info("WS connected: user_id=%s", user_id[:8])

        current_session: SharedSession | None = None
        should_update_activity = False

        try:
            # --- Get or provision container ---
            try:
                container_info = await orchestrator.get(user_id)
                if not container_info:
                    await ws.send_json(status_msg("Starting your assistant..."))
                    container_info = await orchestrator.provision(user_id)
                elif container_info.status == "stopped":
                    await ws.send_json(status_msg("Restarting your assistant..."))
                    container_info = await orchestrator.restart(user_id)
                elif container_info.status == "provisioning":
                    await ws.send_json(status_msg("Your assistant is starting up..."))
                    for _ in range(60):
                        await asyncio.sleep(0.5)
                        container_info = await orchestrator.get(user_id)
                        if container_info and container_info.status == "ready":
                            break
                    else:
                        await ws.send_json(
                            error_msg("CONTAINER_UNAVAILABLE", "Assistant failed to start")
                        )
                        await ws.close()
                        return
            except Exception as e:
                log.error("Container lookup/provision failed: %s", e)
                await ws.send_json(
                    error_msg("CONTAINER_UNAVAILABLE", "Failed to start assistant")
                )
                await ws.close()
                return

            # --- Resolve session ID ---
            registry = getattr(orchestrator, "registry", None)
            stored_session_id = container_info.current_session_id
            session_id: str | None = None
            session_confirmed = False
            session_display_name: str | None = None
            remote_lookup: dict[str, dict] = {}

            if stored_session_id or default_session_id:
                sessions = await fetch_session_list(
                    container_info.http_url,
                    container_info.bearer_token,
                )
                remote_lookup = _session_lookup(sessions)

            if stored_session_id:
                if stored_session_id in remote_lookup:
                    session_id = stored_session_id
                    session_confirmed = True
                    session_display_name = remote_lookup[stored_session_id].get("name")
                else:
                    log.info(
                        "Stored session %s not found on container, creating new",
                        stored_session_id[:8],
                    )
                    await _persist_registry_session_id(registry, user_id, None)

            if not session_id:
                if default_session_id and default_session_id in remote_lookup:
                    session_id = default_session_id
                    session_confirmed = True
                    session_display_name = remote_lookup[default_session_id].get("name")
                else:
                    session_id = default_session_id or str(uuid.uuid4())

            # --- Join or create SharedSession ---
            try:
                async with get_user_lock(user_id):
                    host_data_dir = _orchestrator_host_data_dir(orchestrator)
                    volume_path = f"{host_data_dir}/{user_id}"
                    current_session, start_msg = await _join_or_create_session(
                        user_id,
                        session_id,
                        container_info,
                        ws,
                        confirmed=session_confirmed,
                        display_name=session_display_name,
                        get_supabase_client=get_supabase_client,
                        docker_client=getattr(orchestrator, "docker", None),
                        volume_path=volume_path,
                    )
            except Exception as e:
                # Container may have stopped/crashed while registry still says "ready".
                # Try restarting once before giving up.
                log.warning("Upstream connect failed, attempting container restart: %s", e)
                try:
                    await ws.send_json(status_msg("Restarting your assistant..."))
                    container_info = await orchestrator.restart(user_id)
                    async with get_user_lock(user_id):
                        host_data_dir = _orchestrator_host_data_dir(orchestrator)
                        volume_path = f"{host_data_dir}/{user_id}"
                        current_session, start_msg = await _join_or_create_session(
                            user_id,
                            session_id,
                            container_info,
                            ws,
                            confirmed=session_confirmed,
                            display_name=session_display_name,
                            get_supabase_client=get_supabase_client,
                            docker_client=getattr(orchestrator, "docker", None),
                            volume_path=volume_path,
                        )
                except Exception as retry_err:
                    log.error("Failed to join/create SharedSession after restart: %s", retry_err)
                    await ws.send_json(
                        error_msg("UPSTREAM_ERROR", "Failed to connect to assistant")
                    )
                    await ws.close()
                    return

            # Fetch transcript and send connected
            messages = await fetch_transcript(
                container_info.http_url,
                container_info.bearer_token,
                session_id,
            )
            messages = await _process_transcript_exports(
                messages,
                user_id=user_id,
                container_info=container_info,
                volume_path=current_session.volume_path,
                get_supabase_client=get_supabase_client,
                docker_client=getattr(orchestrator, "docker", None),
            )
            if start_msg is not None:
                message_count = start_msg.get("message_count", len(messages))
            else:
                message_count = len(messages)
            await ws.send_json(connected_msg(session_id, messages, message_count))
            should_update_activity = True

            # --- Receive loop ---
            try:
                while True:
                    raw = await ws.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        await ws.send_json(error_msg("INVALID_MESSAGE", "Invalid JSON"))
                        continue

                    session_cmd = parse_session_command(msg)
                    if session_cmd:
                        async with get_user_lock(user_id):
                            current_session = get_ws_session(ws) or current_session
                            current_session = await handle_session_command(
                                session_cmd,
                                ws,
                                current_session,
                                container_info,
                                user_id,
                                registry,
                                get_supabase_client=get_supabase_client,
                                docker_client=getattr(orchestrator, "docker", None),
                            )
                        continue

                    translated = translate_downstream(msg)
                    if translated:
                        current_session = get_ws_session(ws) or current_session
                        if not current_session.upstream.connected:
                            await ws.send_json(
                                error_msg("UPSTREAM_ERROR", "Not connected to assistant")
                            )
                            continue
                        await _send_chat_serialized(current_session, translated, ws)
                        await _promote_session_if_persisted(
                            current_session,
                            container_info,
                            user_id,
                            registry,
                        )
                    else:
                        await ws.send_json(
                            error_msg(
                                "INVALID_MESSAGE",
                                f"Unknown message type: {msg.get('type')}",
                            )
                        )

            except WebSocketDisconnect:
                log.info("Frontend disconnected: user_id=%s", user_id[:8])
            except Exception as e:
                log.error("WS handler error: %s", e)
        finally:
            current_session = get_ws_session(ws) or current_session
            if current_session is not None:
                async with get_user_lock(user_id):
                    await _leave_session(current_session, ws)
            if should_update_activity:
                await orchestrator.update_activity(user_id)

    return app
