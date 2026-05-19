#!/usr/bin/env python3
"""Deployment test script — list users, create users, login, chat via WS.

Usage:
    # List registered users
    python scripts/test_deploy.py users

    # Create a new user (returns token)
    python scripts/test_deploy.py create --first Alice --last Wonderland
    python scripts/test_deploy.py create --first Bob --last Builder --dob 1985-06-15 --email bob@custom.com --password s3cret

    # Login as existing user (returns token, then stops)
    python scripts/test_deploy.py login --email alice@mail.com
    python scripts/test_deploy.py login --email alice@mail.com --password s3cret

    # Chat interactively (type plain messages, see raw WS output)
    python scripts/test_deploy.py chat --token <jwt>
    python scripts/test_deploy.py chat --token <jwt> --session <id>

    # List sessions for a user
    python scripts/test_deploy.py sessions --token <jwt>

Env vars (or .env file):
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — for user management
    PROXY_URL — base URL of claw-auth-proxy (default: http://localhost:8000)
"""

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urljoin, urlparse

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8000")


def supa_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_users(args):
    """List registered Supabase users (name, email)."""
    resp = httpx.get(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=supa_headers(),
        params={"page": 1, "per_page": 100},
    )
    resp.raise_for_status()
    data = resp.json()
    users = data.get("users", data) if isinstance(data, dict) else data

    if not users:
        print("No users found.")
        return

    print(f"{'Email':<35} {'Name':<30} {'ID'}")
    print("-" * 100)
    for u in users:
        email = u.get("email", "")
        meta = u.get("user_metadata", {})
        name = f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
        uid = u.get("id", "")
        print(f"{email:<35} {name:<30} {uid}")
    print(f"\nTotal: {len(users)}")


def cmd_create(args):
    """Create a new Supabase user and print their access token."""
    first = args.first
    last = args.last
    dob = args.dob or "1990-01-01"
    email = args.email or f"{first.lower()}{last.lower()}@mail.com"
    password = args.password or "password"

    # Create user via admin API
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers={**supa_headers(), "Content-Type": "application/json"},
        json={
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {
                "first_name": first,
                "last_name": last,
                "date_of_birth": dob,
            },
        },
    )
    if resp.status_code == 422:
        print(f"User already exists: {email}")
        print("Use 'login' to get a token.")
        return
    resp.raise_for_status()
    user = resp.json()
    uid = user.get("id", "")
    print(f"Created user: {email} (id: {uid})")

    # Insert profile into profiles table (used by decrypted_profiles view)
    profile_resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/profiles",
        headers={**supa_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
        json={
            "id": uid,
            "first_name": first,
            "last_name": last,
            "date_of_birth": dob,
        },
    )
    if profile_resp.status_code in (200, 201):
        print(f"Profile created: {first} {last}, dob={dob}")
    elif profile_resp.status_code == 409:
        update_resp = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{uid}",
            headers={**supa_headers(), "Content-Type": "application/json", "Prefer": "return=minimal"},
            json={
                "first_name": first,
                "last_name": last,
                "date_of_birth": dob,
            },
        )
        if update_resp.status_code in (200, 204):
            print(f"Profile updated: {first} {last}, dob={dob}")
        else:
            print(f"Warning: profile update returned {update_resp.status_code}: {update_resp.text}")
    else:
        print(f"Warning: profile insert returned {profile_resp.status_code}: {profile_resp.text}")

    # Auto-login
    token = _login(email, password)
    if token:
        print(f"\nAccess token:\n{token}")


def cmd_login(args):
    """Login as existing user and print access token."""
    email = args.email
    password = args.password or "password"
    token = _login(email, password)
    if token:
        print(f"\nAccess token:\n{token}")
    else:
        sys.exit(1)


