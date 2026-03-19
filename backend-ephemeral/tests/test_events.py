"""Tests for SSE event types and serialization."""

import json

from src.events import SSEEvent


class TestSSEEventFormat:
    def test_to_sse_format(self):
        event = SSEEvent(type="TEST", data={"key": "val"}, timestamp="2024-01-01T00:00:00Z")
        sse = event.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")

    def test_to_sse_parses_as_json(self):
        event = SSEEvent(type="TEST", data={"key": "val"}, timestamp="2024-01-01T00:00:00Z")
        sse = event.to_sse()
        payload = json.loads(sse.removeprefix("data: ").strip())
        assert payload["type"] == "TEST"
        assert payload["timestamp"] == "2024-01-01T00:00:00Z"
        assert payload["data"] == {"key": "val"}

    def test_default_timestamp_is_iso(self):
        event = SSEEvent(type="TEST")
        # Should be a valid ISO timestamp with timezone
        assert "T" in event.timestamp


class TestFactoryMethods:
    def test_user_message(self):
        event = SSEEvent.user_message("hello")
        assert event.type == "USER_MESSAGE"
        assert event.data == {"content": "hello"}

    def test_agent_start_default(self):
        event = SSEEvent.agent_start()
        assert event.type == "AGENT_START"
        assert event.data == {"agent_name": "analyst"}

    def test_agent_start_custom(self):
        event = SSEEvent.agent_start("coder")
        assert event.data == {"agent_name": "coder"}

    def test_thinking_start_no_call_id(self):
        event = SSEEvent.thinking_start()
        assert event.type == "THINKING_START"
        assert event.data == {}

    def test_thinking_start_with_call_id(self):
        event = SSEEvent.thinking_start(call_id="abc")
        assert event.data == {"call_id": "abc"}

    def test_thinking_delta(self):
        event = SSEEvent.thinking_delta("chunk")
        assert event.type == "THINKING_DELTA"
        assert event.data["delta"] == "chunk"

    def test_thinking_delta_with_call_id(self):
        event = SSEEvent.thinking_delta("chunk", call_id="abc")
        assert event.data == {"delta": "chunk", "call_id": "abc"}

    def test_thinking_complete(self):
        event = SSEEvent.thinking_complete("full thought")
        assert event.type == "THINKING_COMPLETE"
        assert event.data["content"] == "full thought"

    def test_tool_call_start(self):
        event = SSEEvent.tool_call_start("c1", "execute_sql", {"query": "SELECT 1"})
        assert event.type == "TOOL_CALL_START"
        assert event.data == {
            "call_id": "c1",
            "tool_name": "execute_sql",
            "arguments": {"query": "SELECT 1"},
        }

    def test_tool_call_complete_with_result(self):
        event = SSEEvent.tool_call_complete("c1", "execute_sql", result={"rows": []})
        assert event.type == "TOOL_CALL_COMPLETE"
        assert event.data["result"] == {"rows": []}
        assert "error" not in event.data

    def test_tool_call_complete_with_error(self):
        event = SSEEvent.tool_call_complete("c1", "execute_sql", error="bad query")
        assert event.data["error"] == "bad query"
        assert "result" not in event.data

    def test_tool_call_complete_minimal(self):
        event = SSEEvent.tool_call_complete("c1", "execute_sql")
        assert event.data == {"call_id": "c1", "tool_name": "execute_sql"}

    def test_text_delta(self):
        event = SSEEvent.text_delta("word")
        assert event.type == "TEXT_DELTA"
        assert event.data == {"delta": "word"}

    def test_text_complete(self):
        event = SSEEvent.text_complete("full text")
        assert event.type == "TEXT_COMPLETE"
        assert event.data == {"text": "full text"}

    def test_agent_complete(self):
        event = SSEEvent.agent_complete()
        assert event.type == "AGENT_COMPLETE"
        assert event.data == {}

    def test_error(self):
        event = SSEEvent.error("something broke")
        assert event.type == "ERROR"
        assert event.data == {"message": "something broke"}
