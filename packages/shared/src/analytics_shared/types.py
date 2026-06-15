"""Pydantic models shared between the sample apps."""

from pydantic import BaseModel


class ToolCallInfo(BaseModel):
    """One tool call extracted from a model response."""
    # Responses API output-item id (fc_...); the voice backend keys on it to
    # correlate streamed argument deltas with the item that owns them.
    item_id: str
    call_id: str
    name: str
    arguments: dict
