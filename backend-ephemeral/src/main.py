"""FastAPI application for the analytics agent."""

import asyncio
import logging
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .agent import run_agent_turn
from .database import load_schema
from .events import SSEEvent
from .sessions import create_session, delete_session as remove_session, get_session, list_sessions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Analytics Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Event buffers for reconnection: session_id -> list of SSE strings
_event_buffers: dict[str, list[str]] = defaultdict(list)
_abort_flags: dict[str, bool] = {}

# Signaling for stream subscribers: notified when new events are appended
# or when the turn ends.
_stream_signals: dict[str, asyncio.Event] = {}
_turn_active: dict[str, bool] = {}


def _notify(session_id: str) -> None:
    """Wake any /stream subscribers waiting for new events."""
    sig = _stream_signals.get(session_id)
    if sig:
        sig.set()


@app.on_event("startup")
async def startup():
    load_schema()
    logger.info("Analytics agent backend started")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


class SessionSummary(BaseModel):
    session_id: str
    message_count: int
    preview: str


class CreateSessionResponse(BaseModel):
    session_id: str


@app.get("/api/sessions", response_model=list[SessionSummary])
async def list_sessions_endpoint():
    sessions = list_sessions()
    result = []
    for s in sessions:
        user_msgs = [m for m in s.messages if isinstance(m, dict) and m.get("role") == "user"]
        preview = user_msgs[0]["content"][:80] if user_msgs else "New session"
        result.append(SessionSummary(
            session_id=s.session_id,
            message_count=len(s.messages),
            preview=preview,
        ))
    return result


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session_endpoint():
    session = create_session()
    return CreateSessionResponse(session_id=session.session_id)


@app.get("/api/sessions/{session_id}")
async def get_session_endpoint(session_id: str):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session.session_id}


class RunRequest(BaseModel):
    message: str


@app.post("/api/sessions/{session_id}/run")
async def run_session(session_id: str, request: RunRequest):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    _abort_flags[session_id] = False
    _turn_active[session_id] = True
    if session_id not in _stream_signals:
        _stream_signals[session_id] = asyncio.Event()

    async def generate():
        event_buffer = _event_buffers[session_id]
        try:
            async for event in run_agent_turn(session, request.message):
                if _abort_flags.get(session_id, False):
                    logger.info("Session %s interrupted", session_id)
                    break
                sse_str = event.to_sse()
                event_buffer.append(sse_str)
                _notify(session_id)
                yield sse_str
        except Exception:
            logger.exception("Error in agent turn for session %s", session_id)
            error_event = SSEEvent.error("Internal server error").to_sse()
            event_buffer.append(error_event)
            _notify(session_id)
            yield error_event
        finally:
            _turn_active[session_id] = False
            _notify(session_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _abort_flags[session_id] = True
    return {"status": "interrupted"}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    if not remove_session(session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    # Clean up associated state
    _abort_flags.pop(session_id, None)
    _turn_active.pop(session_id, None)
    _event_buffers.pop(session_id, None)
    _stream_signals.pop(session_id, None)
    return {"status": "deleted"}


@app.get("/api/sessions/{session_id}/stream")
async def stream_events(session_id: str, from_index: int = 0):
    """Replay buffered events then stream live events until the turn ends."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session_id not in _stream_signals:
        _stream_signals[session_id] = asyncio.Event()

    async def generate():
        last_index = from_index
        signal = _stream_signals[session_id]

        while True:
            # Send any buffered events we haven't sent yet
            events = _event_buffers.get(session_id, [])
            new_events = events[last_index:]
            for sse_str in new_events:
                yield sse_str
                last_index += 1

            # If no turn is active and we've sent everything, we're done
            if not _turn_active.get(session_id, False):
                return

            # Wait for new events or turn end
            signal.clear()
            await signal.wait()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sessions/{session_id}/files/{filename:path}")
async def get_session_file(session_id: str, filename: str):
    """Serve files generated by the agent (charts, exports, etc.)."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    file_path = session.working_dir / filename
    if not file_path.is_file() or not file_path.resolve().is_relative_to(session.working_dir.resolve()):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)
