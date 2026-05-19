"""Shared session state for multi-tab fan-out.

When multiple tabs are on the same (user_id, session_id), they share a
single upstream WebSocket and a single relay task. The proxy maintains
one SharedSession per active (user, session) pair. Tabs join the
existing SharedSession on connect; the SharedSession is torn down when
the last tab leaves.

This module is pure mechanism: state, locks, broadcast helpers. The
proxy module owns policy (when to create, switch, delete sessions).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

    from claw_proxy.ws.upstream import UpstreamConnection

log = logging.getLogger(__name__)


@dataclass
class SharedSession:
    user_id: str
    session_id: str
    upstream: "UpstreamConnection"
    confirmed: bool = False
    display_name: str | None = None
    needs_name_persist: bool = False
    message_count: int = 0
    container_id: str = ""
    volume_path: str = ""
    connections: set["WebSocket"] = field(default_factory=set)
    relay_task: asyncio.Task | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    streaming_done: asyncio.Event = field(default_factory=asyncio.Event)


# (user_id, session_id) -> SharedSession
_sessions: dict[tuple[str, str], SharedSession] = {}

# user_id -> lock serializing all SharedSession lifecycle for that user
_user_locks: dict[str, asyncio.Lock] = {}

# ws identity -> current SharedSession
_ws_sessions: dict[int, SharedSession] = {}


def get_user_lock(user_id: str) -> asyncio.Lock:
    """Per-user lock serializing all SharedSession lifecycle ops."""
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def get_session(user_id: str, session_id: str) -> SharedSession | None:
    return _sessions.get((user_id, session_id))


def get_ws_session(ws: "WebSocket") -> SharedSession | None:
    return _ws_sessions.get(id(ws))


def list_user_sessions(user_id: str) -> list[SharedSession]:
    return [s for (uid, _), s in _sessions.items() if uid == user_id]


def register_session(session: SharedSession) -> None:
    _sessions[(session.user_id, session.session_id)] = session


def unregister_session(session: SharedSession) -> None:
    _sessions.pop((session.user_id, session.session_id), None)


def add_tab(session: SharedSession, ws: "WebSocket") -> None:
    previous = _ws_sessions.get(id(ws))
    if previous is not None and previous is not session:
        previous.connections.discard(ws)
    session.connections.add(ws)
    _ws_sessions[id(ws)] = session


def remove_tab(session: SharedSession, ws: "WebSocket") -> bool:
    """Remove ws from session. Returns True if session is now empty."""
    session.connections.discard(ws)
    if _ws_sessions.get(id(ws)) is session:
        _ws_sessions.pop(id(ws), None)
    return len(session.connections) == 0


async def broadcast_to_session(
    session: SharedSession,
    msg: dict,
    *,
    exclude_ws: "WebSocket | None" = None,
) -> None:
    """Send msg to every tab in this session. Drops dead WSes silently."""
    for ws in list(session.connections):
        if exclude_ws is not None and ws is exclude_ws:
            continue
        try:
            await ws.send_json(msg)
        except Exception:
            session.connections.discard(ws)


async def broadcast_to_user(user_id: str, msg: dict) -> None:
    """Send msg to every tab of a user across all sessions. Dedupes by ws identity."""
    seen: set[int] = set()
    for session in list_user_sessions(user_id):
        for ws in list(session.connections):
            if id(ws) in seen:
                continue
            seen.add(id(ws))
            try:
                await ws.send_json(msg)
            except Exception:
                session.connections.discard(ws)


def get_connections() -> dict[str, set]:
    """User-id -> set of all WSes for that user. Used by push.py fan-out."""
    out: dict[str, set] = {}
    for s in _sessions.values():
        out.setdefault(s.user_id, set()).update(s.connections)
    return out


def reset_state() -> None:
    """Clear all state. For test fixtures only."""
    _sessions.clear()
    _user_locks.clear()
    _ws_sessions.clear()
