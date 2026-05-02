"""Pydantic models for the analytics workflow contract."""

from analytics_shared.types import ToolCallInfo
from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStreamState

__all__ = [
    "ModelCallInput",
    "ModelCallResult",
    "SessionInfo",
    "StartTurnInput",
    "TokenUsage",
    "ToolCallInfo",
    "ToolInput",
    "ToolResult",
    "WorkflowState",
]


# -- Workflow input --


class WorkflowState(BaseModel):
    """Workflow input and continue-as-new state."""
    working_dir: str
    model: str = "gpt-5.4"
    reasoning_effort: str | None = "medium"
    messages: list[dict] = []
    response_id: str | None = None
    db_schema: str | None = None
    stream_state: WorkflowStreamState | None = None


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
    reasoning_effort: str | None = None


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


class ModelCallResult(BaseModel):
    response_id: str
    tool_calls: list[ToolCallInfo]
    final_text: str | None = None
    usage: TokenUsage | None = None


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
