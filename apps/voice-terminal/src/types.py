"""Pydantic models for the voice analytics workflow contract."""

from analytics_shared.types import ToolCallInfo
from pydantic import BaseModel
from temporalio.contrib.workflow_streams import WorkflowStreamState


class VoiceWorkflowState(BaseModel):
    """Workflow input and continue-as-new state."""
    messages: list[dict] = []
    response_id: str | None = None
    db_schema: str | None = None
    stream_state: WorkflowStreamState | None = None
    # Carried across CAN so a start_turn or close_session that arrives in the
    # handoff window is honored by the new run instead of being silently lost.
    pending_audio: str | None = None
    closed: bool = False


class StartTurnInput(BaseModel):
    """Signal: client sends recorded audio to start a turn."""
    audio_base64: str


class TranscribeInput(BaseModel):
    audio_base64: str


class ModelCallInput(BaseModel):
    input_messages: list[dict]
    previous_response_id: str | None
    tools: list[dict]
    model: str


class ModelCallResult(BaseModel):
    response_id: str
    tool_calls: list[ToolCallInfo]
    final_text: str | None = None
