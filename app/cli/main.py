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

# Windows 控制台默认 GBK 编码，强制 UTF-8 以支持 emoji 和中文
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

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
    # 抑制 httpx/httpcore 的 HTTP 请求日志（每次 LLM 调用都刷屏）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _get_git_diff(staged: bool = True) -> str:
    """获取本地 Git Diff。

    Args:
        staged: True 获取暂存区 diff (--cached)，False 获取工作区 diff

    Returns:
        diff 文本
    """
    cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=True)
        return result.stdout
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Git diff 失败: {e.stderr}[/red]")
        raise typer.Exit(1)


def _get_git_branch() -> str:
    """获取当前 Git 分支名。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def _get_repo_root() -> str:
    """获取 Git 仓库根目录。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, encoding="utf-8", errors="replace", check=True,
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
    """执行审查流水线 — 两阶段交互模式。

    阶段 1: Reviewer 审查 → 展示问题清单 → 用户选择修复项
    阶段 2: Fixer 修复 → Critic 校验 → Tester 测试 → 提交报告
    """
    from app.agents.workflow import build_review_only_graph, build_fix_only_graph
    from app.core.diff_analyzer import DiffAnalyzer
    from app.schemas.state import ReviewState

    # ── 准备阶段 ──
    analyzer = DiffAnalyzer()
    detected = analyzer.detect_languages(diff_text)
    chunk_list = analyzer.chunk_diff(diff_text)

    if not detected:
        console.print("[yellow]⚠️  无法识别编程语言，使用默认审查[/yellow]")
        detected = ["unknown"]

    console.print(f"[cyan]🔍 检测到语言: {', '.join(detected)}[/cyan]")
    console.print(f"[cyan]📦 切分为 {len(chunk_list)} 个 Chunk (防上下文爆炸)[/cyan]")

    diff_chunks = {c.chunk_id: c.content for c in chunk_list}

    from app.core.repo_mapper import generate_repo_map
    repo_context = generate_repo_map(repo_root)
    console.print(f"[cyan]🗺️  Repo-Map 已生成 ({len(repo_context)} chars)[/cyan]")

    initial_state: ReviewState = {
        "vcs_provider": "cli",
        "pr_id": f"local-{branch}",
        "trigger_type": "cli",
        "repo_id": repo_root,
        "repo_context": repo_context,
        "diff_chunks": diff_chunks,
        "detected_languages": detected,
        "review_issues": [],
        "search_replace_blocks": [],
        "test_logs": "",
        "is_test_passed": False,
        "retry_count": 0,
        "error_count": 0,
        "error_type": "",
        "last_node": "",
    }

    # ── 阶段 1: 审查 ──
    review_graph = build_review_only_graph().compile()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("🤖 Agent 审查中...", total=None)

        try:
            review_state = await review_graph.ainvoke(initial_state)
        except Exception as e:
            progress.update(task, description=f"[red]❌ 审查失败: {e}[/red]")
            console.print_exception()
            raise typer.Exit(1)

        progress.update(task, description="[green]✅ 审查完成[/green]")

    issues = review_state.get("review_issues", [])

    # ── 用户交互：选择修复项 ──
    if not issues:
        console.print(Panel(
            "[bold green]✅ 未发现问题 — 代码审查通过[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))
        return

    selected_issues = _prompt_issue_selection(issues)

    if not selected_issues:
        console.print("[yellow]⏭️  已跳过所有修复，审查结束[/yellow]")
        return

    # ── 阶段 2: 修复（仅处理用户选中的问题）──
    fix_state = dict(review_state)
    fix_state["review_issues"] = selected_issues
    fix_state["search_replace_blocks"] = []
    fix_state["test_logs"] = ""
    fix_state["is_test_passed"] = False
    fix_state["retry_count"] = 0
    fix_state["error_count"] = 0
    fix_state["error_type"] = ""
    fix_state["last_node"] = ""

    fix_graph = build_fix_only_graph().compile()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("🔧 Agent 修复中...", total=None)

        try:
            final_state = await fix_graph.ainvoke(fix_state)
        except Exception as e:
            progress.update(task, description=f"[red]❌ 修复失败: {e}[/red]")
            console.print_exception()
            raise typer.Exit(1)

        progress.update(task, description="[green]✅ 修复完成[/green]")

    # 显示最终结果（合并审查问题和修复结果）
    final_state["review_issues"] = issues  # 显示全部原始问题
    _display_results(final_state)


def _prompt_issue_selection(issues: list) -> list:
    """展示审查问题清单，让用户选择要修复的项。

    Returns:
        用户选中的 issue 子列表
    """
    sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    sev_style = {"critical": "bold red", "warning": "yellow", "info": "cyan"}

    # 展示问题清单
    console.print()
    console.print(Panel(
        f"[bold]🔍 发现 {len(issues)} 个问题 — 请选择要修复的项目[/bold]",
        border_style="blue",
    ))
    console.print()

    for i, issue in enumerate(issues, 1):
        sev = issue.get("severity", "info")
        fp = issue.get("file_path", "")
        ln = issue.get("line_number", "")
        desc = issue.get("description", "")
        sug = issue.get("suggestion", "")

        loc = f"{fp}:{ln}" if ln else fp
        content = (
            f"[{sev_style.get(sev, 'white')}]{sev_icon.get(sev, '•')} [{sev.upper()}][/] "
            f"[cyan]{loc}[/cyan]\n\n{desc}"
        )
        if sug:
            content += f"\n[green]💡 建议:[/green] {sug}"

        console.print(Panel(
            content,
            title=f"[bold]#{i}[/bold]",
            border_style=sev_style.get(sev, "white"),
            padding=(0, 1),
        ))

    # 交互提示
    console.print()
    console.print("[bold]选择操作:[/bold]")
    console.print("  [cyan]a[/cyan] — 修复全部问题")
    console.print("  [cyan]1,3,5[/cyan] — 修复指定编号（逗号分隔）")
    console.print("  [cyan]n[/cyan] — 跳过，不修复")
    console.print()

    choice = console.input("[bold green]请输入选择 > [/bold green]").strip().lower()

    if choice == "n" or choice == "":
        return []
    elif choice == "a":
        return issues
    else:
        try:
            indices = [int(x.strip()) for x in choice.split(",")]
            selected = []
            for idx in indices:
                if 1 <= idx <= len(issues):
                    selected.append(issues[idx - 1])
                else:
                    console.print(f"[yellow]⚠️  忽略无效编号: {idx}[/yellow]")
            return selected
        except ValueError:
            console.print("[yellow]⚠️  输入格式无效，跳过修复[/yellow]")
            return []


def _display_results(state: dict) -> None:
    """在终端显示审查结果（完整版，不截断关键内容）。"""
    issues = state.get("review_issues", [])
    blocks = state.get("search_replace_blocks", [])
    test_logs = state.get("test_logs", "")
    is_passed = state.get("is_test_passed", False)
    retry_count = state.get("retry_count", 0)

    console.print()

    # ── 审查问题 ──
    if issues:
        severity_counts = {"critical": 0, "warning": 0, "info": 0}
        for issue in issues:
            sev = issue.get("severity", "info")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1

        badges = []
        if severity_counts["critical"]:
            badges.append(f"[bold red]🔴 critical {severity_counts['critical']}[/bold red]")
        if severity_counts["warning"]:
            badges.append(f"[yellow]🟡 warning {severity_counts['warning']}[/yellow]")
        if severity_counts["info"]:
            badges.append(f"[blue]🔵 info {severity_counts['info']}[/blue]")

        header = f"🔍 审查报告 — {len(issues)} 个问题  {' │ '.join(badges)}"
        if retry_count > 0:
            header += f"  [dim](重试 {retry_count} 次)[/dim]"

        # 每个 issue 用独立 Panel 展示，避免表格截断
        console.print(Panel(header, style="bold", border_style="blue"))
        console.print()

        sev_style = {"critical": "bold red", "warning": "yellow", "info": "cyan"}
        sev_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}

        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "info")
            fp = issue.get("file_path", "")
            ln = issue.get("line_number", "")
            desc = issue.get("description", "")
            sug = issue.get("suggestion", "")

            # 构建单个 issue 的内容
            loc = f"{fp}:{ln}" if ln else fp
            content = f"[{sev_style.get(sev, 'white')}]{sev_icon.get(sev, '•')} [{sev.upper()}][/] [cyan]{loc}[/cyan]\n\n"
            content += f"{desc}\n"
            if sug:
                content += f"\n[green]💡 建议:[/green] {sug}"

            console.print(Panel(
                content,
                title=f"[dim]#{i}[/dim]",
                border_style=sev_style.get(sev, "white"),
                padding=(0, 1),
            ))
    else:
        console.print(Panel(
            "[bold green]✅ 未发现问题 — 代码审查通过[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))

    # ── Search/Replace Blocks ──
    if blocks:
        console.print()
        console.print(Panel(
            f"[bold cyan]📝 生成 {len(blocks)} 个修复块[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        ))
        console.print()

        for i, block in enumerate(blocks, 1):
            fp = block.get("file_path", "")
            search = block.get("search_block", block.get("search", ""))
            replace = block.get("replace_block", block.get("replace", ""))

            # 用完整 diff 格式展示，不截断
            diff_lines = []
            diff_lines.append(f"--- a/{fp}")
            diff_lines.append(f"+++ b/{fp}")
            diff_lines.append("")
            for line in search.splitlines():
                diff_lines.append(f"- {line}")
            diff_lines.append("")
            for line in replace.splitlines():
                diff_lines.append(f"+ {line}")

            diff_text = "\n".join(diff_lines)

            console.print(Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False, word_wrap=True),
                title=f"[cyan]Block #{i}[/cyan] — [dim]{fp}[/dim]",
                border_style="bright_black",
                padding=(0, 1),
            ))

    # ── 测试结果 ──
    if test_logs:
        status = "[bold green]✅ 通过[/bold green]" if is_passed else "[bold red]❌ 失败[/bold red]"
        console.print()
        console.print(Panel(
            Syntax(test_logs, "text", theme="monokai", word_wrap=True),
            title=f"🧪 测试结果: {status}",
            border_style="green" if is_passed else "red",
        ))

    # ── 最终状态 ──
    console.print()
    if is_passed:
        console.print(Panel(
            "[bold green]🎉 审查完成 — 测试通过，代码可合并[/bold green]",
            border_style="green",
            padding=(0, 2),
        ))
    elif retry_count >= 3:
        console.print(Panel(
            "[bold yellow]⚠️  审查完成 — 达到最大重试次数，降级提交已有结果[/bold yellow]",
            border_style="yellow",
            padding=(0, 2),
        ))
    else:
        console.print(Panel(
            "[bold red]❌ 审查完成 — 存在未解决问题[/bold red]",
            border_style="red",
            padding=(0, 2),
        ))


@app.command()
def version():
    """📌 显示版本信息。"""
    console.print("AutoReviewer-MAS CLI v0.4.0")


if __name__ == "__main__":
    app()
