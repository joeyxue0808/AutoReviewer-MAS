"""交互式会话模块 - 支持实时用户输入。

提供异步输入监听和会话管理，支持用户在审查过程中随时输入指令。
"""

import asyncio
import logging
import sys
import time
from typing import Any, Callable, Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

logger = logging.getLogger(__name__)
console = Console()


class InteractiveSession:
    """交互式会话管理器。

    负责：
    1. 异步监听用户输入
    2. 管理输入队列
    3. 处理会话生命周期
    """
    
    def __init__(self, auto_approve: bool = False, input_timeout: int = 30):
        """初始化交互式会话。
        
        Args:
            auto_approve: 是否自动批准修复
            input_timeout: 用户输入超时时间（秒）
        """
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self.auto_approve = auto_approve
        self.input_timeout = input_timeout
        self.running = False
        self._input_task: Optional[asyncio.Task] = None
        self._current_round = 0
        self._total_issues = 0
        
    async def start(self):
        """启动输入监听任务。"""
        self.running = True
        self._input_task = asyncio.create_task(self._listen_input())
        logger.info("交互式会话已启动")
        
    async def stop(self):
        """停止输入监听任务。"""
        self.running = False
        if self._input_task:
            self._input_task.cancel()
            try:
                await self._input_task
            except asyncio.CancelledError:
                pass
        logger.info("交互式会话已停止")
        
    async def _listen_input(self):
        """异步监听用户输入。"""
        loop = asyncio.get_event_loop()
        
        while self.running:
            try:
                # 使用 run_in_executor 实现非阻塞读取
                line = await asyncio.wait_for(
                    loop.run_in_executor(None, self._read_line),
                    timeout=0.5,  # 短超时，允许检查 self.running
                )
                if line:
                    line = line.strip()
                    if line:
                        await self.input_queue.put(line)
                        logger.debug("收到用户输入: %s", line)
            except asyncio.TimeoutError:
                # 超时是正常的，继续循环
                continue
            except Exception as e:
                logger.error("输入监听错误: %s", e)
                await asyncio.sleep(0.1)
                
    def _read_line(self) -> Optional[str]:
        """读取一行输入（阻塞）。"""
        try:
            return sys.stdin.readline()
        except Exception:
            return None
            
    async def get_user_input(self, timeout: Optional[float] = None) -> Optional[str]:
        """获取用户输入，支持超时。
        
        Args:
            timeout: 超时时间（秒），None 使用默认超时
            
        Returns:
            用户输入字符串，超时返回 None
        """
        effective_timeout = timeout or self.input_timeout
        
        try:
            return await asyncio.wait_for(
                self.input_queue.get(),
                timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            return None
            
    async def put_user_input(self, input_text: str):
        """手动放入用户输入（用于测试或自动化）。"""
        await self.input_queue.put(input_text)
        
    def update_round(self, round_num: int):
        """更新当前轮次。"""
        self._current_round = round_num
        
    def update_issues_count(self, count: int):
        """更新问题数量。"""
        self._total_issues = count


class RealTimeDisplay:
    """实时状态显示。"""
    
    def __init__(self):
        self.console = Console()
        self._current_status = ""
        
    def show_welcome(self):
        """显示欢迎信息。"""
        welcome_text = Text()
        welcome_text.append("🤖 AutoReviewer-MAS 多轮审查模式", style="bold blue")
        welcome_text.append("\n\n")
        welcome_text.append("输入指令:", style="bold")
        welcome_text.append("\n")
        welcome_text.append("  y/yes/是     - 批准修复", style="green")
        welcome_text.append("\n")
        welcome_text.append("  n/no/否      - 拒绝修复", style="red")
        welcome_text.append("\n")
        welcome_text.append("  stop/停止    - 停止执行", style="red")
        welcome_text.append("\n")
        welcome_text.append("  skip/跳过    - 跳过当前轮次", style="yellow")
        welcome_text.append("\n")
        welcome_text.append("  忽略xxx问题  - 忽略特定类别问题", style="yellow")
        welcome_text.append("\n")
        welcome_text.append("  只关注xxx问题 - 只处理特定类别问题", style="yellow")
        welcome_text.append("\n\n")
        welcome_text.append("支持随时输入指令，系统会立即响应。", style="dim")
        
        self.console.print(Panel(welcome_text, title="多轮审查", border_style="blue"))
        
    def show_round_start(self, round_num: int, max_rounds: int):
        """显示轮次开始。"""
        self.console.print(f"\n[bold blue]═══ 第 {round_num}/{max_rounds} 轮审查 ═══[/bold blue]")
        
    def show_review_progress(self, status: str):
        """显示审查进度。"""
        self.console.print(f"[dim]{status}[/dim]")
        
    def show_issues_found(self, issues: list):
        """显示发现的问题。"""
        if not issues:
            self.console.print("[green]✓ 未发现问题[/green]")
            return
            
        self.console.print(f"\n[bold]发现 {len(issues)} 个问题:[/bold]")
        
        # 按严重性分组
        critical = [i for i in issues if i.get("severity") == "critical"]
        warning = [i for i in issues if i.get("severity") == "warning"]
        info = [i for i in issues if i.get("severity") == "info"]
        
        if critical:
            self.console.print(f"  [red]🔴 Critical: {len(critical)}[/red]")
        if warning:
            self.console.print(f"  [yellow]🟡 Warning: {len(warning)}[/yellow]")
        if info:
            self.console.print(f"  [blue]🔵 Info: {len(info)}[/blue]")
            
    def show_approval_prompt(self):
        """显示批准提示。"""
        self.console.print("\n[bold yellow]是否修复这些问题？[/bold yellow]")
        self.console.print("  [green]y[/green] - 修复所有问题")
        self.console.print("  [red]n[/red] - 跳过修复")
        self.console.print("  [dim]输入其他指令可指定具体操作[/dim]")
        
    def show_round_complete(self, round_num: int, fixed_count: int):
        """显示轮次完成。"""
        self.console.print(f"\n[green]✓ 第 {round_num} 轮完成，修复了 {fixed_count} 个问题[/green]")
        
    def show_final_report(self, report: str, stats: Dict[str, Any]):
        """显示最终报告。"""
        self.console.print("\n" + "=" * 60)
        self.console.print("[bold green]多轮审查完成[/bold green]")
        self.console.print(f"总轮次: {stats.get('total_rounds', 0)}")
        self.console.print(f"修复问题数: {stats.get('fixed_issues_count', 0)}")
        self.console.print("=" * 60)
        self.console.print("\n")
        self.console.print(report)
        
    def show_error(self, error: str):
        """显示错误信息。"""
        self.console.print(f"[red]✗ 错误: {error}[/red]")
        
    def show_user_input_received(self, input_text: str):
        """显示收到用户输入。"""
        self.console.print(f"\n[dim]收到指令: {input_text}[/dim]")
        
    def show_waiting_input(self):
        """显示等待输入。"""
        self.console.print("\n[bold cyan]等待输入...[/bold cyan]", end="")


def create_interactive_session(
    auto_approve: bool = False,
    input_timeout: int = 30,
) -> InteractiveSession:
    """创建交互式会话实例。"""
    return InteractiveSession(
        auto_approve=auto_approve,
        input_timeout=input_timeout,
    )
