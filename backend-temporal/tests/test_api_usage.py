"""Tests that verify the OpenAI API usage pattern matches documented requirements.

These tests assert that both the model and tool configuration align with the
README's documented minimum API key requirements (Responses API, gpt-4.1,
no OpenAI-hosted tools).
"""

from pathlib import Path

import pytest

ACTIVITIES_PATH = Path(__file__).parent.parent / "src" / "activities.py"
WORKFLOWS_PATH = Path(__file__).parent.parent / "src" / "workflows.py"

OPENAI_HOSTED_TOOL_TYPES = {"file_search", "web_search", "code_interpreter", "web_search_preview"}


@pytest.fixture()
def activities_source() -> str:
    return ACTIVITIES_PATH.read_text()


@pytest.fixture()
def workflows_source() -> str:
    return WORKFLOWS_PATH.read_text()


def test_uses_responses_stream(activities_source: str):
    """The activity must call client.responses.stream(), confirming Responses API usage."""
    assert "responses.stream(" in activities_source


def test_model_is_gpt_4_1(workflows_source: str):
    """The configured model must be gpt-4.1."""
    assert '"gpt-4.1"' in workflows_source


def test_no_openai_hosted_tools():
    """TOOL_DEFINITIONS must not include OpenAI-hosted tool types."""
    from src.workflows import TOOL_DEFINITIONS

    for tool in TOOL_DEFINITIONS:
        tool_type = tool.get("type", "")
        assert tool_type not in OPENAI_HOSTED_TOOL_TYPES, (
            f"Found OpenAI-hosted tool type '{tool_type}' in TOOL_DEFINITIONS"
        )


def test_all_tools_are_function_type():
    """All tool definitions should be type 'function' (local tools only)."""
    from src.workflows import TOOL_DEFINITIONS

    for tool in TOOL_DEFINITIONS:
        assert tool["type"] == "function", (
            f"Expected tool type 'function', got '{tool['type']}' for tool '{tool.get('name')}'"
        )
