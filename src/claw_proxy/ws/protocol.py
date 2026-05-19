"""Proxy protocol: message types and translation between frontend and ZeroClaw.

Frontend protocol is documented in the design spec. ZeroClaw's native WS
protocol uses: message, chunk, done, chunk_reset, tool_call, tool_result,
session_start, connected, error, web_push.
"""

import logging

log = logging.getLogger(__name__)

# --- Upstream: ZeroClaw → proxy → frontend ---


def translate_upstream(msg: dict) -> dict | None:
    """Translate a ZeroClaw WS message to the proxy frontend protocol.

    Returns None for messages that should be silently consumed (chunk_reset,
    unknown types).
    """
    t = msg.get("type")

    if t == "chunk":
        return {"type": "chat.chunk", "content": msg.get("content", "")}

    if t == "done":
        return {"type": "chat.done", "fullResponse": msg.get("full_response", "")}

    if t == "tool_call":
        return {
            "type": "chat.tool_call",
            "name": msg.get("name"),
            "args": msg.get("args"),
        }

    if t == "tool_result":
        return {
            "type": "chat.tool_result",
            "name": msg.get("name"),
            "output": msg.get("output"),
        }

    if t == "chunk_reset":
        return None  # consumed silently; frontend uses chat.done as authoritative

    if t == "web_push":
        return {"type": "push.message", "content": msg.get("content", "")}

    if t == "session_start":
        return {
            "type": "session_start",
            "sessionId": msg.get("session_id"),
            "resumed": msg.get("resumed", False),
            "messageCount": msg.get("message_count", 0),
        }

    if t == "error":
        return {
            "type": "error",
            "code": msg.get("code", "UPSTREAM_ERROR"),
            "message": msg.get("message", "Unknown upstream error"),
        }

    log.debug("Unknown upstream message type: %s", t)
    return None


# --- Downstream: frontend → proxy → ZeroClaw ---


def translate_downstream(msg: dict) -> dict | None:
    """Translate a frontend message to ZeroClaw WS format.

    Returns None if the message is not a chat message (session commands
    are handled separately by parse_session_command).
    """
    if msg.get("type") != "message":
        return None
    content = msg.get("content", "").strip()
    if not content:
        return None
    return {"type": "message", "content": content}


# --- Session commands ---


def parse_session_command(msg: dict) -> dict | None:
    """Parse a frontend session.* message into a command dict.

    Returns None if the message is not a session command.
    """
    t = msg.get("type", "")
    if not t.startswith("session."):
        return None

    action = t.split(".", 1)[1]

    if action == "list":
        return {"action": "list"}

    if action == "create":
        return {"action": "create", "name": msg.get("name")}

    if action == "switch":
        session_id = msg.get("sessionId")
        if not session_id:
            return None
        return {"action": "switch", "sessionId": session_id}

    if action == "delete":
        session_id = msg.get("sessionId")
        if not session_id:
            return None
        return {"action": "delete", "sessionId": session_id}

    if action == "rename":
        session_id = msg.get("sessionId")
        name = msg.get("name")
        if not session_id or not name:
            return None
        return {"action": "rename", "sessionId": session_id, "name": name}

    return None


# --- Helpers ---


def error_msg(code: str, message: str) -> dict:
    return {"type": "error", "code": code, "message": message}


def status_msg(message: str) -> dict:
    return {"type": "status", "message": message}


def connected_msg(
    session_id: str, messages: list[dict], message_count: int
) -> dict:
    return {
        "type": "connected",
        "sessionId": session_id,
        "messages": messages,
        "messageCount": message_count,
    }
