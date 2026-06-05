"""AutoReviewer CLI 终端极客模式 - Phase 4 全模态接入。

本地伴随开发工具：
    auto-review --local

读取本地 Git 暂存区作为输入，绕过 Webhook，
通过 asyncio 直接调用后端 Graph，
利用 rich 库在终端流式打印 Agent 思考过程。

安装后使用：
    pip install -e .
    auto-review --local
    auto-review --local --branch feature/auth
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

app = typer.Typer(
    name="auto-review",
    help="🤖 AutoReviewer-MAS CLI - 本地伴随代码审查工具",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    """配置 Rich 日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def _get_git_diff(staged: bool = True) -> str:
    """获取本地 Git Diff。

    Args:
        staged: True 获取暂存区 diff (--cached)，False 获取工作区 diff

    Returns:
        diff 文本
    """
    cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Git diff 失败: {e.stderr}[/red]")
        raise typer.Exit(1)


def _get_git_branch() -> str:
    """获取当前 Git 分支名。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _get_repo_root() -> str:
    """获取 Git 仓库根目录。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return os.getcwd()


@app.command()
def local(
    staged: bool = typer.Option(True, "--staged/--all", help="审查暂存区 (--staged) 或全部变更 (--all)"),
    branch: str = typer.Option(None, "--branch", "-b", help="指定分支名（默认当前分支）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细日志"),
):
    """🔍 审查本地 Git 变更（绕过 Webhook，直接调用 Graph）。"""
    _setup_logging(verbose)

    # 显示 Banner
    console.print(Panel.fit(
        "[bold cyan]🤖 AutoReviewer-MAS[/bold cyan]\n"
        "[dim]本地伴随代码审查 - Phase 4 CLI[/dim]",
        border_style="cyan",
    ))

    # 获取 Git 信息
    repo_root = _get_repo_root()
    current_branch = branch or _get_git_branch()
    diff_text = _get_git_diff(staged=staged)

    if not diff_text.strip():
        console.print("[yellow]⚠️  没有检测到代码变更[/yellow]")
        if staged:
            console.print("[dim]提示: 使用 --all 审查工作区变更，或先 git add 暂存文件[/dim]")
        raise typer.Exit(0)

    # 显示变更概览
    _display_diff_summary(diff_text)

    # 执行审查
    console.print()
    asyncio.run(_run_review(diff_text, current_branch, repo_root))


def _display_diff_summary(diff_text: str) -> None:
    """显示 Diff 概览表格。"""
    import re

    files = re.findall(r"^diff --git a/(.+?) b/(.+?)$", diff_text, re.MULTILINE)
    additions = len(re.findall(r"^\+[^+]", diff_text, re.MULTILINE))
    deletions = len(re.findall(r"^-[^-]", diff_text, re.MULTILINE))

    table = Table(title="📋 变更概览", show_lines=True)
    table.add_column("文件", style="cyan")
    table.add_column("状态", style="green")

    for _, new_path in files[:20]:  # 最多显示 20 个文件
        table.add_row(new_path, "modified")

    if len(files) > 20:
        table.add_row(f"... 还有 {len(files) - 20} 个文件", "")

    console.print(table)
    console.print(f"[green]+{additions}[/green] / [red]-{deletions}[/red] 行")


async def _run_review(diff_text: str, branch: str, repo_root: str) -> None:
    """执行审查流水线。"""
    from app.agents.workflow import compile_graph
    from app.core.diff_analyzer import DiffAnalyzer
    from app.schemas.state import ReviewState

    # DiffAnalyzer 检测语言和拆分
    analyzer = DiffAnalyzer()
    detected = analyzer.detect_languages(diff_text)
    chunk_list = analyzer.chunk_diff(diff_text)

    if not detected:
        console.print("[yellow]⚠️  无法识别编程语言，使用默认审查[/yellow]")
        detected = ["unknown"]

    console.print(f"[cyan]🔍 检测到语言: {', '.join(detected)}[/cyan]")
    console.print(f"[cyan]📦 切分为 {len(chunk_list)} 个 Chunk (防上下文爆炸)[/cyan]")

    # 转换为 ReviewState 需要的 Dict[str, str] 格式
    diff_chunks = {c.chunk_id: c.content for c in chunk_list}

    # 生成 Repo-Map 全局上下文 (Implementation Guide Phase 5 Task 5.1)
    from app.core.repo_mapper import generate_repo_map
    repo_context = generate_repo_map(repo_root)
    console.print(f"[cyan]🗺️  Repo-Map 已生成 ({len(repo_context)} chars)[/cyan]")

    # 构建初始状态
    initial_state: ReviewState = {
        "vcs_provider": "cli",
        "pr_id": f"local-{branch}",
        "trigger_type": "cli",
        "repo_context": repo_context,
        "diff_chunks": diff_chunks,
        "detected_languages": detected,
        "review_issues": [],
        "search_replace_blocks": [],
        "test_logs": "",
        "is_test_passed": False,
        "retry_count": 0,
    }

    # 编译 Graph（CLI 模式不启用 HITL 挂起）
    graph = compile_graph(interrupt_before=[])

    # 执行审查（带进度显示）
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("🤖 Agent 审查中...", total=None)

        try:
            final_state = await graph.ainvoke(initial_state)
        except Exception as e:
            progress.update(task, description=f"[red]❌ 审查失败: {e}[/red]")
            console.print_exception()
            raise typer.Exit(1)

        progress.update(task, description="[green]✅ 审查完成[/green]")

    # 显示结果
    _display_results(final_state)


def _display_results(state: dict) -> None:
    """在终端显示审查结果。"""
    issues = state.get("review_issues", [])
    blocks = state.get("search_replace_blocks", [])
    test_logs = state.get("test_logs", "")
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)

    console.print()

    # 审查问题
    if issues:
        tree = Tree(f"🔍 发现 [bold]{len(issues)}[/bold] 个问题 (重试 {retry_count} 次)")

        severity_style = {
            "critical": "bold red",
            "warning": "yellow",
            "info": "blue",
        }

        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "info")
            style = severity_style.get(sev, "white")
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", "")
            desc = issue.get("description", "")
            sug = issue.get("suggestion", "")

            node = tree.add(f"[{style}]{i}. [{sev.upper()}][/] {fp}:{ln}")
            node.add(f"[dim]{desc}[/dim]")
            if sug:
                node.add(f"[green]💡 {sug}[/green]")

        console.print(tree)
    else:
        console.print("[green]✅ 未发现问题[/green]")

    # Search/Replace Blocks
    if blocks:
        console.print()
        console.print(f"[cyan]📝 生成 {len(blocks)} 个修复块:[/cyan]")
        for i, block in enumerate(blocks, 1):
            fp = block.get("file_path", "")
            search = block.get("search_block", block.get("search", ""))[:100]
            replace = block.get("replace_block", block.get("replace", ""))[:100]
            console.print(f"  {i}. [cyan]{fp}[/cyan]")
            console.print(f"     [red]- {search!r}...[/red]")
            console.print(f"     [green]+ {replace!r}...[/green]")

    # 测试结果
    if test_logs:
        console.print()
        status = "✅ 通过" if is_passed else "❌ 失败"
        console.print(Panel(
            Syntax(test_logs[:1000], "text", theme="monokai"),
            title=f"🧪 测试结果: {status}",
            border_style="green" if is_passed else "red",
        ))


@app.command()
def version():
    """📌 显示版本信息。"""
    console.print("AutoReviewer-MAS CLI v0.4.0")


if __name__ == "__main__":
    app()
