"""目录级持久化缓存，避免每次 CLI 执行时冷启动。

将文件内容、Repo-Map、已知问题等缓存到 <repo_root>/.autoreviewer/cache.json，
使得多次执行间共享记忆，大幅减少重复 I/O 和 LLM token 消耗。

缓存结构：
  files: { normalized_path: { mtime, segments: [{start, end, content}] } }
  repo_map: { path, content, generated_at }
  known_issues: [[file_path, line_number, description], ...]
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "cache.json"


class PersistentCache:
    """目录级持久化缓存，按项目目录隔离。"""

    def __init__(self, repo_root: str):
        self._repo_root = os.path.normpath(repo_root)
        self._cache_dir = Path(self._repo_root) / ".autoreviewer"
        self._cache_file = self._cache_dir / _CACHE_FILENAME
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self._load()

    # ── 公共接口 ──

    def get_file_content(self, file_path: str, start: int, end: int) -> Optional[str]:
        key = self._norm(file_path)
        entry = self._data.setdefault("files", {}).get(key)
        if not entry:
            return None
        if entry.get("mtime") != self._mtime(file_path):
            return None
        for seg in entry.get("segments", []):
            if seg["start"] == start and seg["end"] == end:
                return seg["content"]
        return None

    def set_file_content(self, file_path: str, start: int, end: int, content: str) -> None:
        key = self._norm(file_path)
        files = self._data.setdefault("files", {})
        if key not in files:
            files[key] = {"mtime": self._mtime(file_path), "segments": []}
        segments = files[key]["segments"]
        segments[:] = [s for s in segments if not (s["start"] == start and s["end"] == end)]
        segments.append({"start": start, "end": end, "content": content})
        self._dirty = True

    def get_repo_map(self) -> Optional[str]:
        entry = self._data.get("repo_map", {})
        return entry.get("content") if entry.get("path") == self._repo_root else None

    def set_repo_map(self, content: str) -> None:
        self._data["repo_map"] = {
            "path": self._repo_root,
            "content": content,
            "generated_at": time.time(),
        }
        self._dirty = True

    def get_known_issues(self) -> Set[Tuple[str, int, str]]:
        raw = self._data.get("known_issues", [])
        return {tuple(item) for item in raw}

    def add_known_issues(self, issues: List[Dict[str, Any]]) -> None:
        known = self._data.setdefault("known_issues", [])
        existing = {tuple(item) for item in known}
        for issue in issues:
            key = (issue.get("file_path", ""), issue.get("line_number", 0), issue.get("description", ""))
            if key not in existing:
                known.append(list(key))
                existing.add(key)
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            logger.debug("持久缓存无变化，跳过保存")
            return
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = str(self._cache_file) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(self._cache_file))
            self._dirty = False
            logger.info("持久缓存已保存: %s (%d items)", self._cache_file, self._size())
        except Exception as e:
            logger.warning("持久缓存保存失败: %s", e)

    # ── 内部 ──

    def _load(self) -> None:
        if not self._cache_file.exists():
            logger.debug("持久缓存文件不存在，从头开始: %s", self._cache_file)
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            logger.info("持久缓存已加载: %s (%d items)", self._cache_file, self._size())
        except Exception as e:
            logger.warning("持久缓存加载失败，重建: %s", e)
            self._data = {}

    def _norm(self, path: str) -> str:
        return os.path.normpath(path).replace("\\", "/")

    def _mtime(self, file_path: str) -> Optional[float]:
        try:
            full = file_path if os.path.isabs(file_path) else os.path.join(self._repo_root, file_path)
            return os.path.getmtime(full)
        except Exception:
            return None

    def _size(self) -> int:
        n = len(self._data.get("files", {}))
        n += 1 if self._data.get("repo_map") else 0
        n += len(self._data.get("known_issues", []))
        return n


class NullPersistentCache:
    """空缓存（当项目目录不可用时）。"""

    def get_file_content(self, *args, **kwargs) -> None:
        return None

    def set_file_content(self, *args, **kwargs) -> None:
        pass

    def get_repo_map(self) -> None:
        return None

    def set_repo_map(self, *args, **kwargs) -> None:
        pass

    def get_known_issues(self) -> Set:
        return set()

    def add_known_issues(self, *args, **kwargs) -> None:
        pass

    def save(self) -> None:
        pass


_cache_registry: Dict[str, PersistentCache] = {}


def get_persistent_cache(repo_root: str) -> PersistentCache:
    if not repo_root:
        return NullPersistentCache()
    norm = os.path.normpath(repo_root)
    if norm not in _cache_registry:
        _cache_registry[norm] = PersistentCache(norm)
    return _cache_registry[norm]


def save_all() -> None:
    for cache in _cache_registry.values():
        cache.save()
