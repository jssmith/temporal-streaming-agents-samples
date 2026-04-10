"""SQLite database connection and schema loading for Chinook."""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "data" / "chinook.sqlite"

_schema_cache: str | None = None


def get_db_path() -> Path:
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}. Run ./setup.sh first.")
    return DB_PATH


def get_connection(readonly: bool = True) -> sqlite3.Connection:
    db_path = get_db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def load_schema() -> str:
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache

    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        )
        statements = [row[0] for row in cursor.fetchall()]
        _schema_cache = "\n\n".join(statements)
        logger.info("Loaded database schema (%d tables)", len(statements))
        return _schema_cache
    finally:
        conn.close()
