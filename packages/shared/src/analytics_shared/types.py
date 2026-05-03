"""Pydantic models shared between the sample apps."""

from pydantic import BaseModel


class ToolCallInfo(BaseModel):
    """One tool call extracted from a model response."""
    item_id: str
    call_id: str
    name: str
    arguments: dict
