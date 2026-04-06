"""Tests for Pydantic model serialization and defaults."""

from src.types import (
    WorkflowState,
    StartTurnInput,
    SessionInfo,
    ModelCallInput,
    ModelCallResult,
    ToolCallInfo,
    ToolInput,
    ToolResult,
)


class TestWorkflowState:
    def test_defaults(self):
        state = WorkflowState(working_dir="/tmp/test")
        assert state.messages == []
        assert state.response_id is None
        assert state.db_schema is None
        assert state.pubsub_state is None

    def test_round_trip(self):
        state = WorkflowState(
            working_dir="/tmp/test",
            messages=[{"role": "user", "content": "hi"}],
            response_id="resp_123",
            db_schema="CREATE TABLE...",
        )
        data = state.model_dump()
        restored = WorkflowState.model_validate(data)
        assert restored == state


class TestStartTurnInput:
    def test_round_trip(self):
        inp = StartTurnInput(message="hello")
        assert StartTurnInput.model_validate(inp.model_dump()) == inp


class TestSessionInfo:
    def test_defaults(self):
        info = SessionInfo(session_id="s1", messages=[])
        assert info.turn_in_progress is False

    def test_round_trip(self):
        info = SessionInfo(
            session_id="s1",
            messages=[{"role": "user"}],
            turn_in_progress=True,
        )
        restored = SessionInfo.model_validate(info.model_dump())
        assert restored == info


class TestModelCallIO:
    def test_input_round_trip(self):
        inp = ModelCallInput(
            input_messages=[{"role": "user", "content": "hi"}],
            previous_response_id=None,
            tools=[{"type": "function", "name": "sql"}],
            model="gpt-4o",
            operation_id="op1",
        )
        restored = ModelCallInput.model_validate(inp.model_dump())
        assert restored == inp

    def test_result_defaults(self):
        result = ModelCallResult(
            response_id="r1",
            tool_calls=[],
        )
        assert result.final_text is None

    def test_result_with_tool_calls(self):
        tc = ToolCallInfo(item_id="i1", call_id="c1", name="sql", arguments={"query": "SELECT 1"})
        result = ModelCallResult(
            response_id="r1",
            tool_calls=[tc],
            final_text=None,
        )
        data = result.model_dump()
        restored = ModelCallResult.model_validate(data)
        assert restored.tool_calls[0].call_id == "c1"


class TestToolIO:
    def test_input_round_trip(self):
        inp = ToolInput(
            tool_name="execute_sql",
            arguments={"query": "SELECT 1"},
            working_dir="/tmp",
            call_id="c1",
            operation_id="op1",
        )
        restored = ToolInput.model_validate(inp.model_dump())
        assert restored == inp

    def test_result_round_trip(self):
        result = ToolResult(
            call_id="c1",
            tool_name="execute_sql",
            result={"rows": [{"n": 1}], "row_count": 1},
        )
        restored = ToolResult.model_validate(result.model_dump())
        assert restored == result
