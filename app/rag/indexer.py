"""代码向量索引器 - 基于 LanceDB 的语义搜索。

提供两种索引策略：
1. AST 函数级分块（精确，依赖 tree-sitter）
2. 行窗口分块（通用，无需依赖）

嵌入模型支持：
- 远程 OpenAI-compatible API（推荐，零内存占用）
- 本地 sentence-transformers（可选，需 GPU）
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 源文件后缀过滤
_SOURCE_SUFFIXES = {
    ".go", ".py", ".cpp", ".cc", ".h", ".hpp", ".java",
    ".vue", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".dart", ".cs",
}

# 跳过的目录
_SKIP_DIRS = {
    "node_modules", "vendor", "__pycache__", ".git", "dist", "build",
    ".lancedb", ".venv", "venv", "env", ".tox", ".mypy_cache",
}


@dataclass
class CodeChunk:
    """代码分块。"""
    file_path: str
    symbol_name: str  # 函数/类名，或 "module_level"
    content: str
    language: str
    start_line: int
    end_line: int
    embedding: List[float] = field(default_factory=list)


@dataclass
class SearchResult:
    """语义搜索结果。"""
    file_path: str
    symbol_name: str
    content: str
    score: float
    start_line: int
    end_line: int


class RepoIndexer:
    """基于 LanceDB 的代码向量索引器。"""

    def __init__(
        self,
        repo_path: str,
        db_path: str = ".lancedb",
        embedding_model: Optional[str] = None,
        embedding_api_base: Optional[str] = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.db_path = db_path
        self._embedding_model = embedding_model
        self._embedding_api_base = embedding_api_base
        self._table = None
        self._db = None

    async def index(self) -> None:
        """扫描仓库 → 分块 → 嵌入 → 写入 LanceDB。"""
        import lancedb

        # 1. 收集源文件
        files = self._collect_source_files()
        logger.info("收集到 %d 个源文件", len(files))

        # 2. 分块
        chunks = []
        for file_path in files:
            chunks.extend(self._chunk_file(file_path))
        logger.info("生成 %d 个代码分块", len(chunks))

        if not chunks:
            logger.warning("无代码分块可索引")
            return

        # 3. 嵌入
        embeddings = await self._embed_batch([c.content for c in chunks])
        for chunk, emb in zip(chunks, embeddings):
            chunk.embedding = emb

        # 4. 写入 LanceDB
        self._db = lancedb.connect(self.db_path)
        table_data = [
            {
                "file_path": c.file_path,
                "symbol_name": c.symbol_name,
                "content": c.content,
                "language": c.language,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "vector": c.embedding,
            }
            for c in chunks
        ]

        # 删除旧表，重建
        try:
            self._db.drop_table("code_chunks")
        except Exception:
            pass

        self._table = self._db.create_table("code_chunks", table_data)
        logger.info("索引完成: %d 个分块写入 LanceDB", len(chunks))

    async def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        """语义搜索 → 返回最相关的代码片段。"""
        if self._table is None:
            import lancedb
            self._db = lancedb.connect(self.db_path)
            try:
                self._table = self._db.open_table("code_chunks")
            except Exception:
                logger.warning("索引表不存在，请先运行 index()")
                return []

        query_embedding = await self._embed_single(query)
        results = (
            self._table.search(query_embedding)
            .limit(top_k)
            .to_list()
        )

        return [
            SearchResult(
                file_path=r["file_path"],
                symbol_name=r["symbol_name"],
                content=r["content"],
                score=r.get("_distance", 0.0),
                start_line=r["start_line"],
                end_line=r["end_line"],
            )
            for r in results
        ]

    async def index_diff_context(self, diff_chunks: Dict[str, str], top_k: int = 10) -> Dict[str, str]:
        """为 diff 涉及的文件检索相关上下文。

        Args:
            diff_chunks: {chunk_id: diff_content}
            top_k: 每个 chunk 检索的最相关结果数

        Returns:
            {file_path: context_content} — 去重合并后的上下文
        """
        contexts: Dict[str, str] = {}

        for chunk_id, diff_content in diff_chunks.items():
            results = await self.search(diff_content, top_k=top_k)
            for r in results:
                if r.file_path not in contexts:
                    contexts[r.file_path] = ""
                contexts[r.file_path] += f"\n--- {r.symbol_name} (L{r.start_line}-{r.end_line}) ---\n{r.content}\n"

        return contexts

    def _collect_source_files(self) -> List[Path]:
        """收集仓库中的源文件。"""
        files = []
        for root, dirs, filenames in os.walk(self.repo_path):
            # 过滤跳过的目录
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for fname in filenames:
                suffix = Path(fname).suffix
                if suffix in _SOURCE_SUFFIXES:
                    files.append(Path(root) / fname)
        return files

    def _chunk_file(self, file_path: Path) -> List[CodeChunk]:
        """将单个文件分块。"""
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        relative_path = str(file_path.relative_to(self.repo_path)).replace("\\", "/")
        suffix = file_path.suffix
        language = self._suffix_to_language(suffix)

        # 尝试 AST 分块
        chunks = self._ast_chunk(content, relative_path, language)
        if chunks:
            return chunks

        # 降级为行窗口分块
        return self._line_window_chunk(content, relative_path, language)

    def _ast_chunk(self, content: str, file_path: str, language: str) -> List[CodeChunk]:
        """AST 函数级分块（regex-based，不依赖 tree-sitter）。"""
        chunks = []
        lines = content.split("\n")

        # 匹配函数/类定义的正则
        patterns = {
            "python": r"^(?:async\s+)?(?:def|class)\s+(\w+)",
            "go": r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)",
            "javascript": r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\())",
            "typescript": r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*(?::\s*\w+)?\s*=\s*(?:async\s+)?(?:function|\())",
            "java": r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(",
            "cpp": r"^(?:\w+\s+)*(\w+)\s*\([^)]*\)\s*(?:const)?\s*\{",
        }

        pattern = patterns.get(language)
        if not pattern:
            return []

        current_symbol = None
        current_start = 0

        for i, line in enumerate(lines):
            m = re.match(pattern, line)
            if m:
                # 保存前一个 symbol
                if current_symbol:
                    chunk_content = "\n".join(lines[current_start:i])
                    if chunk_content.strip():
                        chunks.append(CodeChunk(
                            file_path=file_path,
                            symbol_name=current_symbol,
                            content=chunk_content,
                            language=language,
                            start_line=current_start + 1,
                            end_line=i,
                        ))

                current_symbol = m.group(1) or m.group(2) if m.lastindex >= 2 else m.group(1)
                current_start = i

        # 最后一个 symbol
        if current_symbol:
            chunk_content = "\n".join(lines[current_start:])
            if chunk_content.strip():
                chunks.append(CodeChunk(
                    file_path=file_path,
                    symbol_name=current_symbol,
                    content=chunk_content,
                    language=language,
                    start_line=current_start + 1,
                    end_line=len(lines),
                ))

        return chunks

    def _line_window_chunk(
        self, content: str, file_path: str, language: str, window_size: int = 50
    ) -> List[CodeChunk]:
        """行窗口分块（通用降级方案）。"""
        lines = content.split("\n")
        chunks = []

        for i in range(0, len(lines), window_size):
            window = lines[i:i + window_size]
            chunk_content = "\n".join(window)
            if chunk_content.strip():
                chunks.append(CodeChunk(
                    file_path=file_path,
                    symbol_name=f"lines_{i+1}_{min(i+window_size, len(lines))}",
                    content=chunk_content,
                    language=language,
                    start_line=i + 1,
                    end_line=min(i + window_size, len(lines)),
                ))

        return chunks

    def _suffix_to_language(self, suffix: str) -> str:
        """后缀 → 语言映射。"""
        mapping = {
            ".go": "go", ".py": "python",
            ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp",
            ".java": "java", ".vue": "vue",
            ".js": "javascript", ".cjs": "javascript", ".mjs": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
            ".dart": "flutter", ".cs": "csharp",
        }
        return mapping.get(suffix, "unknown")

    async def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量嵌入。"""
        if not texts:
            return []

        # 尝试远程 API
        if self._embedding_api_base:
            return await self._embed_remote(texts)

        # 尝试本地模型
        try:
            return self._embed_local(texts)
        except ImportError:
            pass

        # 最终降级：随机向量（仅用于开发测试）
        logger.warning("无可用嵌入模型，使用随机向量（仅用于开发测试）")
        import random
        dim = 384
        return [[random.random() for _ in range(dim)] for _ in texts]

    async def _embed_single(self, text: str) -> List[float]:
        """单条文本嵌入。"""
        results = await self._embed_batch([text])
        return results[0] if results else []

    async def _embed_remote(self, texts: List[str]) -> List[List[float]]:
        """通过远程 API 嵌入。"""
        import aiohttp

        api_base = self._embedding_api_base.rstrip("/")
        model = self._embedding_model or "text-embedding-3-small"

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{api_base}/embeddings",
                json={"input": texts, "model": model},
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"嵌入 API 调用失败: {resp.status}, {text}")
                data = await resp.json()
                return [item["embedding"] for item in data["data"]]

    def _embed_local(self, texts: List[str]) -> List[List[float]]:
        """使用本地 sentence-transformers 嵌入。"""
        from sentence_transformers import SentenceTransformer

        model_name = self._embedding_model or "all-MiniLM-L6-v2"
        model = SentenceTransformer(model_name)
        embeddings = model.encode(texts, show_progress_bar=False)
        return embeddings.tolist()