def _login(email: str, password: str) -> str | None:
    """Authenticate via Supabase GoTrue and return the access token."""
    resp = httpx.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={
            "apikey": SUPABASE_ANON_KEY,
            "Content-Type": "application/json",
        },
        json={"email": email, "password": password},
    )
    if resp.status_code != 200:
        print(f"Login failed ({resp.status_code}): {resp.text}")
        return None
    data = resp.json()
    token = data.get("access_token", "")
    user = data.get("user", {})
    print(f"Logged in as: {user.get('email', email)} (id: {user.get('id', '?')[:8]}...)")
    return token


def cmd_sessions(args):
    """List sessions via the proxy WS protocol (connects, reads session list, disconnects)."""
    token = args.token
    proxy_ws = _ws_url(token)

    async def _list():
        import websockets

        async with websockets.connect(proxy_ws) as ws:
            # Wait for connected message
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            _print_ws("<<", msg)

            # Send session.list
            await ws.send(json.dumps({"type": "session.list"}))
            _print_ws(">>", {"type": "session.list"})

            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            _print_ws("<<", msg)

            if msg.get("type") == "session.list.result":
                sessions = msg.get("sessions", [])
                if not sessions:
                    print("\nNo sessions found.")
                else:
                    print(f"\n{'Session ID':<40} {'Name':<25} {'Messages':<10} {'Last Activity'}")
                    print("-" * 100)
                    for s in sessions:
                        print(
                            f"{s.get('sessionId', ''):<40} "
                            f"{(s.get('name') or '(unnamed)'):<25} "
                            f"{s.get('messageCount', 0):<10} "
                            f"{s.get('lastActivity', '')}"
                        )

    asyncio.run(_list())


def cmd_chat(args):
    """Interactive chat via proxy WebSocket."""
    token = args.token
    session_id = args.session
    proxy_ws = _ws_url(token, session_id)

    asyncio.run(_chat_loop(proxy_ws))


async def _chat_loop(ws_url: str):
    import websockets

    print(f"Connecting to {ws_url}")
    async with websockets.connect(ws_url, ping_interval=30) as ws:
        # Receive initial messages (connected, status, etc.)
        connected = False
        while not connected:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            msg = json.loads(raw)
            _print_ws("<<", msg)
            if msg.get("type") == "connected":
                connected = True
                sid = msg.get("sessionId", "?")
                count = msg.get("messageCount", 0)
                print(f"\n--- Connected to session {sid} ({count} messages) ---")
                history = msg.get("messages", [])
                # Handle envelope: {"messages": [...], ...}
                if isinstance(history, dict):
                    history = history.get("messages", [])
                if not isinstance(history, list):
                    history = []
                if history:
                    print(f"--- Last {len(history)} messages: ---")
                    for m in history:
                        if isinstance(m, str):
                            print(f"  {m[:200]}")
                            continue
                        if not isinstance(m, dict):
                            continue
                        role = m.get("role", "?")
                        content = m.get("content", "")
                        prefix = "You" if role == "user" else "Agent"
                        display = content[:200] + "..." if len(content) > 200 else content
                        print(f"  [{prefix}] {display}")
                    print("---")
            elif msg.get("type") == "error":
                print(f"\nError: [{msg.get('code')}] {msg.get('message')}")
                return

        print("\nType a message and press Enter. Commands:")
        print("  /sessions  — list sessions")
        print("  /switch <id> — switch to session")
        print("  /new [name] — create new session")
        print("  /quit      — disconnect\n")

        # Start receive task
        recv_task = asyncio.create_task(_recv_loop(ws))

        try:
            loop = asyncio.get_event_loop()
            while True:
                line = await loop.run_in_executor(None, _read_line)
                if line is None:
                    break

                line = line.strip()
                if not line:
                    continue

                if line == "/quit":
                    break
                elif line == "/sessions":
                    payload = {"type": "session.list"}
                elif line.startswith("/switch "):
                    payload = {"type": "session.switch", "sessionId": line[8:].strip()}
                elif line.startswith("/new"):
                    name = line[4:].strip() or None
                    payload = {"type": "session.create"}
                    if name:
                        payload["name"] = name
                else:
                    payload = {"type": "message", "content": line}

                _print_ws(">>", payload)
                await ws.send(json.dumps(payload))

        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
            print("\nDisconnected.")


