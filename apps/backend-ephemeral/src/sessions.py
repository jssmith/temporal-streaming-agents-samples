"""In-memory session store."""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent.parent.parent / "sessions"


@dataclass
class Session:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    response_id: str | None = None
    sequence_number: int | None = None
    working_dir: Path = field(default_factory=Path)


_sessions: dict[str, Session] = {}


def create_session() -> Session:
    session_id = str(uuid.uuid4())
    working_dir = SESSIONS_DIR / session_id
    working_dir.mkdir(parents=True, exist_ok=True)
    session = Session(session_id=session_id, working_dir=working_dir)
    _sessions[session_id] = session
    return session


def get_session(session_id: str) -> Session | None:
    return _sessions.get(session_id)


def delete_session(session_id: str) -> bool:
    return _sessions.pop(session_id, None) is not None


def list_sessions() -> list[Session]:
    return list(_sessions.values())
