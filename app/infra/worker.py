"""独立 Worker 进程 - Phase 1 消息队列消费者。

阻塞消费 Redis Stream 中的审查任务，投递给 LangGraph 引擎。
支持断点续传：Worker 崩溃后，未 ACK 的消息会被其他 Worker 重新消费。

启动方式：
    python -m app.infra.worker
"""

import asyncio
import logging
import signal
import sys
from typing import Any, Dict

from app.agents.workflow import compile_graph
from app.core.config import settings
from app.infra.checkpointer import get_checkpointer
from app.infra.queue import ReviewQueue, review_queue
from app.schemas.state import ReviewState

logger = logging.getLogger(__name__)

# 优雅退出标志
_shutdown = asyncio.Event()


def _setup_logging() -> None:
    """配置 Worker 日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | [Worker] %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def _process_task(task: Dict[str, Any], graph) -> None:
    """处理单个审查任务。

    Phase 4 HITL：Graph 在 submit_node 前挂起时，检测高危操作并发送审批通知。
    Tech Lead 审批后，通过 API 端点唤醒 Graph 继续执行。

    Args:
        task: 从队列消费到的任务（含 _message_id 和 ReviewState 字段）
        graph: 编译后的 LangGraph 实例（含 interrupt_before=["submit_node"]）
    """
    message_id = task.pop("_message_id", "")
    vcs_provider = task.get("vcs_provider", "?")
    pr_id = task.get("pr_id", "?")
    thread_id = f"{vcs_provider}-{pr_id}"

    logger.info("开始处理任务: vcs=%s, pr=%s, msg_id=%s", vcs_provider, pr_id, message_id)

    try:
        initial_state: ReviewState = {
            "vcs_provider": task.get("vcs_provider", "gitlab"),
            "pr_id": task.get("pr_id", ""),
            "trigger_type": task.get("trigger_type", "webhook_pr"),
            "repo_context": task.get("repo_context", ""),
            "diff_chunks": task.get("diff_chunks", {}),
            "detected_languages": task.get("detected_languages", []),
            "review_issues": [],
            "search_replace_blocks": [],
            "test_logs": "",
            "is_test_passed": False,
            "retry_count": 0,
        }

        config = {"configurable": {"thread_id": thread_id}}
        final_state = await graph.ainvoke(initial_state, config=config)

        # 检查是否被 HITL 挂起（Graph 返回时 submit_node 未执行）
        # 当 Graph 在 interrupt_before 挂起时，ainvoke 会返回当前状态
        review_issues = final_state.get("review_issues", [])
        blocks = final_state.get("search_replace_blocks", [])

        # Phase 4: 高危操作检测
        from app.infra.hitl import detect_high_risk_operations, send_approval_notification
        from app.api.approval import register_pending_approval

        high_risks = detect_high_risk_operations(review_issues, blocks)

        if high_risks:
            # 注册待审批
            register_pending_approval(
                thread_id=thread_id,
                pr_id=pr_id,
                vcs_provider=vcs_provider,
                high_risk_files=[m.file_path for m in high_risks],
            )

            # 发送审批通知
            approval_url = f"http://localhost:8000/api/v1/approve/{thread_id}"
            await send_approval_notification(
                pr_id=pr_id,
                vcs_provider=vcs_provider,
                high_risk_matches=high_risks,
                approval_url=approval_url,
            )

            logger.info(
                "任务挂起等待审批: vcs=%s, pr=%s, 高危文件=%s",
                vcs_provider, pr_id, [m.file_path for m in high_risks],
            )
        else:
            # 无高危操作，Graph 应已完成（submit_node 执行完毕）
            logger.info(
                "任务完成: vcs=%s, pr=%s, issues=%d, passed=%s, retries=%d",
                vcs_provider, pr_id,
                len(review_issues),
                final_state.get("is_test_passed", False),
                final_state.get("retry_count", 0),
            )

        # ACK 消息（无论是否挂起都 ACK，因为状态已持久化到 checkpointer）
        await review_queue.ack(message_id)

    except Exception as e:
        logger.error(
            "任务处理失败: vcs=%s, pr=%s, msg_id=%s, error=%s",
            vcs_provider, pr_id, message_id, e, exc_info=True,
        )


async def _run_worker_loop(graph) -> None:
    """Worker 主循环：持续消费队列。"""
    logger.info("Worker 主循环启动，等待任务...")

    while not _shutdown.is_set():
        try:
            tasks = await review_queue.consume(count=1, block_ms=5000)

            for task in tasks:
                if _shutdown.is_set():
                    break
                await _process_task(task, graph)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Worker 循环异常: %s", e, exc_info=True)
            await asyncio.sleep(1)  # 避免紧循环

    logger.info("Worker 主循环已退出")


async def main():
    """Worker 入口函数。"""
    _setup_logging()
    logger.info("AutoReviewer-MAS Worker 启动中...")

    # 信号处理（Windows 不支持 add_signal_handler，使用线程兼容方案）
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: _shutdown.set())
    except NotImplementedError:
        # Windows 回退：用 signal.signal 在主线程注册
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, f: _shutdown.set())

    # 连接队列
    await review_queue.connect()
    logger.info("已连接 Redis 队列: %s", settings.queue.redis_url)

    # 初始化 Checkpointer
    checkpointer_ctx = get_checkpointer()
    checkpointer = None
    if checkpointer_ctx:
        async with checkpointer_ctx as cp:
            await cp.setup()
            checkpointer = cp
            logger.info("Postgres Checkpointer 已就绪")

            # 构建 Graph（带 checkpointer）并在其生命周期内运行
            graph = compile_graph(checkpointer=checkpointer)
            try:
                await _run_worker_loop(graph)
            finally:
                await review_queue.close()
                logger.info("Worker 已关闭")
    else:
        # 无 checkpointer 模式
        graph = compile_graph()
        try:
            await _run_worker_loop(graph)
        finally:
            await review_queue.close()
            logger.info("Worker 已关闭")


if __name__ == "__main__":
    import platform
    import selectors

    if platform.system() == "Windows":
        # Windows: psycopg 要求 SelectorEventLoop，不能用默认的 ProactorEventLoop
        asyncio.run(main(), loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    else:
        asyncio.run(main())
