"""LanguageMatrix 单元测试 - 语言配置查询。"""

import pytest
from app.core.language_matrix import get_lang_by_suffix, get_config, invalidate_cache


class TestGetLangBySuffix:
    def test_go(self):
        assert get_lang_by_suffix(".go") == "go"

    def test_python(self):
        assert get_lang_by_suffix(".py") == "python"

    def test_cpp_variants(self):
        assert get_lang_by_suffix(".cpp") == "cpp"
        assert get_lang_by_suffix(".h") == "cpp"
        assert get_lang_by_suffix(".cc") == "cpp"
        assert get_lang_by_suffix(".hpp") == "cpp"

    def test_java(self):
        assert get_lang_by_suffix(".java") == "java"

    def test_vue(self):
        assert get_lang_by_suffix(".vue") == "vue"

    def test_javascript(self):
        assert get_lang_by_suffix(".js") == "javascript"
        assert get_lang_by_suffix(".cjs") == "javascript"

    def test_typescript(self):
        assert get_lang_by_suffix(".ts") == "typescript"
        assert get_lang_by_suffix(".tsx") == "typescript"

    def test_flutter(self):
        assert get_lang_by_suffix(".dart") == "flutter"

    def test_csharp(self):
        assert get_lang_by_suffix(".cs") == "csharp"

    def test_unknown_suffix(self):
        assert get_lang_by_suffix(".xyz") is None
        assert get_lang_by_suffix("") is None


class TestGetConfig:
    def test_go_config(self):
        config = get_config("go")
        assert config.image == "golang:1.23-alpine"
        assert "go test" in config.test_command

    def test_python_config(self):
        config = get_config("python")
        assert config.image == "python:3.12-slim"
        assert "pytest" in config.test_command

    def test_unknown_language(self):
        with pytest.raises(KeyError):
            get_config("unknown_lang")

    def test_all_nine_languages(self):
        """验证 9 种语言都有配置。"""
        for lang in ["go", "python", "cpp", "java", "vue", "javascript", "typescript", "flutter", "csharp"]:
            config = get_config(lang)
            assert config.image
            assert config.test_command
            assert config.suffixes


class TestInvalidateCache:
    def test_invalidate_and_rebuild(self):
        """invalidate_cache 不应抛异常。"""
        invalidate_cache()
        # 重新查询应该仍然正常
        config = get_config("go")
        assert config.image == "golang:1.23-alpine"
