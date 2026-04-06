"""Shared Pydantic models for the workflow contract."""

from typing import Any

from pydantic import BaseModel


# -- Workflow input --


class WorkflowState(BaseModel):
    """Workflow input and continue-as-new state."""
    working_dir: str
    messages: list[dict] = []
    response_id: str | None = None
    db_schema: str | None = None
    pubsub_state: Any = None  # PubSubState, serialized via data converter


# -- Workflow signals --


class StartTurnInput(BaseModel):
    message: str


# -- Workflow query --


class SessionInfo(BaseModel):
    session_id: str
    messages: list[dict]
    turn_in_progress: bool = False


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
