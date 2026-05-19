import pytest


class TestTranslateUpstream:
    """Translate ZeroClaw WS messages to proxy protocol."""

    def test_chunk(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "chunk", "content": "Hello "}
        assert translate_upstream(zc) == {"type": "chat.chunk", "content": "Hello "}

    def test_done(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "done", "full_response": "Hello world"}
        assert translate_upstream(zc) == {
            "type": "chat.done",
            "fullResponse": "Hello world",
        }

    def test_tool_call(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "tool_call", "name": "shell", "args": {"cmd": "ls"}}
        assert translate_upstream(zc) == {
            "type": "chat.tool_call",
            "name": "shell",
            "args": {"cmd": "ls"},
        }

    def test_tool_result(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "tool_result", "name": "shell", "output": "file.txt"}
        assert translate_upstream(zc) == {
            "type": "chat.tool_result",
            "name": "shell",
            "output": "file.txt",
        }

    def test_chunk_reset_consumed(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "chunk_reset"}
        assert translate_upstream(zc) is None

    def test_web_push(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "web_push", "content": "Time to stretch!"}
        assert translate_upstream(zc) == {
            "type": "push.message",
            "content": "Time to stretch!",
        }

    def test_session_start(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {
            "type": "session_start",
            "session_id": "abc-123",
            "resumed": True,
            "message_count": 5,
        }
        result = translate_upstream(zc)
        assert result["type"] == "session_start"
        assert result["sessionId"] == "abc-123"

    def test_unknown_type_returned_as_is(self):
        from claw_proxy.ws.protocol import translate_upstream
        zc = {"type": "unknown_future_type", "data": 42}
        assert translate_upstream(zc) is None


class TestTranslateDownstream:
    """Translate frontend messages to ZeroClaw WS messages."""

    def test_message(self):
        from claw_proxy.ws.protocol import translate_downstream
        fe = {"type": "message", "content": "hello"}
        assert translate_downstream(fe) == {"type": "message", "content": "hello"}

    def test_message_empty_content_rejected(self):
        from claw_proxy.ws.protocol import translate_downstream
        fe = {"type": "message", "content": ""}
        assert translate_downstream(fe) is None

    def test_unknown_type(self):
        from claw_proxy.ws.protocol import translate_downstream
        fe = {"type": "not_a_real_type"}
        assert translate_downstream(fe) is None


class TestSessionCommands:
    """Parse frontend session commands."""

    def test_session_list(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "session.list"}
        cmd = parse_session_command(msg)
        assert cmd is not None
        assert cmd["action"] == "list"

    def test_session_create(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "session.create", "name": "Work chat"}
        cmd = parse_session_command(msg)
        assert cmd["action"] == "create"
        assert cmd["name"] == "Work chat"

    def test_session_switch(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "session.switch", "sessionId": "abc-123"}
        cmd = parse_session_command(msg)
        assert cmd["action"] == "switch"
        assert cmd["sessionId"] == "abc-123"

    def test_session_delete(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "session.delete", "sessionId": "abc-123"}
        cmd = parse_session_command(msg)
        assert cmd["action"] == "delete"
        assert cmd["sessionId"] == "abc-123"

    def test_session_rename(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "session.rename", "sessionId": "abc-123", "name": "New name"}
        cmd = parse_session_command(msg)
        assert cmd["action"] == "rename"
        assert cmd["sessionId"] == "abc-123"
        assert cmd["name"] == "New name"

    def test_non_session_returns_none(self):
        from claw_proxy.ws.protocol import parse_session_command
        msg = {"type": "message", "content": "hello"}
        assert parse_session_command(msg) is None


class TestErrorMessage:
    def test_error_format(self):
        from claw_proxy.ws.protocol import error_msg
        result = error_msg("AUTH_FAILED", "Invalid token")
        assert result == {
            "type": "error",
            "code": "AUTH_FAILED",
            "message": "Invalid token",
        }
