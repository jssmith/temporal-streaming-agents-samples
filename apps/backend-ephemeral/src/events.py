"""SSE event types for the analytics agent."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class SSEEvent:
    type: str
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_sse(self) -> str:
        payload = {
            "type": self.type,
            "timestamp": self.timestamp,
            "data": self.data,
        }
        return f"data: {json.dumps(payload)}\n\n"

    @staticmethod
    def user_message(content: str) -> "SSEEvent":
        return SSEEvent(type="USER_MESSAGE", data={"content": content})

    @staticmethod
    def agent_start(agent_name: str = "analyst") -> "SSEEvent":
        return SSEEvent(type="AGENT_START", data={"agent_name": agent_name})

    @staticmethod
    def thinking_start(call_id: str | None = None) -> "SSEEvent":
        data = {}
        if call_id:
            data["call_id"] = call_id
        return SSEEvent(type="THINKING_START", data=data)

    @staticmethod
    def thinking_delta(delta: str, call_id: str | None = None) -> "SSEEvent":
        data: dict = {"delta": delta}
        if call_id:
            data["call_id"] = call_id
        return SSEEvent(type="THINKING_DELTA", data=data)

    @staticmethod
    def thinking_complete(content: str, call_id: str | None = None) -> "SSEEvent":
        data: dict = {"content": content}
        if call_id:
            data["call_id"] = call_id
        return SSEEvent(type="THINKING_COMPLETE", data=data)

    @staticmethod
    def tool_call_start(call_id: str, tool_name: str, arguments: dict) -> "SSEEvent":
        return SSEEvent(
            type="TOOL_CALL_START",
            data={"call_id": call_id, "tool_name": tool_name, "arguments": arguments},
        )

    @staticmethod
    def tool_call_complete(
        call_id: str, tool_name: str, result: dict | None = None, error: str | None = None
    ) -> "SSEEvent":
        data: dict = {"call_id": call_id, "tool_name": tool_name}
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        return SSEEvent(type="TOOL_CALL_COMPLETE", data=data)

    @staticmethod
    def text_delta(delta: str) -> "SSEEvent":
        return SSEEvent(type="TEXT_DELTA", data={"delta": delta})

    @staticmethod
    def text_complete(text: str) -> "SSEEvent":
        return SSEEvent(type="TEXT_COMPLETE", data={"text": text})

    @staticmethod
    def agent_complete() -> "SSEEvent":
        return SSEEvent(type="AGENT_COMPLETE", data={})

    @staticmethod
    def error(message: str) -> "SSEEvent":
        return SSEEvent(type="ERROR", data={"message": message})
