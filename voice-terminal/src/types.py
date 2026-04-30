"""Pydantic models for the voice analytics workflow contract."""

from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStreamState


# -- Topics --

AUDIO_TOPIC = "audio"
EVENTS_TOPIC = "events"


# -- Workflow state --


class VoiceWorkflowState(BaseModel):
    """Workflow input and continue-as-new state."""
    messages: list[dict] = []
    response_id: str | None = None
    db_schema: str | None = None
    stream_state: WorkflowStreamState | None = None


# -- Signals --


class StartTurnInput(BaseModel):
    """Signal: client sends recorded audio to start a turn."""
    audio_base64: str


# -- Activity I/O --


class TranscribeInput(BaseModel):
    audio_base64: str


class ModelCallInput(BaseModel):
    input_messages: list[dict]
    previous_response_id: str | None
    tools: list[dict]
    model: str


class ModelCallResult(BaseModel):
    response_id: str
    tool_calls: list["ToolCallInfo"]
    final_text: str | None = None


class ToolCallInfo(BaseModel):
    item_id: str
    call_id: str
    name: str
    arguments: dict
