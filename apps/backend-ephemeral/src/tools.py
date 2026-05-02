"""Tool implementations for the analytics agent."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from analytics_shared.database import get_db_path
from analytics_shared.sql_tool import TOOL_DEFINITION as SQL_TOOL_DEFINITION
from analytics_shared.sql_tool import execute_sql

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30


async def execute_python(code: str, working_dir: Path) -> dict:
    """Execute Python code in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            cwd=str(working_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )

        result: dict = {}
        if stdout:
            result["output"] = stdout.decode()
        if stderr:
            result["error"] = stderr.decode()
        if not stdout and not stderr:
            result["output"] = "(no output)"
        return result

    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}


async def execute_bash(command: str, working_dir: Path) -> dict:
    """Execute a shell command in a subprocess."""
    db_path = str(get_db_path().resolve())
    env = {**os.environ, "DB_PATH": db_path}

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(working_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )

        output = (stdout.decode() if stdout else "") + (stderr.decode() if stderr else "")
        return {"output": output, "exit_code": proc.returncode}

    except asyncio.TimeoutError:
        proc.kill()
        return {"error": f"Execution timed out after {TIMEOUT_SECONDS}s"}


# Tool definitions for the Responses API
TOOL_DEFINITIONS = [
    SQL_TOOL_DEFINITION,
    {
        "type": "function",
        "name": "execute_python",
        "description": "Run Python code in a subprocess. pandas, matplotlib, sqlite3, json, math, statistics, collections, itertools are available. DB_PATH env var points to the SQLite file. Save matplotlib figures to files in the current directory. Print output to stdout.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python code to execute",
                }
            },
            "required": ["code"],
        },
    },
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command. DB_PATH env var is available. Working directory is the session directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        },
    },
]


async def run_tool(tool_name: str, arguments: dict, working_dir: Path) -> dict:
    """Dispatch a tool call to the appropriate implementation."""
    if tool_name == "execute_sql":
        return await execute_sql(arguments["query"])
    elif tool_name == "execute_python":
        return await execute_python(arguments["code"], working_dir)
    elif tool_name == "bash":
        return await execute_bash(arguments["command"], working_dir)
    else:
        return {"error": f"Unknown tool: {tool_name}"}