async def _recv_loop(ws):
    """Background task: print everything the server sends."""
    try:
        async for raw in ws:
            msg = json.loads(raw)
            _print_ws("<<", msg)

            # Pretty-print streaming chunks inline
            t = msg.get("type")
            if t == "chat.chunk":
                print(msg.get("content", ""), end="", flush=True)
            elif t == "chat.done":
                print()  # newline after stream ends
            elif t == "chat.tool_call":
                print(f"\n  [tool_call] {msg.get('name')}({json.dumps(msg.get('args', {}))})")
            elif t == "chat.tool_result":
                output = msg.get("output", "")
                display = output[:300] + "..." if len(output) > 300 else output
                print(f"  [tool_result] {msg.get('name')} -> {display}")
            elif t == "push.message":
                print(f"\n  [push] {msg.get('content', '')}")
            elif t == "session.list.result":
                sessions = msg.get("sessions", [])
                print(f"\n  Sessions ({len(sessions)}):")
                for s in sessions:
                    print(f"    {s.get('sessionId', '')} — {s.get('name') or '(unnamed)'} ({s.get('messageCount', 0)} msgs)")
            elif t in ("session.created", "session.switched"):
                print(f"\n  [{t}] {msg.get('sessionId', '')}")
            elif t == "status":
                print(f"\n  [status] {msg.get('message', '')}")
            elif t == "error":
                print(f"\n  [error] [{msg.get('code')}] {msg.get('message')}")
    except asyncio.CancelledError:
        pass


def _read_line() -> str | None:
    """Read a line from stdin (blocking). Returns None on EOF."""
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        return None


def _ws_url(token: str, session_id: str | None = None) -> str:
    """Build the proxy WebSocket URL."""
    parsed = urlparse(PROXY_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.netloc
    url = f"{scheme}://{host}/zeroclaw/ws?token={token}"
    if session_id:
        url += f"&session_id={session_id}"
    return url


def _print_ws(direction: str, msg: dict):
    """Print a WS message with direction indicator (verbose)."""
    t = msg.get("type", "?")
    # Compact display for chunks (they're also printed inline)
    if t in ("chat.chunk",):
        return
    compact = json.dumps(msg, ensure_ascii=False)
    if len(compact) > 200:
        compact = compact[:200] + "..."
    print(f"  {direction} {compact}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="claw-auth-proxy deployment test tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # users
    sub.add_parser("users", help="List registered Supabase users")

    # create
    p_create = sub.add_parser("create", help="Create a new user")
    p_create.add_argument("--first", required=True, help="First name")
    p_create.add_argument("--last", required=True, help="Last name")
    p_create.add_argument("--dob", help="Date of birth (default: 1990-01-01)")
    p_create.add_argument("--email", help="Email (default: <first><last>@mail.com)")
    p_create.add_argument("--password", help="Password (default: password)")

    # login
    p_login = sub.add_parser("login", help="Login and print access token")
    p_login.add_argument("--email", required=True, help="User email")
    p_login.add_argument("--password", help="Password (default: password)")

    # sessions
    p_sess = sub.add_parser("sessions", help="List sessions for a user")
    p_sess.add_argument("--token", required=True, help="JWT access token")

    # chat
    p_chat = sub.add_parser("chat", help="Interactive chat via WebSocket")
    p_chat.add_argument("--token", required=True, help="JWT access token")
    p_chat.add_argument("--session", help="Session ID to connect to")

    args = parser.parse_args()

    cmds = {
        "users": cmd_users,
        "create": cmd_create,
        "login": cmd_login,
        "sessions": cmd_sessions,
        "chat": cmd_chat,
    }
    cmds[args.command](args)


if __name__ == "__main__":
    main()
