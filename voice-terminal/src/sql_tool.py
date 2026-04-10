"""SQL tool for the voice analytics agent."""

import sqlite3

from .database import get_connection

FORBIDDEN_PREFIXES = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE")
ROW_LIMIT = 500

TOOL_DEFINITION = {
    "type": "function",
    "name": "execute_sql",
    "description": "Run a read-only SQL query against the Chinook SQLite database. Returns rows as a list of objects.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The SQL query to execute",
            }
        },
        "required": ["query"],
    },
}


def execute_sql(query: str) -> dict:
    """Execute a read-only SQL query against the Chinook database."""
    stripped = query.strip().upper()
    for prefix in FORBIDDEN_PREFIXES:
        if stripped.startswith(prefix):
            return {"error": f"Write operations not allowed: {prefix}"}

    if "LIMIT" not in stripped:
        query = query.rstrip().rstrip(";") + f" LIMIT {ROW_LIMIT}"

    conn = get_connection(readonly=True)
    try:
        cursor = conn.execute(query)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return {"rows": rows, "row_count": len(rows)}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()
