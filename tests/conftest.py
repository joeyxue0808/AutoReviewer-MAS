"""公共测试 Fixtures - 跨测试模块共享。

提供：
- sample_diff: 各语言的 sample diff 文本
- sample_review_state: 完整的 ReviewState 初始结构
- mock_llm: 模拟 LLM 调用（不实际请求 API）
"""

import pytest


# ─────────────────────────────────────────────
# Sample Diffs
# ─────────────────────────────────────────────

SAMPLE_DIFF_GO = """\
diff --git a/cmd/server/main.go b/cmd/server/main.go
index 1234567..abcdefg 100644
--- a/cmd/server/main.go
+++ b/cmd/server/main.go
@@ -10,6 +10,10 @@ import (
 \t"net/http"
+    "log/slog"
 )

 func main() {
+    slog.Info("server starting", "port", 8080)
     http.HandleFunc("/", handler)
     http.ListenAndServe(":8080", nil)
 }

diff --git a/internal/handler/handler.go b/internal/handler/handler.go
index 2345678..bcdefgh 100644
--- a/internal/handler/handler.go
+++ b/internal/handler/handler.go
@@ -5,8 +5,12 @@ import "net/http"
 func handler(w http.ResponseWriter, r *http.Request) {
-    w.Write([]byte("hello"))
+    name := r.URL.Query().Get("name")
+    if name == "" {
+        name = "world"
+    }
+    w.Write([]byte("hello " + name))
 }
"""

SAMPLE_DIFF_PYTHON = """\
diff --git a/app/utils/helper.py b/app/utils/helper.py
index 1111111..2222222 100644
--- a/app/utils/helper.py
+++ b/app/utils/helper.py
@@ -1,5 +1,10 @@
+import logging
+
+logger = logging.getLogger(__name__)
+
 def process_data(data: list) -> dict:
-    return {"count": len(data)}
+    result = {"count": len(data)}
+    logger.info("processed %d items", len(data))
+    return result

diff --git a/app/models/user.py b/app/models/user.py
index 3333333..4444444 100644
--- a/app/models/user.py
+++ b/app/models/user.py
@@ -8,3 +8,7 @@ class User:
     name: str
     email: str
+
+    @property
+    def display_name(self) -> str:
+        return f"{self.name} <{self.email}>"
"""

SAMPLE_DIFF_MULTI = """\
diff --git a/src/main.py b/src/main.py
index aaa..bbb 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,3 +1,5 @@
 def hello():
-    print("hi")
+    name = "world"
+    print(f"hello {name}")
+    return name
diff --git a/lib/utils.go b/lib/utils.go
index ccc..ddd 100644
--- a/lib/utils.go
+++ b/lib/utils.go
@@ -5,3 +5,7 @@ package lib
 func Add(a, b int) int {
     return a + b
 }
+
+func Multiply(a, b int) int {
+    return a * b
+}
"""


@pytest.fixture
def sample_diff_go():
    return SAMPLE_DIFF_GO


@pytest.fixture
def sample_diff_python():
    return SAMPLE_DIFF_PYTHON


@pytest.fixture
def sample_diff_multi():
    return SAMPLE_DIFF_MULTI


# ─────────────────────────────────────────────
# Sample Review State
# ─────────────────────────────────────────────

@pytest.fixture
def sample_review_state():
    """构建完整的 ReviewState 初始结构。"""
    return {
        "vcs_provider": "cli",
        "pr_id": "test-001",
        "trigger_type": "cli",
        "repo_id": "test-repo",
        "repo_context": "test-repo/\n  cmd/\n    server/\n      main.go\n  internal/\n    handler/\n      handler.go",
        "diff_chunks": {"go_0": SAMPLE_DIFF_GO},
        "detected_languages": ["go"],
        "review_issues": [],
        "search_replace_blocks": [],
        "test_logs": "",
        "is_test_passed": False,
        "retry_count": 0,
        "error_count": 0,
        "error_type": "",
        "last_node": "",
    }


@pytest.fixture
def sample_review_issues():
    """构建示例审查问题列表。"""
    return [
        {
            "file_path": "cmd/server/main.go",
            "line_number": 13,
            "severity": "warning",
            "category": "error_handling",
            "description": "http.ListenAndServe 的返回值未处理",
            "suggestion": "使用 log.Fatal(http.ListenAndServe(...)) 或返回 error",
        },
        {
            "file_path": "internal/handler/handler.go",
            "line_number": 9,
            "severity": "critical",
            "category": "security",
            "description": "用户输入未经验证直接使用",
            "suggestion": "对 name 参数进行长度限制和特殊字符过滤",
        },
    ]


@pytest.fixture
def sample_search_replace_blocks():
    """构建示例 Search/Replace Block 列表。"""
    return [
        {
            "file_path": "cmd/server/main.go",
            "search_block": '    http.HandleFunc("/", handler)\n    http.ListenAndServe(":8080", nil)',
            "replace_block": '    http.HandleFunc("/", handler)\n    if err := http.ListenAndServe(":8080", nil); err != nil {\n        slog.Error("server failed", "error", err)\n    }',
        },
    ]


@pytest.fixture
def sample_source_files():
    """构建示例源文件映射。"""
    return {
        "cmd/server/main.go": """package main

import (
\t"fmt"
\t"net/http"
\t"log/slog"
)

func main() {
\tslog.Info("server starting", "port", 8080)
\thttp.HandleFunc("/", handler)
\thttp.ListenAndServe(":8080", nil)
}

func handler(w http.ResponseWriter, r *http.Request) {
\tfmt.Fprintf(w, "hello")
}
""",
        "internal/handler/handler.go": """package handler

import "net/http"

func handler(w http.ResponseWriter, r *http.Request) {
\tname := r.URL.Query().Get("name")
\tif name == "" {
\t\tname = "world"
\t}
\tw.Write([]byte("hello " + name))
}
""",
    }
