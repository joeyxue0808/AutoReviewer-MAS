"""会话级 + 持久化文件内容缓存，避免多轮间重复读盘。

第一层：内存 LRU（本进程内，最快）
第二层：持久缓存（.autoreviewer/cache.json，跨进程/跨终端窗口）

reviewer.py 和 fixer.py 共享此缓存。
"""

import logging
from collections import OrderedDict
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class FileContentCache:
    """两层文件内容缓存：内存 LRU + 持久化后备。"""

    def __init__(self, max_size: int = 200):
        self._cache: OrderedDict[Tuple[str, int, int], str] = OrderedDict()
        self._max_size = max_size
        self._backend = None  # PersistentCache 实例，懒加载

    def bind_backend(self, backend) -> None:
        self._backend = backend

    def get(self, file_path: str, start_line: int, end_line: int) -> Optional[str]:
        key = (file_path, start_line, end_line)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if self._backend:
            content = self._backend.get_file_content(file_path, start_line, end_line)
            if content is not None:
                logger.debug("文件缓存命中(持久): %s [%d-%d]", file_path, start_line, end_line)
                self._cache[key] = content
                return content
        return None

    def set(self, file_path: str, start_line: int, end_line: int, content: str) -> None:
        key = (file_path, start_line, end_line)
        self._cache[key] = content
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
        if self._backend:
            self._backend.set_file_content(file_path, start_line, end_line, content)

    def clear(self) -> None:
        self._cache.clear()


_cache = FileContentCache()


def get_file_cache() -> FileContentCache:
    return _cache


def init_file_cache(repo_root: str) -> None:
    from app.core.persistent_cache import get_persistent_cache
    backend = get_persistent_cache(repo_root)
    _cache.bind_backend(backend)
    logger.debug("文件缓存已绑定持久后端: %s", repo_root)
