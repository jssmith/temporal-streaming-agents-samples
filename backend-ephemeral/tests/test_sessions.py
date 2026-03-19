"""Tests for in-memory session store."""

import pytest

from src import sessions as sessions_mod
from src.sessions import Session, create_session, get_session, list_sessions


@pytest.fixture(autouse=True)
def clean_sessions():
    """Clear session store between tests."""
    sessions_mod._sessions.clear()
    yield
    sessions_mod._sessions.clear()


class TestSessionStore:
    def test_create_session_returns_session(self):
        session = create_session()
        assert isinstance(session, Session)
        assert session.session_id

    def test_create_session_creates_working_dir(self):
        session = create_session()
        assert session.working_dir.exists()
        assert session.working_dir.is_dir()

    def test_get_session_returns_created(self):
        session = create_session()
        found = get_session(session.session_id)
        assert found is session

    def test_get_session_returns_none_for_missing(self):
        assert get_session("nonexistent") is None

    def test_list_sessions_empty(self):
        assert list_sessions() == []

    def test_list_sessions_returns_all(self):
        s1 = create_session()
        s2 = create_session()
        all_sessions = list_sessions()
        assert len(all_sessions) == 2
        ids = {s.session_id for s in all_sessions}
        assert s1.session_id in ids
        assert s2.session_id in ids

    def test_session_defaults(self):
        session = create_session()
        assert session.messages == []
        assert session.response_id is None
        assert session.sequence_number is None
