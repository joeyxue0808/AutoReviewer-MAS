"""Sandbox Factory 单元测试。"""

import pytest
from app.sandbox.null_engine import NullSandbox


class TestNullSandbox:
    @pytest.mark.asyncio
    async def test_always_returns_success(self):
        sandbox = NullSandbox()
        result = await sandbox.run(
            image="any:latest",
            command="any command",
            source_files={"a.py": "print('hello')"},
        )
        assert result.success
        assert result.exit_code == 0
        assert not result.timed_out

    @pytest.mark.asyncio
    async def test_cleanup_noop(self):
        sandbox = NullSandbox()
        await sandbox.cleanup()  # 不应抛异常

    @pytest.mark.asyncio
    async def test_run_by_language(self):
        sandbox = NullSandbox()
        result = await sandbox.run_by_language(
            language="python",
            source_files={"a.py": "x = 1"},
        )
        assert result.success
