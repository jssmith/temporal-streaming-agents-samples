"""Activity-level cancellation tests for execute_tool.

Deterministic and server-free: ActivityEnvironment.cancel() raises
CancelledError directly into the activity task, so we can prove the subprocess
is actually killed without waiting on heartbeat delivery.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from temporalio.testing import ActivityEnvironment

from src.activities import execute_tool
from src.types import ToolInput


def _tool_input(command: str, working_dir: str) -> ToolInput:
    return ToolInput(
        tool_name="bash",
        arguments={"command": command},
        working_dir=working_dir,
        call_id="c1",
        operation_id="op1",
    )


@pytest.mark.asyncio
async def test_execute_tool_kills_subprocess_on_cancel(tmp_path):
    """Cancelling the activity kills the subprocess group, not just the await.

    The shell records its own PID (it leads its process group because the
    subprocess is started with start_new_session=True), then sleeps. After
    cancellation the whole group must be gone and the post-sleep marker must
    never appear.
    """
    env = ActivityEnvironment()
    pidfile = tmp_path / "pid"
    done = tmp_path / "done"
    command = f"echo $$ > {pidfile}; sleep 30; touch {done}"

    task = asyncio.create_task(env.run(execute_tool, _tool_input(command, str(tmp_path))))

    # Wait until the subprocess has spawned and recorded its PID, so we know
    # there is a live process to kill (and can capture its group).
    async with asyncio.timeout(10):
        while not pidfile.exists() or not pidfile.read_text().strip():
            await asyncio.sleep(0.02)
    pgid = os.getpgid(int(pidfile.read_text().strip()))

    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # signal 0 just probes liveness; the group is gone once killed and reaped.
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)
    assert not done.exists(), "sleep should have been killed before completing"


@pytest.mark.asyncio
async def test_execute_tool_normal_completion(tmp_path):
    """The heartbeat-wrapped happy path still returns tool output."""
    env = ActivityEnvironment()
    result = await env.run(execute_tool, _tool_input("echo hello", str(tmp_path)))
    assert result.tool_name == "bash"
    assert "hello" in result.result["output"]
    assert result.result["exit_code"] == 0
