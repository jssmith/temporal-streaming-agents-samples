"""Shared Pydantic models for the workflow contract."""

from pydantic import BaseModel


# -- Workflow signals --


class StartTurnInput(BaseModel):
    message: str


class ActivityEventsInput(BaseModel):
    """Signal from activity -> workflow with batched events."""
    events: list[dict]


# -- Workflow update --


class PollEventsInput(BaseModel):
    last_seen_index: int


class PollEventsResult(BaseModel):
    events: list[dict]
    turn_complete: bool


# -- Workflow query --


class SessionInfo(BaseModel):
    session_id: str
    messages: list[dict]


# -- Activity I/O --


class ModelCallInput(BaseModel):
    input_messages: list[dict]
    previous_response_id: str | None
    tools: list[dict]
    model: str
    operation_id: str


class ModelCallResult(BaseModel):
    response_id: str
    tool_calls: list["ToolCallInfo"]
    final_text: str | None = None


class ToolCallInfo(BaseModel):
    item_id: str
    call_id: str
    name: str
    arguments: dict


class ToolInput(BaseModel):
    tool_name: str
    arguments: dict
    working_dir: str
    call_id: str
    operation_id: str


class ToolResult(BaseModel):
    call_id: str
    tool_name: str
    result: dict
