"""Upstream WebSocket client — connects to a ZeroClaw container.

Handles connection, reconnection, send/receive, and session switching.
"""

import asyncio
import json
import logging
from urllib.parse import urlencode

import websockets
from websockets.asyncio.client import ClientConnection

log = logging.getLogger(__name__)

# Temporary mitigation: give long-running delegated turns more time before the
# proxy decides the upstream chat socket is dead.
UPSTREAM_PING_INTERVAL_SECONDS = 300
UPSTREAM_PING_TIMEOUT_SECONDS = 60


class UpstreamConnection:
    def __init__(self, ws_url: str, bearer_token: str) -> None:
        self._base_url = ws_url
        self._token = bearer_token
        self._ws: ClientConnection | None = None
        self._session_id: str | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._ws.state.name == "OPEN"

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def _build_url(self, session_id: str, name: str | None = None) -> str:
        params = {"session_id": session_id, "token": self._token}
        if name:
            params["name"] = name
        return f"{self._base_url}?{urlencode(params)}"

    async def connect(self, session_id: str, name: str | None = None) -> dict | None:
        """Connect to ZeroClaw with the given session ID.

        Returns the session_start message from ZeroClaw, or None on failure.
        """
        url = self._build_url(session_id, name)
        try:
            self._ws = await websockets.connect(
                url,
                ping_interval=UPSTREAM_PING_INTERVAL_SECONDS,
                ping_timeout=UPSTREAM_PING_TIMEOUT_SECONDS,
            )
            self._session_id = session_id

            # Wait for session_start
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") == "session_start":
                log.info("Connected to ZeroClaw session %s", session_id[:8])
                return msg
            log.warning("Unexpected first message: %s", msg.get("type"))
            return msg
        except Exception as e:
            log.error("Failed to connect to ZeroClaw: %s", e)
            self._ws = None
            return None

    async def disconnect(self) -> None:
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._session_id = None

    async def send(self, msg: dict) -> None:
        if not self._ws:
            raise RuntimeError("Not connected")
        await self._ws.send(json.dumps(msg))

    async def recv(self) -> dict | None:
        """Receive and parse one message. Returns None on connection close."""
        if not self._ws:
            return None
        try:
            raw = await self._ws.recv()
            return json.loads(raw)
        except websockets.ConnectionClosed:
            log.info("Upstream WS closed")
            self._ws = None
            return None
        except json.JSONDecodeError as e:
            log.warning("Invalid JSON from upstream: %s", e)
            return None

    async def switch_session(
        self, session_id: str, name: str | None = None
    ) -> dict | None:
        """Disconnect and reconnect with a different session ID."""
        await self.disconnect()
        return await self.connect(session_id, name)
