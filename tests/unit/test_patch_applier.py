"""PatchApplier 单元测试 - Search/Replace Block 精确替换。"""

import pytest
from app.utils.patch_applier import PatchApplier, PatchApplyError, ApplyResult


@pytest.fixture
def applier():
    return PatchApplier()


@pytest.fixture
def source_files():
    return {
        "main.py": "def hello():\n    print('hello')\n\ndef world():\n    print('world')\n",
        "utils.py": "def add(a, b):\n    return a + b\n",
    }


# ─────────────────────────────────────────────
# apply (strict mode)
# ─────────────────────────────────────────────

class TestApply:
    def test_simple_replace(self, applier, source_files):
        blocks = [
            {
                "file_path": "main.py",
                "search_block": "print('hello')",
                "replace_block": "print('hi')",
            }
        ]
        result = applier.apply(source_files, blocks)
        assert "print('hi')" in result["main.py"]
        assert "print('hello')" not in result["main.py"]

    def test_multi_block_replace(self, applier, source_files):
        blocks = [
            {
                "file_path": "main.py",
                "search_block": "print('hello')",
                "replace_block": "print('hi')",
            },
            {
                "file_path": "utils.py",
                "search_block": "return a + b",
                "replace_block": "return a - b",
            },
        ]
        result = applier.apply(source_files, blocks)
        assert "print('hi')" in result["main.py"]
        assert "return a - b" in result["utils.py"]

    def test_preserves_original(self, applier, source_files):
        """apply 不应修改原始 source_files。"""
        blocks = [
            {
                "file_path": "main.py",
                "search_block": "print('hello')",
                "replace_block": "print('hi')",
            }
        ]
        applier.apply(source_files, blocks)
        assert "print('hello')" in source_files["main.py"]

    def test_raises_on_missing_file(self, applier, source_files):
        blocks = [
            {
                "file_path": "nonexistent.py",
                "search_block": "x",
                "replace_block": "y",
            }
        ]
        with pytest.raises(PatchApplyError) as exc_info:
            applier.apply(source_files, blocks)
        assert "nonexistent.py" in str(exc_info.value)

    def test_raises_on_search_not_found(self, applier, source_files):
        blocks = [
            {
                "file_path": "main.py",
                "search_block": "this text does not exist in the file at all",
                "replace_block": "replacement",
            }
        ]
        with pytest.raises(PatchApplyError) as exc_info:
            applier.apply(source_files, blocks)
        assert "未找到精确匹配" in str(exc_info.value)

    def test_raises_on_empty_file_path(self, applier, source_files):
        blocks = [{"file_path": "", "search_block": "x", "replace_block": "y"}]
        with pytest.raises(PatchApplyError):
            applier.apply(source_files, blocks)

    def test_raises_on_empty_search(self, applier, source_files):
        blocks = [{"file_path": "main.py", "search_block": "", "replace_block": "y"}]
        with pytest.raises(PatchApplyError):
            applier.apply(source_files, blocks)

    def test_only_replaces_first_occurrence(self, applier):
        source = {"f.py": "aaa\naaa\naaa\n"}
        blocks = [{"file_path": "f.py", "search_block": "aaa", "replace_block": "bbb"}]
        result = applier.apply(source, blocks)
        # 只替换第一次出现
        lines = result["f.py"].split("\n")
        assert lines[0] == "bbb"
        assert lines[1] == "aaa"


# ─────────────────────────────────────────────
# try_apply (lenient mode)
# ─────────────────────────────────────────────

class TestTryApply:
    def test_all_success(self, applier, source_files):
        blocks = [
            {"file_path": "main.py", "search_block": "print('hello')", "replace_block": "print('hi')"},
            {"file_path": "utils.py", "search_block": "return a + b", "replace_block": "return a * b"},
        ]
        result, results = applier.try_apply(source_files, blocks)
        assert len(results) == 2
        assert all(r.success for r in results)

    def test_partial_failure(self, applier, source_files):
        blocks = [
            {"file_path": "main.py", "search_block": "print('hello')", "replace_block": "print('hi')"},
            {"file_path": "main.py", "search_block": "this does not exist at all", "replace_block": "x"},
        ]
        result, results = applier.try_apply(source_files, blocks)
        assert results[0].success
        assert not results[1].success
        assert results[1].error

    def test_missing_file_partial(self, applier, source_files):
        blocks = [
            {"file_path": "main.py", "search_block": "print('hello')", "replace_block": "print('hi')"},
            {"file_path": "missing.py", "search_block": "x", "replace_block": "y"},
        ]
        result, results = applier.try_apply(source_files, blocks)
        assert results[0].success
        assert not results[1].success


# ─────────────────────────────────────────────
# _find_closest_match
# ─────────────────────────────────────────────

class TestFindClosestMatch:
    def test_finds_match(self):
        content = "def hello():\n    print('hello')\n    return True"
        search = "print('hello')\n    return"
        hint = PatchApplier._find_closest_match(content, search)
        assert "print('hello')" in hint

    def test_no_match(self):
        content = "def hello():\n    pass"
        search = "completely different text that is long enough"
        hint = PatchApplier._find_closest_match(content, search)
        assert hint == ""

    def test_short_search(self):
        hint = PatchApplier._find_closest_match("some content", "ab")
        assert hint == ""
