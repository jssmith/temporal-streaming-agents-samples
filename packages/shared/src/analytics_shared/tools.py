"""OpenAI Responses API tool schemas shared by the sample backends.

Both backends (and previously the workflow) hand-rolled the same three
function-tool definitions. They live here once. These are pure data dicts, so
they pass through ``workflow.unsafe.imports_passed_through`` cleanly.

The ``execute_sql`` schema is owned by ``sql_tool`` (next to its
implementation); it is re-exported here so callers can pull all three from one
place.
"""

from .sql_tool import TOOL_DEFINITION as EXECUTE_SQL_TOOL

EXECUTE_PYTHON_TOOL: dict = {
    "type": "function",
    "name": "execute_python",
    "description": (
        "Run Python code in a subprocess. pandas, matplotlib, sqlite3, json, "
        "math, statistics, collections, itertools are available. DB_PATH env "
        "var points to the SQLite file. Save matplotlib figures to files in the "
        "current directory. Print output to stdout."
    ),
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
}

BASH_TOOL: dict = {
    "type": "function",
    "name": "bash",
    "description": (
        "Run a shell command. DB_PATH env var is available. Working directory "
        "is the session directory."
    ),
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
}

# The full toolset for the analytics agent, in a stable order.
TOOL_DEFINITIONS: list[dict] = [
    EXECUTE_SQL_TOOL,
    EXECUTE_PYTHON_TOOL,
    BASH_TOOL,
]
