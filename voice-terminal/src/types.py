"""Pydantic models for the voice analytics workflow contract."""

from pydantic import BaseModel


# -- Workflow state --


class VoiceWorkflowState(BaseModel):
    """Workflow input and continue-as-new state."""
    messages: list[dict] = []
    response_id: str | None = None
    db_schema: str | None = None


# -- Signals --


class StartTurnInput(BaseModel):
    """Signal: client sends recorded audio to start a turn."""
    audio_base64: str


class ActivityEventsInput(BaseModel):
    """Signal from activity -> workflow with batched events."""
    events: list[dict]


class AckAudioInput(BaseModel):
    """Signal: client acknowledges it has consumed audio chunks up to this index."""
    through_index: int


# -- Update (long-poll) --


class PollAudioInput(BaseModel):
    last_seen_index: int


class PollAudioResult(BaseModel):
    audio_chunks: list[str]  # base64-encoded PCM
    next_index: int = 0  # index after the last chunk returned
    transcript: str | None = None
    response_text: str | None = None
    tool_calls: list[dict] = []
    turn_complete: bool = False


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


class TTSInput(BaseModel):
    text: str


class ToolCallSummary(BaseModel):
    name: str
    arguments: dict
    result: dict
