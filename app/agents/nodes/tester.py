"""Tester Agent 节点 - Blueprint V2.0。

Input:  ReviewState (读取 search_replace_blocks, diff_chunks, detected_languages)
逻辑:   调用 Sandbox，应用 Search/Replace Block → 执行对应语言的测试命令
Output: 写入 test_logs 和 is_test_passed
"""

import logging
from typing import Any, Dict, List

from app.core.config import settings
from app.sandbox.base import BaseSandboxEngine, SandboxResult
from app.sandbox.docker_engine import DockerSandbox
from app.sandbox.shell_engine import ShellSandbox
from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)


def _get_sandbox_engine() -> BaseSandboxEngine:
    """根据配置返回对应的沙盒引擎。"""
    engine_name = settings.sandbox.default_engine
    if engine_name == "docker":
        return DockerSandbox()
    elif engine_name == "shell":
        return ShellSandbox()
    else:
        raise ValueError(f"未知的沙盒引擎: {engine_name}")


async def tester_node(state: ReviewState) -> Dict[str, Any]:
    """Tester Agent 节点函数。

    V2 变更：
    - 读取 search_replace_blocks 替代 generated_patches
    - 读取 diff_chunks + detected_languages 替代单一 language
    - 将 diff_chunks 中的文件解析为 source_files 映射
    - 按语言矩阵选择测试命令和镜像
    """
    detected = state.get("detected_languages", [])
    blocks = state.get("search_replace_blocks", [])

    logger.info(
        "Tester 节点开始执行: pr=%s, languages=%s, blocks=%d",
        state.get("pr_id", "?"),
        detected,
        len(blocks),
    )

    if not blocks:
        logger.warning("没有需要测试的 Search/Replace Block")
        return {
            "test_logs": "No search/replace blocks to test.",
            "is_test_passed": False,
        }

    # 按语言分组执行测试
    # Implementation Guide Phase 4 Task 4.2:
    # TesterNode 只传 language 枚举，沙盒内部查询白名单命令
    all_logs: list[str] = []
    all_passed = True

    for lang in detected:
        if lang not in settings.sandbox.matrix:
            logger.warning("不支持的语言: %s，跳过", lang)
            all_logs.append(f"[{lang}] Unsupported language, skipped.")
            continue

        lang_config = settings.sandbox.matrix[lang]

        # 从 diff_chunks 构建 source_files
        source_files = _extract_source_files(state.get("diff_chunks", {}).get(lang, ""))

        # 获取与当前语言相关的 blocks
        lang_blocks = _filter_blocks_for_language(blocks, lang, lang_config.suffixes)

        if not lang_blocks:
            all_logs.append(f"[{lang}] No relevant blocks, skipped.")
            continue

        sandbox = _get_sandbox_engine()
        try:
            # 只传 language 枚举，沙盒内部查询白名单命令
            result: SandboxResult = await sandbox.run_by_language(
                language=lang,
                source_files=source_files,
                search_replace_blocks=lang_blocks,
                timeout=settings.sandbox.timeout,
            )
        except Exception as e:
            logger.error("沙盒执行异常 [%s]: %s", lang, e)
            all_logs.append(f"[{lang}] Sandbox error: {e}")
            all_passed = False
            continue
        finally:
            await sandbox.cleanup()

        lang_log = _build_test_log(lang, result)
        all_logs.append(lang_log)

        if not result.success:
            all_passed = False

    test_logs = "\n".join(all_logs)

    logger.info(
        "Tester 完成: is_test_passed=%s",
        all_passed,
    )

    return {
        "test_logs": test_logs,
        "is_test_passed": all_passed,
    }


def _extract_source_files(diff_chunk: str) -> Dict[str, str]:
    """从 diff 片段中提取源文件内容（简化实现）。

    实际项目中应从 VCS Provider 获取完整源文件。
    这里将 diff 作为 source context 传入。
    """
    if not diff_chunk:
        return {}
    # 简化：将 diff 本身作为一个 source 文件
    return {"__diff_context__": diff_chunk}


def _filter_blocks_for_language(
    blocks: List[Dict[str, Any]],
    lang: str,
    suffixes: List[str],
) -> List[Dict[str, Any]]:
    """过滤出与当前语言相关的 Search/Replace Block。"""
    if not suffixes:
        return blocks

    filtered = []
    for block in blocks:
        file_path = block.get("file_path", "")
        if any(file_path.endswith(suffix) for suffix in suffixes):
            filtered.append(block)
    return filtered


def _build_test_log(lang: str, result: SandboxResult) -> str:
    """构建可读的测试日志。"""
    parts = [
        f"=== [{lang}] Test Result ===",
        f"Exit Code: {result.exit_code}",
        f"Timed Out: {result.timed_out}",
        f"Passed: {result.success}",
    ]

    if result.stdout:
        parts.append("--- STDOUT ---")
        parts.append(result.stdout)

    if result.stderr:
        parts.append("--- STDERR ---")
        parts.append(result.stderr)

    return "\n".join(parts)
