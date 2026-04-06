"""FastAPI proxy for the Temporal-backed analytics agent."""

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.contrib.pubsub import PubSubClient

from .constants import EVENTS_TOPIC
from .types import (
    SessionInfo,
    StartTurnInput,
    WorkflowState,
)
from .workflows import AnalyticsWorkflow

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TASK_QUEUE = "analytics-agent"
SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"

_client: Client | None = None


async def get_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(
            "localhost:7233",
            data_converter=pydantic_data_converter,
        )
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_client()
    logger.info("Analytics agent Temporal backend started")
    yield


app = FastAPI(title="Analytics Agent (Temporal)", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "ok", "implementation": "temporal"}


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


class CreateSessionResponse(BaseModel):
    session_id: str


class SessionSummary(BaseModel):
    session_id: str
    message_count: int
    preview: str


class SessionMessages(BaseModel):
    messages: list[dict]
    turn_in_progress: bool = False


class RunRequest(BaseModel):
    message: str


@app.post("/api/sessions", response_model=CreateSessionResponse)
async def create_session():
    client = await get_client()
    session_id = uuid.uuid4().hex[:8]

    # Create working directory
    working_dir = SESSIONS_DIR / session_id
    working_dir.mkdir(parents=True, exist_ok=True)

    await client.start_workflow(
        AnalyticsWorkflow.run,
        WorkflowState(working_dir=str(working_dir)),
        id=session_id,
        task_queue=TASK_QUEUE,
    )

    return CreateSessionResponse(session_id=session_id)


@app.get("/api/sessions", response_model=list[SessionSummary])
async def list_sessions():
    client = await get_client()
    results = []
    async for wf in client.list_workflows(
        'WorkflowType="AnalyticsWorkflow"'
    ):
        if wf.status == WorkflowExecutionStatus.RUNNING:
            try:
                handle = client.get_workflow_handle(wf.id)
                info: SessionInfo = await handle.query(
                    AnalyticsWorkflow.get_session
                )
                user_msgs = [
                    m for m in info.messages
                    if isinstance(m, dict) and m.get("role") == "user"
                ]
                preview = user_msgs[0]["content"][:80] if user_msgs else "New session"
                results.append(SessionSummary(
                    session_id=info.session_id,
                    message_count=len(info.messages),
                    preview=preview,
                ))
            except Exception:
                logger.exception("Failed to query workflow %s", wf.id)
    return results


@app.get("/api/sessions/{session_id}", response_model=SessionMessages)
async def get_session(session_id: str):
    client = await get_client()
    handle = client.get_workflow_handle(session_id)
    try:
        info: SessionInfo = await handle.query(AnalyticsWorkflow.get_session)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    return SessionMessages(
        messages=info.messages,
        turn_in_progress=info.turn_in_progress,
    )


@app.post("/api/sessions/{session_id}/run")
async def run_session(session_id: str, request: RunRequest):
    client = await get_client()
    handle = client.get_workflow_handle(session_id)

    # Verify workflow is running
    try:
        desc = await handle.describe()
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    if desc.status != WorkflowExecutionStatus.RUNNING:
        raise HTTPException(status_code=404, detail="Session not running")

    # Get current pub/sub offset
    pubsub = PubSubClient.for_workflow(client, session_id)
    start_offset = await pubsub.get_offset()

    # Fire-and-forget: enqueue the user message
    await handle.signal(
        AnalyticsWorkflow.start_turn,
        StartTurnInput(message=request.message),
    )

    async def event_stream():
        async for item in pubsub.subscribe(
            topics=[EVENTS_TOPIC], from_offset=start_offset
        ):
            event = json.loads(item.data)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "AGENT_COMPLETE":
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Signal the workflow to exit gracefully."""
    client = await get_client()
    handle = client.get_workflow_handle(session_id)
    try:
        await handle.signal(AnalyticsWorkflow.close_session)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted"}


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str):
    client = await get_client()
    handle = client.get_workflow_handle(session_id)
    try:
        await handle.signal(AnalyticsWorkflow.interrupt)
    except Exception:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "interrupted"}


@app.get("/api/sessions/{session_id}/stream")
async def stream_events(session_id: str, from_index: int = 0):
    """Resume streaming events for an in-progress turn."""
    client = await get_client()
    pubsub = PubSubClient.for_workflow(client, session_id)

    async def event_stream():
        async for item in pubsub.subscribe(
            topics=[EVENTS_TOPIC], from_offset=from_index
        ):
            event = json.loads(item.data)
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "AGENT_COMPLETE":
                return

    return StreamingResponse(
        event_stream(),
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
    file_path = SESSIONS_DIR / session_id / filename
    if not file_path.is_file() or not file_path.resolve().is_relative_to(
        (SESSIONS_DIR / session_id).resolve()
    ):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)
