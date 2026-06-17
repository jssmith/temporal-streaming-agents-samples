"""Shared event envelope for the analytics agent.

The streaming protocol is a flat ``{"type", "timestamp", "data"}`` dict. It is
built in three places (the workflow's ``_emit``, the model-call activity, and
the ephemeral SSE server), so the shape lives here once.

The timestamp is passed in rather than read here: the workflow needs a
deterministic source (``workflow.now()``), while activities and the server use
wall-clock time. Keeping the envelope agnostic about its clock lets all three
share the same builder.
"""


def make_event(event_type: str, timestamp: str, **data) -> dict:
    """Build a streaming event dict from a type, an ISO timestamp, and data."""
    return {
        "type": event_type,
        "timestamp": timestamp,
        "data": data,
    }
