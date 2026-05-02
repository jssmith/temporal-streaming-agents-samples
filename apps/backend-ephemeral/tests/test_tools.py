"""Tests for tool implementations against real Chinook database."""

import pytest

from src.tools import execute_sql, execute_python, execute_bash, run_tool


@pytest.fixture
def working_dir(tmp_path):
    return tmp_path


class TestExecuteSQL:
    @pytest.mark.asyncio
    async def test_select_returns_rows(self):
        result = await execute_sql("SELECT COUNT(*) AS cnt FROM Artist")
        assert "rows" in result
        assert result["row_count"] > 0
        assert result["rows"][0]["cnt"] > 0

    @pytest.mark.asyncio
    async def test_forbidden_insert(self):
        result = await execute_sql("INSERT INTO Artist VALUES (999, 'Evil')")
        assert "error" in result
        assert "Write operations not allowed" in result["error"]

    @pytest.mark.asyncio
    async def test_forbidden_update(self):
        result = await execute_sql("UPDATE Artist SET Name='X' WHERE ArtistId=1")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_forbidden_delete(self):
        result = await execute_sql("DELETE FROM Artist WHERE ArtistId=1")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_forbidden_drop(self):
        result = await execute_sql("DROP TABLE Artist")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_auto_appends_limit(self):
        result = await execute_sql("SELECT * FROM Track")
        assert result["row_count"] <= 500

    @pytest.mark.asyncio
    async def test_respects_existing_limit(self):
        result = await execute_sql("SELECT * FROM Track LIMIT 3")
        assert result["row_count"] == 3

    @pytest.mark.asyncio
    async def test_bad_sql_returns_error(self):
        result = await execute_sql("SELECT * FROM nonexistent_table")
        assert "error" in result


class TestExecutePython:
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_simple_output(self, working_dir):
        result = await execute_python("print('hello world')", working_dir)
        assert result["output"].strip() == "hello world"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_no_output(self, working_dir):
        result = await execute_python("x = 1", working_dir)
        assert result["output"] == "(no output)"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_error_output(self, working_dir):
        result = await execute_python("raise ValueError('boom')", working_dir)
        assert "error" in result
        assert "boom" in result["error"]


class TestExecuteBash:
    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_simple_command(self, working_dir):
        result = await execute_bash("echo hello", working_dir)
        assert "hello" in result["output"]
        assert result["exit_code"] == 0

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_nonzero_exit_code(self, working_dir):
        result = await execute_bash("exit 42", working_dir)
        assert result["exit_code"] == 42


class TestRunTool:
    @pytest.mark.asyncio
    async def test_dispatch_sql(self, working_dir):
        result = await run_tool("execute_sql", {"query": "SELECT 1 AS n"}, working_dir)
        assert result["rows"][0]["n"] == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_dispatch_python(self, working_dir):
        result = await run_tool("execute_python", {"code": "print(42)"}, working_dir)
        assert "42" in result["output"]

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_dispatch_bash(self, working_dir):
        result = await run_tool("bash", {"command": "echo ok"}, working_dir)
        assert "ok" in result["output"]

    @pytest.mark.asyncio
    async def test_unknown_tool(self, working_dir):
        result = await run_tool("nonexistent", {}, working_dir)
        assert "error" in result
        assert "Unknown tool" in result["error"]
